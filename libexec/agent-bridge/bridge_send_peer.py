#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import math
import os
import select
import subprocess
import sys
from dataclasses import dataclass

from bridge_identity import read_attached_mapping, read_live_by_pane, read_pane_lock, resolve_caller_from_pane
from bridge_participants import active_participants, load_session
from bridge_paths import libexec_dir, python_exe
from bridge_util import MAX_INLINE_SEND_BODY_CHARS, read_limited_text, validate_peer_body_size

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
MISSING_BODY_MESSAGE = (
    "message body is required. Provide it as a single inline argument after the options/target, "
    "or pass --stdin and pipe the body in via heredoc (<<'EOF' ... EOF). "
    "Example: agent_send_peer <alias> 'hello' or agent_send_peer <alias> --stdin <<'EOF' ... EOF."
)
EMPTY_STDIN_MESSAGE = (
    "--stdin received an empty body (no data on stdin / heredoc body was empty). "
    "Pass non-empty body lines between <<'EOF' and EOF."
)
UNEXPECTED_POSITIONAL_AFTER_STDIN_MESSAGE = (
    "--stdin reads body from stdin; extra positional argument(s) after --stdin are not allowed. "
    "Either drop --stdin and pass the inline body, or remove the trailing positional and pipe via heredoc."
)
STDIN_INLINE_CONFLICT_MESSAGE = (
    "--stdin and an inline body cannot be combined. Pick one: either pass the inline body as a single argument, "
    "or use --stdin with stdin/heredoc and no inline body."
)
PIPED_STDIN_INLINE_CONFLICT_MESSAGE = (
    "piped stdin and an inline body cannot be combined. Pick one: either remove the pipe and pass the inline body "
    "as a single argument, or drop the inline body and use --stdin with stdin/heredoc."
)
REQUEST_SENT_HINT = (
    "REQUEST_SENT: result arrives later as a new [bridge:*] prompt. "
    "Do independent work only; do not sleep/poll or keep this turn open waiting."
)
NOTICE_SENT_HINT = (
    "NOTICE_SENT: no reply auto-routes. "
    "Do not wait for one; set agent_alarm only if a follow-up matters."
)
AMBIENT_STDIN_READ_BYTES = (MAX_INLINE_SEND_BODY_CHARS + 1) * 4 + 4


@dataclass(frozen=True)
class BodyInputError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def body_input_error(code: str) -> BodyInputError:
    messages = {
        "missing_body": MISSING_BODY_MESSAGE,
        "empty_stdin": EMPTY_STDIN_MESSAGE,
        "unexpected_positional_after_stdin": UNEXPECTED_POSITIONAL_AFTER_STDIN_MESSAGE,
        "stdin_inline_conflict": STDIN_INLINE_CONFLICT_MESSAGE,
        "piped_stdin_inline_conflict": PIPED_STDIN_INLINE_CONFLICT_MESSAGE,
    }
    return BodyInputError(code, messages[code])


def format_body_input_error(error: BodyInputError) -> str:
    return f"agent_send_peer: {error.code}: {error.message}"


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
        help=(
            "schedule watchdog wakes using the same <sec> per phase. "
            "Per-phase watchdog timer, not a queue timer: delivery/submission starts at "
            "pending -> inflight, response starts at inflight -> delivered after prompt delivery. "
            "A request may wait up to two phase intervals; AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC=300 "
            "means up to 300s delivery + 300s response. Request only; "
            "use --watchdog 0 to disable the default."
        ),
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
            if "=" in token:
                return token, False, False
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


def _precheck_lookup_session_for_pane(pane: str | None) -> str:
    """Best-effort, read-only room lookup for syntax precheck.

    This deliberately does not call resolve_caller_from_pane(): authoritative
    identity validation happens later and may repair/reconnect state. Precheck
    only needs enough participant context to classify the leading-alias
    shorthand, and must fail open without surfacing identity errors.
    """
    pane = pane or ""
    if not pane:
        return ""
    try:
        live = read_live_by_pane(pane)
        if live:
            agent_type = str(live.get("agent") or "")
            session_id = str(live.get("session_id") or "")
            mapping = read_attached_mapping(agent_type, session_id)
            if mapping and str(mapping.get("bridge_session") or ""):
                return str(mapping.get("bridge_session") or "")
        lock = read_pane_lock(pane)
        return str(lock.get("bridge_session") or "")
    except Exception:
        return ""


def _precheck_session(argv: list[str]) -> str:
    session = _extract_session_arg(argv)
    if not session:
        session = _precheck_lookup_session_for_pane(os.environ.get("TMUX_PANE"))
    return session


def _precheck_participants(argv: list[str]) -> set[str]:
    session = _precheck_session(argv)
    if not session:
        return set()
    try:
        return set(active_participants(load_session(session)))
    except Exception:
        return set()


def _remaining_has_cli_tail(argv: list[str], start: int) -> bool:
    # Leading-alias shorthand should be selected when anything follows the
    # alias. Options are allowed after the alias, so don't try to classify
    # option values as body text here.
    return start < len(argv)


