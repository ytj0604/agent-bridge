#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from typing import Any

from bridge_participants import active_participants, load_session, participants_from_state, session_state_exists


def _display(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def format_room_summary(state: dict) -> str:
    raw_participants = state.get("participants")
    if isinstance(raw_participants, dict):
        participants = {
            str(alias): (record if isinstance(record, dict) else {})
            for alias, record in raw_participants.items()
            if not isinstance(record, dict)
            or "status" not in record
            or str(record.get("status") or "").strip() in {"", "active"}
        }
    else:
        participants = active_participants(state) or participants_from_state(state)
    lines = ["Agents:"]
    if not participants:
        lines.append("- (none)")
        return "\n".join(lines)

    for alias in sorted(participants):
        record = participants[alias] or {}
        agent_type = _display(record.get("agent_type") or record.get("type"), "unknown")
        status = _display(record.get("status"), "unknown")
        target = _display(record.get("target"), "?")
        pane = _display(record.get("pane"), "?")
        fields = [
            str(alias),
            agent_type,
            status,
            f"target={target}",
            f"pane={pane}",
        ]
        model = str(record.get("model") or "").strip()
        if model:
            fields.append(f"model={model}")
        lines.append("- " + " ".join(fields))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a concise bridge_manage room summary.")
    parser.add_argument("-s", "--session", required=True)
    args = parser.parse_args()

    if not session_state_exists(args.session):
        print(f"bridge_manage_summary: bridge room {args.session!r} is not active or was stopped/cleaned up", file=sys.stderr)
        return 2
    state = load_session(args.session)
    print(format_room_summary(state))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1)
    except Exception as exc:
        print(f"bridge_manage_summary: {exc}", file=sys.stderr)
        raise SystemExit(2)
