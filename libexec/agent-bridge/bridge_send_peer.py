#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from bridge_participants import active_participants, load_session
from bridge_paths import libexec_dir, python_exe, state_root
from bridge_util import MESSAGE_KINDS, read_json


def infer_from_pane(pane: str, pane_locks_file: Path) -> tuple[str, str]:
    data = read_json(pane_locks_file, {"version": 1, "panes": {}})
    record = (data.get("panes") or {}).get(pane) or {}
    return str(record.get("bridge_session") or ""), str(record.get("alias") or "")


def validate_caller_identity(args: argparse.Namespace, session: str, sender: str) -> tuple[str, str] | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        if args.allow_spoof:
            return session, sender
        if args.session or args.sender:
            print(
                "agent_send_peer: refusing explicit --session/--from outside an attached tmux pane; "
                "pass --allow-spoof only for an explicit admin/test send",
                file=sys.stderr,
            )
            return None
        return session, sender

    pane_locks_file = Path(os.environ.get("AGENT_BRIDGE_PANE_LOCKS", str(state_root() / "pane-locks.json")))
    locked_session, locked_sender = infer_from_pane(pane, pane_locks_file)

    if args.allow_spoof:
        return session or locked_session, sender or locked_sender

    if not locked_session or not locked_sender:
        if args.session or args.sender:
            print(
                "agent_send_peer: refusing --session/--from from a tmux pane that is not attached to this bridge room; "
                "run from an attached agent pane or pass --allow-spoof for an explicit admin/test send",
                file=sys.stderr,
            )
            return None
        return session, sender

    if args.session and args.session != locked_session:
        print(
            f"agent_send_peer: --session {args.session!r} does not match caller pane room {locked_session!r}; "
            "pass --allow-spoof only for an explicit admin/test send",
            file=sys.stderr,
        )
        return None
    if args.sender and args.sender != locked_sender:
        print(
            f"agent_send_peer: --from {args.sender!r} does not match caller pane alias {locked_sender!r}; "
            "pass --allow-spoof only for an explicit admin/test send",
            file=sys.stderr,
        )
        return None
    return session or locked_session, sender or locked_sender


def parse_body_and_target(args: argparse.Namespace, session: str) -> tuple[str | None, str]:
    target = args.target
    target_all = args.target_all
    words = list(args.message or [])
    state = load_session(session) if session else {}
    participants = active_participants(state)

    if target and target_all:
        raise ValueError("use either --to <alias> or --all, not both")

    if target_all and words and words[0] in participants:
        raise ValueError(
            f"--all broadcasts the whole message body; remove leading alias {words[0]!r} "
            f"or use --to {words[0]}"
        )

    if not target and not target_all and len(words) >= 2:
        first = words[0]
        if first in participants:
            target = first
            words = words[1:]
        elif first in {"ALL", "all", "*"}:
            target_all = True
            words = words[1:]

    if words:
        body = " ".join(words)
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        body = ""

    args.target_all = target_all
    return target, body


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_send_peer",
        description="Send a message from the current attached agent pane to one peer or all peers.",
    )
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("-t", "--to", dest="target")
    parser.add_argument("--all", "--broadcast", dest="target_all", action="store_true")
    parser.add_argument("-i", "--intent", default="message")
    parser.add_argument("--kind", choices=sorted(MESSAGE_KINDS), default="request")
    parser.add_argument("--allow-spoof", action="store_true", help="allow --from/--session to differ from the caller tmux pane lock")
    parser.add_argument("message", nargs="*")
    args = parser.parse_args()

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""
    validated = validate_caller_identity(args, session, sender)
    if validated is None:
        return 2
    session, sender = validated

    if not sender:
        print(
            "agent_send_peer: cannot infer sender; this pane is not attached to an active bridge room "
            "(or the room was stopped). Reattach/start a bridge room or pass --from <alias>",
            file=sys.stderr,
        )
        return 2
    if not session:
        print(
            "agent_send_peer: cannot infer session; this pane is not attached to an active bridge room "
            "(or the room was stopped). Reattach/start a bridge room or pass --session <name>",
            file=sys.stderr,
        )
        return 2

    try:
        target, body = parse_body_and_target(args, session)
    except ValueError as exc:
        print(f"agent_send_peer: {exc}", file=sys.stderr)
        return 2
    if not body.strip():
        print("agent_send_peer: message body is required", file=sys.stderr)
        return 2

    cmd = [
        python_exe(),
        str(libexec_dir() / "bridge_enqueue.py"),
        "--session",
        session,
        "--from",
        sender,
        "--kind",
        args.kind,
        "--intent",
        args.intent,
        "--body",
        body,
    ]
    if args.allow_spoof:
        cmd.append("--allow-spoof")
    if args.target_all:
        cmd.append("--all")
    elif target:
        cmd += ["--to", target]

    proc = subprocess.run(cmd)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
