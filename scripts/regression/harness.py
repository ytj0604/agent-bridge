from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBEXEC = ROOT / "libexec" / "agent-bridge"
DIRECT_EXECUTABLE_TARGETS = (
    ("bridge_run_target", Path("bin/bridge_run.sh")),
    ("bridge_manage_target", Path("bin/bridge_manage.sh")),
    ("bridge_healthcheck_target", Path("bin/bridge_healthcheck.sh")),
    ("agent_send_peer_model_tool", Path("model-bin/agent_send_peer")),
    ("agent_list_peers_model_tool", Path("model-bin/agent_list_peers")),
    ("agent_view_peer_model_tool", Path("model-bin/agent_view_peer")),
    ("agent_alarm_model_tool", Path("model-bin/agent_alarm")),
    ("agent_interrupt_peer_model_tool", Path("model-bin/agent_interrupt_peer")),
    ("agent_clear_peer_model_tool", Path("model-bin/agent_clear_peer")),
    ("agent_extend_wait_model_tool", Path("model-bin/agent_extend_wait")),
    ("agent_cancel_message_model_tool", Path("model-bin/agent_cancel_message")),
    ("agent_wait_status_model_tool", Path("model-bin/agent_wait_status")),
    ("agent_aggregate_status_model_tool", Path("model-bin/agent_aggregate_status")),
    ("bridge_hook_entrypoint", Path("hooks/bridge-hook")),
)
INSTALL_SHIM_TARGETS = DIRECT_EXECUTABLE_TARGETS[:-1]
sys.path.insert(0, str(LIBEXEC))

import bridge_daemon  # noqa: E402
from bridge_util import read_json, utc_now, write_json_atomic  # noqa: E402


def make_daemon(tmpdir: Path, participants: dict[str, dict], queue_items: list[dict] | None = None) -> bridge_daemon.BridgeDaemon:
    state_file = tmpdir / "events.raw.jsonl"
    public_state_file = tmpdir / "events.jsonl"
    queue_file = tmpdir / "pending.json"
    session_file = tmpdir / "session.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.touch()
    public_state_file.touch()
    queue_file.write_text(json.dumps(queue_items or [], ensure_ascii=True), encoding="utf-8")
    session_file.write_text(json.dumps({
        "session": "test-session",
        "participants": participants,
    }, ensure_ascii=True), encoding="utf-8")

    args = argparse.Namespace(
        claude_pane=None,
        codex_pane=None,
        state_file=str(state_file),
        public_state_file=str(public_state_file),
        queue_file=str(queue_file),
        submit_delay=0.0,
        submit_timeout=5.0,
        from_start=False,
        dry_run=True,
        bridge_session="test-session",
        session_file=str(session_file),
        stop_file=None,
        command_socket=None,
        once=True,
        stdout_events=False,
    )
    return bridge_daemon.BridgeDaemon(args)


