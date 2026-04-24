#!/usr/bin/env python3
import argparse
import os
import sys
import uuid
from pathlib import Path

from bridge_daemon_client import ensure_daemon_running
from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session, resolve_targets, room_inactive_reason
from bridge_paths import state_root
from bridge_util import MESSAGE_KINDS, append_jsonl, locked_json, normalize_kind, public_record, utc_now


STATE_ROOT = state_root()


def update_queue(path: Path, message: dict) -> None:
    with locked_json(path, []) as queue:
        if not any(item.get("id") == message["id"] for item in queue):
            queue.append(message)


def sender_matches_caller(args: argparse.Namespace, bridge_session: str) -> bool:
    if args.sender == "bridge" or args.allow_spoof:
        return True
    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=bridge_session,
        explicit_alias=args.sender,
        tool_name="agent_send_peer",
    )
    if resolution.ok and resolution.session == bridge_session and resolution.alias == args.sender:
        return True
    print(resolution.error or f"agent_send_peer: refusing spoofed sender {args.sender!r}", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="sender", required=True)
    parser.add_argument("--to", dest="target")
    parser.add_argument("--all", dest="target_all", action="store_true")
    parser.add_argument("--intent", default="message")
    parser.add_argument("--kind", choices=sorted(MESSAGE_KINDS), default="request")
    parser.add_argument("--body")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--causal-id")
    parser.add_argument("--hop-count", type=int, default=1)
    parser.add_argument("--no-auto-return", action="store_true")
    parser.add_argument("--state-file")
    parser.add_argument("--public-state-file")
    parser.add_argument("--queue-file")
    parser.add_argument("--session", dest="bridge_session")
    parser.add_argument("--allow-spoof", action="store_true")
    args = parser.parse_args()

    bridge_session = args.bridge_session or os.environ.get("AGENT_BRIDGE_SESSION")
    if not bridge_session:
        print(
            "agent_send_peer: cannot infer bridge session; this pane is not attached to an active bridge room "
            "(or the room was stopped). Reattach/start a bridge room before sending.",
            file=sys.stderr,
        )
        return 2

    ensure_error = ensure_daemon_running(bridge_session)
    if ensure_error:
        print(f"agent_send_peer: {ensure_error}", file=sys.stderr)
        return 2

    inactive_reason = room_inactive_reason(bridge_session)
    if inactive_reason:
        print(f"agent_send_peer: {inactive_reason}; reattach/start a bridge room before sending.", file=sys.stderr)
        return 2
    if not sender_matches_caller(args, bridge_session):
        return 2

    session_dir = STATE_ROOT / bridge_session
    default_state_file = session_dir / "events.raw.jsonl"
    default_public_state_file = session_dir / "events.jsonl"
    default_queue_file = session_dir / "pending.json"

    state_file = Path(
        args.state_file
        or os.environ.get("AGENT_BRIDGE_BUS")
        or os.environ.get("AGENT_BRIDGE_EVENTS")
        or default_state_file
    )
    public_state_file = args.public_state_file or os.environ.get("AGENT_BRIDGE_PUBLIC_EVENTS")
    if public_state_file is None and bridge_session:
        public_state_file = str(default_public_state_file)
    queue_file = Path(args.queue_file or os.environ.get("AGENT_BRIDGE_QUEUE") or default_queue_file)

    body = sys.stdin.read() if args.stdin else args.body
    if not body or not body.strip():
        print("agent_send_peer: --body or --stdin content is required", file=sys.stderr)
        return 2
    if args.target and args.target_all:
        print("agent_send_peer: use either --to <alias> or --all, not both", file=sys.stderr)
        return 2

    kind = normalize_kind(args.kind)
    state = load_session(bridge_session)
    participants = active_participants(state)
    if args.sender != "bridge" and args.sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_send_peer: sender {args.sender!r} is not active in bridge room {bridge_session!r}; "
            f"active aliases: {aliases}. The room may have been stopped, cleaned up, or reattached.",
            file=sys.stderr,
        )
        return 2
    raw_target = "ALL" if args.target_all else args.target
    try:
        targets = resolve_targets(state, args.sender, raw_target)
    except ValueError as exc:
        print(f"agent_send_peer: {exc}", file=sys.stderr)
        return 2

    causal_id = args.causal_id or f"causal-{uuid.uuid4().hex[:12]}"
    ids = []
    for target in targets:
        auto_return = not args.no_auto_return and kind == "request" and args.sender != "bridge" and target != "bridge"
        message = {
            "id": f"msg-{uuid.uuid4().hex}",
            "created_ts": utc_now(),
            "updated_ts": utc_now(),
            "from": args.sender,
            "to": target,
            "kind": kind,
            "intent": args.intent,
            "body": body,
            "causal_id": causal_id,
            "hop_count": args.hop_count,
            "auto_return": auto_return,
            "reply_to": None,
            "source": "external_enqueue",
            "bridge_session": bridge_session,
            "status": "pending",
            "nonce": None,
            "delivery_attempts": 0,
        }

        record = {
            "ts": utc_now(),
            "agent": "bridge",
            "event": "message_queued",
            "message_id": message["id"],
            "from_agent": message["from"],
            "to": message["to"],
            "kind": message["kind"],
            "intent": message["intent"],
            "causal_id": message["causal_id"],
            "hop_count": message["hop_count"],
            "auto_return": message["auto_return"],
            "source": message["source"],
            "bridge_session": bridge_session,
            "body": message["body"],
        }

        update_queue(queue_file, message)
        append_jsonl(state_file, record)
        if public_state_file and Path(public_state_file) != state_file:
            append_jsonl(Path(public_state_file), public_record(record))
        ids.append(message["id"])
    print("\n".join(ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