def validate_send_peer_argv(
    argv: list[str],
    parser: argparse.ArgumentParser,
    *,
    participants: set[str] | None = None,
) -> str | BodyInputError:
    value_options, flag_options = option_kinds_from_parser(parser)
    precheck_session = _precheck_session(argv) if participants is None else ""
    participants = participants if participants is not None else _precheck_participants(argv)
    destination_selected = False
    body_seen = False
    stdin_seen = False
    allow_spoof_seen = False
    option_after_destination_seen = False
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
            if body_seen:
                if opt_name == STDIN_OPTION:
                    return body_input_error("stdin_inline_conflict")
                return (
                    f"option {opt_name} appeared after the inline body. "
                    f"{SHELL_BODY_HINT}"
                )
            if destination_selected:
                option_after_destination_seen = True
                if opt_name == "--allow-spoof":
                    return "option --allow-spoof must appear before the destination"
            if opt_name == STDIN_OPTION:
                stdin_seen = True
            # For post-destination --allow-spoof, the branch above has already
            # returned. This tracks only the pre-destination scan.
            if opt_name == "--allow-spoof":
                allow_spoof_seen = True
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
                return body_input_error("unexpected_positional_after_stdin")
            if body_seen:
                return f"message body was split into multiple shell arguments. {SHELL_BODY_HINT}"
            body_seen = True
            index += 1
            continue

        if stdin_seen and token not in participants and token not in RESERVED_IMPLICIT_TARGETS:
            return body_input_error("unexpected_positional_after_stdin")

        if (
            allow_spoof_seen
            and not precheck_session
            and token not in RESERVED_IMPLICIT_TARGETS
            and _remaining_has_cli_tail(argv, index + 1)
        ):
            return (
                "leading-alias shorthand cannot be validated under --allow-spoof "
                "without an inferable bridge session. Use --to <alias>, and pass "
                "--session <name> if this is an admin/test operation outside an attached pane."
            )

        if (
            not body_seen
            and (token in participants or token in RESERVED_IMPLICIT_TARGETS)
            and _remaining_has_cli_tail(argv, index + 1)
        ):
            destination_selected = True
            destination_count += 1
            index += 1
            continue

        if body_seen:
            return f"message body was split into multiple shell arguments. {SHELL_BODY_HINT}"
        body_seen = True
        index += 1

    if destination_selected and option_after_destination_seen and not body_seen and not stdin_seen:
        return body_input_error("missing_body")

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


def read_available_ambient_stdin_text(stream, limit: int = MAX_INLINE_SEND_BODY_CHARS) -> str:
    """Read only stdin bytes that are available immediately.

    Ambient stdin is used for pipe-only body inference and for detecting a
    pipe+inline-body collision. Unlike explicit --stdin, it must not wait for a
    future writer: model harnesses can attach fd 0 to an open idle socket.
    """
    if stream.isatty():
        return ""
    try:
        fd = stream.fileno()
    except Exception:
        try:
            return read_limited_text(stream, limit)
        except Exception:
            return ""

    try:
        old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except OSError:
        return ""

    chunks: list[bytes] = []
    remaining = max(0, min(AMBIENT_STDIN_READ_BYTES, (int(limit) + 1) * 4 + 4))
    try:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
        except (OSError, ValueError):
            return ""
        while remaining > 0:
            try:
                ready, _, _ = select.select([fd], [], [], 0)
            except (OSError, ValueError):
                return ""
            if not ready:
                break
            try:
                chunk = os.read(fd, min(remaining, 65536))
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError:
                return ""
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        except OSError:
            pass

    if not chunks:
        return ""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return b"".join(chunks).decode(encoding)[: max(0, int(limit)) + 1]
    except UnicodeDecodeError:
        # Ambient stdin is advisory; skip uncertain partial text rather than blocking or crashing.
        return ""


def parse_body_and_target(args: argparse.Namespace, session: str) -> tuple[str | None, str]:
    target = args.target
    target_all = args.target_all
    words = list(args.message or [])
    state = load_session(session) if session else {}
    participants = active_participants(state)
    stdin_cache: str | None = None

    def non_tty_stdin_text() -> str:
        nonlocal stdin_cache
        if stdin_cache is None:
            stdin_cache = read_available_ambient_stdin_text(sys.stdin)
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
            raise body_input_error("stdin_inline_conflict")
        body = read_limited_text(sys.stdin)
        if not body.strip():
            raise body_input_error("empty_stdin")
    elif words:
        if len(words) > 1:
            raise ValueError(f"message body was split into multiple shell arguments. {SHELL_BODY_HINT}")
        if non_tty_stdin_text():
            # Explicit --stdin conflicts are classified earlier; this branch is
            # only the implicit ambient-pipe plus inline-body collision.
            raise body_input_error("piped_stdin_inline_conflict")
        body = words[0]
        if not body.strip():
            raise body_input_error("missing_body")
    elif not sys.stdin.isatty():
        body = non_tty_stdin_text()
        if not body.strip():
            raise body_input_error("missing_body")
    else:
        raise body_input_error("missing_body")

    args.target_all = target_all
    return target, body


def main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    precheck_error = validate_send_peer_argv(argv, parser)
    if precheck_error:
        if isinstance(precheck_error, BodyInputError):
            # Stable body-input codes are deliberately narrow; unrelated
            # parser, placement, watchdog, and identity diagnostics keep their
            # established wording for compatibility.
            print(format_body_input_error(precheck_error), file=sys.stderr)
        else:
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
    except BodyInputError as exc:
        print(format_body_input_error(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"agent_send_peer: {exc}", file=sys.stderr)
        return 2
    ok, size_error = validate_peer_body_size(body)
    if not ok:
        print(size_error, file=sys.stderr)
        return 2

    if args.watchdog is not None and (not math.isfinite(args.watchdog) or args.watchdog < 0):
        print(
            f"agent_send_peer: --watchdog must be a finite non-negative number, got {format(args.watchdog, 'g')}",
            file=sys.stderr,
        )
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
        cmd.append(f"--watchdog={args.watchdog}")
    if args.target_all:
        cmd.append("--all")
    elif target:
        cmd += ["--to", target]

    proc = subprocess.run(cmd, input=body.encode("utf-8"))
    if proc.returncode == 0:
        print(REQUEST_SENT_HINT if args.kind == "request" else NOTICE_SENT_HINT, file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
