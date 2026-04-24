#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from bridge_daemon_client import ensure_daemon_running
from bridge_instructions import model_cheat_sheet_text
from bridge_participants import active_participants, format_peer_list, load_session, room_inactive_reason
from bridge_paths import state_root
from bridge_util import locked_json_read


def infer_from_pane(pane: str, pane_locks_file: Path) -> tuple[str, str] | tuple[None, None]:
    data = locked_json_read(pane_locks_file, {"version": 1, "panes": {}})
    record = (data.get("panes") or {}).get(pane) or {}
    return record.get("bridge_session"), record.get("alias") or record.get("agent")


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent_list_peers", description="List Agent Bridge peers for the current session.")
    parser.add_argument("--session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pane-locks-file", default=str(state_root() / "pane-locks.json"))
    args = parser.parse_args()

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION")
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT")
    if (not session or not sender) and os.environ.get("TMUX_PANE"):
        inferred_session, inferred_sender = infer_from_pane(os.environ["TMUX_PANE"], Path(args.pane_locks_file))
        session = session or inferred_session
        sender = sender or inferred_sender

    if not session:
        print(
            "agent_list_peers: cannot infer bridge session; this pane is not attached to an active bridge room "
            "(or the room was stopped). Reattach/start a bridge room first.",
            file=sys.stderr,
        )
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_list_peers: {ensure_error}", file=sys.stderr)
        return 2

    inactive_reason = room_inactive_reason(session)
    if inactive_reason:
        print(f"agent_list_peers: {inactive_reason}; reattach/start a bridge room first.", file=sys.stderr)
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
        print(json.dumps({"session": session, "current": sender, "participants": participants}, ensure_ascii=True, indent=2))
    else:
        print(f"Bridge session: {session}")
        print(format_peer_list(state, sender))
        print("")
        print(model_cheat_sheet_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
