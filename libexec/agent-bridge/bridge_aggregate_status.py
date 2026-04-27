#!/usr/bin/env python3
"""Show one caller-owned aggregate broadcast's leg-level status."""

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


def aggregate_status_error_message(aggregate_id: str, error: str) -> str:
    if error == "unsupported command":
        return (
            "agent_aggregate_status: connected daemon does not support aggregate_status yet. "
            "Reload/restart the bridge daemon after updating Agent Bridge."
        )
    if error in {"aggregate_not_found", "aggregate_id_required"}:
        return (
            f"agent_aggregate_status: aggregate {aggregate_id!r} was not found for your sender alias "
            "(unknown id, not yours, expired, or lost across daemon restart)."
        )
    return f"agent_aggregate_status: {error}"


def summary_response(response: dict) -> dict:
    watchdog = response.get("aggregate_response_watchdog")
    legs = response.get("legs") or {}
    summary_legs = []
    for leg in legs.get("items") or []:
        summary_legs.append({
            "target": leg.get("target"),
            "msg_id": leg.get("msg_id"),
            "status": leg.get("status"),
            "response_received": bool(leg.get("response_received")),
            "synthetic": bool(leg.get("synthetic")),
            "terminal_reason": leg.get("terminal_reason") or "",
        })
    return {
        "ok": True,
        "bridge_session": response.get("bridge_session"),
        "caller": response.get("caller"),
        "aggregate_id": response.get("aggregate_id"),
        "status": response.get("status"),
        "mode": response.get("mode"),
        "replied_count": response.get("replied_count"),
        "total_count": response.get("total_count"),
        "missing_count": response.get("missing_count"),
        "legs": {
            "total_count": legs.get("total_count", 0),
            "returned_count": legs.get("returned_count", 0),
            "truncated": bool(legs.get("truncated")),
            "items": summary_legs,
        },
        "aggregate_response_watchdog": {"present": True} if watchdog else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_aggregate_status",
        description=(
            "Show leg-level status for one aggregate broadcast or partial broadcast you started. "
            "agent_wait_status summarizes all of your waits; this inspects one aggregate id. "
            "Use only for a human-prompted check, after a watchdog wake, or bridge-state debugging; do not poll."
        ),
    )
    parser.add_argument("aggregate_id", help="aggregate id to inspect")
    parser.add_argument("--summary", action="store_true", help="print compact leg status without per-leg timestamps or response sizes")
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
        tool_name="agent_aggregate_status",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_aggregate_status: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_aggregate_status: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_read:
        print(f"agent_aggregate_status: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_aggregate_status: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    ok, response, error = send_command(
        session,
        {"op": "aggregate_status", "from": sender, "aggregate_id": args.aggregate_id},
    )
    if not ok:
        print(aggregate_status_error_message(args.aggregate_id, error), file=sys.stderr)
        return 1

    output = summary_response(response) if args.summary else response
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
