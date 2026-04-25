#!/usr/bin/env python3
"""Schedule a self-addressed wake-up alarm via the bridge daemon.

The alarm fires after the requested delay and arrives as a notice in the
caller's pane, same delivery path as peer messages. Useful for deciding to
wait longer after a watchdog wake-up.
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


def request_alarm(bridge_session: str, sender: str, delay_seconds: float, body: str | None) -> tuple[bool, str, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, "", f"daemon socket not found: {socket_path}"
    payload: dict = {"op": "alarm", "from": sender, "delay_seconds": float(delay_seconds)}
    if body:
        payload["body"] = body
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
        return False, "", f"daemon socket error: {exc}"
    try:
        response = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return False, "", f"invalid daemon response: {exc}"
    if not response.get("ok"):
        return False, "", str(response.get("error") or "daemon rejected alarm")
    return True, str(response.get("wake_id") or ""), ""


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_alarm",
        description="Schedule a notice to be delivered to yourself after N seconds.",
    )
    parser.add_argument("delay_seconds", type=float, help="seconds until the wake notice is delivered")
    parser.add_argument("--note", dest="note", help="optional text appended to the wake notice", default=None)
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
        tool_name="agent_alarm",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_alarm: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_alarm: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_enqueue:
        print(f"agent_alarm: {status.reason}; reattach/start a bridge room before scheduling.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_alarm: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    if args.delay_seconds < 0:
        print("agent_alarm: delay_seconds must be non-negative", file=sys.stderr)
        return 2

    ok, wake_id, error = request_alarm(session, sender, args.delay_seconds, args.note)
    if not ok:
        print(f"agent_alarm: {error}", file=sys.stderr)
        return 1
    print(wake_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
