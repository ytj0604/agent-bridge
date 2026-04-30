from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .harness import (
    LIBEXEC,
    assert_true,
)

import bridge_instructions  # noqa: E402
import bridge_response_guard  # noqa: E402


def scenario_view_peer_doc_surfaces_disclose_search_semantics(label: str, tmpdir: Path) -> None:
    phrase = "case-insensitive literal substring (no regex)"
    search_shape = "agent_view_peer <alias> --search 'text' [--live]"

    search_lines = [line for line in bridge_instructions.model_cheat_sheet() if search_shape in line]
    assert_true(len(search_lines) == 1, f"{label}: expected one cheat-sheet search line, got {search_lines!r}")
    assert_true(phrase in search_lines[0], f"{label}: cheat-sheet search line must disclose search semantics: {search_lines[0]!r}")

    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    assert_true("agent_view_peer" in probe, f"{label}: probe prompt must keep compact view command reference")
    assert_true("debug surfaces" in probe and "do not poll" in probe.lower(), f"{label}: probe prompt must frame view/status commands as debug, not polling: {probe!r}")

    help_result = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_view_peer.py"), "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    help_text = help_result.stdout + help_result.stderr
    normalized_help = " ".join(help_text.split())
    assert_true(help_result.returncode == 0, f"{label}: agent_view_peer --help should exit 0, got {help_result.returncode}: {help_text!r}")
    assert_true("--search" in help_text, f"{label}: --help output must include --search")
    assert_true(phrase in normalized_help, f"{label}: --help output must disclose search semantics: {help_text!r}")
    print(f"  PASS  {label}")


