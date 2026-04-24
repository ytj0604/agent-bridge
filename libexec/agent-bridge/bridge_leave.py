#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bridge_attach import PANE_LOCKS_FILE, REGISTRY_FILE
from bridge_participants import active_participants, format_peer_summary, load_session, save_session_state
from bridge_paths import libexec_dir
from bridge_util import locked_json


def remove_registry_entries(session: str, alias: str, pane: str, hook_session_id: str) -> None:
    for path, top_key in ((REGISTRY_FILE, "sessions"), (PANE_LOCKS_FILE, "panes")):
        with locked_json(path, {"version": 1, top_key: {}}) as data:
            records = data.setdefault(top_key, {})
            for key, record in list(records.items()):
                if not isinstance(record, dict):
                    continue
                if record.get("bridge_session") != session:
                    continue
                if record.get("alias") == alias or record.get("pane") == pane or record.get("hook_session_id") == hook_session_id:
                    del records[key]


def enqueue_membership_notice(session: str, body: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(libexec_dir() / "bridge_enqueue.py"),
            "--session",
            session,
            "--from",
            "bridge",
            "--all",
            "--kind",
            "notice",
            "--intent",
            "membership_update",
            "--body",
            body,
        ],
        check=False,
    )


def remove_pending_messages(state: dict, alias: str) -> int:
    raw_queue_file = state.get("queue_file")
    if not raw_queue_file:
        return 0
    queue_file = Path(raw_queue_file)
    with locked_json(queue_file, []) as queue:
        kept = [item for item in queue if item.get("to") != alias and item.get("from") != alias]
        removed = len(queue) - len(kept)
        queue[:] = kept
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove a participant alias from a bridge session.")
    parser.add_argument("-s", "--session", default="agent-bridge-auto")
    parser.add_argument("--alias", required=True)
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--json", action="store_true", help="print leave result as JSON")
    args = parser.parse_args()

    state = load_session(args.session)
    participants = active_participants(state)
    record = participants.get(args.alias)
    if not record:
        raise SystemExit(f"alias not found in {args.session}: {args.alias}")

    state.setdefault("participants", {}).pop(args.alias, None)
    state.setdefault("panes", {}).pop(args.alias, None)
    state.setdefault("targets", {}).pop(args.alias, None)
    state.setdefault("hook_session_ids", {}).pop(args.alias, None)
    save_session_state(state)
    remove_registry_entries(args.session, args.alias, str(record.get("pane") or ""), str(record.get("hook_session_id") or ""))
    removed_messages = remove_pending_messages(state, args.alias)

    remaining = active_participants(state)
    if remaining and not args.no_notify:
        body = (
            f"Bridge membership: {args.alias} left. "
            f"{format_peer_summary(state)} "
            "Use agent_list_peers for details."
        )
        enqueue_membership_notice(args.session, body)

    result = {
        "session": args.session,
        "left": args.alias,
        "participants": sorted(remaining),
        "removed_pending_messages": removed_messages,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        print(f"Left {args.alias} from {args.session}")
        print(f"Participants: {', '.join(result['participants']) or '(none)'}")
        if removed_messages:
            print(f"Removed pending messages: {removed_messages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
