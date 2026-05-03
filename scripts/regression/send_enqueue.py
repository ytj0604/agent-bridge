from __future__ import annotations

import argparse
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .harness import (
    LIBEXEC,
    ROOT,
    _delivered_request,
    _import_enqueue_module,
    _participants_state,
    _patch_enqueue_for_unit,
    _run_enqueue_main,
    _write_json,
    assert_true,
    isolated_identity_env,
    patched_environ,
    read_events,
    test_message,
    write_identity_fixture,
)

import bridge_identity  # noqa: E402
from bridge_util import MAX_INLINE_SEND_BODY_CHARS, utc_now  # noqa: E402


def scenario_enqueue_aggregate_metadata_modes(label: str, tmpdir: Path) -> None:
    cases = [
        ("partial", ["--to", "bob,carol"], tmpdir / "partial.json"),
        ("all", ["--all"], tmpdir / "all.json"),
    ]
    for expected_mode, target_args, queue_file in cases:
        be = _import_enqueue_module()
        state = _participants_state(["alice", "bob", "carol"])
        _patch_enqueue_for_unit(be, state)
        code, out, err = _run_enqueue_main(
            be,
            [
                "--session", "test-session",
                "--from", "alice",
                *target_args,
                "--body", "hello",
                "--queue-file", str(queue_file),
                "--state-file", str(tmpdir / f"{expected_mode}.events.raw.jsonl"),
                "--public-state-file", str(tmpdir / f"{expected_mode}.events.jsonl"),
            ],
        )
        assert_true(code == 0, f"{label}: aggregate enqueue {expected_mode} should succeed: code={code} err={err!r}")
        lines = _stdout_lines(out)
        queue = json.loads(queue_file.read_text(encoding="utf-8"))
        assert_true(len(lines) == 3 and len(queue) == 2, f"{label}: aggregate enqueue should create two legs plus aggregate id: out={out!r} queue={queue}")
        ids = lines[:2]
        stdout_aggregate_id = _aggregate_id_from_stdout(out)
        aggregate_ids = {item.get("aggregate_id") for item in queue}
        started_values = {item.get("aggregate_started_ts") for item in queue}
        assert_true(aggregate_ids == {stdout_aggregate_id}, f"{label}: legs should share printed aggregate id: out={out!r} queue={queue}")
        assert_true(len(started_values) == 1 and next(iter(started_values)), f"{label}: legs should share aggregate_started_ts: {queue}")
        assert_true({item.get("aggregate_mode") for item in queue} == {expected_mode}, f"{label}: aggregate mode mismatch for {expected_mode}: {queue}")
        assert_true(all(item.get("aggregate_message_ids") == {"bob": ids[0], "carol": ids[1]} for item in queue), f"{label}: aggregate message map mismatch: ids={ids} queue={queue}")
    print(f"  PASS  {label}")


def _import_send_peer_module():
    import importlib
    bs = importlib.import_module("bridge_send_peer")
    return importlib.reload(bs)


def _run_send_peer_main(bs, argv: list[str], stdin_text: str = "", stdin_isatty: bool | None = None) -> tuple[int, str, str]:
    import contextlib
    import io

    class FakeStdin(io.StringIO):
        def __init__(self, text: str, is_tty: bool):
            super().__init__(text)
            self._is_tty = is_tty

        def isatty(self) -> bool:
            return self._is_tty

    old_argv = sys.argv[:]
    old_stdin = sys.stdin
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_send_peer", *argv]
        sys.stdin = FakeStdin(stdin_text, stdin_text == "" if stdin_isatty is None else stdin_isatty)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = bs.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
        sys.stdin = old_stdin
    return int(code), out.getvalue(), err.getvalue()


def _run_send_peer_script(argv: list[str], stdin_text: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LIBEXEC / "bridge_send_peer.py"), *argv],
        cwd=str(ROOT),
        input=stdin_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )


def _stdout_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line]


def _aggregate_id_from_stdout(text: str) -> str:
    lines = [line for line in _stdout_lines(text) if line.startswith("AGGREGATE_ID: ")]
    assert_true(len(lines) == 1, f"expected exactly one AGGREGATE_ID line, got {lines!r} in {text!r}")
    aggregate_id = lines[0].split(": ", 1)[1]
    assert_true(aggregate_id.startswith("agg-"), f"aggregate id line should carry agg- id: {lines[0]!r}")
    return aggregate_id


def _assert_aggregate_stdout_order(label: str, text: str, expected_ids: list[str], expected_aggregate_id: str) -> None:
    lines = _stdout_lines(text)
    assert_true(lines == [*expected_ids, f"AGGREGATE_ID: {expected_aggregate_id}"], f"{label}: aggregate stdout order mismatch: {lines!r}")


def scenario_enqueue_rejects_body_and_stdin_before_session_lookup(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    calls = {"read": 0, "ensure": 0}

    def fail_read(_stream):
        calls["read"] += 1
        raise AssertionError("stdin must not be consumed when --body and --stdin conflict")

    def fail_ensure(_session: str) -> str:
        calls["ensure"] += 1
        raise AssertionError("daemon ensure must not run when --body and --stdin conflict")

    be.read_limited_text = fail_read
    be.ensure_daemon_running = fail_ensure
    code, out, err = _run_enqueue_main(be, ["--from", "alice", "--body", "inline", "--stdin"], stdin_text="stdin body")
    assert_true(code == 2, f"{label}: body+stdin conflict exits 2, got {code}")
    assert_true(out == "", f"{label}: conflict has no stdout: {out!r}")
    assert_true("use either --body or --stdin, not both" in err, f"{label}: conflict error missing: {err!r}")
    assert_true("cannot infer bridge session" not in err, f"{label}: conflict must win before session lookup: {err!r}")
    assert_true("--body or --stdin content is required" not in err, f"{label}: conflict must win before empty-body check: {err!r}")
    assert_true(calls == {"read": 0, "ensure": 0}, f"{label}: conflict must not consume stdin or ensure daemon: {calls}")
    print(f"  PASS  {label}")


def scenario_enqueue_rejects_empty_body_and_stdin(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    be.read_limited_text = lambda _stream: (_ for _ in ()).throw(AssertionError("stdin must not be consumed"))
    be.ensure_daemon_running = lambda _session: (_ for _ in ()).throw(AssertionError("daemon ensure must not run"))
    code, out, err = _run_enqueue_main(be, ["--from", "alice", "--body", "", "--stdin"], stdin_text="stdin body")
    assert_true(code == 2, f"{label}: empty body+stdin conflict exits 2, got {code}")
    assert_true(out == "", f"{label}: conflict has no stdout: {out!r}")
    assert_true("use either --body or --stdin, not both" in err, f"{label}: conflict error expected: {err!r}")
    assert_true("--body or --stdin content is required" not in err, f"{label}: empty-body error must not mask explicit conflict: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_rejects_body_and_stdin_argv_order_independent(label: str, tmpdir: Path) -> None:
    for suffix in (["--body", "inline", "--stdin"], ["--stdin", "--body", "inline"]):
        be = _import_enqueue_module()
        be.read_limited_text = lambda _stream: (_ for _ in ()).throw(AssertionError("stdin must not be consumed"))
        be.ensure_daemon_running = lambda _session: (_ for _ in ()).throw(AssertionError("daemon ensure must not run"))
        code, out, err = _run_enqueue_main(be, ["--from", "alice", *suffix], stdin_text="stdin body")
        assert_true(code == 2 and out == "", f"{label}: conflict {suffix} exits 2 with no stdout: code={code} out={out!r}")
        assert_true("use either --body or --stdin, not both" in err, f"{label}: order {suffix} should use conflict error: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_body_only_still_works(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob",
            "--body", "inline",
            "--queue-file", str(queue_file),
            "--state-file", str(tmpdir / "events.raw.jsonl"),
            "--public-state-file", str(tmpdir / "events.jsonl"),
        ],
    )
    assert_true(code == 0, f"{label}: body-only enqueue should succeed, got {code}, err={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: body-only enqueue returns id: {out!r}")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    assert_true(queue and queue[0].get("body") == "inline", f"{label}: inline body preserved: {queue}")
    print(f"  PASS  {label}")


def scenario_enqueue_stdin_only_still_works(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob",
            "--stdin",
            "--queue-file", str(queue_file),
            "--state-file", str(tmpdir / "events.raw.jsonl"),
            "--public-state-file", str(tmpdir / "events.jsonl"),
        ],
        stdin_text="stdin body",
    )
    assert_true(code == 0, f"{label}: stdin-only enqueue should succeed, got {code}, err={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: stdin-only enqueue returns id: {out!r}")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    assert_true(queue and queue[0].get("body") == "stdin body", f"{label}: stdin body preserved: {queue}")
    print(f"  PASS  {label}")


def scenario_enqueue_aggregate_stdout_socket_and_fallback(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob", "carol"])

    _patch_enqueue_for_unit(be, state)
    socket_messages: list[dict] = []

    def fake_socket(_session: str, messages: list[dict], **_kwargs):
        socket_messages[:] = [dict(message) for message in messages]
        return True, [str(message["id"]) for message in messages], [], "", ""

    be.enqueue_via_daemon_socket = fake_socket
    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob,carol",
            "--body", "socket aggregate",
            "--queue-file", str(tmpdir / "socket-pending.json"),
            "--state-file", str(tmpdir / "socket-events.raw.jsonl"),
            "--public-state-file", str(tmpdir / "socket-events.jsonl"),
        ],
    )
    assert_true(code == 0 and err == "", f"{label}: socket aggregate should succeed: code={code} err={err!r}")
    socket_ids = [str(message["id"]) for message in socket_messages]
    socket_aggregate_id = str(socket_messages[0].get("aggregate_id") or "")
    assert_true(socket_aggregate_id and all(message.get("aggregate_id") == socket_aggregate_id for message in socket_messages), f"{label}: socket messages must share aggregate_id: {socket_messages}")
    _assert_aggregate_stdout_order(label, out, socket_ids, socket_aggregate_id)
    assert_true("AGGREGATE_ID:" not in err, f"{label}: aggregate id must not go to stderr: {err!r}")

    be = _import_enqueue_module()
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "fallback-pending.json"
    state_file = tmpdir / "fallback-events.raw.jsonl"
    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob,carol",
            "--body", "fallback aggregate",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(tmpdir / "fallback-events.jsonl"),
        ],
    )
    assert_true(code == 0 and err == "", f"{label}: fallback aggregate should succeed: code={code} err={err!r}")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    fallback_ids = [str(message["id"]) for message in queue]
    fallback_aggregate_id = str(queue[0].get("aggregate_id") or "")
    assert_true(fallback_aggregate_id and all(message.get("aggregate_id") == fallback_aggregate_id for message in queue), f"{label}: fallback queue rows must share aggregate_id: {queue}")
    _assert_aggregate_stdout_order(label, out, fallback_ids, fallback_aggregate_id)
    events = read_events(state_file)
    event_aggregate_ids = {event.get("aggregate_id") for event in events if event.get("event") == "message_queued"}
    assert_true(event_aggregate_ids == {fallback_aggregate_id}, f"{label}: queued events must use printed aggregate_id: {events}")
    print(f"  PASS  {label}")


