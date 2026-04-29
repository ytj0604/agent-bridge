#!/usr/bin/env python3
"""Clear a peer's model context through a controlled /clear probe."""

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


CLEAR_SOCKET_TIMEOUT_SECONDS = 180.0


def send_command(bridge_session: str, payload: dict) -> tuple[bool, dict, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, {}, f"daemon socket not found: {socket_path}"
    request = json.dumps(payload, ensure_ascii=True) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(CLEAR_SOCKET_TIMEOUT_SECONDS)
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
        return False, response if isinstance(response, dict) else {}, str(response.get("error") or "daemon rejected clear")
    return True, response, ""


def clear_summary(target: str, response: dict) -> dict:
    return {
        "target": response.get("target") or target,
        "cleared": bool(response.get("cleared")),
        "deferred": bool(response.get("deferred")),
        "force": bool(response.get("force")),
        "new_session_id": response.get("new_session_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_clear_peer",
        description="Run a controlled /clear for a bridge peer and wait for the bridge clear probe to finish.",
    )
    parser.add_argument("target", nargs="?", help="peer alias to clear")
    parser.add_argument("--to", dest="target_opt", help="peer alias to clear")
    parser.add_argument("--force", action="store_true", help="force-clear soft blockers such as pending rows, target-owned alarms, and target-originated auto-return routes")
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--allow-spoof", action="store_true")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args()

    if args.target and args.target_opt:
        print("agent_clear_peer: use either positional target or --to, not both", file=sys.stderr)
        return 2
    target = args.target_opt or args.target or ""
    if not target:
        print("agent_clear_peer: target alias required", file=sys.stderr)
        return 2

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""
    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=sender,
        allow_spoof=args.allow_spoof,
        tool_name="agent_clear_peer",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias
    if not session or not sender:
        print("agent_clear_peer: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_clear_peer: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_enqueue:
        print(f"agent_clear_peer: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender != "bridge" and sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(f"agent_clear_peer: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.", file=sys.stderr)
        return 2
    if target not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(f"agent_clear_peer: target {target!r} is not active in bridge room {session!r}; active aliases: {aliases}.", file=sys.stderr)
        return 2

    ok, response, error = send_command(session, {"op": "clear_peer", "from": sender, "target": target, "force": bool(args.force)})
    if not ok:
        if args.json:
            print(json.dumps({"target": target, "ok": False, "error": error, **response}, ensure_ascii=False, indent=2))
        else:
            print(f"agent_clear_peer: {error}", file=sys.stderr)
        return 1

    summary = clear_summary(target, response)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif summary.get("deferred"):
        print(f"agent_clear_peer: self-clear for {target} is scheduled after the current turn ends (deferred).")
        print("Hint: further sends from this alias are blocked once the clear is reserved.", file=sys.stderr)
    else:
        print(f"agent_clear_peer: cleared {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