def read_events(state_file: Path) -> list[dict]:
    if not state_file.exists():
        return []
    out = []
    for line in state_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def assert_true(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class FakeCommandConn:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.used = False

    def recv(self, _size: int) -> bytes:
        if self.used:
            return b""
        self.used = True
        return self.payload

    def getsockopt(self, *_args) -> bytes:
        raise OSError("no peer credentials in test")


@contextmanager
def patched_environ(**updates: str | None):
    old = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def patched_redirect_root(path: Path):
    old = bridge_daemon.SHARED_PAYLOAD_ROOT
    bridge_daemon.SHARED_PAYLOAD_ROOT = str(path)
    try:
        yield path
    finally:
        bridge_daemon.SHARED_PAYLOAD_ROOT = old


def test_message(message_id: str, frm: str = "claude", to: str = "codex", status: str = "pending") -> dict:
    return {
        "id": message_id,
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": frm,
        "to": to,
        "kind": "request",
        "intent": "test",
        "body": "hello",
        "causal_id": f"causal-{uuid.uuid4().hex[:12]}",
        "hop_count": 1,
        "auto_return": True,
        "reply_to": None,
        "source": "test",
        "bridge_session": "test-session",
        "status": status,
        "nonce": None,
        "delivery_attempts": 0,
    }


@contextmanager
def isolated_identity_env(tmpdir: Path):
    keys = [
        "AGENT_BRIDGE_RUNTIME_DIR",
        "AGENT_BRIDGE_ATTACH_REGISTRY",
        "AGENT_BRIDGE_PANE_LOCKS",
        "AGENT_BRIDGE_LIVE_SESSIONS",
        "AGENT_BRIDGE_NO_RESUME_FROM_UNKNOWN",
        "AGENT_BRIDGE_NO_TARGET_RECOVERY",
    ]
    old = {key: os.environ.get(key) for key in keys}
    runtime = tmpdir / "identity-runtime"
    os.environ["AGENT_BRIDGE_RUNTIME_DIR"] = str(runtime)
    os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"] = str(tmpdir / "attached-sessions.json")
    os.environ["AGENT_BRIDGE_PANE_LOCKS"] = str(tmpdir / "pane-locks.json")
    os.environ["AGENT_BRIDGE_LIVE_SESSIONS"] = str(tmpdir / "live-sessions.json")
    try:
        yield runtime / "state"
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def write_identity_fixture(state_root_path: Path, *, alias: str = "codex", agent: str = "codex", session_id: str = "sess-a", pane: str = "%20") -> dict:
    state_dir = state_root_path / "test-session"
    participant = {
        "alias": alias,
        "agent_type": agent,
        "pane": pane,
        "target": "tmux:1.0",
        "hook_session_id": session_id,
        "status": "active",
    }
    state = {"session": "test-session", "participants": {alias: participant}, "panes": {alias: pane}, "targets": {alias: "tmux:1.0"}}
    write_json_atomic(state_dir / "session.json", state)
    write_json_atomic(
        Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]),
        {
            "version": 1,
            "sessions": {
                f"{agent}:{session_id}": {
                    "agent": agent,
                    "alias": alias,
                    "session_id": session_id,
                    "bridge_session": "test-session",
                    "pane": pane,
                    "target": "tmux:1.0",
                }
            },
        },
    )
    write_json_atomic(
        Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]),
        {
            "version": 1,
            "panes": {
                pane: {
                    "bridge_session": "test-session",
                    "agent": agent,
                    "alias": alias,
                    "target": "tmux:1.0",
                    "hook_session_id": session_id,
                }
            },
        },
    )
    write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {}, "sessions": {}})
    return participant


def verified_identity(agent: str = "codex", pane: str = "%20", pid: int = 1234, start_time: str = "55") -> dict:
    return {
        "status": "verified",
        "reason": "ok",
        "pane": pane,
        "target": "tmux:1.0",
        "agent": agent,
        "pane_pid": "100",
        "boot_id": "boot-a",
        "processes": [{"pid": pid, "start_time": start_time, "score": 99, "args_hint": agent}],
    }


def identity_live_record(
    *,
    agent: str = "codex",
    session_id: str = "sess-a",
    pane: str = "%20",
    alias: str = "codex",
    pid: int = 1234,
    start_time: str = "55",
    last_seen_at: str | None = None,
    process_identity: dict | None = None,
) -> dict:
    return {
        "agent": agent,
        "session_id": session_id,
        "pane": pane,
        "target": "tmux:1.0",
        "bridge_session": "test-session",
        "alias": alias,
        "last_seen_at": last_seen_at or utc_now(),
        "process_identity": process_identity or verified_identity(agent, pane, pid=pid, start_time=start_time),
    }


def read_raw_events(state_root_path: Path) -> list[dict]:
    path = state_root_path / "test-session" / "events.raw.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_live_identity_records(*records: dict, index_record: dict | None = None) -> None:
    panes = {str(record.get("pane") or ""): record for record in records if record.get("pane")}
    sessions: dict[str, dict] = {}
    if index_record:
        sessions[f"{index_record.get('agent')}:{index_record.get('session_id')}"] = index_record
    write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": panes, "sessions": sessions})


