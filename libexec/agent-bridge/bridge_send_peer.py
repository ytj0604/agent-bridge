#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session
from bridge_paths import libexec_dir, python_exe
from bridge_util import read_limited_text, validate_peer_body_size

# Agents may only originate "request" or "notice"; "result" is system-only
# (set by the daemon when auto-returning a single reply or when an aggregate
# completes). Hard-rejecting --kind result at the CLI prevents a user from
# accidentally injecting a fake auto-routed reply.
USER_SENDABLE_KINDS = sorted({"request", "notice"})
RESERVED_IMPLICIT_TARGETS = {"ALL", "all", "*"}
STDIN_OPTION = "--stdin"
DESTINATION_OPTIONS = {"--to", "-t", "--all", "--broadcast"}
SHELL_BODY_HINT = (
    "Inline body must be one shell argument. For apostrophes, newlines, option-like text, "
    "or any body that may be split by shell quoting, use: "
    "agent_send_peer --to <alias> --stdin <<'EOF' ... EOF"
)
FOLLOW_UP_WAIT_HINT = (
    "Waiting for a bridge follow-up? Do not sleep or poll; "
    "continue independent local work or end your turn."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_send_peer",
        description="Send a message from the current attached agent pane to one peer, a comma-separated subset, or all peers.",
        allow_abbrev=False,
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
    parser.add_argument("--stdin", dest="stdin_body", action="store_true", help="read the message body from stdin")
    parser.add_argument("--allow-spoof", action="store_true", help="allow --from/--session to differ from the caller tmux pane lock")
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("message", nargs="*")
    return parser


def option_kinds_from_parser(parser: argparse.ArgumentParser) -> tuple[set[str], set[str]]:
    value_options: set[str] = set()
    flag_options: set[str] = set()
    for action in parser._actions:
        option_strings = list(getattr(action, "option_strings", []) or [])
        if not option_strings:
            continue
        if getattr(action, "nargs", None) == 0:
            flag_options.update(option_strings)
        else:
            value_options.update(option_strings)
    return value_options, flag_options


def _classify_option_token(
    token: str,
    value_options: set[str],
    flag_options: set[str],
) -> tuple[str, bool, bool] | None:
    if token == "--" or not token.startswith("-") or token == "-":
        return None
    if token.startswith("--"):
        base = token.split("=", 1)[0]
        if base in value_options:
            return base, True, "=" in token
        if base in flag_options:
            return base, False, False
        return token, False, False
    base = token[:2]
    if base in value_options:
        return base, True, len(token) > 2
    if token in flag_options:
        return token, False, False
    return token, False, False


def _extract_session_arg(argv: list[str]) -> str:
    for index, token in enumerate(argv):
        if token.startswith("--session="):
            return token.split("=", 1)[1]
        if token == "--session" and index + 1 < len(argv):
            return argv[index + 1]
    return os.environ.get("AGENT_BRIDGE_SESSION") or ""


def _precheck_participants(argv: list[str]) -> set[str]:
    session = _extract_session_arg(argv)
    if not session:
        return set()
    try:
        return set(active_participants(load_session(session)))
    except Exception:
        return set()


def _remaining_has_body_or_stdin(argv: list[str], start: int) -> bool:
    return any(token == STDIN_OPTION or not token.startswith("-") for token in argv[start:])


def validate_send_peer_argv(
    argv: list[str],
    parser: argparse.ArgumentParser,
    *,
    participants: set[str] | None = None,
) -> str:
    value_options, flag_options = option_kinds_from_parser(parser)
    participants = participants if participants is not None else _precheck_participants(argv)
    destination_selected = False
    body_seen = False
    stdin_seen = False
    destination_count = 0
    index = 0

    while index < len(argv):
        token = argv[index]
        if token == "--":
            return (
                "the -- separator is not supported by agent_send_peer. "
                f"{SHELL_BODY_HINT}"
            )

        opt = _classify_option_token(token, value_options, flag_options)
        if opt is not None and (opt[0] in value_options or opt[0] in flag_options):
            opt_name, takes_value, attached_value = opt
            if destination_selected:
                if opt_name == STDIN_OPTION and not body_seen:
                    stdin_seen = True
                    index += 1
                    continue
                return (
                    f"option {opt_name} appeared after the destination. "
                    "Put all options before --to/--all, or use --stdin for complex message bodies. "
                    f"{SHELL_BODY_HINT}"
                )
            if body_seen:
                return (
                    f"option {opt_name} appeared after the inline body. "
                    f"{SHELL_BODY_HINT}"
                )
            if opt_name == STDIN_OPTION:
                stdin_seen = True
            if opt_name in DESTINATION_OPTIONS:
                destination_count += 1
                if destination_count > 1:
                    return "use exactly one destination selector: --to <alias>, --to <a>,<b>, or --all"
                destination_selected = True
            if takes_value and not attached_value:
                index += 1
                if index >= len(argv):
                    break
            index += 1
            continue

        if token.startswith("-"):
            return (
                f"unrecognized option or option-like inline body {token!r}. "
                "Bodies that begin with '-' must be sent with --stdin. "
                f"{SHELL_BODY_HINT}"
            )

        if destination_selected:
            if stdin_seen:
                return "cannot combine --stdin with a positional inline body"
            if body_seen:
                return f"message body was split into multiple shell arguments. {SHELL_BODY_HINT}"
            body_seen = True
            index += 1
            continue

        if stdin_seen and token not in participants and token not in RESERVED_IMPLICIT_TARGETS:
            return "cannot combine --stdin with a positional inline body"

        if (
            not body_seen
            and (token in participants or token in RESERVED_IMPLICIT_TARGETS)
            and _remaining_has_body_or_stdin(argv, index + 1)
        ):
            destination_selected = True
            destination_count += 1
            index += 1
            continue

        if body_seen:
            return f"message body was split into multiple shell arguments. {SHELL_BODY_HINT}"
        body_seen = True
        index += 1

    return ""


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
    stdin_cache: str | None = None

    def non_tty_stdin_text() -> str:
        nonlocal stdin_cache
        if sys.stdin.isatty():
            return ""
        if stdin_cache is None:
            stdin_cache = read_limited_text(sys.stdin)
        return stdin_cache

    if target and target_all:
        raise ValueError("use either --to <alias> or --all, not both")

    if target_all and words and words[0] in participants:
        raise ValueError(
            f"--all broadcasts the whole message body; remove leading alias {words[0]!r} "
            f"or use --to {words[0]}"
        )

    if not target and not target_all and words and (len(words) >= 2 or args.stdin_body):
        first = words[0]
        if first in participants:
            target = first
            words = words[1:]
        elif first in {"ALL", "all", "*"}:
            target_all = True
            words = words[1:]

    if args.stdin_body:
        if words:
            raise ValueError("cannot combine --stdin with a positional inline body")
        body = read_limited_text(sys.stdin)
    elif words:
        if len(words) > 1:
            raise ValueError(f"message body was split into multiple shell arguments. {SHELL_BODY_HINT}")
        if non_tty_stdin_text():
            raise ValueError("cannot combine piped stdin with a positional inline body")
        body = words[0]
    elif not sys.stdin.isatty():
        body = non_tty_stdin_text()
    else:
        body = ""

    args.target_all = target_all
    return target, body


def main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    precheck_error = validate_send_peer_argv(argv, parser)
    if precheck_error:
        print(f"agent_send_peer: {precheck_error}", file=sys.stderr)
        return 2
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
    ok, size_error = validate_peer_body_size(body)
    if not ok:
        print(size_error, file=sys.stderr)
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
        "--stdin",
    ]
    if args.allow_spoof:
        cmd.append("--allow-spoof")
    if args.force:
        cmd.append("--force")
    if args.watchdog is not None:
        cmd += ["--watchdog", str(args.watchdog)]
    if args.target_all:
        cmd.append("--all")
    elif target:
        cmd += ["--to", target]

    proc = subprocess.run(cmd, input=body.encode("utf-8"))
    if proc.returncode == 0 and args.kind == "notice":
        # Notices have no auto-routed reply. Hint the model to set an alarm
        # so they have a safety wake if the follow-up never comes.
        print(
            "notice sent. If you expect a follow-up message and want a safety wake, "
            "run: agent_alarm <sec> --note '<desc>'",
            file=sys.stderr,
        )
    if proc.returncode == 0:
        print(FOLLOW_UP_WAIT_HINT, file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
