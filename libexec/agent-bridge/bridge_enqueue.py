#!/usr/bin/env python3
import argparse
import errno
import json
import os
import socket
import sys
import uuid
from pathlib import Path

from bridge_daemon_client import ensure_daemon_running
from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session, resolve_targets, room_status
from bridge_paths import run_root, state_root
from bridge_response_guard import (
    contexts_from_queue,
    format_response_send_violation,
    response_send_violation,
)
from bridge_util import MESSAGE_KINDS, append_jsonl, locked_json, locked_json_read, normalize_kind, public_record, short_id, utc_now

# v1.5: enforce request-only watchdog and provide a default delay for
# requests. Default 5 minutes, override via env. The watchdog itself is
# armed at delivery time inside the daemon (mark_message_delivered uses
# the watchdog_delay_sec metadata).
USER_SENDABLE_KINDS = sorted({"request", "notice"})


def should_create_aggregate(kind: str, sender: str, no_auto_return: bool, targets: list[str]) -> bool:
    """Return True iff this enqueue should generate a shared aggregate_id.

    Aggregate is meaningful only when there are multiple recipients AND a
    reply route exists. notice / sender=="bridge" / single-target /
    --no-auto-return all skip aggregate. Whether the multi-target came
    from --all or from --to a,b,c is irrelevant — both deserve the same
    aggregate UX.
    """
    return (
        kind == "request"
        and sender != "bridge"
        and not no_auto_return
        and len(targets) > 1
    )


def _resolve_default_watchdog_seconds() -> float | None:
    raw = os.environ.get("AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC")
    if raw is None:
        return 300.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 300.0
    if val <= 0:
        return None
    return val


WRITE_FAILURE_ERRNOS = {errno.EROFS, errno.EACCES, errno.EPERM}


def diagnostic_error(value: str, limit: int = 200) -> str:
    return str(value or "").replace("\n", " ")[:limit]


def update_queue(path: Path, message: dict) -> None:
    with locked_json(path, []) as queue:
        if not any(item.get("id") == message["id"] for item in queue):
            queue.append(message)


def write_failure_message(path: Path, exc: OSError, ipc_error: str = "") -> str:
    suffix = f" Daemon socket fallback also failed: {ipc_error}." if ipc_error else ""
    if exc.errno in WRITE_FAILURE_ERRNOS:
        return (
            f"agent_send_peer: cannot enqueue message because bridge state is not writable: {path}: {exc.strerror}. "
            "Identity lookup can read existing room state, but sending requires write access to the room queue. "
            f"Restore write permissions on {path.parent}, or recreate the room with AGENT_BRIDGE_RUNTIME_DIR/AGENT_BRIDGE_STATE_DIR pointing to a writable filesystem shared by the agents."
            f"{suffix}"
        )
    return f"agent_send_peer: failed to write bridge queue/event state: {path}: {exc}.{suffix}"


