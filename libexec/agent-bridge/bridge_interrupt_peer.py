#!/usr/bin/env python3
"""Interrupt a peer or manage legacy interrupt hold state.

Modes:
- (default) interrupt key sequence + cancel active peer turn; pending
  corrections may continue after a complete sequence. Claude targets may
  receive one Ctrl-C after ESC to clear submitted-prompt input residue:
    agent_interrupt_peer <alias>
- Clear legacy hold residue without sending ESC. Normally unnecessary for
  post-v1.5 corrections (UNSAFE if peer might still be running — see warning
  printed by daemon):
    agent_interrupt_peer <alias> --clear-hold
- Inspect status of a peer or all peers:
    agent_interrupt_peer [<alias>] --status
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from bridge_daemon_client import ensure_daemon_running
from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session, room_status
from bridge_paths import run_root


def send_command(bridge_session: str, payload: dict) -> tuple[bool, dict, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, {}, f"daemon socket not found: {socket_path}"
    request = json.dumps(payload, ensure_ascii=True) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(str(socket_path))
            client.sendall(request.encode("utf-8"))
            raw = b""
            while b"\n" not in raw and len(raw) < 2_000_000:
                chunk = client.recv(65536)
                if not chunk:
                    break
                raw += chunk
    except OSError as exc:
        return False, {}, f"daemon socket error: {exc}"
    try:
        response = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return False, {}, f"invalid daemon response: {exc}"
    if not response.get("ok"):
        return False, {}, str(response.get("error") or "daemon rejected request")
    return True, response, ""


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_interrupt_peer",
        description=(
            "Send the agent-type-specific interrupt key sequence (Codex: Escape; Claude: Escape then "
            "one Ctrl-C when active work exists) and cancel the active peer message (default), "
            "force-clear a legacy interrupt hold, or inspect peer status. If the peer is already past "
            "the inflight phase, interrupt can be a no-op: the response and queued follow-ups still flow."
        ),
    )
    parser.add_argument("target", nargs="?", help="peer alias to act on (omit with --status to query all peers)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--clear-hold", action="store_true", help="clear legacy held_interrupt residue or partial interrupt-failure delivery gate without sending keys (unsafe if peer is still running or input is dirty)")
    mode.add_argument("--status", action="store_true", help="show busy/held/queue state for the target (or all peers if no target)")
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--allow-spoof", action="store_true")
    args = parser.parse_args()

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""

    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=sender,
        allow_spoof=args.allow_spoof,
        tool_name="agent_interrupt_peer",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_interrupt_peer: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_interrupt_peer: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_enqueue:
        print(f"agent_interrupt_peer: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_interrupt_peer: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    # --status branch (target optional)
    if args.status:
        target = args.target or ""
        if target and target not in participants:
            aliases = ", ".join(sorted(participants)) or "(none)"
            print(
                f"agent_interrupt_peer: target {target!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
                file=sys.stderr,
            )
            return 2
        ok, response, error = send_command(session, {"op": "status", "from": sender, "target": target})
        if not ok:
            print(f"agent_interrupt_peer: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"peers": response.get("peers") or []}, ensure_ascii=False, indent=2))
        return 0

    # --clear-hold and default branches require a target
    if not args.target:
        print("agent_interrupt_peer: target alias required (or use --status)", file=sys.stderr)
        return 2
    if args.target not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_interrupt_peer: target {args.target!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2
    if args.target == sender and not args.clear_hold:
        print("agent_interrupt_peer: cannot interrupt yourself", file=sys.stderr)
        return 2

    if args.clear_hold:
        ok, response, error = send_command(session, {"op": "clear_hold", "from": sender, "target": args.target})
        if not ok:
            print(f"agent_interrupt_peer: {error}", file=sys.stderr)
            return 1
        summary = {
            "target": args.target,
            "had_hold": bool(response.get("had_hold")),
            "info": response.get("info") or {},
            "warning": response.get("warning"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    # default: ESC + interrupt
    ok, response, error = send_command(session, {"op": "interrupt", "from": sender, "target": args.target})
    if not ok:
        print(f"agent_interrupt_peer: {error}", file=sys.stderr)
        return 1
    summary = {
        "target": args.target,
        "esc_sent": bool(response.get("esc_sent")),
        "interrupt_ok": bool(response.get("interrupt_ok", response.get("esc_sent"))),
        "interrupt_keys": response.get("interrupt_keys") or [],
        "cc_sent": response.get("cc_sent"),
        "held": bool(response.get("held")),
        "cancelled_message_ids": response.get("cancelled_message_ids") or [],
        "prior_active_message_id": response.get("prior_active_message_id"),
    }
    if response.get("esc_error"):
        summary["esc_error"] = response["esc_error"]
    if response.get("cc_error"):
        summary["cc_error"] = response["cc_error"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["interrupt_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