def scenario_enqueue_aggregate_stdout_all_and_negative_cases(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob", "carol"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "all-pending.json"
    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--all",
            "--body", "all aggregate",
            "--queue-file", str(queue_file),
            "--state-file", str(tmpdir / "all-events.raw.jsonl"),
            "--public-state-file", str(tmpdir / "all-events.jsonl"),
        ],
    )
    assert_true(code == 0 and err == "", f"{label}: --all aggregate should succeed: code={code} err={err!r}")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    aggregate_id = _aggregate_id_from_stdout(out)
    _assert_aggregate_stdout_order(label, out, [str(message["id"]) for message in queue], aggregate_id)
    assert_true(all(message.get("aggregate_id") == aggregate_id for message in queue), f"{label}: --all queue rows must match stdout aggregate_id: {queue}")

    negative_cases = [
        ("single-target", ["--to", "bob"]),
        ("no-auto-return", ["--to", "bob,carol", "--no-auto-return"]),
        ("notice", ["--kind", "notice", "--to", "bob,carol"]),
        ("dedup-to-single", ["--to", "bob,bob"]),
    ]
    for case, target_args in negative_cases:
        be = _import_enqueue_module()
        _patch_enqueue_for_unit(be, state)
        case_queue = tmpdir / f"{case}-pending.json"
        code, out, err = _run_enqueue_main(
            be,
            [
                "--session", "test-session",
                "--from", "alice",
                *target_args,
                "--body", f"{case} body",
                "--queue-file", str(case_queue),
                "--state-file", str(tmpdir / f"{case}-events.raw.jsonl"),
                "--public-state-file", str(tmpdir / f"{case}-events.jsonl"),
            ],
        )
        assert_true(code == 0 and err == "", f"{label}: {case} should succeed without aggregate: code={code} err={err!r}")
        assert_true("AGGREGATE_ID:" not in out, f"{label}: {case} must not print aggregate id: {out!r}")
        rows = json.loads(case_queue.read_text(encoding="utf-8"))
        assert_true(not any(row.get("aggregate_id") for row in rows), f"{label}: {case} rows must not carry aggregate_id: {rows}")
    print(f"  PASS  {label}")


def scenario_enqueue_bare_body_without_value_remains_argparse_owned(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    code, out, err = _run_enqueue_main(be, ["--from", "alice", "--body"])
    assert_true(code == 2, f"{label}: bare --body should exit 2, got {code}")
    assert_true(out == "", f"{label}: argparse error has no stdout: {out!r}")
    assert_true("argument --body: expected one argument" in err, f"{label}: argparse should own bare --body error: {err!r}")
    assert_true("use either --body or --stdin" not in err, f"{label}: custom conflict must not mask bare --body argparse error: {err!r}")
    print(f"  PASS  {label}")


def _enqueue_watchdog_argv(tmpdir: Path, *extra: str, kind: str = "request") -> list[str]:
    return [
        "--session", "test-session",
        "--from", "alice",
        "--to", "bob",
        "--kind", kind,
        "--body", "hello",
        "--queue-file", str(tmpdir / "pending.json"),
        "--state-file", str(tmpdir / "events.raw.jsonl"),
        "--public-state-file", str(tmpdir / "events.jsonl"),
        *extra,
    ]


def _assert_send_peer_watchdog_rejected(label: str, tmpdir: Path, raw: str, expected_value: str) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    watchdog_args = (f"--watchdog={raw}",) if raw.startswith("-") else ("--watchdog", raw)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, *watchdog_args))
    assert_true(code == 2, f"{label}: invalid watchdog {raw!r} exits 2, got {code}, err={err!r}")
    assert_true(out == "", f"{label}: invalid watchdog has no stdout: {out!r}")
    assert_true("finite non-negative" in err and f"got {expected_value}" in err, f"{label}: error should explain rule and value: {err!r}")
    assert_true(not (tmpdir / "pending.json").exists(), f"{label}: invalid watchdog must not enqueue")


