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


CANCEL_ERROR_FACTS = {
    "message_not_found": "was not found",
    "not_owner": "was not sent by you",
    "message_active_use_interrupt": "is already active peer work",
    "message_not_cancellable_state": "is not cancellable in its current state",
    "invalid_sender": "has an invalid sender",
}

CANCEL_ERROR_HINTS = {
    "message_not_found": "It may already have responded, been cancelled or interrupted, the daemon may have restarted, or the id may be invalid.",
    "not_owner": "Only the original sender can cancel this message; check the id you intended.",
    "message_not_cancellable_state": "Use agent_cancel_message only before active peer work; use agent_interrupt_peer for active/post-pane-touch work.",
    "invalid_sender": "Reattach or start a bridge room and retry from the original sender.",
}


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
            "agent_cancel_message: connected daemon does not support cancel_message (unsupported_command). "
            "Hint: Reload/restart the bridge daemon after updating Agent Bridge."
        )
    hint = CANCEL_ERROR_HINTS.get(error, "")
    if error == "message_not_found":
        base = f"agent_cancel_message: message {message_id!r} was not found ({error})."
    elif error == "not_owner":
        base = f"agent_cancel_message: message {message_id!r} was not sent by you ({error})."
    elif error == "message_active_use_interrupt":
        target = str(response.get("target") or "<peer>")
        status = str(response.get("status") or "active")
        base = f"agent_cancel_message: message {message_id!r} is already {status} at {target} ({error})."
        hint = f"Use agent_interrupt_peer {target} for active/post-pane-touch work."
    elif error == "message_not_cancellable_state":
        status = str(response.get("status") or "unknown")
        base = f"agent_cancel_message: message {message_id!r} is not cancellable in state {status!r} ({error})."
    elif error in CANCEL_ERROR_FACTS:
        base = f"agent_cancel_message: message {message_id!r} {CANCEL_ERROR_FACTS[error]} ({error})."
    else:
        base = f"agent_cancel_message: {error}"
    if hint:
        return f"{base} Hint: {hint}"
    return base


def cancel_summary(message_id: str, response: dict) -> dict:
    summary = {
        "message_id": response.get("message_id") or message_id,
        "cancelled": bool(response.get("cancelled")),
        "already_terminal": bool(response.get("already_terminal")),
        "target": response.get("target"),
        "status_before": response.get("status_before"),
        "aggregate_id": response.get("aggregate_id"),
    }
    if response.get("terminal_reason"):
        summary["terminal_reason"] = response.get("terminal_reason")
    return summary


def cancel_success_text(message_id: str, response: dict) -> tuple[str, str]:
    summary = cancel_summary(message_id, response)
    if summary["already_terminal"]:
        terminal_reason = str(summary.get("terminal_reason") or "already_terminal")
        body = f"agent_cancel_message: message {summary['message_id']!r} was already terminal ({terminal_reason})."
        hint = "No cancellation was needed; do not retry this id."
        return body, hint
    if summary["cancelled"]:
        status = str(summary.get("status_before") or "unknown")
        target = str(summary.get("target") or "<peer>")
        body = (
            f"agent_cancel_message: cancelled message {summary['message_id']!r} before active peer work "
            f"(cancelled); target={target} status_before={status}."
        )
        hint = "No further action is needed."
        return body, hint
    body = f"agent_cancel_message: message {summary['message_id']!r} did not require cancellation (ok)."
    return body, "No further action is needed."


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
    parser.add_argument("--json", action="store_true", help="print the compatibility JSON summary instead of text")
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

    if args.json:
        print(json.dumps(cancel_summary(args.message_id, response), ensure_ascii=False, indent=2))
    else:
        body, hint = cancel_success_text(args.message_id, response)
        print(body)
        if hint:
            print(f"Hint: {hint}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
