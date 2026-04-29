from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from .harness import (
    LIBEXEC,
    _import_daemon_ctl,
    _queue_item,
    assert_true,
    make_daemon,
    read_events,
    test_message,
)

import bridge_attach  # noqa: E402
import bridge_daemon  # noqa: E402
import bridge_join  # noqa: E402
import bridge_leave  # noqa: E402

from bridge_util import (
    read_json,
    utc_now,
    write_json_atomic,
)  # noqa: E402


@contextmanager
def isolated_bridge_cli_env(tmpdir: Path):
    keys = [
        "AGENT_BRIDGE_STATE_DIR",
        "AGENT_BRIDGE_RUN_DIR",
        "AGENT_BRIDGE_LOG_DIR",
        "AGENT_BRIDGE_CONFIG_DIR",
        "AGENT_BRIDGE_RUNTIME_DIR",
        "AGENT_BRIDGE_RUNTIME_FILE",
    ]
    old = {key: os.environ.get(key) for key in keys}
    state_dir = tmpdir / "state"
    try:
        os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
        os.environ["AGENT_BRIDGE_RUN_DIR"] = str(tmpdir / "run")
        os.environ["AGENT_BRIDGE_LOG_DIR"] = str(tmpdir / "log")
        os.environ["AGENT_BRIDGE_CONFIG_DIR"] = str(tmpdir / "config")
        os.environ.pop("AGENT_BRIDGE_RUNTIME_DIR", None)
        os.environ.pop("AGENT_BRIDGE_RUNTIME_FILE", None)
        yield state_dir
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def active_cli_participant(alias: str, *, agent: str = "codex", pane: str = "%41", target: str = "test:1.0", session_id: str = "sess-active") -> dict:
    return {
        "alias": alias,
        "agent_type": agent,
        "pane": pane,
        "target": target,
        "hook_session_id": session_id,
        "cwd": "/active",
        "model": "model-active",
        "status": "active",
    }

def detached_cli_participant(alias: str, *, agent: str = "codex", pane: str = "%42", target: str = "test:2.0", session_id: str = "sess-detached") -> dict:
    return {
        "alias": alias,
        "agent_type": agent,
        "pane": pane,
        "target": target,
        "hook_session_id": session_id,
        "cwd": "/detached",
        "model": "model-detached",
        "status": "detached",
        "detached_at": "2026-04-28T00:00:00Z",
        "detach_reason": "test detach",
        "last_pane": pane,
        "endpoint_status": "endpoint_lost",
        "endpoint_lost_at": "2026-04-28T00:01:00Z",
        "endpoint_lost_reason": "test endpoint lost",
    }

def write_cli_session(state_dir: Path, participants: dict[str, dict], *, queue: list[dict] | None = None) -> Path:
    session_dir = state_dir / "test-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    queue_file = session_dir / "pending.json"
    write_json_atomic(queue_file, queue or [])
    state = {
        "session": "test-session",
        "state_dir": str(session_dir),
        "queue_file": str(queue_file),
        "bus_file": str(session_dir / "events.raw.jsonl"),
        "events_file": str(session_dir / "events.jsonl"),
        "participants": participants,
        "panes": {alias: record.get("pane") for alias, record in participants.items() if record.get("pane")},
        "targets": {alias: record.get("target") for alias, record in participants.items() if record.get("target")},
        "hook_session_ids": {
            alias: record.get("hook_session_id") for alias, record in participants.items() if record.get("hook_session_id")
        },
    }
    write_json_atomic(session_dir / "session.json", state)
    return session_dir / "session.json"

def run_bridge_join_cli(alias: str, pane: str, *, target: str = "test:9.0", session_id: str = "sess-new") -> tuple[int, str, str]:
    old_argv = sys.argv[:]
    old_send_prompt = bridge_join.send_prompt
    old_wait_for_probe = bridge_join.wait_for_probe
    old_backfill = bridge_join.backfill_session_process_identities
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        bridge_join.send_prompt = lambda *args, **kwargs: None  # type: ignore[assignment]
        bridge_join.wait_for_probe = lambda *args, **kwargs: {"session_id": session_id, "cwd": "/joined", "model": "model-new"}  # type: ignore[assignment]
        bridge_join.backfill_session_process_identities = lambda *args, **kwargs: {alias: {"status": "verified"}}  # type: ignore[assignment]
        sys.argv = [
            "bridge_join.py",
            "--session",
            "test-session",
            "--agent",
            "codex",
            "--alias",
            alias,
            "--pane",
            pane,
            "--pane-target",
            target,
            "--no-resolve-pane",
            "--no-notify",
        ]
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = bridge_join.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int):
                stderr.write(str(exc.code))
    finally:
        sys.argv = old_argv
        bridge_join.send_prompt = old_send_prompt  # type: ignore[assignment]
        bridge_join.wait_for_probe = old_wait_for_probe  # type: ignore[assignment]
        bridge_join.backfill_session_process_identities = old_backfill  # type: ignore[assignment]
    return int(code), stdout.getvalue(), stderr.getvalue()