def scenario_send_peer_watchdog_negative_one_rejected(label: str, tmpdir: Path) -> None:
    _assert_send_peer_watchdog_rejected(label, tmpdir, "-1", "-1")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_nan_rejected(label: str, tmpdir: Path) -> None:
    _assert_send_peer_watchdog_rejected(label, tmpdir, "nan", "nan")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_inf_rejected(label: str, tmpdir: Path) -> None:
    _assert_send_peer_watchdog_rejected(label, tmpdir, "inf", "inf")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_minus_inf_rejected(label: str, tmpdir: Path) -> None:
    _assert_send_peer_watchdog_rejected(label, tmpdir, "-inf", "-inf")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_zero_disables_for_request(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--watchdog", "0"))
    assert_true(code == 0, f"{label}: watchdog 0 should succeed, got {code}, err={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: stdout should contain queued id: {out!r}")
    queue = json.loads((tmpdir / "pending.json").read_text(encoding="utf-8"))
    assert_true("watchdog_delay_sec" not in queue[0], f"{label}: watchdog 0 should disable metadata: {queue}")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_finite_positive_succeeds(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--watchdog", "1.5"))
    assert_true(code == 0, f"{label}: finite watchdog should succeed, got {code}, err={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: stdout should contain queued id: {out!r}")
    queue = json.loads((tmpdir / "pending.json").read_text(encoding="utf-8"))
    assert_true(queue[0].get("watchdog_delay_sec") == 1.5, f"{label}: finite watchdog preserved: {queue}")
    print(f"  PASS  {label}")


def scenario_send_peer_no_auto_return_explicit_watchdog_rejected(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--no-auto-return", "--watchdog", "10"))
    assert_true(code == 2, f"{label}: no-auto-return + watchdog should exit 2, got {code}, err={err!r}")
    assert_true(out == "", f"{label}: rejected enqueue has no stdout: {out!r}")
    assert_true("watchdog requires auto_return" in err and "--watchdog 0" in err, f"{label}: error should guide disable/use: {err!r}")
    assert_true(not (tmpdir / "pending.json").exists(), f"{label}: rejected enqueue must write zero rows")
    print(f"  PASS  {label}")


def scenario_send_peer_no_auto_return_watchdog_zero_succeeds(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--no-auto-return", "--watchdog", "0"))
    assert_true(code == 0, f"{label}: no-auto-return + watchdog 0 should succeed, got {code}, err={err!r}")
    queue = json.loads((tmpdir / "pending.json").read_text(encoding="utf-8"))
    assert_true(queue[0].get("auto_return") is False, f"{label}: row should disable auto_return: {queue}")
    assert_true("watchdog_delay_sec" not in queue[0], f"{label}: watchdog 0 should leave no metadata: {queue}")
    print(f"  PASS  {label}")


def scenario_send_peer_no_auto_return_invalid_watchdog_value_precedence(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    for raw, expected in [("-1", "-1"), ("nan", "nan"), ("inf", "inf")]:
        case_dir = tmpdir / raw.replace("-", "minus_")
        case_dir.mkdir(parents=True, exist_ok=True)
        _patch_enqueue_for_unit(be, state)
        watchdog_args = (f"--watchdog={raw}",) if raw.startswith("-") else ("--watchdog", raw)
        code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(case_dir, "--no-auto-return", *watchdog_args))
        assert_true(code == 2 and out == "", f"{label}: invalid {raw!r} should fail before auto_return check: code={code} out={out!r} err={err!r}")
        assert_true("finite non-negative" in err and f"got {expected}" in err, f"{label}: value diagnostic should win for {raw!r}: {err!r}")
        assert_true("watchdog requires auto_return" not in err, f"{label}: auto_return error must not mask invalid value: {err!r}")
        assert_true(not (case_dir / "pending.json").exists(), f"{label}: invalid watchdog must not enqueue for {raw!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_no_auto_return_skips_default_watchdog(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    with patched_environ(AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC="42"):
        code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--no-auto-return"))
    assert_true(code == 0, f"{label}: no-auto-return should succeed with default watchdog env, got {code}, err={err!r}")
    queue = json.loads((tmpdir / "pending.json").read_text(encoding="utf-8"))
    assert_true(queue[0].get("auto_return") is False, f"{label}: row should have auto_return false: {queue}")
    assert_true("watchdog_delay_sec" not in queue[0], f"{label}: default watchdog must be skipped: {queue}")
    print(f"  PASS  {label}")


def scenario_send_peer_auto_return_default_watchdog_still_attaches(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    with patched_environ(AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC="42"):
        code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir))
    assert_true(code == 0, f"{label}: normal request should succeed with default watchdog, got {code}, err={err!r}")
    queue = json.loads((tmpdir / "pending.json").read_text(encoding="utf-8"))
    assert_true(queue[0].get("auto_return") is True, f"{label}: row should have auto_return true: {queue}")
    assert_true(queue[0].get("watchdog_delay_sec") == 42.0, f"{label}: default watchdog should attach: {queue}")
    print(f"  PASS  {label}")


def scenario_send_peer_no_auto_return_broadcast_watchdog_rejected(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob", "carol"])
    _patch_enqueue_for_unit(be, state)
    argv = [
        "--session", "test-session",
        "--from", "alice",
        "--all",
        "--kind", "request",
        "--body", "hello",
        "--queue-file", str(tmpdir / "pending.json"),
        "--state-file", str(tmpdir / "events.raw.jsonl"),
        "--public-state-file", str(tmpdir / "events.jsonl"),
        "--no-auto-return",
        "--watchdog", "10",
    ]
    code, out, err = _run_enqueue_main(be, argv)
    assert_true(code == 2 and out == "", f"{label}: broadcast no-auto watchdog should reject: code={code} out={out!r} err={err!r}")
    assert_true("watchdog requires auto_return" in err, f"{label}: error should explain auto_return requirement: {err!r}")
    assert_true(not (tmpdir / "pending.json").exists(), f"{label}: rejected broadcast must write zero rows")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_inf_with_notice_reports_finite_error_first(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--watchdog", "inf", kind="notice"))
    assert_true(code == 2 and out == "", f"{label}: notice+inf should fail cleanly: code={code} out={out!r}")
    assert_true("finite non-negative" in err and "got inf" in err, f"{label}: malformed value should win precedence: {err!r}")
    assert_true("--watchdog only applies" not in err, f"{label}: kind error must not mask malformed value: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_zero_with_notice_still_rejects_request_only(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--watchdog", "0", kind="notice"))
    assert_true(code == 2 and out == "", f"{label}: notice+0 should fail cleanly: code={code} out={out!r}")
    assert_true("--watchdog only applies" in err and "finite non-negative" not in err, f"{label}: finite 0 should reach kind check: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_abc_argparse_error(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    code, out, err = _run_enqueue_main(be, _enqueue_watchdog_argv(tmpdir, "--watchdog", "abc"))
    assert_true(code == 2, f"{label}: argparse invalid float exits 2, got {code}")
    assert_true(out == "", f"{label}: argparse error has no stdout: {out!r}")
    assert_true("invalid float value" in err and "abc" in err, f"{label}: argparse diagnostic preserved: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_oversized_body_before_subprocess(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    bs.validate_caller_identity = lambda args, session, sender: (session or "test-session", sender or "alice")
    bs.load_session = lambda session: _participants_state(["alice", "bob"])
    called = {"subprocess": False}
    old_run = bs.subprocess.run

    def fail_subprocess(_cmd):
        called["subprocess"] = True
        raise AssertionError("bridge_enqueue subprocess must not be spawned for oversized body")

    try:
        bs.subprocess.run = fail_subprocess
        code, out, err = _run_send_peer_main(
            bs,
            ["--session", "test-session", "--from", "alice", "--to", "bob"],
            stdin_text="x" * (MAX_INLINE_SEND_BODY_CHARS + 1),
        )
    finally:
        bs.subprocess.run = old_run
    assert_true(code == 2, f"{label}: wrapper rejects oversized body, got {code}")
    assert_true(out == "", f"{label}: rejection has no stdout: {out!r}")
    assert_true(not called["subprocess"], f"{label}: enqueue subprocess was not spawned")
    assert_true(str(MAX_INLINE_SEND_BODY_CHARS) in err and "/tmp/agent-bridge-share" in err, f"{label}: stderr explains limit: {err!r}")
    print(f"  PASS  {label}")


def _patch_send_peer_for_unit(bs) -> None:
    bs.validate_caller_identity = lambda args, session, sender: (session or "test-session", sender or "alice")
    bs.load_session = lambda session: _participants_state(["alice", "bob", "carol"])


def _run_send_peer_with_fake_subprocess(
    bs,
    argv: list[str],
    *,
    stdin_text: str = "",
    stdin_isatty: bool | None = None,
    stdout_text: str = "",
    stderr_text: str = "",
    returncode: int = 0,
):
    calls: list[tuple[list[str], dict]] = []
    old_run = bs.subprocess.run

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs)))
        if stdout_text:
            print(stdout_text, end="")
        if stderr_text:
            print(stderr_text, end="", file=sys.stderr)
        return argparse.Namespace(returncode=returncode)

    try:
        bs.subprocess.run = fake_run
        code, out, err = _run_send_peer_main(bs, argv, stdin_text=stdin_text, stdin_isatty=stdin_isatty)
    finally:
        bs.subprocess.run = old_run
    return code, out, err, calls


def scenario_send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--watchdog", "inf", "--to", "bob", "body"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and out == "" and calls == [], f"{label}: notice+inf should reject before subprocess: code={code} out={out!r} calls={calls}")
    assert_true("finite non-negative" in err and "got inf" in err, f"{label}: malformed watchdog value should win at shim: {err!r}")
    assert_true("only applies to --kind request" not in err, f"{label}: kind error must not mask malformed value: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--watchdog", "0", "--to", "bob", "body"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and out == "" and calls == [], f"{label}: notice+0 should reject before subprocess: code={code} out={out!r} calls={calls}")
    assert_true("only applies to --kind request" in err and "finite non-negative" not in err, f"{label}: finite zero should reach kind check: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_bare_minus_inf_argparse_error_at_shim(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--watchdog", "-inf", "--to", "bob", "body"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and out == "" and calls == [], f"{label}: bare -inf should fail in shim argparse: code={code} out={out!r} calls={calls}")
    assert_true("argument --watchdog" in err and "expected one argument" in err, f"{label}: argparse should own bare -inf diagnostic: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--watchdog=-inf", "--to", "bob", "body"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and out == "" and calls == [], f"{label}: equals -inf should reject before subprocess: code={code} out={out!r} calls={calls}")
    assert_true("finite non-negative" in err and "got -inf" in err, f"{label}: equals -inf should reach shim finite validator: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_watchdog_finite_value_forwarded_with_equals(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--watchdog", "1.5", "--to", "bob", "body"],
        stdin_isatty=True,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: wrapper should invoke enqueue once: code={code} err={err!r}")
    cmd, _kwargs = calls[0]
    assert_true("--watchdog=1.5" in cmd, f"{label}: watchdog value should be forwarded with equals: {cmd}")
    assert_true("--watchdog" not in cmd, f"{label}: wrapper must not forward watchdog as a separate argv token: {cmd}")
    print(f"  PASS  {label}")


def _assert_send_peer_body_error(
    label: str,
    case: str,
    argv: list[str],
    *,
    code: str,
    stdin_text: str = "",
    tokens: tuple[str, ...],
) -> None:
    proc = _run_send_peer_script(
        ["--allow-spoof", "--session", "test-session", "--from", "alice", *argv],
        stdin_text=stdin_text,
    )
    assert_true(proc.returncode == 2, f"{label}: {case} should exit 2, got {proc.returncode}, stderr={proc.stderr!r}")
    assert_true(proc.stdout == "", f"{label}: {case} should not print stdout: {proc.stdout!r}")
    assert_true(proc.stderr.startswith(f"agent_send_peer: {code}: "), f"{label}: {case} missing stable code {code!r}: {proc.stderr!r}")
    assert_true("run agent_list_peers if command syntax is unclear." in proc.stderr, f"{label}: {case} missing syntax recovery hint: {proc.stderr!r}")
    for token in tokens:
        assert_true(token in proc.stderr, f"{label}: {case} missing token {token!r}: {proc.stderr!r}")


def scenario_send_peer_body_input_diagnostics_are_specific(label: str, tmpdir: Path) -> None:
    _assert_send_peer_body_error(
        label,
        "missing body",
        ["--to", "bob"],
        code="missing_body",
        tokens=(
            "message body is required",
            "single inline argument",
            "agent_send_peer <alias> 'hello'",
            "agent_send_peer <alias> --stdin <<'EOF' ... EOF",
        ),
    )
    for case, stdin_text in (("empty stdin", ""), ("whitespace stdin", " \n\t")):
        _assert_send_peer_body_error(
            label,
            case,
            ["--to", "bob", "--stdin"],
            code="empty_stdin",
            stdin_text=stdin_text,
            tokens=("--stdin received an empty body", "heredoc body was empty", "non-empty body lines"),
        )
    _assert_send_peer_body_error(
        label,
        "positional after stdin",
        ["--to", "bob", "--stdin", "some-body"],
        code="unexpected_positional_after_stdin",
        stdin_text="stdin body",
        tokens=("--stdin reads body from stdin", "extra positional argument", "drop --stdin", "pipe via heredoc"),
    )
    _assert_send_peer_body_error(
        label,
        "stdin plus inline body",
        ["--to", "bob", "inline body", "--stdin"],
        code="stdin_inline_conflict",
        stdin_text="stdin body",
        tokens=("--stdin and an inline body cannot be combined", "Pick one", "single argument", "stdin/heredoc"),
    )
    _assert_send_peer_body_error(
        label,
        "piped stdin plus inline body",
        ["--to", "bob", "inline body"],
        code="piped_stdin_inline_conflict",
        stdin_text="piped body",
        tokens=("piped stdin and an inline body cannot be combined", "remove the pipe", "inline body", "--stdin", "heredoc"),
    )
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_split_inline_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "hello", "world"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: split explicit body must reject before enqueue")
    assert_true("multiple shell arguments" in err and "--stdin" in err, f"{label}: stderr must explain heredoc path: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_implicit_split_inline_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "bob", "hello", "world"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: split implicit-target body must reject")
    assert_true("multiple shell arguments" in err and "--stdin" in err, f"{label}: stderr must explain stdin: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_accepts_option_after_destination_with_stdin(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    for argv in (
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--watchdog", "1800", "--stdin"],
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--watchdog=1800", "--stdin"],
        ["--session", "test-session", "--from", "alice", "--all", "--watchdog", "1800", "--stdin"],
    ):
        code, out, err, calls = _run_send_peer_with_fake_subprocess(
            bs,
            argv,
            stdin_text="stdin body",
            stdin_isatty=False,
        )
        assert_true(code == 0 and len(calls) == 1, f"{label}: option after destination before stdin should succeed for {argv}: code={code} err={err!r}")
        cmd, kwargs = calls[0]
        if "--all" in argv:
            assert_true("--all" in cmd and "--to" not in cmd, f"{label}: broadcast destination preserved: {cmd}")
        else:
            assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: target preserved: {cmd}")
        assert_true("--watchdog=1800.0" in cmd and "--stdin" in cmd, f"{label}: watchdog and stdin forwarded: {cmd}")
        assert_true(kwargs.get("input") == b"stdin body", f"{label}: stdin body preserved: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_accepts_option_after_implicit_target_with_stdin(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "bob", "--watchdog", "1800", "--stdin"],
        stdin_text="stdin body",
        stdin_isatty=False,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: option after implicit target before stdin should succeed: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: implicit target forwarded: {cmd}")
    assert_true("--watchdog=1800.0" in cmd and "--stdin" in cmd, f"{label}: watchdog and stdin forwarded: {cmd}")
    assert_true(kwargs.get("input") == b"stdin body", f"{label}: stdin body preserved: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_accepts_options_after_destination_before_inline_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--kind", "request", "--watchdog", "0", "hello world"],
        stdin_isatty=True,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: options after destination before inline body should succeed: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: target preserved: {cmd}")
    assert_true("--kind" in cmd and cmd[cmd.index("--kind") + 1] == "request", f"{label}: kind forwarded: {cmd}")
    assert_true("--watchdog=0.0" in cmd, f"{label}: watchdog 0 forwarded: {cmd}")
    assert_true(kwargs.get("input") == b"hello world", f"{label}: inline body preserved via stdin handoff: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_option_after_inline_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "hello", "--watchdog", "1800"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: option after body must reject")
    assert_true("after the inline body" in err and "--watchdog" in err, f"{label}: stderr explains option position: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_duplicate_destination_after_destination(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    for argv in (
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--to", "carol", "--stdin"],
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--to", "carol"],
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--all"],
    ):
        code, out, err, calls = _run_send_peer_with_fake_subprocess(
            bs,
            argv,
            stdin_text="stdin body",
            stdin_isatty=False,
        )
        assert_true(code == 2 and not calls and out == "", f"{label}: duplicate destination must reject for {argv}")
        assert_true("use exactly one destination selector" in err, f"{label}: duplicate destination diagnostic for {argv}: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_implicit_target_option_without_body_reports_body_required(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "bob", "--watchdog", "1800"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: implicit target with options but no body should reject")
    assert_true("missing_body" in err and "message body is required" in err and "after the inline body" not in err, f"{label}: missing-body diagnostic should stay clear: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_allow_spoof_after_implicit_target(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "bob", "--allow-spoof", "--stdin"],
        stdin_text="stdin body",
        stdin_isatty=False,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: --allow-spoof after implicit target must reject")
    assert_true("--allow-spoof" in err and "before the destination" in err, f"{label}: placement diagnostic should be clear: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_single_inline_body_uses_stdin_handoff(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    body = "  can't stop"
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--to", "bob", body],
        stdin_isatty=True,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: single argv body should enqueue once: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true("--stdin" in cmd and "--body" not in cmd, f"{label}: wrapper must hand body to enqueue via stdin: {cmd}")
    assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: target preserved: {cmd}")
    assert_true(kwargs.get("input") == body.encode("utf-8"), f"{label}: body encoded as utf-8 bytes: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_request_success_prints_anti_wait_hint(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "hello world"],
        stdin_isatty=True,
        stdout_text="msg-test123\n",
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: request should succeed: code={code} err={err!r}")
    assert_true(out == "msg-test123\n", f"{label}: enqueue stdout must be preserved exactly: {out!r}")
    assert_true("AGGREGATE_ID:" not in out and "AGGREGATE_ID:" not in err, f"{label}: single target must not expose aggregate id: out={out!r} err={err!r}")
    for token in ("REQUEST_SENT", "[bridge:*]", "do not sleep/poll"):
        assert_true(token in err, f"{label}: request hint missing token {token!r}: {err!r}")
    assert_true("notice sent" not in err and "Safety wake" not in err and "NOTICE_SENT" not in err, f"{label}: request must not print notice hint: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_prior_hint_precedes_success_hint(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    prior_hint = "PRIOR_MESSAGE_HINT: bob is processing your earlier message msg-old (status: delivered).\n"
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "hello world"],
        stdin_isatty=True,
        stdout_text="msg-new\n",
        stderr_text=prior_hint,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: request should succeed: code={code} err={err!r}")
    assert_true(out == "msg-new\n", f"{label}: enqueue stdout must be preserved exactly: {out!r}")
    assert_true("PRIOR_MESSAGE_HINT" in err and "REQUEST_SENT" in err, f"{label}: stderr should include prior and success hints: {err!r}")
    assert_true(
        err.index("PRIOR_MESSAGE_HINT") < err.index("REQUEST_SENT"),
        f"{label}: prior hint should appear before wrapper success hint: {err!r}",
    )
    print(f"  PASS  {label}")


def scenario_send_peer_notice_success_prints_alarm_and_anti_wait_hints(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--to", "bob", "hello world"],
        stdin_isatty=True,
        stdout_text="msg-notice123\n",
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: notice should succeed: code={code} err={err!r}")
    assert_true(out == "msg-notice123\n", f"{label}: enqueue stdout must be preserved exactly: {out!r}")
    for token in ("NOTICE_SENT", "no reply auto-routes"):
        assert_true(token in err, f"{label}: notice hint missing token {token!r}: {err!r}")
    assert_true("notice sent. Safety wake" not in err, f"{label}: legacy notice safety line must stay absent: {err!r}")
    assert_true("REQUEST_SENT" not in err and "sleep/polling blocks" not in err, f"{label}: notice must not print request/legacy hint: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_aggregate_request_success_prints_result_hint(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    for argv, stdout_text in (
        (["--session", "test-session", "--from", "alice", "--to", "bob,carol", "hello world"], "msg-bob\nmsg-carol\nAGGREGATE_ID: agg-wrapper-partial\n"),
        (["--session", "test-session", "--from", "alice", "--all", "hello world"], "msg-bob\nmsg-carol\nAGGREGATE_ID: agg-wrapper-all\n"),
    ):
        code, out, err, calls = _run_send_peer_with_fake_subprocess(
            bs,
            argv,
            stdin_isatty=True,
            stdout_text=stdout_text,
        )
        assert_true(code == 0 and len(calls) == 1, f"{label}: aggregate request should succeed for {argv}: code={code} err={err!r}")
        assert_true(out == stdout_text, f"{label}: enqueue stdout must be preserved exactly for {argv}: {out!r}")
        assert_true(out.count("AGGREGATE_ID: agg-") == 1, f"{label}: aggregate stdout should include exactly one aggregate id for {argv}: {out!r}")
        assert_true("AGGREGATE_ID:" not in err, f"{label}: aggregate id belongs on stdout, not stderr: {err!r}")
        for token in ("REQUEST_SENT", "result arrives later", "[bridge:*]", "do not sleep/poll"):
            assert_true(token in err, f"{label}: aggregate request hint missing token {token!r} for {argv}: {err!r}")
        assert_true("reply arrives later" not in err, f"{label}: aggregate hint must say result, not reply: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_subprocess_failure_prints_no_success_hint(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--to", "bob", "hello world"],
        stdin_isatty=True,
        stdout_text="enqueue failed details\n",
        returncode=1,
    )
    assert_true(code == 1 and len(calls) == 1, f"{label}: subprocess failure should propagate: code={code}")
    assert_true(out == "enqueue failed details\n", f"{label}: failure stdout still comes from subprocess: {out!r}")
    assert_true("notice sent" not in err and "Safety wake" not in err and "sleep/polling blocks" not in err, f"{label}: legacy success hints must not print on failure: {err!r}")
    assert_true("REQUEST_SENT" not in err and "NOTICE_SENT" not in err, f"{label}: success hints must not print on failure: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_ambient_socket_stdin_does_not_block(label: str, tmpdir: Path) -> None:
    script = f"""
import argparse
import json
import os
import socket
import sys
sys.path.insert(0, {str(LIBEXEC)!r})
import bridge_send_peer as bs

case = sys.argv[1]
left, right = socket.socketpair()
if case == "partial-inline":
    right.sendall(b"x")
sys.stdin = os.fdopen(left.detach(), "r", encoding="utf-8", buffering=1)
try:
    if case == "idle-empty":
        args = argparse.Namespace(target="bob", target_all=False, message=[], stdin_body=False)
    else:
        args = argparse.Namespace(target="bob", target_all=False, message=["hello"], stdin_body=False)
    target, body = bs.parse_body_and_target(args, "")
    print(json.dumps({{"ok": True, "target": target, "body": body, "target_all": args.target_all}}, ensure_ascii=True))
except Exception as exc:
    print(json.dumps({{"ok": False, "type": type(exc).__name__, "error": str(exc)}}, ensure_ascii=True))
"""
    cases = {
        "idle-inline": {"ok": True, "target": "bob", "body": "hello"},
        "idle-empty": {"ok": False, "error_contains": "missing_body"},
        "partial-inline": {"ok": False, "error_contains": "piped_stdin_inline_conflict"},
    }
    for case, expected in cases.items():
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script, case],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"{label}: {case} must not block on ambient socket stdin") from exc
        assert_true(proc.returncode == 0, f"{label}: {case} child exited {proc.returncode}, stderr={proc.stderr!r}")
        result = json.loads(proc.stdout.strip())
        assert_true(result.get("ok") is expected["ok"], f"{label}: {case} ok mismatch: {result}")
        if expected["ok"]:
            assert_true(result.get("target") == expected["target"], f"{label}: {case} target mismatch: {result}")
            assert_true(result.get("body") == expected["body"], f"{label}: {case} body mismatch: {result}")
        else:
            assert_true(expected["error_contains"] in str(result.get("error") or ""), f"{label}: {case} expected pipe collision error: {result}")
    print(f"  PASS  {label}")


def scenario_send_peer_inline_body_accepts_empty_non_tty_stdin(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--to", "bob", "hello world"],
        stdin_text="",
        stdin_isatty=False,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: empty non-tty stdin must not look like a pipe collision: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true("--stdin" in cmd and "--body" not in cmd, f"{label}: enqueue still uses --stdin: {cmd}")
    assert_true(kwargs.get("input") == b"hello world", f"{label}: inline body preserved: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_explicit_stdin_multibyte_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    body = "  first line\ncan't --kind request `x`\n"
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--to", "bob", "--stdin"],
        stdin_text=body,
        stdin_isatty=False,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: explicit stdin should enqueue once: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true("--stdin" in cmd and "--body" not in cmd, f"{label}: enqueue subprocess uses --stdin: {cmd}")
    assert_true(kwargs.get("input") == body.encode("utf-8"), f"{label}: multibyte body must be utf-8 bytes: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_implicit_target_allows_stdin(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "bob", "--stdin"],
        stdin_text="stdin body",
        stdin_isatty=False,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: implicit target + --stdin should work: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: implicit target passed to enqueue: {cmd}")
    assert_true(kwargs.get("input") == b"stdin body", f"{label}: stdin body forwarded: {kwargs}")
    print(f"  PASS  {label}")


def _write_send_peer_precheck_identity(state_root_path: Path, *, pane: str = "%20") -> list[Path]:
    write_identity_fixture(state_root_path, alias="alice", agent="codex", session_id="sess-a", pane=pane)
    live_record = {
        "agent": "codex",
        "session_id": "sess-a",
        "pane": pane,
        "target": "tmux:1.0",
        "last_seen_at": utc_now(),
    }
    _write_json(
        Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]),
        {
            "version": 1,
            "panes": {pane: live_record},
            "sessions": {bridge_identity.identity_key("codex", "sess-a"): live_record},
        },
    )
    return [
        Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]),
        Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]),
        Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]),
    ]


def scenario_send_peer_implicit_target_resolves_session_from_pane(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    with isolated_identity_env(tmpdir) as state_root_path:
        identity_files = _write_send_peer_precheck_identity(state_root_path, pane="%20")
        before = {path: path.read_bytes() for path in identity_files}
        with patched_environ(AGENT_BRIDGE_SESSION=None, AGENT_BRIDGE_AGENT=None, TMUX_PANE="%20"):
            code, out, err, calls = _run_send_peer_with_fake_subprocess(
                bs,
                ["--kind", "notice", "bob", "hello world"],
                stdin_isatty=True,
            )
        after = {path: path.read_bytes() for path in identity_files}
    assert_true(before == after, f"{label}: precheck pane lookup must be read-only")
    assert_true(code == 0 and len(calls) == 1, f"{label}: implicit target should succeed without env session: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: implicit target forwarded as --to bob: {cmd}")
    assert_true(kwargs.get("input") == b"hello world", f"{label}: body forwarded via stdin bytes: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_implicit_target_stdin_resolves_session_from_pane(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    with isolated_identity_env(tmpdir) as state_root_path:
        _write_send_peer_precheck_identity(state_root_path, pane="%21")
        with patched_environ(AGENT_BRIDGE_SESSION=None, AGENT_BRIDGE_AGENT=None, TMUX_PANE="%21"):
            code, out, err, calls = _run_send_peer_with_fake_subprocess(
                bs,
                ["bob", "--stdin"],
                stdin_text="stdin body",
                stdin_isatty=False,
            )
    assert_true(code == 0 and len(calls) == 1, f"{label}: implicit target + stdin should succeed without env session: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: implicit target forwarded as --to bob: {cmd}")
    assert_true(kwargs.get("input") == b"stdin body", f"{label}: stdin body forwarded: {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_precheck_fails_open_identity_errors_owned_by_validator(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    parser = bs.build_parser()
    bs._precheck_lookup_session_for_pane = lambda pane: ""  # type: ignore[attr-defined]
    split_error = bs.validate_send_peer_argv(["bob", "hello"], parser)
    assert_true("multiple shell arguments" in split_error, f"{label}: unresolved implicit target should fail closed: {split_error!r}")

    def identity_error(args, session, sender):
        print("agent_send_peer: duplicate resume identity error", file=sys.stderr)
        return None

    bs.validate_caller_identity = identity_error
    bs.load_session = lambda session: _participants_state(["alice", "bob"])
    with patched_environ(AGENT_BRIDGE_SESSION=None, AGENT_BRIDGE_AGENT=None, TMUX_PANE="%99"):
        code, out, err = _run_send_peer_main(bs, ["--to", "bob", "hello"], stdin_isatty=True)
    assert_true(code == 2 and out == "", f"{label}: validator identity error should reject: code={code} out={out!r}")
    assert_true("duplicate resume identity error" in err and "multiple shell arguments" not in err, f"{label}: identity error should be authoritative: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_allow_spoof_requires_explicit_destination_without_session(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    bs._precheck_lookup_session_for_pane = lambda pane: ""  # type: ignore[attr-defined]
    with patched_environ(AGENT_BRIDGE_SESSION=None, AGENT_BRIDGE_AGENT=None, TMUX_PANE=None):
        code, out, err, calls = _run_send_peer_with_fake_subprocess(
            bs,
            ["--allow-spoof", "bob", "hello world"],
            stdin_isatty=True,
        )
    assert_true(code == 2 and not calls and out == "", f"{label}: allow-spoof shorthand without session should reject before enqueue")
    assert_true("leading-alias shorthand" in err and "--to <alias>" in err and "multiple shell arguments" not in err, f"{label}: error should require explicit destination: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_allow_spoof_attached_value(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--allow-spoof=1", "--to", "bob", "hello world"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: attached value on flag must reject")
    assert_true("unrecognized option" in err and "--allow-spoof=1" in err, f"{label}: stderr identifies bad flag form: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_all_rejects_leading_alias_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--all", "bob"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: --all with leading alias body must reject")
    assert_true("remove leading alias" in err and "use --to bob" in err, f"{label}: stderr should explain --all alias confusion: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_stdin_with_positional_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--stdin", "body"],
        stdin_text="stdin body",
        stdin_isatty=False,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: --stdin + positional body must reject")
    assert_true("unexpected_positional_after_stdin" in err and "extra positional argument" in err, f"{label}: stderr explains collision: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_pipe_with_positional_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "body"],
        stdin_text="pipe body",
        stdin_isatty=False,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: pipe + positional body must reject")
    assert_true(err.startswith("agent_send_peer: piped_stdin_inline_conflict: "), f"{label}: stderr includes stable pipe collision code: {err!r}")
    for token in ("piped stdin and an inline body cannot be combined", "remove the pipe", "--stdin", "heredoc"):
        assert_true(token in err, f"{label}: stderr explains pipe collision with token {token!r}: {err!r}")
    assert_true("cannot combine piped stdin with a positional inline body" not in err, f"{label}: legacy un-coded phrase must stay absent: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_pipe_only_body_still_supported(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob"],
        stdin_text="pipe body",
        stdin_isatty=False,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: pipe-only body remains supported: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true("--stdin" in cmd and kwargs.get("input") == b"pipe body", f"{label}: pipe body forwarded via stdin: {cmd} {kwargs}")
    print(f"  PASS  {label}")


def scenario_send_peer_precheck_option_table_matches_parser(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    parser = bs.build_parser()
    value_options, flag_options = bs.option_kinds_from_parser(parser)
    covered = value_options | flag_options
    parser_options = {
        opt
        for action in parser._actions
        for opt in (getattr(action, "option_strings", []) or [])
        if opt not in {"-h", "--help"}
    }
    assert_true(parser_options <= covered, f"{label}: precheck option table missing {sorted(parser_options - covered)}")
    assert_true("--stdin" in flag_options and "--to" in value_options and "-t" in value_options, f"{label}: expected key options classified")
    print(f"  PASS  {label}")


def scenario_enqueue_rejects_oversized_body_unchanged(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    queue_file.write_text("[]", encoding="utf-8")
    state_file.write_text(json.dumps({"event": "initial_raw"}) + "\n", encoding="utf-8")
    public_file.write_text(json.dumps({"event": "initial_public"}) + "\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    be.enqueue_via_daemon_socket = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket enqueue must not run"))

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob",
            "--body", "x" * (MAX_INLINE_SEND_BODY_CHARS + 1),
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 2, f"{label}: enqueue rejects oversized body, got {code}")
    assert_true(out == "", f"{label}: rejection has no stdout: {out!r}")
    assert_true(str(MAX_INLINE_SEND_BODY_CHARS) in err and "/tmp/agent-bridge-share" in err, f"{label}: stderr explains limit: {err!r}")
    after = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    assert_true(before == after, f"{label}: rejection must not mutate queue or events")
    print(f"  PASS  {label}")


def scenario_enqueue_stdin_rejects_oversized_body_unchanged(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    queue_file.write_text("[]", encoding="utf-8")
    state_file.write_text(json.dumps({"event": "initial_raw"}) + "\n", encoding="utf-8")
    public_file.write_text(json.dumps({"event": "initial_public"}) + "\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    be.enqueue_via_daemon_socket = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket enqueue must not run"))

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob",
            "--stdin",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
        stdin_text="x" * (MAX_INLINE_SEND_BODY_CHARS + 100),
    )
    assert_true(code == 2, f"{label}: enqueue --stdin rejects oversized body, got {code}")
    assert_true(out == "", f"{label}: rejection has no stdout: {out!r}")
    assert_true(str(MAX_INLINE_SEND_BODY_CHARS) in err and "/tmp/agent-bridge-share" in err, f"{label}: stderr explains limit: {err!r}")
    after = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    assert_true(before == after, f"{label}: rejection must not mutate queue or events")
    print(f"  PASS  {label}")


def _enqueue_prior_hint_argv(tmpdir: Path, *, kind: str = "request") -> list[str]:
    return [
        "--session", "test-session",
        "--from", "alice",
        "--to", "bob",
        "--kind", kind,
        "--body", "replacement",
        "--queue-file", str(tmpdir / "pending.json"),
        "--state-file", str(tmpdir / "events.raw.jsonl"),
        "--public-state-file", str(tmpdir / "events.jsonl"),
    ]


def scenario_enqueue_socket_success_hints_stderr_stdout_unchanged(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    be.enqueue_via_daemon_socket = lambda session, messages, **kwargs: (  # type: ignore[assignment]
        True,
        ["msg-socket-hint"],
        [{"text": "PRIOR_MESSAGE_HINT: bob still has your earlier message msg-prior queued (status: pending)."}],
        "",
        "",
    )

    code, out, err = _run_enqueue_main(be, _enqueue_prior_hint_argv(tmpdir))

    assert_true(code == 0, f"{label}: socket enqueue should succeed: code={code} err={err!r}")
    assert_true(out == "msg-socket-hint\n", f"{label}: stdout must contain ids only: {out!r}")
    assert_true("PRIOR_MESSAGE_HINT:" in err and "PRIOR_MESSAGE_HINT:" not in out, f"{label}: hints must go to stderr only: out={out!r} err={err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_prior_hint_pending_cancel(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    _write_json(tmpdir / "pending.json", [test_message("msg-fb-prior-pending", frm="alice", to="bob", status="pending")])

    code, out, err = _run_enqueue_main(be, _enqueue_prior_hint_argv(tmpdir))

    assert_true(code == 0, f"{label}: fallback enqueue should succeed: code={code} err={err!r}")
    assert_true(out.startswith("msg-") and "PRIOR_MESSAGE_HINT" not in out, f"{label}: stdout should contain ids only: {out!r}")
    assert_true("PRIOR_MESSAGE_HINT:" in err and "agent_cancel_message msg-fb-prior-pending" in err, f"{label}: cancel hint missing: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_prior_hint_submitted_interrupt(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    _write_json(tmpdir / "pending.json", [test_message("msg-fb-prior-submitted", frm="alice", to="bob", status="submitted")])

    code, out, err = _run_enqueue_main(be, _enqueue_prior_hint_argv(tmpdir))

    assert_true(code == 0, f"{label}: fallback enqueue should succeed: code={code} err={err!r}")
    assert_true("agent_interrupt_peer bob --status" in err and "msg-fb-prior-submitted" in err, f"{label}: interrupt hint missing: {err!r}")
    assert_true("so this new message can deliver" not in err, f"{label}: unsafe wording absent: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_prior_hint_plain_inflight_omitted(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    _write_json(tmpdir / "pending.json", [test_message("msg-fb-prior-inflight", frm="alice", to="bob", status="inflight")])

    code, out, err = _run_enqueue_main(be, _enqueue_prior_hint_argv(tmpdir))

    assert_true(code == 0, f"{label}: fallback enqueue should succeed: code={code} err={err!r}")
    assert_true(out.startswith("msg-"), f"{label}: stdout should contain id: {out!r}")
    assert_true("PRIOR_MESSAGE_HINT" not in err, f"{label}: plain inflight fallback must omit hint: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_prior_hint_pane_mode_inflight_cancel(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    prior = test_message("msg-fb-prior-pane-mode", frm="alice", to="bob", status="inflight")
    prior["pane_mode_enter_deferred_since_ts"] = time.time()
    _write_json(tmpdir / "pending.json", [prior])

    code, out, err = _run_enqueue_main(be, _enqueue_prior_hint_argv(tmpdir))

    assert_true(code == 0, f"{label}: fallback enqueue should succeed: code={code} err={err!r}")
    assert_true("agent_cancel_message msg-fb-prior-pane-mode" in err, f"{label}: pane-mode inflight should get cancel hint: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_prior_hint_write_failure_suppresses_hint(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    _write_json(tmpdir / "pending.json", [test_message("msg-fb-prior-write-fail", frm="alice", to="bob", status="pending")])
    old_update_queue = be.update_queue

    def fail_update_queue(_path, _message):
        raise OSError(errno.EACCES, "denied")

    try:
        be.update_queue = fail_update_queue  # type: ignore[assignment]
        code, out, err = _run_enqueue_main(be, _enqueue_prior_hint_argv(tmpdir))
    finally:
        be.update_queue = old_update_queue  # type: ignore[assignment]

    assert_true(code == 1 and out == "", f"{label}: write failure should fail before stdout: code={code} out={out!r}")
    assert_true("not writable" in err or "failed to write" in err, f"{label}: write failure error missing: {err!r}")
    assert_true("PRIOR_MESSAGE_HINT" not in err, f"{label}: failed enqueue must not print prior hint: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_response_send_guard_no_hint(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    _write_json(
        queue_file,
        [
            _delivered_request("msg-guard-active", "alice", "bob"),
            test_message("msg-prior-would-hint", frm="bob", to="alice", status="pending"),
        ],
    )
    state_file.write_text("raw-before\n", encoding="utf-8")
    public_file.write_text("public-before\n", encoding="utf-8")

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--to", "alice",
            "--body", "guarded replacement",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )

    assert_true(code == 2, f"{label}: response-send guard should reject: code={code} err={err!r}")
    assert_true(out == "", f"{label}: rejected fallback send must have no stdout: {out!r}")
    assert_true("response-time guard" in err and "current_prompt.from=alice" in err, f"{label}: response-send guard text missing: {err!r}")
    assert_true("PRIOR_MESSAGE_HINT" not in err, f"{label}: rejected fallback send must not print prior hint: {err!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_success_silent_with_raw_diagnostic(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state, socket_error="/tmp/secret.sock: permission denied\nextra line")
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    queue_file.write_text("[]", encoding="utf-8")
    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob",
            "--body", "hello",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 0, f"{label}: enqueue succeeds: code={code}, stderr={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: stdout contains message id: {out!r}")
    assert_true(err == "", f"{label}: successful fallback must be silent on stderr: {err!r}")
    assert_true("daemon socket unavailable" not in err and "falling back to direct file" not in err, f"{label}: warning suppressed")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    assert_true(queue and queue[0].get("status") == "ingressing", f"{label}: queue item is ingressing: {queue}")
    raw_events = read_events(state_file)
    public_events = read_events(public_file)
    assert_true(any(item.get("event") == "message_queued" for item in raw_events), f"{label}: message_queued raw event present")
    fallback_events = [item for item in raw_events if item.get("event") == "enqueue_file_fallback"]
    assert_true(len(fallback_events) == 1, f"{label}: one raw fallback diagnostic: {raw_events}")
    assert_true(all(item.get("event") != "enqueue_file_fallback" for item in public_events), f"{label}: fallback diagnostic not public: {public_events}")
    socket_error = str(fallback_events[0].get("socket_error") or "")
    assert_true("\n" not in socket_error and len(socket_error) <= 200, f"{label}: socket_error sanitized: {socket_error!r}")
    print(f"  PASS  {label}")


def scenario_enqueue_fallback_write_failure_preserves_stderr(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)

    def fail_update_queue(path: Path, message: dict) -> None:
        raise OSError(errno.EACCES, "denied")

    be.update_queue = fail_update_queue
    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "alice",
            "--to", "bob",
            "--body", "hello",
            "--queue-file", str(tmpdir / "pending.json"),
            "--state-file", str(tmpdir / "events.raw.jsonl"),
            "--public-state-file", str(tmpdir / "events.jsonl"),
        ],
    )
    assert_true(code == 1, f"{label}: write failure exits 1, got {code}")
    assert_true(out == "", f"{label}: no stdout on write failure: {out!r}")
    assert_true("cannot enqueue message" in err or "failed to write bridge queue" in err, f"{label}: stderr preserves failure: {err!r}")
    print(f"  PASS  {label}")


SCENARIOS = [
    ('send_peer_watchdog_negative_one_rejected', scenario_send_peer_watchdog_negative_one_rejected),
    ('send_peer_watchdog_nan_rejected', scenario_send_peer_watchdog_nan_rejected),
    ('send_peer_watchdog_inf_rejected', scenario_send_peer_watchdog_inf_rejected),
    ('send_peer_watchdog_minus_inf_rejected', scenario_send_peer_watchdog_minus_inf_rejected),
    ('send_peer_watchdog_zero_disables_for_request', scenario_send_peer_watchdog_zero_disables_for_request),
    ('send_peer_watchdog_finite_positive_succeeds', scenario_send_peer_watchdog_finite_positive_succeeds),
    ('send_peer_no_auto_return_explicit_watchdog_rejected', scenario_send_peer_no_auto_return_explicit_watchdog_rejected),
    ('send_peer_no_auto_return_watchdog_zero_succeeds', scenario_send_peer_no_auto_return_watchdog_zero_succeeds),
    ('send_peer_no_auto_return_invalid_watchdog_value_precedence', scenario_send_peer_no_auto_return_invalid_watchdog_value_precedence),
    ('send_peer_no_auto_return_skips_default_watchdog', scenario_send_peer_no_auto_return_skips_default_watchdog),
    ('send_peer_auto_return_default_watchdog_still_attaches', scenario_send_peer_auto_return_default_watchdog_still_attaches),
    ('send_peer_no_auto_return_broadcast_watchdog_rejected', scenario_send_peer_no_auto_return_broadcast_watchdog_rejected),
    ('send_peer_watchdog_inf_with_notice_reports_finite_error_first', scenario_send_peer_watchdog_inf_with_notice_reports_finite_error_first),
    ('send_peer_watchdog_zero_with_notice_still_rejects_request_only', scenario_send_peer_watchdog_zero_with_notice_still_rejects_request_only),
    ('send_peer_watchdog_abc_argparse_error', scenario_send_peer_watchdog_abc_argparse_error),
    ('enqueue_aggregate_metadata_modes', scenario_enqueue_aggregate_metadata_modes),
    ('send_peer_rejects_oversized_body_before_subprocess', scenario_send_peer_rejects_oversized_body_before_subprocess),
    ('send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim', scenario_send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim),
    ('send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim', scenario_send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim),
    ('send_peer_watchdog_bare_minus_inf_argparse_error_at_shim', scenario_send_peer_watchdog_bare_minus_inf_argparse_error_at_shim),
    ('send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim', scenario_send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim),
    ('send_peer_watchdog_finite_value_forwarded_with_equals', scenario_send_peer_watchdog_finite_value_forwarded_with_equals),
    ('send_peer_body_input_diagnostics_are_specific', scenario_send_peer_body_input_diagnostics_are_specific),
    ('send_peer_rejects_split_inline_body', scenario_send_peer_rejects_split_inline_body),
    ('send_peer_rejects_implicit_split_inline_body', scenario_send_peer_rejects_implicit_split_inline_body),
    ('send_peer_accepts_option_after_destination_with_stdin', scenario_send_peer_accepts_option_after_destination_with_stdin),
    ('send_peer_accepts_option_after_implicit_target_with_stdin', scenario_send_peer_accepts_option_after_implicit_target_with_stdin),
    ('send_peer_accepts_options_after_destination_before_inline_body', scenario_send_peer_accepts_options_after_destination_before_inline_body),
    ('send_peer_rejects_option_after_inline_body', scenario_send_peer_rejects_option_after_inline_body),
    ('send_peer_rejects_duplicate_destination_after_destination', scenario_send_peer_rejects_duplicate_destination_after_destination),
    ('send_peer_implicit_target_option_without_body_reports_body_required', scenario_send_peer_implicit_target_option_without_body_reports_body_required),
    ('send_peer_rejects_allow_spoof_after_implicit_target', scenario_send_peer_rejects_allow_spoof_after_implicit_target),
    ('send_peer_single_inline_body_uses_stdin_handoff', scenario_send_peer_single_inline_body_uses_stdin_handoff),
    ('send_peer_request_success_prints_anti_wait_hint', scenario_send_peer_request_success_prints_anti_wait_hint),
    ('send_peer_prior_hint_precedes_success_hint', scenario_send_peer_prior_hint_precedes_success_hint),
    ('send_peer_notice_success_prints_alarm_and_anti_wait_hints', scenario_send_peer_notice_success_prints_alarm_and_anti_wait_hints),
    ('send_peer_aggregate_request_success_prints_result_hint', scenario_send_peer_aggregate_request_success_prints_result_hint),
    ('send_peer_subprocess_failure_prints_no_success_hint', scenario_send_peer_subprocess_failure_prints_no_success_hint),
    ('send_peer_ambient_socket_stdin_does_not_block', scenario_send_peer_ambient_socket_stdin_does_not_block),
    ('send_peer_inline_body_accepts_empty_non_tty_stdin', scenario_send_peer_inline_body_accepts_empty_non_tty_stdin),
    ('send_peer_explicit_stdin_multibyte_body', scenario_send_peer_explicit_stdin_multibyte_body),
    ('send_peer_implicit_target_allows_stdin', scenario_send_peer_implicit_target_allows_stdin),
    ('send_peer_implicit_target_resolves_session_from_pane', scenario_send_peer_implicit_target_resolves_session_from_pane),
    ('send_peer_implicit_target_stdin_resolves_session_from_pane', scenario_send_peer_implicit_target_stdin_resolves_session_from_pane),
    ('send_peer_precheck_fails_open_identity_errors_owned_by_validator', scenario_send_peer_precheck_fails_open_identity_errors_owned_by_validator),
    ('send_peer_allow_spoof_requires_explicit_destination_without_session', scenario_send_peer_allow_spoof_requires_explicit_destination_without_session),
    ('send_peer_rejects_allow_spoof_attached_value', scenario_send_peer_rejects_allow_spoof_attached_value),
    ('send_peer_all_rejects_leading_alias_body', scenario_send_peer_all_rejects_leading_alias_body),
    ('send_peer_rejects_stdin_with_positional_body', scenario_send_peer_rejects_stdin_with_positional_body),
    ('send_peer_rejects_pipe_with_positional_body', scenario_send_peer_rejects_pipe_with_positional_body),
    ('send_peer_pipe_only_body_still_supported', scenario_send_peer_pipe_only_body_still_supported),
    ('send_peer_precheck_option_table_matches_parser', scenario_send_peer_precheck_option_table_matches_parser),
    ('enqueue_rejects_body_and_stdin_before_session_lookup', scenario_enqueue_rejects_body_and_stdin_before_session_lookup),
    ('enqueue_rejects_empty_body_and_stdin', scenario_enqueue_rejects_empty_body_and_stdin),
    ('enqueue_rejects_body_and_stdin_argv_order_independent', scenario_enqueue_rejects_body_and_stdin_argv_order_independent),
    ('enqueue_body_only_still_works', scenario_enqueue_body_only_still_works),
    ('enqueue_stdin_only_still_works', scenario_enqueue_stdin_only_still_works),
    ('enqueue_aggregate_stdout_socket_and_fallback', scenario_enqueue_aggregate_stdout_socket_and_fallback),
    ('enqueue_aggregate_stdout_all_and_negative_cases', scenario_enqueue_aggregate_stdout_all_and_negative_cases),
    ('enqueue_bare_body_without_value_remains_argparse_owned', scenario_enqueue_bare_body_without_value_remains_argparse_owned),
    ('enqueue_rejects_oversized_body_unchanged', scenario_enqueue_rejects_oversized_body_unchanged),
    ('enqueue_stdin_rejects_oversized_body_unchanged', scenario_enqueue_stdin_rejects_oversized_body_unchanged),
    ('enqueue_socket_success_hints_stderr_stdout_unchanged', scenario_enqueue_socket_success_hints_stderr_stdout_unchanged),
    ('enqueue_fallback_prior_hint_pending_cancel', scenario_enqueue_fallback_prior_hint_pending_cancel),
    ('enqueue_fallback_prior_hint_submitted_interrupt', scenario_enqueue_fallback_prior_hint_submitted_interrupt),
    ('enqueue_fallback_prior_hint_plain_inflight_omitted', scenario_enqueue_fallback_prior_hint_plain_inflight_omitted),
    ('enqueue_fallback_prior_hint_pane_mode_inflight_cancel', scenario_enqueue_fallback_prior_hint_pane_mode_inflight_cancel),
    ('enqueue_fallback_prior_hint_write_failure_suppresses_hint', scenario_enqueue_fallback_prior_hint_write_failure_suppresses_hint),
    ('enqueue_fallback_response_send_guard_no_hint', scenario_enqueue_fallback_response_send_guard_no_hint),
    ('enqueue_fallback_success_silent_with_raw_diagnostic', scenario_enqueue_fallback_success_silent_with_raw_diagnostic),
    ('enqueue_fallback_write_failure_preserves_stderr', scenario_enqueue_fallback_write_failure_preserves_stderr),
]