def set_identity_target(state_root_path: Path, *, alias: str = "codex", session_id: str = "sess-a", pane: str = "%20", target: str = "0:1.2") -> None:
    state_path = state_root_path / "test-session" / "session.json"
    state = read_json(state_path, {})
    participant = (state.get("participants") or {}).get(alias) or {}
    participant["target"] = target
    state.setdefault("targets", {})[alias] = target
    write_json_atomic(state_path, state)
    registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"version": 1, "sessions": {}})
    mapping = (registry.get("sessions") or {}).get(f"codex:{session_id}") or {}
    mapping["target"] = target
    registry.setdefault("sessions", {})[f"codex:{session_id}"] = mapping
    write_json_atomic(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), registry)
    locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"version": 1, "panes": {}})
    lock = (locks.get("panes") or {}).get(pane) or {}
    lock["target"] = target
    locks.setdefault("panes", {})[pane] = lock
    write_json_atomic(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), locks)


def _qualifying_message(sender: str, target: str, kind: str = "notice", body: str = "hi") -> dict:
    # Mirrors what bridge_enqueue.py emits: a fresh message starts in
    # transient "ingressing" state and gets finalized to "pending" by
    # the daemon's _apply_alarm_cancel_to_queued_message helper.
    return {
        "id": f"msg-{uuid.uuid4().hex}", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": sender, "to": target, "kind": kind, "intent": "test", "body": body,
        "causal_id": "c", "hop_count": 1, "auto_return": (kind == "request"),
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "ingressing", "nonce": None, "delivery_attempts": 0,
    }


def _delivered_request(message_id: str, requester: str, responder: str, *, auto_return: bool = True) -> dict:
    msg = _qualifying_message(requester, responder, kind="request", body="delivered request")
    msg.update({
        "id": message_id,
        "status": "delivered",
        "auto_return": auto_return,
        "nonce": f"n-{message_id}",
        "delivered_ts": utc_now(),
    })
    return msg


def _participants_state(aliases: list[str]) -> dict:
    return {
        "session": "test-session",
        "participants": {a: {"alias": a, "agent_type": "codex", "pane": f"%{i+10}", "status": "active"} for i, a in enumerate(aliases)},
    }


def _write_seed_hook_configs(home: Path) -> tuple[Path, Path]:
    claude = home / ".claude" / "settings.json"
    codex = home / ".codex" / "hooks.json"
    claude.parent.mkdir(parents=True, exist_ok=True)
    codex.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "/tmp/bridge-hook --agent claude"},
                        {"type": "command", "command": "echo keep-claude"},
                    ]
                }
            ],
            "Notification": [
                {
                    "hooks": [
                        {"type": "command", "command": "/tmp/agent-bridge/hooks/bridge-hook --agent claude"},
                    ]
                }
            ],
        },
        "user": "preserve",
    }, indent=2) + "\n", encoding="utf-8")
    codex.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "/tmp/bridge-hook --agent codex"},
                        {"type": "command", "command": "echo keep-codex"},
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": "/tmp/agent-bridge/hooks/bridge-hook --agent codex"},
                    ]
                }
            ],
        },
        "user": "preserve",
    }, indent=2) + "\n", encoding="utf-8")
    return claude, codex


def _fake_install_env(tmpdir: Path, *, path_prefix: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("AGENT_BRIDGE" + "_PYTHON", None)
    env["HOME"] = str(tmpdir / "home")
    env["SHELL"] = "/bin/bash"
    env["XDG_BIN_HOME"] = str(tmpdir / "xdg-bin")
    env["XDG_CONFIG_HOME"] = str(tmpdir / "xdg-config")
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    env["AGENT_BRIDGE_RUN_DIR"] = str(tmpdir / "run")
    env["AGENT_BRIDGE_LOG_DIR"] = str(tmpdir / "log")
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env.get('PATH', '')}"
    return env


def _import_enqueue_module():
    import importlib
    be = importlib.import_module("bridge_enqueue")
    return importlib.reload(be)


def _run_enqueue_main(be, argv: list[str], stdin_text: str = "") -> tuple[int, str, str]:
    import contextlib
    import io
    old_argv = sys.argv[:]
    old_stdin = sys.stdin
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["bridge_enqueue.py", *argv]
        sys.stdin = io.StringIO(stdin_text)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = be.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
        sys.stdin = old_stdin
    return int(code), out.getvalue(), err.getvalue()


