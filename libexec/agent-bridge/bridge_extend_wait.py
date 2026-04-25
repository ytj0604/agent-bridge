#!/usr/bin/env python3
"""Re-arm the watchdog for a still-pending request.

Used after a watchdog wake when the sender decides to keep waiting on the
same request rather than interrupting. Only the original sender of the
request can extend it; aggregate-member messages are not supported in v1.5.
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


def request_extend(bridge_session: str, sender: str, message_id: str, seconds: float) -> tuple[bool, dict, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, {}, f"daemon socket not found: {socket_path}"
    payload = {
        "op": "extend_watchdog",
        "from": sender,
        "message_id": message_id,
        "seconds": float(seconds),
    }
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
        prog="agent_extend_wait",
        description="Re-arm the watchdog for a request that was previously sent and is still pending; takes effect from now + <sec>.",
    )
    parser.add_argument("message_id", help="message id of the request to extend (printed in watchdog wake notice)")
    parser.add_argument("seconds", type=float, help="additional seconds to wait before the next watchdog wake")
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--allow-spoof", action="store_true")
    args = parser.parse_args()

    if args.seconds <= 0:
        print("agent_extend_wait: seconds must be positive", file=sys.stderr)
        return 2

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""

    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=sender,
        allow_spoof=args.allow_spoof,
        tool_name="agent_extend_wait",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_extend_wait: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_extend_wait: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_enqueue:
        print(f"agent_extend_wait: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_extend_wait: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    ok, response, error = request_extend(session, sender, args.message_id, args.seconds)
    if not ok:
        if error == "message_not_found":
            msg = f"agent_extend_wait: message {args.message_id!r} not found in queue (already responded, interrupted, or invalid id)."
        elif error == "not_owner":
            msg = f"agent_extend_wait: message {args.message_id!r} was not sent by you; only the original sender can extend its watchdog."
        elif error == "aggregate_extend_not_supported":
            msg = f"agent_extend_wait: message {args.message_id!r} is part of an aggregate broadcast; per-message extend is not supported in v1.5."
        elif error == "message_not_in_delivered_state":
            msg = f"agent_extend_wait: message {args.message_id!r} has not been delivered to the peer yet (still pending/inflight). Watchdogs only count down after delivery; use agent_interrupt_peer --status to inspect the target, or interrupt a stuck hold."
        else:
            msg = f"agent_extend_wait: {error}"
        print(msg, file=sys.stderr)
        return 1
    summary = {
        "message_id": response.get("message_id") or args.message_id,
        "new_deadline": response.get("new_deadline"),
        "seconds": args.seconds,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