def scenario_interrupt_peer_doc_surfaces_disclose_no_op_race(label: str, tmpdir: Path) -> None:
    phrase = "interrupt can be a no-op: the response and queued follow-ups still flow"
    key_sequence_phrase = "Claude"
    ctrl_c_phrase = "Ctrl-C"
    command_shape = "agent_interrupt_peer <alias>"
    boundary_tokens = [
        "agent_cancel_message",
        "pending",
        "inflight pre-pane-touch/pre-paste",
        "pane-mode-deferred",
        "active/post-pane-touch",
        "submitted",
        "delivered",
        "not other pending queued messages",
    ]
    trigger_tokens = ["wrong prompt", "stuck peer"]
    clear_hold_tokens = [
        "held=true",
        "interrupt_partial_failure_blocked=true",
        "busy=false",
        "current_prompt_id=null",
        "inflight_count=0",
        "delivered_count=0",
        "idle",
        "dirty input",
        "turn in progress",
        "active delivered/inflight work",
    ]

    interrupt_lines = [line for line in bridge_instructions.model_cheat_sheet() if line.startswith(f"- {command_shape} :")]
    assert_true(len(interrupt_lines) == 1, f"{label}: expected one default interrupt cheat-sheet line, got {interrupt_lines!r}")
    assert_true(phrase in interrupt_lines[0], f"{label}: cheat-sheet interrupt line must disclose no-op race: {interrupt_lines[0]!r}")
    assert_true(key_sequence_phrase in interrupt_lines[0] and ctrl_c_phrase in interrupt_lines[0], f"{label}: cheat-sheet interrupt line must disclose Claude C-c sequence: {interrupt_lines[0]!r}")
    assert_true("force-cancel" not in interrupt_lines[0], f"{label}: default interrupt line must not imply force-cancel: {interrupt_lines[0]!r}")
    for token in boundary_tokens:
        assert_true(token.lower() in interrupt_lines[0].lower(), f"{label}: interrupt cheat line missing boundary token {token!r}: {interrupt_lines[0]!r}")
    for token in trigger_tokens:
        assert_true(token in interrupt_lines[0].lower(), f"{label}: interrupt cheat line missing trigger token {token!r}: {interrupt_lines[0]!r}")
    assert_true(
        "replacement waiting behind active/post-pane-touch work" in interrupt_lines[0].lower(),
        f"{label}: interrupt cheat line missing active-only replacement trigger: {interrupt_lines[0]!r}",
    )
    assert_true("Use --json" in interrupt_lines[0], f"{label}: interrupt cheat line should document JSON opt-in: {interrupt_lines[0]!r}")
    cancel_lines = [line for line in bridge_instructions.model_cheat_sheet() if line.startswith("- agent_cancel_message <message_id> :")]
    assert_true(len(cancel_lines) == 1, f"{label}: expected one cancel cheat-sheet line, got {cancel_lines!r}")
    for token in ("pending", "inflight pre-pane-touch/pre-paste", "pane-mode-deferred", "agent_interrupt_peer", "submitted", "delivered"):
        assert_true(token.lower() in cancel_lines[0].lower(), f"{label}: cancel cheat line missing symmetric token {token!r}: {cancel_lines[0]!r}")
    assert_true("Use --json" in cancel_lines[0], f"{label}: cancel cheat line should document JSON opt-in: {cancel_lines[0]!r}")
    clear_lines = [line for line in bridge_instructions.model_cheat_sheet() if line.startswith("- agent_interrupt_peer <alias> --clear-hold :")]
    assert_true(len(clear_lines) == 1, f"{label}: expected one clear-hold cheat-sheet line, got {clear_lines!r}")
    for token in clear_hold_tokens:
        assert_true(token.lower() in clear_lines[0].lower(), f"{label}: clear-hold cheat line missing token {token!r}: {clear_lines[0]!r}")
    assert_true("Use --json" in clear_lines[0], f"{label}: clear-hold cheat line should document JSON opt-in: {clear_lines[0]!r}")
    status_lines = [line for line in bridge_instructions.model_cheat_sheet() if line.startswith("- agent_interrupt_peer [<alias>] --status :")]
    assert_true(len(status_lines) == 1 and "JSON" in status_lines[0], f"{label}: status cheat line should keep JSON inspection wording: {status_lines!r}")

    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    assert_true(command_shape in probe, f"{label}: probe prompt must keep compact interrupt command shape")
    assert_true("force-cancel" not in probe, f"{label}: probe prompt must not imply force-cancel")
    for token in ("agent_cancel_message", "pending", "inflight pre-pane-touch/pre-paste", "pane-mode-deferred", "agent_interrupt_peer", "active/post-pane-touch"):
        assert_true(token.lower() in probe.lower(), f"{label}: probe prompt missing boundary token {token!r}")
    for token in ("--json", "--status is JSON"):
        assert_true(token in probe, f"{label}: probe prompt missing action JSON token {token!r}")
    for token in trigger_tokens:
        assert_true(token in probe.lower(), f"{label}: probe prompt missing trigger token {token!r}")
    assert_true(
        "replacement waiting behind active/post-pane-touch work" in probe.lower(),
        f"{label}: probe prompt missing active-only replacement trigger",
    )

    help_result = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_interrupt_peer.py"), "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    help_text = help_result.stdout + help_result.stderr
    normalized_help = " ".join(help_text.split())
    normalized_help_tokens = normalized_help.replace("- ", "-")
    normalized_help_noop_ok = phrase in normalized_help or phrase.replace("no-op", "no- op") in normalized_help
    assert_true(help_result.returncode == 0, f"{label}: agent_interrupt_peer --help should exit 0, got {help_result.returncode}: {help_text!r}")
    assert_true("--clear-hold" in help_text, f"{label}: --help output must include interrupt-specific options")
    assert_true(normalized_help_noop_ok, f"{label}: --help output must disclose no-op race: {help_text!r}")
    assert_true(key_sequence_phrase in normalized_help and ctrl_c_phrase in normalized_help, f"{label}: --help output must disclose Claude C-c sequence: {help_text!r}")
    assert_true("force-cancel" not in normalized_help, f"{label}: --help output must not imply force-cancel: {help_text!r}")
    for token in boundary_tokens:
        assert_true(token.lower() in normalized_help_tokens.lower(), f"{label}: --help missing boundary token {token!r}: {help_text!r}")
    for token in clear_hold_tokens:
        assert_true(token.lower() in normalized_help_tokens.lower(), f"{label}: --help missing clear-hold token {token!r}: {help_text!r}")
    assert_true("--json" in normalized_help, f"{label}: --help should document action JSON opt-in: {help_text!r}")
    print(f"  PASS  {label}")