def _patch_enqueue_for_unit(be, state: dict, *, socket_error: str = "") -> None:
    be.ensure_daemon_running = lambda session: ""
    be.room_status = lambda session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")
    be.sender_matches_caller = lambda args, session: True
    be.load_session = lambda session: state
    be.enqueue_via_daemon_socket = lambda session, messages, **kwargs: (False, [], [], socket_error, "")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _enqueue_alarm(d, owner: str, note: str = "") -> str:
    return d.register_alarm(owner, 600.0, note) or ""

def _set_response_context(
    d,
    responder: str,
    requester: str,
    *,
    auto_return: bool = True,
    message_id: str = "msg-active-response",
    aggregate_id: str | None = None,
) -> None:
    d.current_prompt_by_agent[responder] = {
        "id": message_id,
        "nonce": "n-active-response",
        "causal_id": "c",
        "hop_count": 1,
        "from": requester,
        "kind": "request",
        "intent": "test",
        "auto_return": auto_return,
        "aggregate_id": aggregate_id,
        "aggregate_expected": [responder] if aggregate_id else None,
        "aggregate_message_ids": {responder: message_id} if aggregate_id else None,
        "turn_id": "t-active-response",
    }

def _watchdogs_for_message(d, message_id: str, phase: str | None = None) -> list[tuple[str, dict]]:
    return [
        (wake_id, wd)
        for wake_id, wd in d.watchdogs.items()
        if wd.get("ref_message_id") == message_id
        and not wd.get("is_alarm")
        and (phase is None or wd.get("watchdog_phase") == phase)
    ]

def _make_inflight(
    d,
    message_id: str,
    frm: str,
    to: str,
    nonce: str,
    *,
    auto_return: bool = True,
    kind: str = "request",
    aggregate_id: str | None = None,
    aggregate_expected: list[str] | None = None,
    aggregate_message_ids: dict[str, str] | None = None,
) -> None:
    """Plant a queue item already in inflight state (as if try_deliver ran)."""
    msg = {
        "id": message_id,
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": frm, "to": to,
        "kind": kind, "intent": "test",
        "body": "hello",
        "causal_id": f"causal-{uuid.uuid4().hex[:12]}",
        "hop_count": 1, "auto_return": auto_return,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "inflight", "nonce": nonce, "delivery_attempts": 1,
    }
    if aggregate_id:
        msg["aggregate_id"] = aggregate_id
        msg["aggregate_expected"] = aggregate_expected or [to]
        msg["aggregate_message_ids"] = aggregate_message_ids or {to: message_id}
    def add(queue):
        queue.append(msg)
        return None
    d.queue.update(add)
    d.reserved[to] = message_id

def _make_delivered_context(
    d,
    message_id: str,
    frm: str,
    to: str,
    nonce: str,
    *,
    auto_return: bool = True,
    kind: str = "request",
    source: str = "test",
    turn_id: str | None = None,
    aggregate_id: str | None = None,
    aggregate_expected: list[str] | None = None,
    aggregate_message_ids: dict[str, str] | None = None,
    watchdog: bool = False,
) -> dict:
    msg = {
        "id": message_id,
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "delivered_ts": utc_now(),
        "from": frm, "to": to,
        "kind": kind, "intent": "test",
        "body": "hello",
        "causal_id": f"causal-{uuid.uuid4().hex[:12]}",
        "hop_count": 1, "auto_return": auto_return,
        "reply_to": None, "source": source, "bridge_session": "test-session",
        "status": "delivered", "nonce": nonce, "delivery_attempts": 1,
    }
    if aggregate_id:
        msg["aggregate_id"] = aggregate_id
        msg["aggregate_expected"] = aggregate_expected or [to]
        msg["aggregate_message_ids"] = aggregate_message_ids or {to: message_id}
    def add(queue):
        queue.append(msg)
        return None
    d.queue.update(add)
    d.current_prompt_by_agent[to] = {
        "id": message_id,
        "nonce": nonce,
        "causal_id": msg["causal_id"],
        "hop_count": 1,
        "from": frm,
        "kind": kind,
        "intent": "test",
        "auto_return": auto_return,
        "aggregate_id": aggregate_id,
        "aggregate_expected": msg.get("aggregate_expected"),
        "aggregate_message_ids": msg.get("aggregate_message_ids"),
        "turn_id": turn_id,
    }
    if watchdog:
        d.watchdogs[f"wake-{message_id}"] = {
            "sender": frm,
            "deadline": time.time() + 600.0,
            "ref_message_id": message_id,
            "ref_aggregate_id": None,
            "ref_to": to,
            "is_alarm": False,
        }
    return msg