def pane_locks(state_dir: Path) -> dict:
    return read_json(state_dir / "pane-locks.json", {"version": 1, "panes": {}}).get("panes") or {}

def assert_pane_lock(label: str, state_dir: Path, pane: str, *, alias: str, session_id: str) -> None:
    lock = pane_locks(state_dir).get(pane) or {}
    assert_true(lock.get("bridge_session") == "test-session", f"{label}: pane lock should point at test-session: {lock}")
    assert_true(lock.get("alias") == alias, f"{label}: pane lock should point at {alias}: {lock}")
    assert_true(lock.get("hook_session_id") == session_id, f"{label}: pane lock should point at {session_id}: {lock}")

def scenario_detached_leave_explicit_cleanup(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        participants = {
            "alice": active_cli_participant("alice", agent="claude", pane="%41", target="test:1.0", session_id="sess-alice"),
            "bob": detached_cli_participant("bob", pane="%42", target="test:2.0", session_id="sess-bob"),
        }
        queue = [
            {"id": "msg-to-bob", "from": "alice", "to": "bob"},
            {"id": "msg-from-bob", "from": "bob", "to": "alice"},
            {"id": "msg-kept", "from": "alice", "to": "carol"},
        ]
        session_path = write_cli_session(state_dir, participants, queue=queue)

        notices: list[tuple[str, str]] = []
        tmux_calls: list[tuple[str, str]] = []
        old_argv = sys.argv[:]
        old_resolve = bridge_leave.resolve_participant_endpoint_detail
        old_tmux_send = bridge_leave.tmux_send_literal
        old_enqueue = bridge_leave.enqueue_membership_notice
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            bridge_leave.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
            bridge_leave.tmux_send_literal = lambda pane, text, *args, **kwargs: tmux_calls.append((pane, text))  # type: ignore[assignment]
            bridge_leave.enqueue_membership_notice = lambda session, body: notices.append((session, body))  # type: ignore[assignment]
            sys.argv = ["bridge_leave.py", "--session", "test-session", "--alias", "bob", "--json"]
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = bridge_leave.main()
        finally:
            sys.argv = old_argv
            bridge_leave.resolve_participant_endpoint_detail = old_resolve  # type: ignore[assignment]
            bridge_leave.tmux_send_literal = old_tmux_send  # type: ignore[assignment]
            bridge_leave.enqueue_membership_notice = old_enqueue  # type: ignore[assignment]

        result = json.loads(stdout.getvalue())
        state = read_json(session_path, {})
        queue_after = read_json(Path(state.get("queue_file") or ""), [])
        assert_true(code == 0, f"{label}: bridge_leave.main should return 0")
        assert_true(result.get("left") == "bob" and result.get("left_notice_sent") == 0, f"{label}: unexpected leave result: {result}")
        assert_true(result.get("left_notice_error") == "process_mismatch", f"{label}: stale endpoint reason should be reported: {result}")
        assert_true(tmux_calls == [], f"{label}: stale detached endpoint must not receive direct tmux notice")
        assert_true("bob" not in (state.get("participants") or {}), f"{label}: detached alias should be removed from session")
        assert_true("bob" not in (state.get("panes") or {}), f"{label}: legacy pane map should be removed")
        assert_true("bob" not in (state.get("targets") or {}), f"{label}: legacy target map should be removed")
        assert_true("bob" not in (state.get("hook_session_ids") or {}), f"{label}: legacy hook id map should be removed")
        assert_true([item.get("id") for item in queue_after] == ["msg-kept"], f"{label}: pending messages involving alias should be removed: {queue_after}")
        assert_true(
            len(notices) == 1 and notices[0][1].startswith("Bridge membership: bob left."),
            f"{label}: active remaining participant should receive membership notice: {notices}",
        )
        assert_true(result.get("removed_pending_messages") == 2, f"{label}: removed pending count should be 2: {result}")

    print(f"  PASS  {label}")

def scenario_detached_leave_no_notice_when_only_inactive_remain(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        write_cli_session(
            state_dir,
            {
                "bob": detached_cli_participant("bob", pane="%42", session_id="sess-bob"),
                "carol": detached_cli_participant("carol", pane="%43", session_id="sess-carol"),
            },
        )

        notices: list[tuple[str, str]] = []
        old_argv = sys.argv[:]
        old_resolve = bridge_leave.resolve_participant_endpoint_detail
        old_enqueue = bridge_leave.enqueue_membership_notice
        stdout = io.StringIO()
        try:
            bridge_leave.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
            bridge_leave.enqueue_membership_notice = lambda session, body: notices.append((session, body))  # type: ignore[assignment]
            sys.argv = ["bridge_leave.py", "--session", "test-session", "--alias", "bob", "--json"]
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = bridge_leave.main()
        finally:
            sys.argv = old_argv
            bridge_leave.resolve_participant_endpoint_detail = old_resolve  # type: ignore[assignment]
            bridge_leave.enqueue_membership_notice = old_enqueue  # type: ignore[assignment]

        assert_true(code == 0, f"{label}: bridge_leave.main should return 0")
        assert_true(notices == [], f"{label}: no active remaining participants should suppress membership notice: {notices}")

    print(f"  PASS  {label}")

def scenario_detached_join_same_alias_same_pane(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        session_path = write_cli_session(
            state_dir,
            {"bob": detached_cli_participant("bob", pane="%42", target="old:2.0", session_id="sess-old")},
        )

        code, _stdout, stderr = run_bridge_join_cli("bob", "%42", target="new:2.0", session_id="sess-new")

        state = read_json(session_path, {})
        record = (state.get("participants") or {}).get("bob") or {}
        stale_keys = {"detached_at", "detach_reason", "last_pane", "endpoint_status", "endpoint_lost_at", "endpoint_lost_reason"}
        assert_true(code == 0, f"{label}: detached same-pane rejoin should succeed: {stderr}")
        assert_true(record.get("status") == "active", f"{label}: participant should become active: {record}")
        assert_true(record.get("pane") == "%42" and record.get("target") == "new:2.0", f"{label}: endpoint should be refreshed: {record}")
        assert_true(record.get("hook_session_id") == "sess-new", f"{label}: hook_session_id should be refreshed: {record}")
        assert_true(record.get("cwd") == "/joined" and record.get("model") == "model-new", f"{label}: probe metadata should be refreshed: {record}")
        assert_true(stale_keys.isdisjoint(record), f"{label}: stale detached/endpoint metadata should be cleared: {record}")
        assert_true((state.get("panes") or {}).get("bob") == "%42", f"{label}: legacy pane map should be refreshed: {state}")
        assert_true((state.get("targets") or {}).get("bob") == "new:2.0", f"{label}: legacy target map should be refreshed: {state}")
        assert_true((state.get("hook_session_ids") or {}).get("bob") == "sess-new", f"{label}: legacy hook id map should be refreshed: {state}")
        assert_pane_lock(label, state_dir, "%42", alias="bob", session_id="sess-new")

    print(f"  PASS  {label}")

def scenario_detached_join_same_alias_different_pane(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        session_path = write_cli_session(
            state_dir,
            {"bob": detached_cli_participant("bob", pane="%42", target="old:2.0", session_id="sess-old")},
        )

        code, _stdout, stderr = run_bridge_join_cli("bob", "%44", target="new:4.0", session_id="sess-new-pane")

        state = read_json(session_path, {})
        record = (state.get("participants") or {}).get("bob") or {}
        assert_true(code == 0, f"{label}: detached different-pane rejoin should succeed: {stderr}")
        assert_true(record.get("status") == "active", f"{label}: participant should become active: {record}")
        assert_true(record.get("pane") == "%44" and record.get("target") == "new:4.0", f"{label}: endpoint should switch panes: {record}")
        assert_true(record.get("hook_session_id") == "sess-new-pane", f"{label}: hook_session_id should switch: {record}")
        assert_true((state.get("panes") or {}).get("bob") == "%44", f"{label}: legacy pane map should switch: {state}")
        assert_true((state.get("targets") or {}).get("bob") == "new:4.0", f"{label}: legacy target map should switch: {state}")
        assert_true((state.get("hook_session_ids") or {}).get("bob") == "sess-new-pane", f"{label}: legacy hook id map should switch: {state}")
        locks = pane_locks(state_dir)
        assert_true("%42" not in locks, f"{label}: detached old pane should not keep a stale lock: {locks}")
        assert_pane_lock(label, state_dir, "%44", alias="bob", session_id="sess-new-pane")

    print(f"  PASS  {label}")

def scenario_detached_join_different_alias_same_pane_allowed(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        session_path = write_cli_session(
            state_dir,
            {"bob": detached_cli_participant("bob", pane="%42", target="old:2.0", session_id="sess-old")},
        )

        code, _stdout, stderr = run_bridge_join_cli("alice", "%42", target="new:2.0", session_id="sess-alice")

        state = read_json(session_path, {})
        participants = state.get("participants") or {}
        bob = participants.get("bob") or {}
        alice = participants.get("alice") or {}
        assert_true(code == 0, f"{label}: explicit different-alias join should be able to reuse a detached pane: {stderr}")
        assert_true(bob.get("status") == "detached", f"{label}: original detached record should remain non-routable: {bob}")
        assert_true(alice.get("status") == "active" and alice.get("pane") == "%42", f"{label}: new alias should own active pane: {alice}")
        assert_pane_lock(label, state_dir, "%42", alias="alice", session_id="sess-alice")

    print(f"  PASS  {label}")

def scenario_active_join_same_alias_still_rejected(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        write_cli_session(state_dir, {"bob": active_cli_participant("bob", pane="%42", session_id="sess-active")})

        code, _stdout, stderr = run_bridge_join_cli("bob", "%44", target="new:4.0", session_id="sess-new")

        assert_true(code != 0 and "alias already exists in test-session: bob" in stderr, f"{label}: active alias should still be rejected: code={code} stderr={stderr!r}")

    print(f"  PASS  {label}")

def scenario_join_blank_status_same_pane_still_rejected(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        bob = active_cli_participant("bob", pane="%42", session_id="sess-blank")
        bob["status"] = ""
        write_cli_session(state_dir, {"bob": bob})

        code, _stdout, stderr = run_bridge_join_cli("alice", "%42", target="new:4.0", session_id="sess-new")

        assert_true(
            code != 0 and "pane is already registered: %42" in stderr,
            f"{label}: blank status should normalize to active and keep pane reserved: code={code} stderr={stderr!r}",
        )

    print(f"  PASS  {label}")

def scenario_join_other_session_pane_lock_still_rejected(label: str, tmpdir: Path) -> None:
    with isolated_bridge_cli_env(tmpdir) as state_dir:
        write_cli_session(state_dir, {})
        write_json_atomic(
            state_dir / "pane-locks.json",
            {
                "version": 1,
                "panes": {
                    "%45": {
                        "bridge_session": "other-session",
                        "agent": "codex",
                        "alias": "other",
                        "target": "other:5.0",
                        "hook_session_id": "sess-other",
                    }
                },
            },
        )

        code, _stdout, stderr = run_bridge_join_cli("bob", "%45", target="new:5.0", session_id="sess-new")

        assert_true(code != 0 and "pane is already registered: %45" in stderr, f"{label}: other-session pane lock should still reject join: code={code} stderr={stderr!r}")

    print(f"  PASS  {label}")

def scenario_join_probe_passes_pane_id_to_wait(label: str, tmpdir: Path) -> None:
    state_dir = tmpdir / "state"
    session_dir = state_dir / "test-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(session_dir / "session.json", {"session": "test-session", "participants": {}})

    captured: dict[str, object] = {}
    old_env = {key: os.environ.get(key) for key in ("AGENT_BRIDGE_STATE_DIR", "AGENT_BRIDGE_CONFIG_DIR")}
    old_argv = sys.argv[:]
    old_send_prompt = bridge_join.send_prompt
    old_wait_for_probe = bridge_join.wait_for_probe
    old_update_registry = bridge_join.update_registry
    old_update_pane_lock = bridge_join.update_pane_lock
    old_backfill = bridge_join.backfill_session_process_identities
    try:
        os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
        os.environ["AGENT_BRIDGE_CONFIG_DIR"] = str(tmpdir / "config")
        bridge_join.send_prompt = lambda pane, prompt, delay: captured.update({"sent_pane": pane, "prompt": prompt, "delay": delay})  # type: ignore[assignment]

        def fake_wait_for_probe(probe_id: str, agent: str, timeout: float, **kwargs) -> dict:
            captured["probe_id"] = probe_id
            captured["agent"] = agent
            captured["timeout"] = timeout
            captured["wait_kwargs"] = dict(kwargs)
            return {"session_id": "sess-join", "cwd": "/work", "model": "model-x"}

        bridge_join.wait_for_probe = fake_wait_for_probe  # type: ignore[assignment]
        bridge_join.update_registry = lambda mapping: captured.update({"registry": mapping})  # type: ignore[assignment]
        bridge_join.update_pane_lock = lambda mapping: captured.update({"pane_lock": mapping})  # type: ignore[assignment]
        bridge_join.backfill_session_process_identities = lambda *args, **kwargs: {"codex-reviewer": {"status": "verified"}}  # type: ignore[assignment]
        sys.argv = [
            "bridge_join.py",
            "--session",
            "test-session",
            "--agent",
            "codex",
            "--alias",
            "codex-reviewer",
            "--pane",
            "%42",
            "--pane-target",
            "test:1.0",
            "--no-resolve-pane",
            "--no-notify",
        ]
        code = bridge_join.main()
    finally:
        sys.argv = old_argv
        bridge_join.send_prompt = old_send_prompt  # type: ignore[assignment]
        bridge_join.wait_for_probe = old_wait_for_probe  # type: ignore[assignment]
        bridge_join.update_registry = old_update_registry  # type: ignore[assignment]
        bridge_join.update_pane_lock = old_update_pane_lock  # type: ignore[assignment]
        bridge_join.backfill_session_process_identities = old_backfill  # type: ignore[assignment]
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    wait_kwargs = captured.get("wait_kwargs") or {}
    assert_true(code == 0, f"{label}: bridge_join.main should return 0, got {code}")
    assert_true(captured.get("sent_pane") == "%42", f"{label}: probe should be sent to selected pane")
    assert_true(isinstance(wait_kwargs, dict) and wait_kwargs.get("pane_id") == "%42", f"{label}: join must pass pane_id to wait_for_probe, got {wait_kwargs}")
    print(f"  PASS  {label}")

def scenario_bridge_attach_start_daemon_argv_includes_from_start(label: str, tmpdir: Path) -> None:
    captured: dict[str, list[str]] = {}
    old_run = bridge_attach.run

    def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        captured["cmd"] = list(cmd)
        payload = {"pid": 12345, "pid_file": "/tmp/pid", "log_file": "/tmp/log", "command_socket": "/tmp/sock"}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    state = {
        "bus_file": str(tmpdir / "events.raw.jsonl"),
        "events_file": str(tmpdir / "events.jsonl"),
        "queue_file": str(tmpdir / "pending.json"),
        "participants": {
            "claude": {"agent_type": "claude", "pane": "%1"},
            "codex": {"agent_type": "codex", "pane": "%2"},
        },
    }
    try:
        bridge_attach.run = fake_run  # type: ignore[assignment]
        result = bridge_attach.start_daemon(argparse.Namespace(session="test-session"), state)
    finally:
        bridge_attach.run = old_run  # type: ignore[assignment]

    cmd = captured.get("cmd") or []
    assert_true(result.get("pid") == 12345, f"{label}: fake daemon result should parse")
    assert_true("--from-start" in cmd, f"{label}: attach-started daemon ctl command must include --from-start: {cmd}")
    assert_true(cmd[0].endswith("bridge_daemon_ctl.py") and "start" in cmd, f"{label}: expected daemon_ctl start command: {cmd}")
    for flag, value in (
        ("--state-file", state["bus_file"]),
        ("--public-state-file", state["events_file"]),
        ("--queue-file", state["queue_file"]),
        ("--session", "test-session"),
    ):
        assert_true(flag in cmd and cmd[cmd.index(flag) + 1] == value, f"{label}: {flag} not preserved in command: {cmd}")
    print(f"  PASS  {label}")

def scenario_bridge_daemon_ctl_start_subparser_accepts_from_start(label: str, tmpdir: Path) -> None:
    import contextlib
    import importlib
    import io

    ctl = _import_daemon_ctl()
    old_argv = sys.argv[:]
    old_env = {key: os.environ.get(key) for key in ("AGENT_BRIDGE_STATE_DIR", "AGENT_BRIDGE_RUN_DIR", "AGENT_BRIDGE_LOG_DIR")}
    out = io.StringIO()
    try:
        os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
        os.environ["AGENT_BRIDGE_RUN_DIR"] = str(tmpdir / "run")
        os.environ["AGENT_BRIDGE_LOG_DIR"] = str(tmpdir / "log")
        importlib.reload(ctl)
        sys.argv = [
            "bridge_daemon_ctl.py",
            "start",
            "-s",
            "test-session",
            "--state-file",
            str(tmpdir / "events.raw.jsonl"),
            "--public-state-file",
            str(tmpdir / "events.jsonl"),
            "--queue-file",
            str(tmpdir / "pending.json"),
            "--from-start",
            "--dry-run",
            "--json",
        ]
        with contextlib.redirect_stdout(out):
            code = ctl.main()
    finally:
        sys.argv = old_argv
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(ctl)

    result = json.loads(out.getvalue())
    command = result.get("command") or []
    assert_true(code == 0, f"{label}: ctl main should accept --from-start, got {code}")
    assert_true(result.get("dry_run") is True, f"{label}: start dry-run result expected: {result}")
    assert_true("--from-start" in command, f"{label}: parsed --from-start should reach dry-run daemon argv: {command}")
    print(f"  PASS  {label}")

def scenario_daemon_command_forwards_from_start(label: str, tmpdir: Path) -> None:
    ctl = _import_daemon_ctl()
    args = argparse.Namespace(
        session="test-session",
        state_file=str(tmpdir / "events.raw.jsonl"),
        public_state_file=str(tmpdir / "events.jsonl"),
        queue_file=str(tmpdir / "pending.json"),
        claude_pane="",
        codex_pane="",
        submit_delay=None,
        submit_timeout=None,
        from_start=True,
    )
    cmd = ctl.daemon_command(args)
    assert_true("--from-start" in cmd, f"{label}: daemon_command must forward from_start=True: {cmd}")
    args.from_start = False
    cmd_without = ctl.daemon_command(args)
    assert_true("--from-start" not in cmd_without, f"{label}: daemon_command must omit from_start=False: {cmd_without}")
    print(f"  PASS  {label}")

def scenario_daemon_command_ignores_legacy_max_hops(label: str, tmpdir: Path) -> None:
    ctl = _import_daemon_ctl()
    args = argparse.Namespace(
        session="test-session",
        state_file=str(tmpdir / "events.raw.jsonl"),
        public_state_file=str(tmpdir / "events.jsonl"),
        queue_file=str(tmpdir / "pending.json"),
        claude_pane="",
        codex_pane="",
        submit_delay=None,
        submit_timeout=None,
        from_start=False,
        max_hops=9,
    )
    cmd = ctl.daemon_command(args)
    assert_true("--max-hops" not in cmd, f"{label}: daemon_command must ignore stale max_hops attr: {cmd}")
    print(f"  PASS  {label}")

def _run_daemon_ctl_main_isolated(tmpdir: Path, argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import importlib
    import io

    ctl = _import_daemon_ctl()
    old_argv = sys.argv[:]
    old_env = {key: os.environ.get(key) for key in ("AGENT_BRIDGE_STATE_DIR", "AGENT_BRIDGE_RUN_DIR", "AGENT_BRIDGE_LOG_DIR")}
    out = io.StringIO()
    err = io.StringIO()
    try:
        os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
        os.environ["AGENT_BRIDGE_RUN_DIR"] = str(tmpdir / "run")
        os.environ["AGENT_BRIDGE_LOG_DIR"] = str(tmpdir / "log")
        importlib.reload(ctl)
        sys.argv = ["bridge_daemon_ctl.py", *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ctl.main()
            except SystemExit as exc:
                code = int(exc.code or 0) if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(ctl)
    return code, out.getvalue(), err.getvalue()

def scenario_bridge_daemon_ctl_subcommands_reject_max_hops(label: str, tmpdir: Path) -> None:
    base_start = [
        "start",
        "-s", "test-session",
        "--state-file", str(tmpdir / "events.raw.jsonl"),
        "--public-state-file", str(tmpdir / "events.jsonl"),
        "--queue-file", str(tmpdir / "pending.json"),
        "--dry-run",
        "--json",
    ]
    cases = [
        ("start", [*base_start, "--max-hops", "9"]),
        ("ensure", ["ensure", "-s", "test-session", "--dry-run", "--json", "--max-hops", "9"]),
        ("restart", ["restart", "-s", "test-session", "--dry-run", "--json", "--max-hops", "9"]),
    ]
    for subcommand, argv in cases:
        code, out, err = _run_daemon_ctl_main_isolated(tmpdir / subcommand, argv)
        assert_true(code == 2, f"{label}: {subcommand} should reject --max-hops with exit 2, got {code}, out={out!r}, err={err!r}")
        assert_true("--max-hops" in err, f"{label}: {subcommand} stderr should mention --max-hops: {err!r}")
        assert_true("unrecognized arguments" in err, f"{label}: {subcommand} stderr should be argparse-owned: {err!r}")
    print(f"  PASS  {label}")

def scenario_bridge_daemon_rejects_max_hops_cli(label: str, tmpdir: Path) -> None:
    state_file = tmpdir / "events.raw.jsonl"
    public_state_file = tmpdir / "events.jsonl"
    queue_file = tmpdir / "pending.json"
    session_file = tmpdir / "session.json"
    state_file.touch()
    public_state_file.touch()
    queue_file.write_text("[]", encoding="utf-8")
    session_file.write_text(json.dumps({"session": "test-session", "participants": {}}, ensure_ascii=True), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(LIBEXEC / "bridge_daemon.py"),
            "--state-file", str(state_file),
            "--public-state-file", str(public_state_file),
            "--queue-file", str(queue_file),
            "--session-file", str(session_file),
            "--bridge-session", "test-session",
            "--once",
            "--dry-run",
            "--max-hops", "9",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert_true(proc.returncode == 2, f"{label}: daemon CLI should reject --max-hops with exit 2, got {proc.returncode}")
    assert_true("--max-hops" in proc.stderr and "unrecognized arguments" in proc.stderr, f"{label}: stderr should be argparse-owned: {proc.stderr!r}")
    print(f"  PASS  {label}")

def scenario_bridge_daemon_ctl_start_argv_includes_from_start_end_to_end(label: str, tmpdir: Path) -> None:
    import importlib

    ctl = _import_daemon_ctl()
    old_env = {key: os.environ.get(key) for key in ("AGENT_BRIDGE_STATE_DIR", "AGENT_BRIDGE_RUN_DIR", "AGENT_BRIDGE_LOG_DIR")}
    try:
        os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
        os.environ["AGENT_BRIDGE_RUN_DIR"] = str(tmpdir / "run")
        os.environ["AGENT_BRIDGE_LOG_DIR"] = str(tmpdir / "log")
        importlib.reload(ctl)
        paths = ctl.session_paths("test-session")
        for path in (paths["pid"], paths["meta"], paths["lock"], paths["stop"], paths["socket"], paths["log"]):
            path.parent.mkdir(parents=True, exist_ok=True)
        args = argparse.Namespace(
            session="test-session",
            state_file=str(tmpdir / "events.raw.jsonl"),
            public_state_file=str(tmpdir / "events.jsonl"),
            queue_file=str(tmpdir / "pending.json"),
            claude_pane="",
            codex_pane="",
            replace=True,
            dry_run=True,
            stop_timeout=1.0,
            submit_delay=None,
            submit_timeout=None,
            from_start=True,
        )
        result = ctl.start_under_lock(args, paths)
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(ctl)

    command = result.get("command") or []
    assert_true(result.get("dry_run") is True, f"{label}: start_under_lock dry-run expected: {result}")
    assert_true("--from-start" in command, f"{label}: start_under_lock dry-run argv must include --from-start: {command}")
    print(f"  PASS  {label}")

def _plant_attach_window_prompt_submit(d: bridge_daemon.BridgeDaemon, *, record_file: bool = True) -> dict:
    message = test_message("msg-attach-window", frm="alice", to="bob", status="inflight")
    message["nonce"] = "nonce-attach-window"
    message["delivery_attempts"] = 1
    d.queue.update(lambda queue: queue.append(message))
    record = {
        "ts": utc_now(),
        "agent": "codex",
        "bridge_agent": "bob",
        "event": "prompt_submitted",
        "bridge_session": "test-session",
        "nonce": "nonce-attach-window",
        "turn_id": "turn-attach-window",
    }
    if record_file:
        d.state_file.write_text(json.dumps(record, ensure_ascii=True) + "\n", encoding="utf-8")
    return record

def scenario_daemon_follow_from_start_replays_prompt_submitted(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.from_start = True
    _plant_attach_window_prompt_submit(d)
    d.follow()
    ctx = d.current_prompt_by_agent.get("bob") or {}
    item = _queue_item(d, "msg-attach-window") or {}
    assert_true(ctx.get("id") == "msg-attach-window", f"{label}: from_start must replay prompt_submitted and bind ctx: {ctx}")
    assert_true(ctx.get("turn_id") == "turn-attach-window", f"{label}: turn id must be preserved: {ctx}")
    assert_true(d.busy.get("bob") is True, f"{label}: replayed prompt_submitted should mark bob busy")
    assert_true(item.get("status") == "delivered", f"{label}: replayed prompt_submitted should mark queue delivered: {item}")
    print(f"  PASS  {label}")

def scenario_daemon_follow_from_start_false_skips_pre_existing_record(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.from_start = False
    _plant_attach_window_prompt_submit(d)
    d.follow()
    ctx = d.current_prompt_by_agent.get("bob") or {}
    item = _queue_item(d, "msg-attach-window") or {}
    assert_true(not ctx.get("id"), f"{label}: default seek-EOF path must skip pre-existing prompt_submitted: {ctx}")
    assert_true(d.busy.get("bob") is not True, f"{label}: bob should not become busy from skipped record")
    assert_true(item.get("status") == "inflight", f"{label}: skipped prompt_submitted should leave queue inflight: {item}")
    print(f"  PASS  {label}")

def scenario_daemon_follow_from_start_handles_self_daemon_started_safely(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.from_start = True
    before_participants = dict(d.participants)
    before_queue = d.queue.read()
    d.follow()
    after_queue = d.queue.read()
    events = read_events(d.state_file)
    daemon_started = next((e for e in events if e.get("event") == "daemon_started"), None)
    assert_true(d.participants == before_participants, f"{label}: daemon_started self-record must not mutate participants")
    assert_true(after_queue == before_queue, f"{label}: daemon_started self-record must not mutate queue: {after_queue}")
    assert_true(daemon_started is not None, f"{label}: daemon_started should be logged")
    assert_true("max_hops" not in daemon_started, f"{label}: daemon_started must not report removed max_hops option: {daemon_started}")
    assert_true(not any(e.get("event") == "record_handler_failed" for e in events), f"{label}: daemon_started replay must not fail handler: {events}")
    print(f"  PASS  {label}")


SCENARIOS = [
    ('detached_leave_explicit_cleanup', scenario_detached_leave_explicit_cleanup),
    ('detached_leave_no_notice_when_only_inactive_remain', scenario_detached_leave_no_notice_when_only_inactive_remain),
    ('detached_join_same_alias_same_pane', scenario_detached_join_same_alias_same_pane),
    ('detached_join_same_alias_different_pane', scenario_detached_join_same_alias_different_pane),
    ('detached_join_different_alias_same_pane_allowed', scenario_detached_join_different_alias_same_pane_allowed),
    ('active_join_same_alias_still_rejected', scenario_active_join_same_alias_still_rejected),
    ('join_blank_status_same_pane_still_rejected', scenario_join_blank_status_same_pane_still_rejected),
    ('join_other_session_pane_lock_still_rejected', scenario_join_other_session_pane_lock_still_rejected),
    ('join_probe_passes_pane_id_to_wait', scenario_join_probe_passes_pane_id_to_wait),
    ('bridge_attach_start_daemon_argv_includes_from_start', scenario_bridge_attach_start_daemon_argv_includes_from_start),
    ('bridge_daemon_ctl_start_subparser_accepts_from_start', scenario_bridge_daemon_ctl_start_subparser_accepts_from_start),
    ('daemon_command_forwards_from_start', scenario_daemon_command_forwards_from_start),
    ('daemon_command_ignores_legacy_max_hops', scenario_daemon_command_ignores_legacy_max_hops),
    ('bridge_daemon_ctl_subcommands_reject_max_hops', scenario_bridge_daemon_ctl_subcommands_reject_max_hops),
    ('bridge_daemon_rejects_max_hops_cli', scenario_bridge_daemon_rejects_max_hops_cli),
    ('bridge_daemon_ctl_start_argv_includes_from_start_end_to_end', scenario_bridge_daemon_ctl_start_argv_includes_from_start_end_to_end),
    ('daemon_follow_from_start_replays_prompt_submitted', scenario_daemon_follow_from_start_replays_prompt_submitted),
    ('daemon_follow_from_start_false_skips_pre_existing_record', scenario_daemon_follow_from_start_false_skips_pre_existing_record),
    ('daemon_follow_from_start_handles_self_daemon_started_safely', scenario_daemon_follow_from_start_handles_self_daemon_started_safely),
]