def enqueue_via_daemon_socket(bridge_session: str, messages: list[dict], *, force_response_send: bool = False) -> tuple[bool, list[str], str, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, [], "", ""
    payload = {"op": "enqueue", "messages": messages}
    if force_response_send:
        payload["force_response_send"] = True
    request = json.dumps(payload, ensure_ascii=True) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(socket_path))
            client.sendall(request.encode("utf-8"))
            raw = b""
            while b"\n" not in raw and len(raw) < 2_000_000:
                chunk = client.recv(65536)
                if not chunk:
                    break
                raw += chunk
    except OSError as exc:
        return False, [], f"{socket_path}: {exc}", ""
    try:
        response = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return True, [], f"invalid daemon socket response: {exc}", ""
    if not response.get("ok"):
        return (
            True,
            [],
            str(response.get("error") or "daemon socket enqueue failed"),
            str(response.get("error_kind") or ""),
        )
    ids = response.get("ids") or []
    return True, [str(item) for item in ids], "", ""


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
    parser.add_argument("--kind", choices=USER_SENDABLE_KINDS, default="request")
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
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--watchdog",
        type=float,
        default=None,
        help="seconds (counted from delivery, i.e. when the prompt is injected into the peer's pane) after which to wake the sender if no response_finished arrived. 0 disables.",
    )
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

    status = room_status(bridge_session)
    if not status.active_enough_for_enqueue:
        print(f"agent_send_peer: {status.reason}; reattach/start a bridge room before sending.", file=sys.stderr)
        return 2
    if not sender_matches_caller(args, bridge_session):
        return 2

    session_dir = state_root() / bridge_session
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

    causal_id = args.causal_id or short_id("causal")
    aggregate_id = ""
    if should_create_aggregate(kind, args.sender, args.no_auto_return, targets):
        aggregate_id = short_id("agg")

    # v1.5 watchdog: enforce request-only at the enqueue boundary as well
    # (defense-in-depth; bridge_send_peer also rejects). Resolve the delay
    # in seconds: explicit --watchdog overrides; if unset for kind=request,
    # apply the env-driven default. --watchdog 0 disables.
    if args.watchdog is not None and kind != "request":
        print(
            f"agent_send_peer: --watchdog only applies to --kind request (got {kind!r}). For notice, use agent_alarm.",
            file=sys.stderr,
        )
        return 2
    watchdog_delay_sec: float | None = None
    if args.watchdog is not None:
        try:
            val = float(args.watchdog)
        except (TypeError, ValueError):
            print(f"agent_send_peer: invalid --watchdog value {args.watchdog!r}", file=sys.stderr)
            return 2
        if val > 0:
            watchdog_delay_sec = val
        else:
            watchdog_delay_sec = None  # explicit disable
    elif kind == "request" and args.sender != "bridge":
        watchdog_delay_sec = _resolve_default_watchdog_seconds()
    messages_and_records = []
    for target in targets:
        auto_return = not args.no_auto_return and kind == "request" and args.sender != "bridge" and target != "bridge"
        message = {
            "id": short_id("msg"),
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
            # Transient ingress status. The daemon promotes it to "pending"
            # inside _apply_alarm_cancel_to_queued_message after applying
            # alarm cancel, all under state_lock so it cannot be picked up
            # by reserve_next mid-ingest. This is what closes the race
            # where the file-fallback path (codex sandbox) wrote queue.json
            # before the daemon had a chance to apply alarm cancel.
            "status": "ingressing",
            "nonce": None,
            "delivery_attempts": 0,
        }
        if aggregate_id and auto_return:
            message["aggregate_id"] = aggregate_id
            message["aggregate_expected"] = list(targets)
        if watchdog_delay_sec is not None:
            message["watchdog_delay_sec"] = watchdog_delay_sec

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
        if aggregate_id and auto_return:
            record["aggregate_id"] = aggregate_id
            record["aggregate_expected"] = list(targets)

        messages_and_records.append((message, record))

    messages = [message for message, _record in messages_and_records]
    if aggregate_id:
        aggregate_message_ids = {str(message["to"]): str(message["id"]) for message in messages}
        for message, record in messages_and_records:
            if message.get("aggregate_id") == aggregate_id:
                message["aggregate_message_ids"] = aggregate_message_ids
                record["aggregate_message_ids"] = aggregate_message_ids
    attempted_ipc, ipc_ids, ipc_error, ipc_error_kind = enqueue_via_daemon_socket(bridge_session, messages, force_response_send=args.force)
    if attempted_ipc:
        if ipc_error:
            if ipc_error_kind == "response_send_guard":
                print(ipc_error, file=sys.stderr)
                return 2
            print(f"agent_send_peer: {ipc_error}", file=sys.stderr)
            return 1
        print("\n".join(ipc_ids))
        return 0

    violation = response_send_violation(
        sender=args.sender,
        targets=targets,
        outgoing_kind=kind,
        force=args.force,
        contexts=contexts_from_queue(args.sender, locked_json_read(queue_file, [])),
        source="queue",
    )
    if violation:
        print(format_response_send_violation(violation), file=sys.stderr)
        return 2

    # File-write fallback: the message is written directly to queue.json
    # in the transient status="ingressing" state and the message_queued
    # event is appended to events.raw.jsonl. If the daemon is alive, it
    # will pick up the event in its tail loop and run the same finalize
    # step as the socket path (alarm cancel + body prepend + promote to
    # pending). If the daemon is down, the message sits as "ingressing"
    # until daemon startup recovery promotes it (alarms cancelled at that
    # point are in-memory only and lost across the restart, so the
    # recovery skips alarm cancel). This successful fallback is silent to
    # the model pane; operator diagnostics are recorded in raw events.

    ids = []
    for message, record in messages_and_records:
        try:
            update_queue(queue_file, message)
        except OSError as exc:
            print(write_failure_message(queue_file, exc, ipc_error), file=sys.stderr)
            return 1
        try:
            append_jsonl(state_file, record)
        except OSError as exc:
            print(write_failure_message(state_file, exc, ipc_error), file=sys.stderr)
            return 1
        if public_state_file and Path(public_state_file) != state_file:
            public_path = Path(public_state_file)
            try:
                append_jsonl(public_path, public_record(record))
            except OSError as exc:
                print(write_failure_message(public_path, exc, ipc_error), file=sys.stderr)
                return 1
        ids.append(message["id"])
    fallback_record = {
        "ts": utc_now(),
        "agent": "bridge",
        "event": "enqueue_file_fallback",
        "bridge_session": bridge_session,
        "from_agent": args.sender,
        "targets": list(targets),
        "kind": kind,
        "message_ids": list(ids),
        "reason": "daemon_socket_unavailable",
    }
    if ipc_error:
        fallback_record["socket_error"] = diagnostic_error(ipc_error)
    try:
        append_jsonl(state_file, fallback_record)
    except OSError:
        pass
    print("\n".join(ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