def scenario_interrupt_peer_doc_surfaces_no_queued_active_directive(label: str, tmpdir: Path) -> None:
    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    interrupt_lines = [
        line for line in bridge_instructions.model_cheat_sheet()
        if line.startswith("- agent_interrupt_peer <alias> :")
    ]
    assert_true(len(interrupt_lines) == 1, f"{label}: expected one interrupt cheat-sheet line, got {interrupt_lines!r}")
    interrupt_line = interrupt_lines[0]
    for surface_name, surface in (("probe", probe), ("cheat sheet", interrupt_line)):
        lowered = surface.lower()
        assert_true("queued/active turn" not in lowered, f"{label}: {surface_name} must not direct interrupt at queued/active turn wording: {surface!r}")
        assert_true(
            "replacement waiting behind active/post-pane-touch work" in lowered,
            f"{label}: {surface_name} must keep active-only replacement wording: {surface!r}",
        )
    print(f"  PASS  {label}")


def scenario_prompt_intercepted_doc_surfaces_disclose_user_typing_collision(label: str, tmpdir: Path) -> None:
    phrase = (
        "If a human types into a pane while a bridge prompt is delivered but unsubmitted, "
        "the bridge cancels that delivered message, emits [bridge:interrupted] "
        "prompt_intercepted to the original sender, and drops that turn; expect "
        "model-driven retries."
    )
    critical_substrings = [
        "delivered but unsubmitted",
        "[bridge:interrupted] prompt_intercepted",
        "drops that turn",
        "expect model-driven retries",
    ]

    matching_lines = [
        line for line in bridge_instructions.model_cheat_sheet()
        if "prompt_intercepted" in line or "delivered but unsubmitted" in line
    ]
    assert_true(len(matching_lines) == 1, f"{label}: expected one prompt_intercepted cheat-sheet bullet, got {matching_lines!r}")
    assert_true(phrase in matching_lines[0], f"{label}: cheat-sheet bullet must disclose user-typing collision exactly: {matching_lines[0]!r}")

    for needle in critical_substrings:
        assert_true(needle in matching_lines[0], f"{label}: cheat-sheet bullet missing critical substring {needle!r}: {matching_lines[0]!r}")
    print(f"  PASS  {label}")


def scenario_response_send_guard_doc_surfaces_are_precise(label: str, tmpdir: Path) -> None:
    required_doc_tokens = [
        "response-time send guard",
        "auto-return peer request",
        "requester",
        "current_prompt.from",
        "blocked/rejected",
        "target list containing that requester",
        "third-party peer sends",
        "review/collaboration",
        "other validations still apply",
    ]
    forbidden_fragments = [
        "do not call agent_send_peer",
        "cannot send peer messages during response",
        "no agent_send_peer during response",
    ]
    cheat = "\n".join(bridge_instructions.model_cheat_sheet())
    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    lowered_cheat = cheat.lower()
    for token in required_doc_tokens:
        assert_true(token.lower() in lowered_cheat, f"{label}: cheat sheet missing response guard token {token!r}")
    compact_probe_tokens = [
        "response-time send guard",
        "auto-return peer request",
        "requester",
        "current_prompt.from",
        "blocked/rejected",
        "third-party",
        "review/collaboration",
        "other validations still apply",
    ]
    lowered_probe = probe.lower()
    for token in compact_probe_tokens:
        assert_true(token.lower() in lowered_probe, f"{label}: probe prompt missing compact response guard token {token!r}")
    for surface_name, surface in (("cheat sheet", cheat), ("probe prompt", probe)):
        lowered = surface.lower()
        for fragment in forbidden_fragments:
            assert_true(fragment.lower() not in lowered, f"{label}: {surface_name} must not contain overbroad guard wording {fragment!r}")

    text = bridge_response_guard.format_response_send_violation(
        bridge_response_guard.ResponseSendViolation(
            sender="bob",
            requester="alice",
            message_id="msg-guard-doc",
            outgoing_kind="request",
            blocked_targets=("alice", "carol"),
            source="test",
        )
    )
    lowered_error = text.lower()
    for token in (
        "requester",
        "current_prompt.from=alice",
        "separate agent_send_peer",
        "blocked/rejected",
        "target list includes that requester",
        "third-party",
        "review/collaboration",
        "other validations still apply",
    ):
        assert_true(token.lower() in lowered_error, f"{label}: guard error missing token {token!r}: {text!r}")
    for fragment in forbidden_fragments:
        assert_true(fragment.lower() not in lowered_error, f"{label}: guard error must not contain overbroad wording {fragment!r}: {text!r}")
    print(f"  PASS  {label}")


