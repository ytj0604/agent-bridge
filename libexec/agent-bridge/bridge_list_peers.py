#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import sys

from bridge_daemon_client import ensure_daemon_running
from bridge_identity import resolve_caller_from_pane
from bridge_instructions import model_cheat_sheet_text
from bridge_participants import active_participants, format_peer_list, load_session, room_status


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent_list_peers", description="List Agent Bridge peers for the current session.")
    parser.add_argument("--session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION")
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT")
    if not session or not sender or os.environ.get("TMUX_PANE"):
        resolution = resolve_caller_from_pane(
            pane=os.environ.get("TMUX_PANE"),
            explicit_session=session or "",
            explicit_alias=sender or "",
            tool_name="agent_list_peers",
            allow_explicit_without_pane=True,
        )
        if not resolution.ok:
            print(resolution.error, file=sys.stderr)
            return 2
        session = session or resolution.session
        sender = sender or resolution.alias

    if not session:
        print(
            "agent_list_peers: cannot infer bridge session; this pane is not attached to an active bridge room "
            "(or the room was stopped). Reattach/start a bridge room first.",
            file=sys.stderr,
        )
        return 2

    status = room_status(session)
    if status.state not in {"alive", "unknown"}:
        ensure_error = ensure_daemon_running(session)
        if ensure_error:
            print(f"agent_list_peers: {ensure_error}", file=sys.stderr)
            return 2
        status = room_status(session)
    if not status.active_enough_for_read:
        print(f"agent_list_peers: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender and sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_list_peers: current alias {sender!r} is not active in bridge room {session!r}; "
            f"active aliases: {aliases}. The room may have been stopped, cleaned up, or reattached.",
            file=sys.stderr,
        )
        return 2
    participants = state.get("participants") or {}
    if args.json:
        print(json.dumps({"session": session, "current": sender, "daemon_status": asdict(status), "participants": participants}, ensure_ascii=True, indent=2))
    else:
        print(f"Bridge session: {session}")
        if status.state == "unknown":
            print(f"Warning: {status.reason}; showing read-only session state.")
        print(format_peer_list(state, sender))
        print("")
        print(model_cheat_sheet_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
