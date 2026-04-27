#!/usr/bin/env python3
"""Show the caller's own bridge wait/queue status."""

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
        return False, response if isinstance(response, dict) else {}, str(response.get("error") or "daemon rejected request")
    return True, response, ""


def wait_status_error_message(error: str) -> str:
    if error == "unsupported command":
        return (
            "agent_wait_status: connected daemon does not support wait_status yet. "
            "Reload/restart the bridge daemon after updating Agent Bridge."
        )
    return f"agent_wait_status: {error}"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_wait_status",
        description=(
            "Show your own bridge waits and queued inbound messages as JSON. "
            "Use only for a human-prompted check, after a watchdog wake, or when debugging suspected bridge state; "
            "do not poll for progress or replace waiting for [bridge:*] prompts."
        ),
    )
    parser.add_argument("--summary", action="store_true", help="print only summary counts and truncation flags")
    parser.add_argument("--json", action="store_true", help="print JSON output (default)")
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
        tool_name="agent_wait_status",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_wait_status: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_wait_status: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_read:
        print(f"agent_wait_status: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_wait_status: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    ok, response, error = send_command(session, {"op": "wait_status", "from": sender})
    if not ok:
        print(wait_status_error_message(error), file=sys.stderr)
        return 1

    output = {
        "ok": True,
        "bridge_session": response.get("bridge_session") or session,
        "caller": response.get("caller") or sender,
        "generated_ts": response.get("generated_ts"),
        "limits": response.get("limits") or {},
        "summary": response.get("summary") or {},
    } if args.summary else response
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