def _queue_item(d, message_id: str) -> dict | None:
    return next((item for item in d.queue.read() if item.get("id") == message_id), None)

def _active_turn(
    d,
    *,
    message_id: str,
    frm: str = "codex",
    to: str = "claude",
    nonce: str = "n-active-turn",
    turn_id: str = "active-turn",
    auto_return: bool = True,
    kind: str = "request",
    aggregate_id: str | None = None,
    aggregate_expected: list[str] | None = None,
    aggregate_message_ids: dict[str, str] | None = None,
) -> dict:
    msg = _make_delivered_context(
        d,
        message_id,
        frm=frm,
        to=to,
        nonce=nonce,
        turn_id=turn_id,
        auto_return=auto_return,
        kind=kind,
        aggregate_id=aggregate_id,
        aggregate_expected=aggregate_expected,
        aggregate_message_ids=aggregate_message_ids,
    )
    d.busy[to] = True
    d.reserved[to] = None
    d.last_enter_ts[message_id] = time.time()
    d.remember_nonce(nonce, msg)
    return msg

def _plant_watchdog(
    d,
    wake_id: str,
    *,
    sender: str = "codex",
    message_id: str | None = None,
    aggregate_id: str | None = None,
    to: str = "claude",
    deadline: float | None = None,
) -> str:
    d.watchdogs[wake_id] = {
        "sender": sender,
        "deadline": time.time() if deadline is None else deadline,
        "ref_message_id": message_id,
        "ref_aggregate_id": aggregate_id,
        "ref_to": to,
        "ref_kind": "request",
        "ref_intent": "test",
        "ref_causal_id": "causal-watchdog",
        "ref_aggregate_expected": ["w1", "w2"] if aggregate_id else [],
        "is_alarm": False,
    }
    return wake_id

def _auto_return_results(d, sender: str, target: str) -> list[dict]:
    return [
        item for item in d.queue.read()
        if item.get("from") == sender
        and item.get("to") == target
        and item.get("kind") == "result"
        and item.get("source") == "auto_return"
    ]

def _assert_auto_return_result_shape(
    label: str,
    result: dict,
    *,
    sender: str,
    target: str,
    reply_to: str,
    causal_id: str,
    hop_count: int,
    body: str,
) -> None:
    assert_true(result.get("from") == sender, f"{label}: result sender mismatch: {result}")
    assert_true(result.get("to") == target, f"{label}: result target mismatch: {result}")
    assert_true(result.get("kind") == "result", f"{label}: result kind mismatch: {result}")
    assert_true(result.get("source") == "auto_return", f"{label}: result source mismatch: {result}")
    assert_true(result.get("auto_return") is False, f"{label}: result must not auto-return: {result}")
    assert_true(result.get("reply_to") == reply_to, f"{label}: reply_to mismatch: {result}")
    assert_true(result.get("causal_id") == causal_id, f"{label}: causal_id mismatch: {result}")
    assert_true(int(result.get("hop_count") or 0) == hop_count, f"{label}: hop_count mismatch: {result}")
    assert_true(result.get("body") == body, f"{label}: body mismatch: {result.get('body')!r}")

def _import_daemon_ctl():
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    return importlib.import_module("bridge_daemon_ctl")

def _daemon_command_result(d: bridge_daemon.BridgeDaemon, payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8") + b"\n"
    return d.handle_command_connection(FakeCommandConn(raw))  # type: ignore[arg-type]
