#!/usr/bin/env python3
"""Cancel one sender-owned bridge message before it becomes active peer work."""

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


def cancel_error_message(message_id: str, error: str, response: dict) -> str:
    if error == "unsupported command":
        return (
            "agent_cancel_message: connected daemon does not support cancel_message yet. "
            "Reload/restart the bridge daemon after updating Agent Bridge."
        )
    if error == "message_not_found":
        return (
            f"agent_cancel_message: message {message_id!r} not found in queue "
            "(already responded, cancelled, interrupted, daemon restarted, or invalid id)."
        )
    if error == "not_owner":
        return f"agent_cancel_message: message {message_id!r} was not sent by you; only the original sender can cancel it."
    if error == "message_active_use_interrupt":
        target = str(response.get("target") or "<peer>")
        status = str(response.get("status") or "active")
        return (
            f"agent_cancel_message: message {message_id!r} is already {status} at {target}; "
            f"use agent_interrupt_peer {target} to stop active peer work."
        )
    if error == "message_not_cancellable_state":
        status = str(response.get("status") or "unknown")
        return f"agent_cancel_message: message {message_id!r} is not cancellable in state {status!r}."
    return f"agent_cancel_message: {error}"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_cancel_message",
        description=(
            "Cancel one message you originally sent while it is still pending or in bridge delivery. "
            "Use agent_interrupt_peer for submitted/delivered active peer work."
        ),
    )
    parser.add_argument("message_id", help="message id to cancel")
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
        tool_name="agent_cancel_message",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_cancel_message: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_cancel_message: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_enqueue:
        print(f"agent_cancel_message: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_cancel_message: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    ok, response, error = send_command(
        session,
        {"op": "cancel_message", "from": sender, "message_id": args.message_id},
    )
    if not ok:
        print(cancel_error_message(args.message_id, error, response), file=sys.stderr)
        return 1

    summary = {
        "message_id": response.get("message_id") or args.message_id,
        "cancelled": bool(response.get("cancelled")),
        "already_terminal": bool(response.get("already_terminal")),
        "target": response.get("target"),
        "status_before": response.get("status_before"),
        "aggregate_id": response.get("aggregate_id"),
    }
    if response.get("terminal_reason"):
        summary["terminal_reason"] = response.get("terminal_reason")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