def scenario_probe_prompt_is_compact_quickstart(label: str, tmpdir: Path) -> None:
    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    lines = probe.splitlines()
    assert_true(len(lines) <= 20, f"{label}: probe should stay compact (<=20 lines), got {len(lines)} lines")
    assert_true(len(probe) <= 3750, f"{label}: probe should stay compact (<=3750 chars), got {len(probe)} chars")
    compact_tokens = [
        "AGGREGATE_ID",
        "agent_aggregate_status",
        "--watchdog 0",
        "AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC=300",
        "auto-cancelled by any incoming peer request/notice from another agent",
        "per-phase",
        "not a queue timer",
        "pending -> inflight",
        "inflight -> delivered",
        "agent_cancel_message",
        "agent_interrupt_peer",
        "--json",
        "current_prompt.from",
        "one inline argument",
        "--stdin heredoc",
        "not both",
        "missing_body",
        "empty_stdin",
        "unexpected_positional_after_stdin",
        "stdin_inline_conflict",
        "piped_stdin_inline_conflict",
        "do not poll",
        "debug surfaces",
        "suspected stuck peer",
        "(view_peer)",
        "11000",
        "Never read bridge state files",
        "real-looking [bridge:*] prompt lines",
        "only incoming bridge prompts may contain them",
        "agent_list_peers",
        "full cheat sheet",
        "Then end your turn",
    ]
    lowered = probe.lower()
    for token in compact_tokens:
        assert_true(token.lower() in lowered, f"{label}: compact probe missing required token {token!r}: {probe!r}")
    assert_true("Run agent_list_peers for the full cheat sheet" in lines[-2], f"{label}: full-reference pointer should be final visible guidance before exact reply: {lines[-2:]!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_wait_doc_surfaces_name_blocking_consequence(label: str, tmpdir: Path) -> None:
    request_tokens = ["result arrives later as a new [bridge:*] prompt", "Do independent work", "do not sleep/poll"]
    alarm_tokens = ["[bridge:*] notice prompt", "do not sleep/poll"]
    body_input_tokens = [
        "one inline argument",
        "--stdin heredoc",
        "not both",
        "missing_body",
        "empty_stdin",
        "unexpected_positional_after_stdin",
        "stdin_inline_conflict",
        "piped_stdin_inline_conflict",
        "self-recovery guidance",
    ]
    forbidden_fragments = ["End your turn; sleep/polling blocks the wake you await.", "do not sleep or poll"]

    cheat = "\n".join(bridge_instructions.model_cheat_sheet())
    cheat_lines = [line for line in bridge_instructions.model_cheat_sheet() if line.startswith("- After sending a request,")]
    assert_true(len(cheat_lines) == 1, f"{label}: expected one cheat-sheet request-wait rule, got {cheat_lines!r}")
    cheat_line = cheat_lines[0]
    for token in request_tokens:
        assert_true(token in cheat_line, f"{label}: cheat-sheet request-wait rule missing token {token!r}: {cheat_line!r}")
    for forbidden in forbidden_fragments:
        assert_true(forbidden not in cheat_line, f"{label}: cheat-sheet request-wait rule should drop old wording {forbidden!r}: {cheat_line!r}")
    notice_lines = [line for line in bridge_instructions.model_cheat_sheet() if line.startswith("- agent_send_peer --kind notice")]
    assert_true(len(notice_lines) == 1, f"{label}: expected one cheat-sheet notice rule, got {notice_lines!r}")
    assert_true("no reply auto-routes" in notice_lines[0].lower(), f"{label}: notice rule must mention no auto-route: {notice_lines[0]!r}")
    assert_true("Set agent_alarm if a follow-up matters" in notice_lines[0], f"{label}: notice rule should use direct follow-up wording: {notice_lines[0]!r}")
    assert_true("only if a follow-up matters" not in notice_lines[0], f"{label}: notice rule should drop old only-if wording: {notice_lines[0]!r}")
    alarm_lines = [line for line in bridge_instructions.model_cheat_sheet() if line.startswith("- agent_alarm <sec>")]
    assert_true(len(alarm_lines) == 1, f"{label}: expected one cheat-sheet alarm rule, got {alarm_lines!r}")
    for token in alarm_tokens:
        assert_true(token.lower() in alarm_lines[0].lower(), f"{label}: alarm rule missing token {token!r}: {alarm_lines[0]!r}")
    assert_true("[bridge:alarm_cancelled]" in alarm_lines[0] and "re-arm" in alarm_lines[0].lower(), f"{label}: alarm rule must mention cancellation marker and re-arm guidance: {alarm_lines[0]!r}")

    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    for token in body_input_tokens:
        assert_true(token.lower() in cheat.lower(), f"{label}: cheat sheet missing body-input token {token!r}")
        assert_true(token.lower() in probe.lower(), f"{label}: probe prompt missing body-input token {token!r}")
    for token in request_tokens:
        assert_true(token.lower() in probe.lower(), f"{label}: compact probe missing request token {token!r}")
    for forbidden in forbidden_fragments:
        assert_true(forbidden not in probe, f"{label}: compact probe should drop old wording {forbidden!r}")
    assert_true("no reply auto-routes" in probe.lower(), f"{label}: probe notice text must mention no auto-route")
    assert_true("Set agent_alarm if a follow-up matters" in probe, f"{label}: probe should use direct follow-up wording: {probe!r}")
    assert_true("only if a follow-up matters" not in probe, f"{label}: probe should drop old only-if wording: {probe!r}")
    assert_true("agent_alarm <sec>" in probe and "[bridge:*] notice" in probe, f"{label}: compact probe should retain alarm command and wake shape")
    print(f"  PASS  {label}")


def scenario_watchdog_phase_doc_surfaces_are_consistent(label: str, tmpdir: Path) -> None:
    def _line_containing(lines: list[str], token: str) -> str:
        matches = [line for line in lines if token in line]
        assert_true(len(matches) == 1, f"{label}: expected one line containing {token!r}, got {matches!r}")
        return matches[0]

    canonical_watchdog_tokens = [
        "AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC",
        "300",
        "phase",
        "delivery",
        "response",
        "pending",
        "inflight",
        "delivered",
        "not a queue timer",
        "same <sec> per phase",
        "up to two phase intervals",
        "request",
        "--watchdog 0",
    ]
    model_watchdog_forbidden_tokens = [
        "--no-auto-return",
        "no-auto-return",
        "no auto-return",
        "requires auto-return",
        "auto-return",
    ]
    forbidden_bare_default_fragments = [
        "for example default 300s",
        "default 300s delivery",
        "default 300s response",
    ]
    extend_tokens = ["inflight/submitted delivery", "delivered aggregate response", "stale watchdog wakes", "[bridge:result]", "queued/arriving"]

    cheat_lines = bridge_instructions.model_cheat_sheet()
    cheat = "\n".join(cheat_lines)
    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    probe_lines = probe.splitlines()
    cheat_watchdog_line = _line_containing(cheat_lines, "--watchdog <sec>")
    probe_watchdog_line = _line_containing(probe_lines, "--watchdog <sec>")
    response_guard_line = _line_containing(cheat_lines, "Response-time send guard")
    probe_response_guard_line = _line_containing(probe_lines, "Response-time send guard")

    send_help = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_send_peer.py"), "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    send_help_text = " ".join((send_help.stdout + send_help.stderr).split())
    assert_true(send_help.returncode == 0, f"{label}: agent_send_peer --help should exit 0, got {send_help.returncode}: {send_help_text!r}")
    for surface_name, surface in (("cheat sheet", cheat), ("bridge_send_peer help", send_help_text)):
        lowered = surface.lower()
        for token in canonical_watchdog_tokens:
            assert_true(token.lower() in lowered, f"{label}: {surface_name} missing watchdog token {token!r}: {surface!r}")
        for forbidden in forbidden_bare_default_fragments:
            assert_true(forbidden not in lowered, f"{label}: {surface_name} has bare hard-coded default wording {forbidden!r}: {surface!r}")
    compact_probe_watchdog_tokens = [
        "AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC=300",
        "per-phase",
        "not a queue timer",
        "pending -> inflight",
        "inflight -> delivered",
        "300s delivery + 300s response",
        "--watchdog 0",
    ]
    lowered_probe = probe.lower()
    for token in compact_probe_watchdog_tokens:
        assert_true(token.lower() in lowered_probe, f"{label}: compact probe missing watchdog token {token!r}: {probe!r}")
    for forbidden in forbidden_bare_default_fragments:
        assert_true(forbidden not in lowered_probe, f"{label}: compact probe has bare hard-coded default wording {forbidden!r}: {probe!r}")
    for surface_name, surface in (("cheat sheet watchdog line", cheat_watchdog_line), ("probe watchdog line", probe_watchdog_line), ("bridge_send_peer help", send_help_text)):
        lowered = surface.lower()
        for forbidden in model_watchdog_forbidden_tokens:
            assert_true(forbidden not in lowered, f"{label}: {surface_name} should not expose model-facing no-auto-return wording {forbidden!r}: {surface!r}")
    assert_true("Request only" in cheat_watchdog_line, f"{label}: cheat sheet watchdog line must keep request-only boundary: {cheat_watchdog_line!r}")
    assert_true("request" in probe_watchdog_line.lower(), f"{label}: probe watchdog line must keep request boundary: {probe_watchdog_line!r}")
    assert_true("Request only" in send_help_text, f"{label}: help watchdog text must keep request-only boundary: {send_help_text!r}")
    assert_true("auto-return peer request" in response_guard_line, f"{label}: cheat sheet response guard must keep auto-return wording: {response_guard_line!r}")
    assert_true("auto-return peer request" in probe_response_guard_line, f"{label}: probe response guard must keep auto-return wording: {probe_response_guard_line!r}")
    assert_true("pending -> inflight" in cheat and "inflight -> delivered" in cheat, f"{label}: cheat sheet missing exact phase transitions")
    assert_true("pending -> inflight" in probe and "inflight -> delivered" in probe, f"{label}: probe prompt missing exact phase transitions")
    assert_true("pending -> inflight" in send_help_text and "inflight -> delivered" in send_help_text, f"{label}: help missing exact phase transitions: {send_help_text!r}")
    for token in extend_tokens:
        assert_true(token.lower() in cheat.lower(), f"{label}: cheat sheet missing extend token {token!r}")

    extend_help = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_extend_wait.py"), "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    extend_help_text = " ".join((extend_help.stdout + extend_help.stderr).split())
    assert_true(extend_help.returncode == 0, f"{label}: agent_extend_wait --help should exit 0, got {extend_help.returncode}: {extend_help_text!r}")
    for token in ("delivery", "response"):
        assert_true(token in extend_help_text, f"{label}: bridge_extend_wait help missing {token!r}: {extend_help_text!r}")

    send_no_auto = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_send_peer.py"), "--no-auto-return", "--to", "bob", "hello"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert_true(send_no_auto.returncode == 2, f"{label}: model-facing --no-auto-return should reject, got {send_no_auto.returncode}: {send_no_auto.stderr!r}")
    assert_true(send_no_auto.stdout == "", f"{label}: rejected --no-auto-return should not print stdout: {send_no_auto.stdout!r}")
    assert_true("--no-auto-return" in send_no_auto.stderr and "unrecognized" in send_no_auto.stderr, f"{label}: --no-auto-return rejection should be explicit: {send_no_auto.stderr!r}")

    enqueue_help = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_enqueue.py"), "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    enqueue_help_text = enqueue_help.stdout + enqueue_help.stderr
    assert_true(enqueue_help.returncode == 0, f"{label}: bridge_enqueue --help should exit 0, got {enqueue_help.returncode}: {enqueue_help_text!r}")
    assert_true("--no-auto-return" in enqueue_help_text, f"{label}: internal enqueue surface must preserve --no-auto-return: {enqueue_help_text!r}")
    print(f"  PASS  {label}")


def scenario_wait_status_doc_surfaces_anti_polling(label: str, tmpdir: Path) -> None:
    cheat = "\n".join(bridge_instructions.model_cheat_sheet())
    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    cheat_tokens = [
        "agent_wait_status",
        "do not poll",
        "human-prompted",
        "watchdog",
        "agent_view_peer",
        "peer pane debugging",
    ]
    for token in cheat_tokens:
        assert_true(token.lower() in cheat.lower(), f"{label}: cheat sheet missing wait_status token {token!r}")
    compact_probe_tokens = ["agent_wait_status", "debug surfaces", "do not poll", "human prompt", "watchdog", "bridge-state debug"]
    for token in compact_probe_tokens:
        assert_true(token.lower() in probe.lower(), f"{label}: probe prompt missing wait_status token {token!r}")

    help_result = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_wait_status.py"), "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    help_text = " ".join((help_result.stdout + help_result.stderr).split())
    assert_true(help_result.returncode == 0, f"{label}: agent_wait_status --help should exit 0, got {help_result.returncode}: {help_text!r}")
    for token in ("human-prompted", "watchdog", "do not poll", "[bridge:*]"):
        assert_true(token.lower() in help_text.lower(), f"{label}: help missing anti-polling token {token!r}: {help_text!r}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_doc_surfaces_leg_level_and_anti_polling(label: str, tmpdir: Path) -> None:
    cheat = "\n".join(bridge_instructions.model_cheat_sheet())
    probe = bridge_instructions.probe_prompt("attach", "probe-doc", "codex1", "claude1,codex1")
    cheat_tokens = [
        "agent_aggregate_status",
        "leg-level",
        "agent_wait_status",
        "one aggregate",
        "AGGREGATE_ID",
        "aggregate_id",
        "human prompt",
        "watchdog",
        "do not poll",
    ]
    compact_probe_tokens = ["agent_aggregate_status", "leg-level", "AGGREGATE_ID", "watchdog", "do not poll", "debug surfaces"]
    forbidden_tokens = ["track progress", "monitor progress"]
    for token in cheat_tokens:
        assert_true(token.lower() in cheat.lower(), f"{label}: cheat sheet missing aggregate_status token {token!r}")
    for token in compact_probe_tokens:
        assert_true(token.lower() in probe.lower(), f"{label}: probe prompt missing aggregate_status token {token!r}")
    for token in forbidden_tokens:
        assert_true(token.lower() not in cheat.lower(), f"{label}: cheat sheet should avoid polling-like token {token!r}")
        assert_true(token.lower() not in probe.lower(), f"{label}: probe prompt should avoid polling-like token {token!r}")

    help_result = subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_aggregate_status.py"), "--help"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    help_text = " ".join((help_result.stdout + help_result.stderr).split())
    assert_true(help_result.returncode == 0, f"{label}: agent_aggregate_status --help should exit 0, got {help_result.returncode}: {help_text!r}")
    for token in ("leg-level", "agent_wait_status", "do not poll"):
        assert_true(token.lower() in help_text.lower(), f"{label}: help missing token {token!r}: {help_text!r}")
    print(f"  PASS  {label}")


SCENARIOS = [
    ('view_peer_doc_surfaces_disclose_search_semantics', scenario_view_peer_doc_surfaces_disclose_search_semantics),
    ('interrupt_peer_doc_surfaces_disclose_no_op_race', scenario_interrupt_peer_doc_surfaces_disclose_no_op_race),
    ('interrupt_peer_doc_surfaces_no_queued_active_directive', scenario_interrupt_peer_doc_surfaces_no_queued_active_directive),
    ('prompt_intercepted_doc_surfaces_disclose_user_typing_collision', scenario_prompt_intercepted_doc_surfaces_disclose_user_typing_collision),
    ('response_send_guard_doc_surfaces_are_precise', scenario_response_send_guard_doc_surfaces_are_precise),
    ('probe_prompt_is_compact_quickstart', scenario_probe_prompt_is_compact_quickstart),
    ('send_peer_wait_doc_surfaces_name_blocking_consequence', scenario_send_peer_wait_doc_surfaces_name_blocking_consequence),
    ('watchdog_phase_doc_surfaces_are_consistent', scenario_watchdog_phase_doc_surfaces_are_consistent),
    ('wait_status_doc_surfaces_anti_polling', scenario_wait_status_doc_surfaces_anti_polling),
    ('aggregate_status_doc_surfaces_leg_level_and_anti_polling', scenario_aggregate_status_doc_surfaces_leg_level_and_anti_polling),
]
