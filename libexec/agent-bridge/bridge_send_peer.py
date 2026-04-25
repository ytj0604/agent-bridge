#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session
from bridge_paths import libexec_dir, python_exe
from bridge_util import MESSAGE_KINDS

# Agents may only originate "request" or "notice"; "result" is system-only
# (set by the daemon when auto-returning a single reply or when an aggregate
# completes). Hard-rejecting --kind result at the CLI prevents a user from
# accidentally injecting a fake auto-routed reply.
USER_SENDABLE_KINDS = sorted({"request", "notice"})


def validate_caller_identity(args: argparse.Namespace, session: str, sender: str) -> tuple[str, str] | None:
    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=sender,
        allow_spoof=args.allow_spoof,
        tool_name="agent_send_peer",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return None
    return session or resolution.session, sender or resolution.alias


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
        description="Send a message from the current attached agent pane to one peer, a comma-separated subset, or all peers.",
    )
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("-t", "--to", dest="target")
    parser.add_argument("--all", "--broadcast", dest="target_all", action="store_true")
    parser.add_argument("-i", "--intent", default="message")
    parser.add_argument("--kind", choices=USER_SENDABLE_KINDS, default="request")
    parser.add_argument(
        "--watchdog",
        type=float,
        metavar="SEC",
        help="schedule a watchdog wake after SEC seconds since the prompt is delivered to the peer. Request only. Use 0 to explicitly disable the default watchdog.",
    )
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

    # --watchdog only makes sense for kind=request. Notices have no return
    # route, so a watchdog watching for "no reply" is meaningless. For
    # follow-ups expected on a notice-driven flow, use agent_alarm.
    if args.watchdog is not None and args.kind != "request":
        print(
            "agent_send_peer: --watchdog only applies to --kind request. "
            "For notice, set agent_alarm <sec> --note '<desc>' separately to avoid blocking forever on a follow-up that may never come.",
            file=sys.stderr,
        )
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
    if args.watchdog is not None:
        cmd += ["--watchdog", str(args.watchdog)]
    if args.target_all:
        cmd.append("--all")
    elif target:
        cmd += ["--to", target]

    proc = subprocess.run(cmd)
    if proc.returncode == 0 and args.kind == "notice":
        # Notices have no auto-routed reply. Hint the model to set an alarm
        # so they have a safety wake if the follow-up never comes.
        print(
            "notice sent. If you expect a follow-up message and want a safety wake, "
            "run: agent_alarm <sec> --note '<desc>'",
            file=sys.stderr,
        )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
