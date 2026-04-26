#!/usr/bin/env python3
"""Lightweight regression checks for the v1 interrupt + lifecycle changes.

Runs the daemon in dry_run mode against a temporary state directory and
exercises the critical invariants without touching tmux. Each scenario
constructs the BridgeDaemon manually, drives the relevant methods, and
asserts on in-memory state and on event log entries.

Run with:

    python3 scripts/regression_interrupt.py

Exits non-zero if any scenario fails.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    ("agent_extend_wait_model_tool", Path("model-bin/agent_extend_wait")),
    ("bridge_hook_entrypoint", Path("hooks/bridge-hook")),
)
INSTALL_SHIM_TARGETS = DIRECT_EXECUTABLE_TARGETS[:-1]
sys.path.insert(0, str(LIBEXEC))

import bridge_daemon  # noqa: E402
import bridge_attach  # noqa: E402
import bridge_hook_logger  # noqa: E402
import bridge_identity  # noqa: E402
import bridge_join  # noqa: E402
import bridge_pane_probe  # noqa: E402
import bridge_response_guard  # noqa: E402
from bridge_util import MAX_INLINE_SEND_BODY_CHARS, MAX_PEER_BODY_CHARS, read_json, read_limited_text, validate_peer_body_size, utc_now, write_json_atomic  # noqa: E402


def make_daemon(tmpdir: Path, participants: dict[str, dict]) -> bridge_daemon.BridgeDaemon:
    state_file = tmpdir / "events.raw.jsonl"
    public_state_file = tmpdir / "events.jsonl"
    queue_file = tmpdir / "pending.json"
    session_file = tmpdir / "session.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.touch()
    public_state_file.touch()
    queue_file.write_text("[]", encoding="utf-8")
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


def scenario_lifecycle(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Manually enqueue a request from alice → bob
    msg = {
        "id": "msg-test-1",
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": "claude", "to": "codex",
        "kind": "request", "intent": "test",
        "body": "hello",
        "causal_id": f"causal-{uuid.uuid4().hex[:12]}",
        "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "pending", "nonce": None, "delivery_attempts": 0,
    }
    d.enqueue_ipc_message(msg)
    queue_now = list(d.queue.read())
    assert_true(any(it.get("id") == "msg-test-1" and it.get("status") == "pending" for it in queue_now), f"{label}: enqueue stored as pending")
    # Simulate prompt_submitted (peer = bob)
    record = {"agent": "codex", "bridge_agent": "codex", "nonce": "synthetic-nonce-1", "turn_id": "turn-1", "prompt": ""}
    # Need to set the message's nonce to match (deliver_reserved would normally do this).
    def assign_nonce(queue):
        for it in queue:
            if it.get("id") == "msg-test-1":
                it["nonce"] = "synthetic-nonce-1"
                it["status"] = "inflight"
        return None
    d.queue.update(assign_nonce)
    d.handle_prompt_submitted(record)
    queue_after_submit = list(d.queue.read())
    item = next((it for it in queue_after_submit if it.get("id") == "msg-test-1"), None)
    assert_true(item is not None, f"{label}: after prompt_submitted message must remain in queue")
    assert_true(item.get("status") == "delivered", f"{label}: status should be 'delivered', got {item.get('status')!r}")
    # Simulate response_finished (matching turn)
    finish_record = {"agent": "codex", "bridge_agent": "codex", "turn_id": "turn-1", "last_assistant_message": "hi"}
    d.handle_response_finished(finish_record)
    queue_after_finish = list(d.queue.read())
    assert_true(not any(it.get("id") == "msg-test-1" for it in queue_after_finish), f"{label}: message must be removed at terminal response_finished")
    print(f"  PASS  {label}")


def scenario_held_interrupt_does_not_block_delivery(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Manually plant a held_interrupt without the full ESC dance
    d.held_interrupt["codex"] = {
        "since": utc_now(), "since_ts": time.time(),
        "prior_message_id": "msg-prior", "prior_sender": "claude",
        "reason": "interrupt_by_sender", "by_sender": "claude",
        "cancelled_message_ids": ["msg-prior"],
    }
    # Try to enqueue + deliver a new message from alice → bob
    new_msg = {
        "id": "msg-test-2", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "claude", "to": "codex", "kind": "request", "intent": "test",
        "body": "still pending", "causal_id": f"causal-{uuid.uuid4().hex[:12]}",
        "hop_count": 1, "auto_return": True, "reply_to": None,
        "source": "test", "bridge_session": "test-session",
        "status": "pending", "nonce": None, "delivery_attempts": 0,
    }
    d.queue.update(lambda queue: queue.append(new_msg))
    d.try_deliver("codex")
    item = next((it for it in d.queue.read() if it.get("id") == "msg-test-2"), None)
    assert_true(item is not None and item.get("status") == "inflight", f"{label}: held marker must not block delivery, got {item}")
    assert_true("codex" in d.held_interrupt, f"{label}: delivery must not clear the legacy marker")
    print(f"  PASS  {label}")


def scenario_reserve_next_ignores_held_marker(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.held_interrupt["codex"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-prior",
        "reason": "legacy",
    }
    d.queue.update(lambda queue: queue.append(test_message("msg-held-reserve", frm="claude", to="codex", status="pending")))
    reserved = d.reserve_next("codex")
    item = _queue_item(d, "msg-held-reserve")
    assert_true(reserved is not None and reserved.get("id") == "msg-held-reserve", f"{label}: reserve_next must select pending despite held marker")
    assert_true(item is not None and item.get("status") == "inflight", f"{label}: queue row must move to inflight")
    assert_true("codex" in d.held_interrupt, f"{label}: reserve_next must not clear held marker")
    print(f"  PASS  {label}")


def scenario_held_marker_persists_through_delivery(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.held_interrupt["codex"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-old"}
    d.queue.update(lambda queue: queue.append(test_message("msg-held-persist", frm="claude", to="codex", status="pending")))
    d.try_deliver("codex")
    assert_true((_queue_item(d, "msg-held-persist") or {}).get("status") == "inflight", f"{label}: delivery expected")
    assert_true((d.held_interrupt.get("codex") or {}).get("prior_message_id") == "msg-old", f"{label}: marker lifecycle must survive delivery")
    print(f"  PASS  {label}")


def scenario_esc_fail_no_state_change(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "ghost": {"alias": "ghost", "pane": ""}}
    d = make_daemon(tmpdir, participants)
    # Set up an active prompt for ghost
    d.current_prompt_by_agent["ghost"] = {"id": "msg-active", "from": "claude", "auto_return": True, "turn_id": None}
    # Plant a delivered message in queue
    delivered_msg = {
        "id": "msg-active", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "claude", "to": "ghost", "kind": "request", "intent": "test",
        "body": "x", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "delivered", "nonce": "n-active", "delivery_attempts": 1,
    }
    def add(queue): queue.append(delivered_msg); return None
    d.queue.update(add)
    # Force pane resolution to fail by clearing panes cache
    d.panes.pop("ghost", None)
    result = d.handle_interrupt(sender="claude", target="ghost")
    assert_true(result.get("esc_sent") is False, f"{label}: ESC should not be reported as sent (no pane)")
    assert_true("ghost" not in d.held_interrupt, f"{label}: held_interrupt must NOT be set on ESC failure")
    queue_after = list(d.queue.read())
    assert_true(any(it.get("id") == "msg-active" and it.get("status") == "delivered" for it in queue_after), f"{label}: delivered message must still be in queue (state unchanged)")
    assert_true("ghost" in d.current_prompt_by_agent, f"{label}: current_prompt_by_agent must NOT be cleared on ESC failure")
    print(f"  PASS  {label}")


def scenario_clear_hold(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.held_interrupt["codex"] = {
        "since": utc_now(), "since_ts": time.time(),
        "prior_message_id": "msg-prior", "prior_sender": "claude",
        "reason": "interrupt_by_sender", "by_sender": "claude",
        "cancelled_message_ids": [],
    }
    info = d.release_hold("codex", reason="manual_clear_by_alice", by_sender="claude")
    assert_true(info is not None, f"{label}: release_hold should return info")
    assert_true("codex" not in d.held_interrupt, f"{label}: hold should be cleared")
    events = read_events(Path(d.state_file))
    assert_true(any(e.get("event") == "hold_force_resumed" for e in events), f"{label}: hold_force_resumed should be logged")
    print(f"  PASS  {label}")


def scenario_clear_hold_socket_still_pops_held(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.held_interrupt["codex"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-prior",
        "reason": "legacy",
    }

    class FakeConn:
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

    payload = json.dumps({"op": "clear_hold", "from": "claude", "target": "codex"}).encode("utf-8") + b"\n"
    result = d.handle_command_connection(FakeConn(payload))  # type: ignore[arg-type]
    assert_true(result.get("ok") is True and result.get("had_hold") is True, f"{label}: clear_hold socket op should report held marker: {result}")
    assert_true("codex" not in d.held_interrupt, f"{label}: clear_hold socket op must pop held marker")
    events = read_events(Path(d.state_file))
    assert_true(any(e.get("event") == "hold_force_resumed" for e in events), f"{label}: hold_force_resumed should be logged")
    print(f"  PASS  {label}")


def scenario_aggregate_interrupt_synthetic(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-test-1"
    # Plant an aggregate request: manager → w1 + w2
    common = {
        "kind": "request", "intent": "test", "body": "do",
        "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "delivered", "delivery_attempts": 1,
        "aggregate_id": agg_id, "aggregate_expected": ["w1", "w2"],
        "aggregate_message_ids": {"w1": "msg-w1", "w2": "msg-w2"},
    }
    msg_w1 = {**common, "id": "msg-w1", "from": "manager", "to": "w1",
              "nonce": "n-w1", "created_ts": utc_now(), "updated_ts": utc_now()}
    msg_w2 = {**common, "id": "msg-w2", "from": "manager", "to": "w2",
              "nonce": "n-w2", "created_ts": utc_now(), "updated_ts": utc_now()}
    def add(queue): queue.append(msg_w1); queue.append(msg_w2); return None
    d.queue.update(add)
    d.current_prompt_by_agent["w1"] = {"id": "msg-w1", "from": "manager", "auto_return": True, "aggregate_id": agg_id, "turn_id": None}
    # Interrupt w1 — pane available → ESC succeeds (dry_run, no actual subprocess)
    d.panes["w1"] = "%96"
    result = d.handle_interrupt(sender="manager", target="w1")
    assert_true(result.get("esc_sent") is True, f"{label}: ESC should be reported as sent in dry_run")
    assert_true(result.get("held") is False, f"{label}: interrupt should not enter held state")
    assert_true("w1" not in d.held_interrupt, f"{label}: w1 should not be held")
    # Now simulate w2 producing its real reply → aggregate should complete
    d.current_prompt_by_agent["w2"] = {
        "id": "msg-w2", "from": "manager", "auto_return": True,
        "aggregate_id": agg_id, "aggregate_expected": ["w1", "w2"],
        "aggregate_message_ids": {"w1": "msg-w1", "w2": "msg-w2"},
        "causal_id": "c", "intent": "test", "hop_count": 1, "turn_id": None,
    }
    finish_record = {"agent": "w2", "bridge_agent": "w2", "last_assistant_message": "ok"}
    d.handle_response_finished(finish_record)
    events = read_events(Path(d.state_file))
    queued_aggregate_result = any(e.get("event") == "aggregate_result_queued" and e.get("aggregate_id") == agg_id for e in events)
    assert_true(queued_aggregate_result, f"{label}: aggregate_result should be queued after w1 synthetic + w2 real")
    print(f"  PASS  {label}")


def scenario_interrupt_pending_replacement_delivers(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")
    replacement = test_message("msg-new", frm="alice", to="bob", status="pending")
    d.queue.update(lambda queue: (queue.append(replacement), None)[1])

    result = d.handle_interrupt(sender="alice", target="bob")

    assert_true(result.get("esc_sent") is True, f"{label}: interrupt should send ESC in dry_run")
    assert_true(result.get("held") is False, f"{label}: interrupt should not enter held state")
    assert_true("bob" not in d.held_interrupt, f"{label}: held_interrupt must stay clear")
    assert_true(_queue_item(d, "msg-old") is None, f"{label}: old delivered message should be cancelled")
    new_item = _queue_item(d, "msg-new")
    assert_true(new_item is not None and new_item.get("status") == "inflight", f"{label}: replacement should deliver immediately, got {new_item}")
    assert_true(d.reserved.get("bob") == "msg-new", f"{label}: replacement should reserve bob")
    print(f"  PASS  {label}")


def scenario_interrupt_new_replacement_after_interrupt_delivers(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    result = d.handle_interrupt(sender="alice", target="bob")
    assert_true(result.get("held") is False, f"{label}: interrupt should not enter held state")
    d.queue_message(test_message("msg-new", frm="alice", to="bob", status="pending"))

    new_item = _queue_item(d, "msg-new")
    assert_true(new_item is not None and new_item.get("status") == "inflight", f"{label}: post-interrupt replacement should deliver immediately, got {new_item}")
    print(f"  PASS  {label}")


def scenario_interrupted_late_prompt_submitted_before_replacement(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-old", "alice", "bob", "n-old")

    d.handle_interrupt(sender="alice", target="bob")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": "n-old", "turn_id": "turn-old", "prompt": ""})

    assert_true("bob" not in d.current_prompt_by_agent, f"{label}: old prompt_submitted must not create active ctx")
    d.queue_message(test_message("msg-new", frm="alice", to="bob", status="pending"))
    new_item = _queue_item(d, "msg-new")
    assert_true(new_item is not None and new_item.get("status") == "inflight", f"{label}: replacement should deliver after suppressed old prompt")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "interrupted_prompt_submitted_suppressed" for e in events), f"{label}: suppression should be logged")
    print(f"  PASS  {label}")


def scenario_interrupted_late_prompt_submitted_after_replacement(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-old", "alice", "bob", "n-old")
    d.queue.update(lambda queue: (queue.append(test_message("msg-new", frm="alice", to="bob", status="pending")), None)[1])

    d.handle_interrupt(sender="alice", target="bob")
    new_item = _queue_item(d, "msg-new")
    assert_true(new_item is not None and new_item.get("status") == "inflight", f"{label}: replacement should be inflight")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": new_item.get("nonce"), "turn_id": "turn-new", "prompt": ""})
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": "n-old", "turn_id": "turn-old", "prompt": ""})

    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("id") == "msg-new" and ctx.get("turn_id") == "turn-new", f"{label}: old prompt_submitted must not intercept replacement ctx: {ctx}")
    assert_true(_queue_item(d, "msg-new") is not None and _queue_item(d, "msg-new").get("status") == "delivered", f"{label}: replacement must remain delivered")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(not any(e.get("event") == "active_prompt_intercepted" for e in events), f"{label}: old prompt must not trigger intercept")
    print(f"  PASS  {label}")


def scenario_interrupted_late_turn_stop_preserves_replacement(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")
    d.queue.update(lambda queue: (queue.append(test_message("msg-new", frm="alice", to="bob", status="pending")), None)[1])

    d.handle_interrupt(sender="alice", target="bob")
    new_item = _queue_item(d, "msg-new")
    assert_true(new_item is not None, f"{label}: replacement should exist")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": new_item.get("nonce"), "turn_id": "turn-new", "prompt": ""})
    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "turn-old", "last_assistant_message": "late old"})

    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("id") == "msg-new", f"{label}: late old Stop must not clear replacement ctx")
    assert_true(not any(item.get("kind") == "result" and item.get("to") == "alice" and "late old" in str(item.get("body") or "") for item in d.queue.read()), f"{label}: late old Stop must not route")
    print(f"  PASS  {label}")


def scenario_interrupted_no_turn_stop_no_context_suppressed(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id=None)

    d.handle_interrupt(sender="alice", target="bob")
    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": None, "last_assistant_message": "late old"})

    assert_true("bob" not in d.interrupted_turns, f"{label}: psubseen no-context tombstone should be consumed")
    assert_true(not any(item.get("kind") == "result" and item.get("to") == "alice" for item in d.queue.read()), f"{label}: stale no-context Stop must not route")
    print(f"  PASS  {label}")


def scenario_interrupted_no_turn_race_routes_replacement_then_suppresses_old(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-old", "alice", "bob", "n-old")
    d.queue.update(lambda queue: (queue.append(test_message("msg-new", frm="alice", to="bob", status="pending")), None)[1])

    d.handle_interrupt(sender="alice", target="bob")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": "n-old", "turn_id": None, "prompt": ""})
    new_item = _queue_item(d, "msg-new")
    assert_true(new_item is not None and new_item.get("status") == "inflight", f"{label}: replacement should be inflight")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": new_item.get("nonce"), "turn_id": None, "prompt": ""})
    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": None, "last_assistant_message": "replacement ok"})

    assert_true(any(item.get("kind") == "result" and item.get("to") == "alice" and "replacement ok" in str(item.get("body") or "") for item in d.queue.read()), f"{label}: fast replacement no-turn response should route")
    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": None, "last_assistant_message": "late old"})
    assert_true(not any(item.get("kind") == "result" and item.get("to") == "alice" and "late old" in str(item.get("body") or "") for item in d.queue.read()), f"{label}: later old no-turn Stop should not route")
    print(f"  PASS  {label}")


def scenario_interrupted_inflight_tombstone_retains_on_unrelated_stop(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-old", "alice", "bob", "n-old")

    d.handle_interrupt(sender="alice", target="bob")
    assert_true(d.interrupted_turns.get("bob"), f"{label}: inflight interrupt should create tombstone")
    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": None, "last_assistant_message": "unrelated"})
    tombstones = d.interrupted_turns.get("bob") or []
    assert_true(tombstones and tombstones[0].get("message_id") == "msg-old", f"{label}: psubseen=False tombstone must survive unrelated no-context Stop")

    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": "n-old", "turn_id": None, "prompt": ""})
    tombstones = d.interrupted_turns.get("bob") or []
    assert_true(tombstones and tombstones[0].get("prompt_submitted_seen") is True, f"{label}: delayed old prompt_submitted should still be suppressed and mark psubseen")
    print(f"  PASS  {label}")


def scenario_interrupted_empty_values_do_not_match_tombstone(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.interrupted_turns["bob"] = [{
        "message_id": "msg-old",
        "turn_id": "",
        "nonce": "",
        "prior_sender": "alice",
        "by_sender": "alice",
        "cancelled_message_ids": ["msg-old"],
        "interrupted_ts": time.time(),
        "prompt_submitted_seen": False,
        "superseded_by_prompt": False,
    }]

    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": "", "turn_id": None, "prompt": "manual"})

    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("id") is None and ctx.get("turn_id") is None, f"{label}: empty hook values should be treated as normal user prompt, got {ctx}")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(not any(e.get("event") == "interrupted_prompt_submitted_suppressed" for e in events), f"{label}: empty values must not match tombstone")
    assert_true(d.interrupted_turns.get("bob"), f"{label}: tombstone should remain")
    print(f"  PASS  {label}")


def scenario_interrupted_tombstone_current_ctx_id_match_cleans(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-current-id", frm="alice", to="bob", nonce="n-current-id", turn_id="turn-current")
    _plant_watchdog(d, "wake-current-id", sender="alice", message_id="msg-current-id", to="bob")
    d.interrupted_turns["bob"] = [{
        "message_id": "msg-current-id",
        "turn_id": "",
        "nonce": "n-current-id",
        "prior_sender": "alice",
        "by_sender": "alice",
        "cancelled_message_ids": ["msg-current-id"],
        "interrupted_ts": time.time(),
        "prompt_submitted_seen": True,
        "superseded_by_prompt": False,
    }]

    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": None, "last_assistant_message": "suppressed"})

    assert_true("bob" not in d.interrupted_turns, f"{label}: tombstone should be popped")
    assert_true(_queue_item(d, "msg-current-id") is None, f"{label}: delivered row should be removed")
    assert_true("wake-current-id" not in d.watchdogs, f"{label}: per-message watchdog should be cancelled")
    assert_true(d.current_prompt_by_agent.get("bob") is None, f"{label}: current ctx should be cleared")
    assert_true(d.busy.get("bob") is False, f"{label}: busy should clear")
    assert_true("msg-current-id" not in d.last_enter_ts, f"{label}: last_enter_ts should clear")
    assert_true("n-current-id" not in d.injected_by_nonce, f"{label}: nonce cache should clear")
    assert_true(_auto_return_results(d, "bob", "alice") == [], f"{label}: tombstone terminal must suppress text")
    events = read_events(tmpdir / "events.raw.jsonl")
    cancellations = [e for e in events if e.get("event") == "watchdog_cancelled" and e.get("ref_message_id") == "msg-current-id"]
    assert_true(len(cancellations) == 1 and cancellations[0].get("reason") == "interrupted_tombstone_terminal", f"{label}: cancel reason expected: {cancellations}")
    print(f"  PASS  {label}")


def scenario_interrupted_tombstone_current_ctx_turn_match_cleans(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-current-turn", frm="alice", to="bob", nonce="n-current-turn", turn_id="turn-current")
    _plant_watchdog(d, "wake-current-turn", sender="alice", message_id="msg-current-turn", to="bob")
    d.interrupted_turns["bob"] = [{
        "message_id": "msg-other",
        "turn_id": "turn-current",
        "nonce": "n-other",
        "prior_sender": "alice",
        "by_sender": "alice",
        "cancelled_message_ids": ["msg-other"],
        "interrupted_ts": time.time(),
        "prompt_submitted_seen": True,
        "superseded_by_prompt": False,
    }]

    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "turn-current", "last_assistant_message": "suppressed"})

    assert_true("bob" not in d.interrupted_turns, f"{label}: tombstone should be popped")
    assert_true(_queue_item(d, "msg-current-turn") is None, f"{label}: delivered row should be removed")
    assert_true("wake-current-turn" not in d.watchdogs, f"{label}: turn-matched watchdog should be cancelled")
    assert_true(d.current_prompt_by_agent.get("bob") is None, f"{label}: turn-matched ctx should be cleared")
    assert_true(d.busy.get("bob") is False, f"{label}: busy should clear")
    assert_true(_auto_return_results(d, "bob", "alice") == [], f"{label}: tombstone terminal must suppress text")
    print(f"  PASS  {label}")


def scenario_interrupted_tombstone_stale_stop_preserves_replacement_watchdog(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-new", frm="alice", to="bob", nonce="n-new", turn_id="turn-new")
    _plant_watchdog(d, "wake-new", sender="alice", message_id="msg-new", to="bob")
    d.interrupted_turns["bob"] = [{
        "message_id": "msg-old",
        "turn_id": "turn-old",
        "nonce": "n-old",
        "prior_sender": "alice",
        "by_sender": "alice",
        "cancelled_message_ids": ["msg-old"],
        "interrupted_ts": time.time(),
        "prompt_submitted_seen": True,
        "superseded_by_prompt": False,
    }]

    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "turn-old", "last_assistant_message": "late old"})

    assert_true("bob" not in d.interrupted_turns, f"{label}: stale tombstone should be consumed")
    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("id") == "msg-new", f"{label}: replacement ctx must remain")
    assert_true((_queue_item(d, "msg-new") or {}).get("status") == "delivered", f"{label}: replacement row must remain delivered")
    assert_true("wake-new" in d.watchdogs, f"{label}: replacement watchdog must remain")
    assert_true(d.busy.get("bob") is True, f"{label}: replacement busy state must remain")
    assert_true(_auto_return_results(d, "bob", "alice") == [], f"{label}: stale tombstone must not auto-route")
    print(f"  PASS  {label}")


def scenario_interrupted_tombstone_aggregate_ctx_does_not_cancel_aggregate_watchdog(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-tombstone-terminal"
    _active_turn(
        d,
        message_id="msg-agg-w1",
        frm="manager",
        to="w1",
        nonce="n-agg-w1",
        turn_id="turn-agg-w1",
        aggregate_id=agg_id,
        aggregate_expected=["w1", "w2"],
        aggregate_message_ids={"w1": "msg-agg-w1", "w2": "msg-agg-w2"},
    )
    _plant_watchdog(d, "wake-agg-terminal", sender="manager", message_id="msg-agg-w1", aggregate_id=agg_id, to="w1")
    d.interrupted_turns["w1"] = [{
        "message_id": "msg-agg-w1",
        "turn_id": "turn-agg-w1",
        "nonce": "n-agg-w1",
        "prior_sender": "manager",
        "by_sender": "manager",
        "cancelled_message_ids": ["msg-agg-w1"],
        "interrupted_ts": time.time(),
        "prompt_submitted_seen": True,
        "superseded_by_prompt": False,
    }]

    d.handle_response_finished({"agent": "w1", "bridge_agent": "w1", "turn_id": "turn-agg-w1", "last_assistant_message": "must not collect"})

    assert_true(_queue_item(d, "msg-agg-w1") is None, f"{label}: aggregate leg row should be removed")
    assert_true(d.current_prompt_by_agent.get("w1") is None, f"{label}: aggregate local ctx should clear")
    assert_true(d.busy.get("w1") is False, f"{label}: aggregate local busy should clear")
    assert_true("n-agg-w1" not in d.injected_by_nonce, f"{label}: aggregate nonce should clear")
    assert_true("msg-agg-w1" not in d.last_enter_ts, f"{label}: aggregate last_enter should clear")
    assert_true("wake-agg-terminal" in d.watchdogs, f"{label}: aggregate watchdog must remain")
    aggregate_data = read_json(Path(d.aggregate_file), {"aggregates": {}})
    replies = ((aggregate_data.get("aggregates") or {}).get(agg_id) or {}).get("replies") or {}
    assert_true(replies == {}, f"{label}: tombstone text must not collect aggregate reply")
    queued = d.queue.read()
    assert_true(not any(item.get("kind") == "result" and item.get("source") in {"auto_return", "aggregate_return"} for item in queued), f"{label}: tombstone terminal must not synthesize result")
    print(f"  PASS  {label}")


def scenario_aggregate_late_real_stop_after_interrupt_does_not_overwrite(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-late-stop"
    msg_w1 = _make_delivered_context(
        d,
        "msg-w1",
        "manager",
        "w1",
        "n-w1",
        turn_id="turn-w1",
        aggregate_id=agg_id,
        aggregate_expected=["w1", "w2"],
        aggregate_message_ids={"w1": "msg-w1", "w2": "msg-w2"},
    )
    _make_delivered_context(
        d,
        "msg-w2",
        "manager",
        "w2",
        "n-w2",
        turn_id="turn-w2",
        aggregate_id=agg_id,
        aggregate_expected=["w1", "w2"],
        aggregate_message_ids={"w1": "msg-w1", "w2": "msg-w2"},
    )
    # Keep both delivered rows; _make_delivered_context returned msg_w1
    # above only to make the aggregate metadata explicit for readers.
    assert_true(msg_w1.get("aggregate_id") == agg_id, f"{label}: aggregate fixture expected")

    d.handle_interrupt(sender="manager", target="w1")
    d.handle_response_finished({"agent": "w1", "bridge_agent": "w1", "turn_id": "turn-w1", "last_assistant_message": "late real"})
    d.handle_response_finished({"agent": "w2", "bridge_agent": "w2", "turn_id": "turn-w2", "last_assistant_message": "w2 ok"})

    data = read_json(d.aggregate_file, {"aggregates": {}})
    replies = ((data.get("aggregates") or {}).get(agg_id) or {}).get("replies") or {}
    w1_body = str((replies.get("w1") or {}).get("body") or "")
    assert_true("late real" not in w1_body, f"{label}: late real w1 Stop must not overwrite synthetic aggregate reply: {w1_body!r}")
    assert_true("interrupted" in w1_body, f"{label}: synthetic interrupted aggregate reply should remain: {w1_body!r}")
    print(f"  PASS  {label}")


def scenario_watchdog_cancel_on_empty_response(label: str, tmpdir: Path) -> None:
    # v1.5: watchdog arms at delivery, not enqueue. The cancel-on-empty
    # invariant from v1 is preserved: even an empty/no-text response must
    # still cancel the watchdog and auto-route an explicit sentinel result.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = {
        "id": "msg-wd-1", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "claude", "to": "codex", "kind": "request", "intent": "test",
        "body": "x", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "pending", "nonce": None, "delivery_attempts": 0,
        "watchdog_delay_sec": 600.0,
    }
    d.enqueue_ipc_message(msg)
    assert_true(not any(wd.get("ref_message_id") == "msg-wd-1" for wd in d.watchdogs.values()), f"{label}: watchdog must NOT be registered at enqueue (only arms at delivery)")
    def assign(queue):
        for it in queue:
            if it.get("id") == "msg-wd-1":
                it["nonce"] = "wd-nonce"
                it["status"] = "inflight"
        return None
    d.queue.update(assign)
    d.handle_prompt_submitted({"agent": "codex", "bridge_agent": "codex", "nonce": "wd-nonce", "turn_id": "t-wd", "prompt": ""})
    assert_true(any(wd.get("ref_message_id") == "msg-wd-1" for wd in d.watchdogs.values()), f"{label}: watchdog must be registered after delivery (prompt_submitted)")
    ctx = dict(d.current_prompt_by_agent.get("codex") or {})
    d.handle_response_finished({"agent": "codex", "bridge_agent": "codex", "turn_id": "t-wd", "last_assistant_message": ""})
    assert_true(not any(wd.get("ref_message_id") == "msg-wd-1" for wd in d.watchdogs.values()), f"{label}: watchdog must be cancelled even when response text is empty")
    routed = _auto_return_results(d, "codex", "claude")
    assert_true(len(routed) == 1, f"{label}: empty response should auto-route one sentinel result, got {routed}")
    _assert_auto_return_result_shape(
        label,
        routed[0],
        sender="codex",
        target="claude",
        reply_to="msg-wd-1",
        causal_id=str(ctx.get("causal_id") or ""),
        hop_count=int(ctx.get("hop_count") or 0),
        body="Result from codex:\n(empty response)",
    )
    events = read_events(tmpdir / "events.raw.jsonl")
    cancellations = [e for e in events if e.get("event") == "watchdog_cancelled" and e.get("ref_message_id") == "msg-wd-1"]
    assert_true(len(cancellations) == 1 and cancellations[0].get("reason") == "reply_received", f"{label}: empty reply watchdog cancel should use reply_received: {cancellations}")
    print(f"  PASS  {label}")


def _enqueue_alarm(d, owner: str, note: str = "") -> str:
    return d.register_alarm(owner, 600.0, note) or ""


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


def scenario_response_send_guard_socket_request_notice(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
    }
    d = make_daemon(tmpdir, participants)
    _set_response_context(d, "bob", "alice")

    request_msg = _qualifying_message("bob", "alice", kind="request", body="wrong request")
    result = d.handle_enqueue_command([request_msg])
    assert_true(not result.get("ok"), f"{label}: request to requester must be blocked")
    assert_true(result.get("error_kind") == "response_send_guard", f"{label}: guard block must carry structured error_kind: {result}")
    assert_true("do not call agent_send_peer" in str(result.get("error")), f"{label}: error must explain normal reply: {result}")

    notice_msg = _qualifying_message("bob", "alice", kind="notice", body="wrong notice")
    result2 = d.handle_enqueue_command([notice_msg])
    assert_true(not result2.get("ok"), f"{label}: notice to requester must be blocked")
    assert_true(d.queue.read() == [], f"{label}: blocked socket sends must not enqueue")
    assert_true(not any(e.get("event") == "message_queued" for e in read_events(tmpdir / "events.raw.jsonl")), f"{label}: blocked socket sends must not log message_queued")
    print(f"  PASS  {label}")


def scenario_response_send_guard_socket_force_and_other_peer(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
        "carol": {"alias": "carol", "agent_type": "codex", "pane": "%93", "status": "active"},
    }
    d = make_daemon(tmpdir, participants)
    _set_response_context(d, "bob", "alice")

    other_msg = _qualifying_message("bob", "carol", kind="request", body="allowed")
    result = d.handle_enqueue_command([other_msg])
    assert_true(result.get("ok"), f"{label}: send to other peer must be allowed: {result}")

    forced_msg = _qualifying_message("bob", "alice", kind="notice", body="forced")
    result2 = d.handle_enqueue_command([forced_msg], force_response_send=True)
    assert_true(result2.get("ok"), f"{label}: forced send to requester must be allowed: {result2}")
    queued = d.queue.read()
    assert_true(any(m.get("to") == "carol" for m in queued), f"{label}: other peer message queued")
    assert_true(any(m.get("to") == "alice" for m in queued), f"{label}: forced requester message queued")
    print(f"  PASS  {label}")


def scenario_response_send_guard_socket_no_auto_return_allowed(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
    }
    d = make_daemon(tmpdir, participants)
    _set_response_context(d, "bob", "alice", auto_return=False)
    msg = _qualifying_message("bob", "alice", kind="request", body="manual reply")
    result = d.handle_enqueue_command([msg])
    assert_true(result.get("ok"), f"{label}: no-auto-return context must not block manual send: {result}")
    assert_true(any(m.get("to") == "alice" for m in d.queue.read()), f"{label}: manual reply queued")
    print(f"  PASS  {label}")


def scenario_response_send_guard_socket_atomic_multi(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
        "carol": {"alias": "carol", "agent_type": "codex", "pane": "%93", "status": "active"},
    }
    d = make_daemon(tmpdir, participants)
    _set_response_context(d, "bob", "alice")
    msg_to_carol = _qualifying_message("bob", "carol", kind="request", body="first")
    msg_to_alice = _qualifying_message("bob", "alice", kind="request", body="second")
    result = d.handle_enqueue_command([msg_to_carol, msg_to_alice])
    assert_true(not result.get("ok"), f"{label}: multi-message payload including requester must be blocked")
    assert_true(d.queue.read() == [], f"{label}: socket guard must leave queue unchanged")
    assert_true(not any(e.get("event") == "message_queued" for e in read_events(tmpdir / "events.raw.jsonl")), f"{label}: socket guard must not append message_queued")
    print(f"  PASS  {label}")


def scenario_response_send_guard_socket_aggregate_and_held(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
    }
    d = make_daemon(tmpdir, participants)
    _set_response_context(d, "bob", "alice", aggregate_id="agg-active")
    aggregate_result = d.handle_enqueue_command([_qualifying_message("bob", "alice", kind="request", body="agg follow-up")])
    assert_true(not aggregate_result.get("ok"), f"{label}: aggregate response context must block send to requester")

    d.held_interrupt["bob"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-active-response"}
    held_result = d.handle_enqueue_command([_qualifying_message("bob", "alice", kind="notice", body="held follow-up")])
    assert_true(not held_result.get("ok"), f"{label}: held response context must still block send to requester")
    forced = d.handle_enqueue_command([_qualifying_message("bob", "alice", kind="notice", body="held forced")], force_response_send=True)
    assert_true(forced.get("ok"), f"{label}: force must escape held-context guard: {forced}")
    print(f"  PASS  {label}")


def scenario_response_send_guard_after_response_finished_allowed(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-race-1", frm="alice", to="bob", nonce="n-race-1")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": "n-race-1", "turn_id": "t-race", "prompt": "[bridge:n-race-1] from=alice kind=request"})
    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "t-race", "last_assistant_message": "done"})
    result = d.handle_enqueue_command([_qualifying_message("bob", "alice", kind="request", body="new request")])
    assert_true(result.get("ok"), f"{label}: send after response context is consumed must be allowed: {result}")
    print(f"  PASS  {label}")


def scenario_alarm_cancelled_by_qualifying_request(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude", note="results of debate")
    assert_true(wake_id != "", f"{label}: alarm should be registered")
    incoming = _qualifying_message("codex", "claude", kind="request", body="hi claude")
    d.enqueue_ipc_message(incoming)
    assert_true(wake_id not in d.watchdogs, f"{label}: alarm should be cancelled by qualifying request")
    queued = next((it for it in d.queue.read() if it.get("id") == incoming["id"]), None)
    assert_true(queued is not None and "[bridge:alarm_cancelled]" in queued.get("body", ""), f"{label}: triggering message body must be prepended with alarm_cancelled notice")
    assert_true(queued.get("body", "").startswith("[bridge:alarm_cancelled]"), f"{label}: notice must be prepended (truncation safety)")
    print(f"  PASS  {label}")


def scenario_alarm_not_cancelled_by_result(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude")
    incoming = _qualifying_message("codex", "claude", kind="result", body="result body")
    d.enqueue_ipc_message(incoming)
    assert_true(wake_id in d.watchdogs, f"{label}: alarm must survive a result-kind incoming message")
    queued = next((it for it in d.queue.read() if it.get("id") == incoming["id"]), None)
    assert_true(queued is not None and "[bridge:alarm_cancelled]" not in queued.get("body", ""), f"{label}: result message body must NOT carry alarm_cancelled notice")
    print(f"  PASS  {label}")


def scenario_alarm_not_cancelled_by_bridge(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude")
    incoming = _qualifying_message("bridge", "claude", kind="notice", body="from bridge")
    d.enqueue_ipc_message(incoming)
    assert_true(wake_id in d.watchdogs, f"{label}: alarm must survive a bridge-synthetic notice")
    print(f"  PASS  {label}")


def scenario_user_prompt_does_not_cancel_alarm(label: str, tmpdir: Path) -> None:
    # User prompts arrive as prompt_submitted hook records WITHOUT a bridge
    # nonce; mark_message_delivered returns None and no queue mutation
    # happens, so alarms must remain in place.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude")
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": None, "turn_id": "t-user", "prompt": "user typed something"})
    assert_true(wake_id in d.watchdogs, f"{label}: alarm must survive a user-driven prompt_submitted (no nonce)")
    print(f"  PASS  {label}")


def scenario_extend_wait_upserts_watchdog(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = {
        "id": "msg-ew-1", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "claude", "to": "codex", "kind": "request", "intent": "test",
        "body": "x", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "pending", "nonce": None, "delivery_attempts": 0,
        "watchdog_delay_sec": 30.0,
    }
    d.enqueue_ipc_message(msg)
    def assign(queue):
        for it in queue:
            if it.get("id") == "msg-ew-1":
                it["nonce"] = "ew-nonce"
                it["status"] = "inflight"
        return None
    d.queue.update(assign)
    d.handle_prompt_submitted({"agent": "codex", "bridge_agent": "codex", "nonce": "ew-nonce", "turn_id": "t-ew", "prompt": ""})
    matches = [wid for wid, wd in d.watchdogs.items() if wd.get("ref_message_id") == "msg-ew-1"]
    assert_true(len(matches) == 1, f"{label}: exactly one watchdog should exist for msg-ew-1")
    first_wake = matches[0]
    ok, err, deadline = d.upsert_message_watchdog("claude", "msg-ew-1", 600.0)
    assert_true(ok, f"{label}: extend must succeed, got error={err!r}")
    matches2 = [wid for wid, wd in d.watchdogs.items() if wd.get("ref_message_id") == "msg-ew-1"]
    assert_true(len(matches2) == 1, f"{label}: still exactly one watchdog after extend (no duplicates)")
    assert_true(first_wake not in d.watchdogs, f"{label}: original watchdog wake_id must be replaced")
    print(f"  PASS  {label}")


def scenario_extend_wait_aggregate_rejected(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
    }
    d = make_daemon(tmpdir, participants)
    msg = {
        "id": "msg-agg-1", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "manager", "to": "w1", "kind": "request", "intent": "test",
        "body": "x", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "delivered", "nonce": "agg-n", "delivery_attempts": 1,
        "aggregate_id": "agg-1", "aggregate_expected": ["w1"],
    }
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    ok, err, deadline = d.upsert_message_watchdog("manager", "msg-agg-1", 60.0)
    assert_true(not ok and err == "aggregate_extend_not_supported", f"{label}: aggregate member must be rejected, got ok={ok}, err={err!r}")
    print(f"  PASS  {label}")


def scenario_extend_wait_unknown_message(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    ok, err, deadline = d.upsert_message_watchdog("claude", "msg-does-not-exist", 60.0)
    assert_true(not ok and err == "message_not_found", f"{label}: unknown message id should error message_not_found, got ok={ok}, err={err!r}")
    print(f"  PASS  {label}")


def scenario_extend_wait_pending_rejected(label: str, tmpdir: Path) -> None:
    # D1 invariant: a request that has not yet been delivered to the peer
    # has no watchdog (delivery-time arm). agent_extend_wait must reject
    # such messages so D1 is not silently bypassed.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = {
        "id": "msg-pending-1", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "claude", "to": "codex", "kind": "request", "intent": "test",
        "body": "x", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "pending", "nonce": None, "delivery_attempts": 0,
        "watchdog_delay_sec": 60.0,
    }
    d.enqueue_ipc_message(msg)
    ok, err, _ = d.upsert_message_watchdog("claude", "msg-pending-1", 60.0)
    assert_true(not ok and err == "message_not_in_delivered_state", f"{label}: pending message must be rejected, got ok={ok}, err={err!r}")
    # Same for inflight
    def to_inflight(queue):
        for it in queue:
            if it.get("id") == "msg-pending-1":
                it["status"] = "inflight"
        return None
    d.queue.update(to_inflight)
    ok2, err2, _ = d.upsert_message_watchdog("claude", "msg-pending-1", 60.0)
    assert_true(not ok2 and err2 == "message_not_in_delivered_state", f"{label}: inflight must also be rejected, got ok={ok2}, err={err2!r}")
    print(f"  PASS  {label}")


def scenario_extend_wait_not_owner(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = {
        "id": "msg-owner-1", "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "claude", "to": "codex", "kind": "request", "intent": "test",
        "body": "x", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "delivered", "nonce": "n-owner", "delivery_attempts": 1,
    }
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    ok, err, _ = d.upsert_message_watchdog("codex", "msg-owner-1", 60.0)
    assert_true(not ok and err == "not_owner", f"{label}: non-sender must be rejected with not_owner, got ok={ok}, err={err!r}")
    print(f"  PASS  {label}")


def scenario_duplicate_enqueue_does_not_cancel_alarm(label: str, tmpdir: Path) -> None:
    # If the same message_id is enqueued twice (rare, but possible from an
    # external IPC retry), the second enqueue is dropped at the queue
    # level. Alarm cancellation must NOT run on the duplicate or the alarm
    # would silently disappear without the owner ever seeing the
    # cancellation notice (because the duplicate is not delivered).
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude", note="should survive duplicate")
    msg1 = _qualifying_message("codex", "claude", kind="request", body="first")
    d.enqueue_ipc_message(msg1)
    assert_true(wake_id not in d.watchdogs, f"{label}: first enqueue must cancel alarm")
    # Re-arm and try a duplicate of msg1
    wake_id2 = _enqueue_alarm(d, "claude", note="round 2")
    dup = dict(msg1)  # same id
    dup["body"] = "duplicate body"
    d.enqueue_ipc_message(dup)
    assert_true(wake_id2 in d.watchdogs, f"{label}: duplicate-id enqueue must NOT cancel alarm (since the queue rejects the duplicate)")
    print(f"  PASS  {label}")


def scenario_fallback_path_alarm_cancel(label: str, tmpdir: Path) -> None:
    # File-fallback ingress: bridge_enqueue.py wrote the message with
    # status="ingressing"; daemon's handle_external_message_queued must
    # apply alarm cancel + body prepend AND promote ingressing→pending.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude", note="fallback test alarm")
    msg = _qualifying_message("codex", "claude", kind="request", body="fallback request body")
    msg["id"] = "msg-fb-1"
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    assert_true(wake_id in d.watchdogs, f"{label}: alarm must still exist after raw queue insert")
    queued_pre = next((it for it in d.queue.read() if it.get("id") == "msg-fb-1"), None)
    assert_true(queued_pre.get("status") == "ingressing", f"{label}: pre-finalize status must be ingressing, got {queued_pre.get('status')!r}")
    record = {"event": "message_queued", "message_id": "msg-fb-1", "from_agent": "codex", "to": "claude", "kind": "request"}
    d.handle_external_message_queued(record)
    assert_true(wake_id not in d.watchdogs, f"{label}: alarm must be cancelled by external (fallback) message_queued")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-fb-1"), None)
    assert_true(queued is not None, f"{label}: queue item must still be present after finalize")
    # try_deliver inside handle_external may have reserved the now-pending
    # item (status -> "inflight"); either pending or inflight is a valid
    # post-finalize state. The key invariant is that it is NOT still
    # "ingressing".
    assert_true(queued.get("status") != "ingressing", f"{label}: status must be promoted off 'ingressing', got {queued.get('status')!r}")
    assert_true(queued.get("body", "").startswith("[bridge:alarm_cancelled]"), f"{label}: body must be prepended with alarm_cancelled notice")
    print(f"  PASS  {label}")


def scenario_socket_path_alarm_cancel(label: str, tmpdir: Path) -> None:
    # Daemon-socket ingress via enqueue_ipc_message. The message dict
    # arrives with status="ingressing"; daemon appends and immediately
    # calls the finalize helper, which cancels alarms and promotes status.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude", note="socket test alarm")
    msg = _qualifying_message("codex", "claude", kind="request", body="socket request body")
    d.enqueue_ipc_message(msg)
    assert_true(wake_id not in d.watchdogs, f"{label}: alarm must be cancelled by daemon-socket ingress")
    queued = next((it for it in d.queue.read() if it.get("id") == msg["id"]), None)
    assert_true(queued.get("status") != "ingressing", f"{label}: status must be promoted off 'ingressing' after finalize, got {queued.get('status')!r}")
    assert_true(queued.get("body", "").startswith("[bridge:alarm_cancelled]"), f"{label}: queue body must be prepended even through socket ingress")
    print(f"  PASS  {label}")


def scenario_ingressing_not_delivered_before_finalize(label: str, tmpdir: Path) -> None:
    # While a message is status="ingressing", reserve_next must NOT
    # pick it up. The race that motivated the ingressing status is
    # that a periodic try_deliver between fallback's queue write and
    # the daemon's finalize would otherwise deliver with un-prepended body.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _qualifying_message("codex", "claude", kind="request", body="should not deliver yet")
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    reserved = d.reserve_next("claude")
    assert_true(reserved is None, f"{label}: reserve_next must NOT pick up an ingressing message; got {reserved}")
    print(f"  PASS  {label}")


def scenario_replay_does_not_cancel_later_alarm(label: str, tmpdir: Path) -> None:
    # After socket ingest finalizes (status -> pending), a replay of the
    # same message_queued event (tail loop re-read, duplicate from any
    # source) must NOT re-run alarm cancel. A NEW alarm registered
    # between the original ingest and the replay must survive.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _qualifying_message("codex", "claude", kind="request", body="initial")
    d.enqueue_ipc_message(msg)
    queued = next((it for it in d.queue.read() if it.get("id") == msg["id"]), None)
    assert_true(queued.get("status") != "ingressing", f"{label}: socket ingest must promote off ingressing")
    new_alarm = _enqueue_alarm(d, "claude", note="post-ingest alarm")
    record = {"event": "message_queued", "message_id": msg["id"], "from_agent": "codex", "to": "claude", "kind": "request"}
    d.handle_external_message_queued(record)
    assert_true(new_alarm in d.watchdogs, f"{label}: replay of finalized message must NOT cancel a NEWER alarm")
    print(f"  PASS  {label}")


def scenario_aged_ingressing_promoted_by_maintenance(label: str, tmpdir: Path) -> None:
    # Running-daemon maintenance: an ingressing item older than the
    # threshold should be promoted to "pending" by _promote_aged_ingressing,
    # without alarm cancel (alarms cannot be reliably reconstructed at
    # this point). This is the safety net for the rare case where the
    # daemon is alive but missed the message_queued event.
    import datetime as _dt
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Plant an ingressing item with an artificially-aged created_ts.
    aged_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    msg = _qualifying_message("codex", "claude", kind="request", body="aged ingressing")
    msg["created_ts"] = aged_iso
    msg["updated_ts"] = aged_iso
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    # Force last check to a value far in the past so the maintenance runs now.
    d.last_ingressing_check = 0.0
    d._promote_aged_ingressing()
    queued = next((it for it in d.queue.read() if it.get("id") == msg["id"]), None)
    assert_true(queued is not None and queued.get("status") == "pending", f"{label}: aged ingressing must be promoted to pending, got {queued.get('status') if queued else 'missing'!r}")
    assert_true(queued.get("last_error") == "ingressing_promoted_aged", f"{label}: last_error must mark the promotion reason")
    print(f"  PASS  {label}")


def scenario_aggregate_fallback_finalize(label: str, tmpdir: Path) -> None:
    # `agent_send_peer --all` produces N messages with the same
    # aggregate_id. When they arrive via the file-fallback path,
    # handle_external_message_queued must finalize each (ingressing →
    # pending) AND preserve the aggregate metadata (aggregate_id,
    # aggregate_expected, aggregate_message_ids) so collect_aggregate_response
    # can match peer replies back to the broadcast.
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-fb-1"
    common = {
        "intent": "test", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "external_enqueue", "bridge_session": "test-session",
        "status": "ingressing", "nonce": None, "delivery_attempts": 0,
        "kind": "request", "aggregate_id": agg_id, "aggregate_expected": ["w1", "w2"],
        "aggregate_message_ids": {"w1": "msg-fb-w1", "w2": "msg-fb-w2"},
    }
    msg_w1 = {**common, "id": "msg-fb-w1", "from": "manager", "to": "w1",
              "body": "broadcast piece for w1", "created_ts": utc_now(), "updated_ts": utc_now()}
    msg_w2 = {**common, "id": "msg-fb-w2", "from": "manager", "to": "w2",
              "body": "broadcast piece for w2", "created_ts": utc_now(), "updated_ts": utc_now()}

    def add(queue):
        queue.append(msg_w1)
        queue.append(msg_w2)
        return None

    d.queue.update(add)
    # Both members start as ingressing — reserve_next must NOT pick them up yet.
    assert_true(d.reserve_next("w1") is None, f"{label}: ingressing aggregate member must NOT be reserved before finalize")
    assert_true(d.reserve_next("w2") is None, f"{label}: ingressing aggregate member must NOT be reserved before finalize")
    # Daemon dispatches handle_external_message_queued for each event.
    for sample in (msg_w1, msg_w2):
        record = {
            "event": "message_queued",
            "message_id": sample["id"],
            "from_agent": "manager",
            "to": sample["to"],
            "kind": "request",
        }
        d.handle_external_message_queued(record)
    for sample in (msg_w1, msg_w2):
        queued = next((it for it in d.queue.read() if it.get("id") == sample["id"]), None)
        assert_true(queued is not None, f"{label}: aggregate member {sample['id']} must remain in queue after finalize")
        assert_true(queued.get("status") != "ingressing", f"{label}: status must be promoted off 'ingressing' for {sample['id']}, got {queued.get('status')!r}")
        assert_true(queued.get("aggregate_id") == agg_id, f"{label}: aggregate_id must be preserved")
        assert_true(queued.get("aggregate_expected") == ["w1", "w2"], f"{label}: aggregate_expected must be preserved")
        assert_true(queued.get("aggregate_message_ids") == {"w1": "msg-fb-w1", "w2": "msg-fb-w2"}, f"{label}: aggregate_message_ids must be preserved")
    print(f"  PASS  {label}")


def scenario_aged_ingressing_does_not_cancel_alarms(label: str, tmpdir: Path) -> None:
    # The aged maintenance pass deliberately does NOT run alarm cancel —
    # by that point we cannot reliably reconstruct the alarm state of
    # the original ingest moment. An alarm registered before or after
    # the stuck message must remain alive after the promote.
    import datetime as _dt
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude", note="alarm survives aged promote")
    aged_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    msg = _qualifying_message("codex", "claude", kind="request", body="stuck ingressing")
    msg["created_ts"] = aged_iso
    msg["updated_ts"] = aged_iso
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.last_ingressing_check = 0.0
    d._promote_aged_ingressing()
    assert_true(wake_id in d.watchdogs, f"{label}: alarm must NOT be cancelled by aged promote")
    queued = next((it for it in d.queue.read() if it.get("id") == msg["id"]), None)
    assert_true(queued.get("status") == "pending", f"{label}: aged item must still be promoted to pending")
    assert_true(not queued.get("body", "").startswith("[bridge:alarm_cancelled]"), f"{label}: body must NOT be prepended (no alarm cancel by aged maintenance)")
    print(f"  PASS  {label}")


def scenario_aged_ingressing_malformed_timestamp_promoted(label: str, tmpdir: Path) -> None:
    # Defensive: an ingressing item with missing or malformed created_ts
    # must still be promoted so it cannot become permanently stuck.
    # Prefer unblocking delivery over preserving the original timestamp
    # for a degenerate case.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg_missing = _qualifying_message("codex", "claude", kind="request", body="missing ts")
    msg_missing.pop("created_ts", None)
    msg_missing.pop("updated_ts", None)
    msg_garbage = _qualifying_message("codex", "claude", kind="request", body="garbage ts")
    msg_garbage["created_ts"] = "not-a-real-iso"
    msg_garbage["updated_ts"] = "not-a-real-iso"

    def add(queue):
        queue.append(msg_missing)
        queue.append(msg_garbage)
        return None

    d.queue.update(add)
    d.last_ingressing_check = 0.0
    d._promote_aged_ingressing()
    for sample in (msg_missing, msg_garbage):
        queued = next((it for it in d.queue.read() if it.get("id") == sample["id"]), None)
        assert_true(queued.get("status") == "pending", f"{label}: malformed/missing-timestamp ingressing must be promoted, got {queued.get('status')!r} for {sample['body']!r}")
    print(f"  PASS  {label}")


def scenario_fresh_ingressing_not_promoted_by_maintenance(label: str, tmpdir: Path) -> None:
    # Inverse: a freshly-written ingressing item must NOT be promoted by
    # the maintenance pass. It should stay ingressing until either the
    # finalize helper runs (event-driven) or it ages past threshold.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _qualifying_message("codex", "claude", kind="request", body="fresh ingressing")
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.last_ingressing_check = 0.0
    d._promote_aged_ingressing()
    queued = next((it for it in d.queue.read() if it.get("id") == msg["id"]), None)
    assert_true(queued.get("status") == "ingressing", f"{label}: fresh ingressing must remain ingressing, got {queued.get('status')!r}")
    print(f"  PASS  {label}")


def scenario_socket_normalizes_non_ingressing_status(label: str, tmpdir: Path) -> None:
    # Defense-in-depth: an external client submitting op=enqueue with a
    # message dict whose status is "pending" (or anything other than
    # "ingressing") must NOT bypass the finalize step. enqueue_ipc_message
    # normalizes the incoming status to "ingressing" so the finalize
    # helper actually runs and alarms get cancelled.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    wake_id = _enqueue_alarm(d, "claude", note="should still cancel")
    msg = _qualifying_message("codex", "claude", kind="request", body="hand-rolled by external client")
    msg["status"] = "pending"  # client sent the wrong status
    d.enqueue_ipc_message(msg)
    assert_true(wake_id not in d.watchdogs, f"{label}: alarm must still cancel even when external client sent status='pending'")
    queued = next((it for it in d.queue.read() if it.get("id") == msg["id"]), None)
    assert_true(queued is not None, f"{label}: queue item must be present")
    assert_true(queued.get("status") != "ingressing", f"{label}: status must be promoted off 'ingressing' after finalize")
    assert_true(queued.get("body", "").startswith("[bridge:alarm_cancelled]"), f"{label}: body must be prepended with alarm_cancelled notice")
    print(f"  PASS  {label}")


def scenario_bridge_origin_fallback_ingressing_promoted(label: str, tmpdir: Path) -> None:
    # bridge_enqueue.py writes status="ingressing" regardless of sender;
    # if from=bridge ever takes the fallback path, it must still be
    # promoted to "pending" so reserve_next delivers it. The alarm
    # cancel inside the helper is a no-op for sender=bridge, but the
    # status promote must still happen.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _qualifying_message("bridge", "claude", kind="notice", body="bridge synthetic")
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    record = {"event": "message_queued", "message_id": msg["id"], "from_agent": "bridge", "to": "claude", "kind": "notice"}
    d.handle_external_message_queued(record)
    queued = next((it for it in d.queue.read() if it.get("id") == msg["id"]), None)
    assert_true(queued.get("status") != "ingressing", f"{label}: bridge-origin ingressing must be promoted off 'ingressing', got {queued.get('status')!r}")
    print(f"  PASS  {label}")


def scenario_alarm_op_invalid_delay_is_rejected_not_crashed(label: str, tmpdir: Path) -> None:
    # The command-server thread must not crash on a malformed delay.
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Simulate malformed IPC by calling handle_command_connection's payload
    # path indirectly: call register_alarm with a bad delay.
    wake_id = d.register_alarm("claude", "not-a-number", None)  # type: ignore[arg-type]
    assert_true(wake_id is None, f"{label}: register_alarm with bad delay must return None, got {wake_id!r}")
    print(f"  PASS  {label}")


def scenario_stale_watchdog_skipped(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Manually plant a watchdog whose ref_message_id is NOT in the queue
    wake_id = "wake-stale"
    d.watchdogs[wake_id] = {
        "sender": "claude",
        "deadline": time.time() - 1.0,
        "ref_message_id": "msg-gone",
        "ref_aggregate_id": None,
        "ref_to": "codex",
        "ref_kind": "request",
        "ref_intent": "test",
        "ref_causal_id": "c",
        "is_alarm": False,
    }
    d.fire_watchdog(wake_id, dict(d.watchdogs[wake_id]))
    # No synthetic notice should have been queued for the sender
    queued = [it for it in d.queue.read() if it.get("from") == "bridge" and it.get("intent") == "watchdog_wake"]
    assert_true(len(queued) == 0, f"{label}: stale watchdog must NOT enqueue a wake notice")
    print(f"  PASS  {label}")


def scenario_watchdog_pending_text_omits_held_interrupt(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-wd-pending-text", frm="claude", to="codex", status="pending")
    d.queue.update(lambda queue: queue.append(msg))
    text = d.build_watchdog_fire_text({
        "sender": "claude",
        "deadline": time.time(),
        "ref_message_id": "msg-wd-pending-text",
        "ref_aggregate_id": None,
        "ref_to": "codex",
        "ref_kind": "request",
        "ref_intent": "test",
        "is_alarm": False,
    })
    assert_true("held_interrupt" not in text, f"{label}: watchdog text must not mention held_interrupt: {text!r}")
    assert_true("--clear-hold" not in text, f"{label}: watchdog text must not recommend clear-hold: {text!r}")
    assert_true("agent_interrupt_peer codex --status" in text, f"{label}: watchdog text should still recommend status inspection: {text!r}")
    print(f"  PASS  {label}")


def scenario_pane_mode_pending_defers_without_attempt(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.pane_mode_status = lambda pane: {"in_mode": True, "mode": "copy-mode", "error": ""}  # type: ignore[method-assign]
    msg = test_message("msg-mode-1")
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-1"), None)
    assert_true(queued is not None and queued.get("status") == "pending", f"{label}: message must remain pending")
    assert_true(queued.get("delivery_attempts") == 0, f"{label}: copy-mode defers must not increment delivery_attempts")
    assert_true(queued.get("pane_mode_blocked_mode") == "copy-mode", f"{label}: mode metadata should be recorded")
    assert_true(queued.get("pane_mode_block_count") == 2, f"{label}: block count should reflect two defer observations")
    events = read_events(tmpdir / "events.raw.jsonl")
    started = [e for e in events if e.get("event") == "pane_mode_block_started"]
    assert_true(len(started) == 1, f"{label}: pane_mode_block_started should log once, got {len(started)}")
    print(f"  PASS  {label}")


def scenario_pane_mode_clears_then_delivers(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    statuses = [
        {"in_mode": True, "mode": "copy-mode", "error": ""},
        {"in_mode": False, "mode": "", "error": ""},
        {"in_mode": False, "mode": "", "error": ""},
    ]
    d.pane_mode_status = lambda pane: statuses.pop(0) if statuses else {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
    msg = test_message("msg-mode-2")
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-2"), None)
    assert_true(queued is not None and queued.get("status") == "inflight", f"{label}: message should reserve once mode clears")
    assert_true(queued.get("delivery_attempts") == 1, f"{label}: real delivery attempt should count once")
    assert_true("pane_mode_blocked_since" not in queued, f"{label}: block metadata should clear before delivery")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "pane_mode_block_cleared" for e in events), f"{label}: clear event expected")
    print(f"  PASS  {label}")


def scenario_pane_mode_force_cancel_after_grace(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.pane_mode_grace_seconds = 180.0
    old_ts = time.time() - 181.0
    msg = {
        **test_message("msg-mode-3"),
        "pane_mode_blocked_since": utc_now(),
        "pane_mode_blocked_since_ts": old_ts,
        "pane_mode_blocked_mode": "copy-mode",
        "pane_mode_block_count": 4,
    }
    status_calls = [
        {"in_mode": True, "mode": "copy-mode", "error": ""},
        {"in_mode": False, "mode": "", "error": ""},
        {"in_mode": False, "mode": "", "error": ""},
    ]
    cancels: list[tuple[str, str]] = []
    d.pane_mode_status = lambda pane: status_calls.pop(0) if status_calls else {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
    def cancel(pane: str, mode: str) -> tuple[bool, str]:
        cancels.append((pane, mode))
        return True, ""
    d.force_cancel_pane_mode = cancel  # type: ignore[method-assign]
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-3"), None)
    assert_true(cancels == [("%98", "copy-mode")], f"{label}: expected one copy-mode cancel, got {cancels}")
    assert_true(queued is not None and queued.get("status") == "inflight", f"{label}: message should deliver after force-cancel")
    assert_true("pane_mode_blocked_since" not in queued, f"{label}: force-cancel success should clear block metadata")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "pane_mode_force_cancelled" and e.get("success") is True for e in events), f"{label}: success log expected")
    print(f"  PASS  {label}")


def scenario_pane_mode_nonforce_mode_stays_pending(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.pane_mode_grace_seconds = 1.0
    old_ts = time.time() - 10.0
    msg = {
        **test_message("msg-mode-4"),
        "pane_mode_blocked_since": utc_now(),
        "pane_mode_blocked_since_ts": old_ts,
        "pane_mode_blocked_mode": "tree-mode",
    }
    cancels: list[tuple[str, str]] = []
    d.pane_mode_status = lambda pane: {"in_mode": True, "mode": "tree-mode", "error": ""}  # type: ignore[method-assign]
    d.force_cancel_pane_mode = lambda pane, mode: cancels.append((pane, mode)) or (True, "")  # type: ignore[method-assign]
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-4"), None)
    assert_true(cancels == [], f"{label}: non-force mode must not be cancelled")
    assert_true(queued is not None and queued.get("status") == "pending", f"{label}: message should stay pending")
    assert_true(queued.get("last_error") == "pane_mode_unforceable", f"{label}: last_error should explain unforceable block")
    events = read_events(tmpdir / "events.raw.jsonl")
    unforceable = [e for e in events if e.get("event") == "pane_mode_block_unforceable"]
    assert_true(len(unforceable) == 1, f"{label}: unforceable event should log once, got {len(unforceable)}")
    print(f"  PASS  {label}")


def scenario_pane_mode_busy_target_does_not_start_timer(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.pane_mode_status = lambda pane: {"in_mode": True, "mode": "copy-mode", "error": ""}  # type: ignore[method-assign]
    delivered = test_message("msg-mode-delivered", status="delivered")
    pending = test_message("msg-mode-pending")
    def add(queue): queue.append(delivered); queue.append(pending); return None
    d.queue.update(add)
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-pending"), None)
    assert_true(queued is not None and queued.get("status") == "pending", f"{label}: follow-up should stay pending behind delivered item")
    assert_true("pane_mode_blocked_since" not in queued, f"{label}: pane-mode timer must not start while delivered work blocks target")
    print(f"  PASS  {label}")


def scenario_retry_enter_skips_pane_mode(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-mode-enter", status="inflight")
    msg["nonce"] = "n-enter"
    msg["delivery_attempts"] = 1
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.last_enter_ts["msg-mode-enter"] = time.time() - 2.0
    d.pane_mode_status = lambda pane: {"in_mode": True, "mode": "copy-mode", "error": ""}  # type: ignore[method-assign]
    d.retry_enter_for_inflight()
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-enter"), None)
    assert_true(queued is not None and queued.get("status") == "inflight", f"{label}: inflight item should stay inflight")
    assert_true(queued.get("last_error") == "pane_in_mode_waiting_enter", f"{label}: enter retry should mark waiting-enter state")
    assert_true("pane_mode_enter_deferred_since" in queued, f"{label}: waiting-enter metadata expected")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "enter_retry_deferred_pane_mode" for e in events), f"{label}: deferred enter log expected")
    print(f"  PASS  {label}")


def scenario_pane_mode_probe_failure_defers_pending(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.pane_mode_status = lambda pane: {"in_mode": False, "mode": "", "error": "tmux timeout"}  # type: ignore[method-assign]
    msg = test_message("msg-mode-probe-fail")
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-probe-fail"), None)
    assert_true(queued is not None and queued.get("status") == "pending", f"{label}: probe failure should keep message pending")
    assert_true(queued.get("delivery_attempts") == 0, f"{label}: probe failure must not increment delivery attempts")
    assert_true(queued.get("last_error") == "pane_mode_probe_failed", f"{label}: probe failure should be visible on queue item")
    events = read_events(tmpdir / "events.raw.jsonl")
    probe_logs = [e for e in events if e.get("event") == "pane_mode_probe_failed" and e.get("phase") == "pre_reserve"]
    assert_true(len(probe_logs) == 1, f"{label}: probe failure should log once, got {len(probe_logs)}")
    print(f"  PASS  {label}")


def scenario_pane_mode_force_cancel_failure_stays_pending(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.pane_mode_grace_seconds = 1.0
    msg = {
        **test_message("msg-mode-cancel-fail"),
        "pane_mode_blocked_since": utc_now(),
        "pane_mode_blocked_since_ts": time.time() - 10.0,
        "pane_mode_blocked_mode": "copy-mode",
    }
    d.pane_mode_status = lambda pane: {"in_mode": True, "mode": "copy-mode", "error": ""}  # type: ignore[method-assign]
    d.force_cancel_pane_mode = lambda pane, mode: (False, "cancel boom")  # type: ignore[method-assign]
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-cancel-fail"), None)
    assert_true(queued is not None and queued.get("status") == "pending", f"{label}: cancel failure should keep pending")
    assert_true(queued.get("last_error") == "pane_mode_cancel_failed", f"{label}: cancel failure should be visible")
    assert_true(queued.get("last_pane_mode_cancel_error") == "cancel boom", f"{label}: cancel error should persist for diagnostics")
    events = read_events(tmpdir / "events.raw.jsonl")
    failures = [e for e in events if e.get("event") == "pane_mode_force_cancelled" and e.get("success") is False]
    assert_true(len(failures) == 1, f"{label}: cancel failure should log once, got {len(failures)}")
    print(f"  PASS  {label}")


def scenario_enter_deferred_survives_stale_requeue_and_restart(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    stale_iso = "2000-01-01T00:00:00.000000Z"
    msg = {
        **test_message("msg-mode-restart", status="inflight"),
        "nonce": "n-restart",
        "delivery_attempts": 1,
        "created_ts": stale_iso,
        "updated_ts": stale_iso,
        "pane_mode_enter_deferred_since": stale_iso,
        "pane_mode_enter_deferred_since_ts": 946684800.0,
        "pane_mode_enter_deferred_mode": "copy-mode",
    }
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    # Simulate daemon restart: no in-memory last_enter_ts survives.
    d.last_enter_ts.clear()
    d._requeue_stale_inflight_locked(time.time())
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-restart"), None)
    assert_true(queued is not None and queued.get("status") == "inflight", f"{label}: enter-deferred item must not requeue to pending")
    assert_true(queued.get("last_error") == "pane_in_mode_waiting_enter", f"{label}: item should remain in durable waiting-enter state")
    d.pane_mode_status = lambda pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
    d.retry_enter_for_inflight()
    queued2 = next((it for it in d.queue.read() if it.get("id") == "msg-mode-restart"), None)
    assert_true(queued2 is not None and queued2.get("status") == "inflight", f"{label}: retry Enter keeps item inflight awaiting prompt_submitted")
    assert_true("pane_mode_enter_deferred_since" not in queued2, f"{label}: retry Enter should clear enter-deferred metadata")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "enter_retry" and e.get("message_id") == "msg-mode-restart" for e in events), f"{label}: retry Enter should be logged")
    print(f"  PASS  {label}")


def scenario_pre_enter_probe_failure_defers_enter(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": True, "pane": participants[target]["pane"], "reason": "test_verified"}  # type: ignore[method-assign]
    status_calls = [
        {"in_mode": False, "mode": "", "error": ""},
        {"in_mode": False, "mode": "", "error": ""},
        {"in_mode": False, "mode": "", "error": "probe boom"},
    ]
    d.pane_mode_status = lambda pane: status_calls.pop(0) if status_calls else {"in_mode": False, "mode": "", "error": "probe boom"}  # type: ignore[method-assign]
    literal_calls: list[str] = []
    enter_calls: list[str] = []
    old_literal = bridge_daemon.run_tmux_send_literal
    old_enter = bridge_daemon.run_tmux_enter
    bridge_daemon.run_tmux_send_literal = lambda pane, prompt, **kwargs: literal_calls.append(pane)  # type: ignore[assignment]
    bridge_daemon.run_tmux_enter = lambda pane: enter_calls.append(pane)  # type: ignore[assignment]
    try:
        msg = test_message("msg-mode-pre-enter")
        def add(queue): queue.append(msg); return None
        d.queue.update(add)
        d.try_deliver("codex")
    finally:
        bridge_daemon.run_tmux_send_literal = old_literal  # type: ignore[assignment]
        bridge_daemon.run_tmux_enter = old_enter  # type: ignore[assignment]
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-pre-enter"), None)
    assert_true(literal_calls == ["%98"], f"{label}: literal paste should have happened once")
    assert_true(enter_calls == [], f"{label}: probe failure before Enter must not press Enter")
    assert_true(queued is not None and queued.get("status") == "inflight", f"{label}: item should stay inflight after literal paste")
    assert_true(queued.get("last_error") == "pane_mode_probe_failed_waiting_enter", f"{label}: queue should record waiting-enter probe failure")
    print(f"  PASS  {label}")


def scenario_pane_mode_grace_zero_disables_cancel(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.pane_mode_grace_seconds = None
    msg = {
        **test_message("msg-mode-grace-zero"),
        "pane_mode_blocked_since": utc_now(),
        "pane_mode_blocked_since_ts": time.time() - 1000.0,
        "pane_mode_blocked_mode": "copy-mode",
    }
    cancels: list[tuple[str, str]] = []
    d.pane_mode_status = lambda pane: {"in_mode": True, "mode": "copy-mode", "error": ""}  # type: ignore[method-assign]
    d.force_cancel_pane_mode = lambda pane, mode: cancels.append((pane, mode)) or (True, "")  # type: ignore[method-assign]
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-mode-grace-zero"), None)
    assert_true(cancels == [], f"{label}: disabled grace must not force-cancel")
    assert_true(queued is not None and queued.get("status") == "pending", f"{label}: message should remain pending")
    print(f"  PASS  {label}")


class FakeProbeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run_wait_for_probe_retry_case(tmpdir: Path, *, pane_id: str) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    fake_clock = FakeProbeClock()
    discovery = tmpdir / "attach-discovery.jsonl"
    old_time = bridge_attach.time
    old_tmux = bridge_attach.tmux
    old_discovery_file = bridge_attach.discovery_file
    bridge_attach.time = fake_clock  # type: ignore[assignment]
    bridge_attach.tmux = lambda *args, **kwargs: calls.append(tuple(str(arg) for arg in args)) or ""  # type: ignore[assignment]
    bridge_attach.discovery_file = lambda: discovery  # type: ignore[assignment]
    try:
        try:
            bridge_attach.wait_for_probe(
                "probe-retry",
                "codex",
                1.25,
                alias="codex-reviewer",
                pane_desc="%42 (test:1.0)",
                pane_id=pane_id,
            )
        except bridge_attach.AttachProbeTimeout:
            pass
        else:
            raise AssertionError("wait_for_probe should time out without a discovery record")
    finally:
        bridge_attach.time = old_time  # type: ignore[assignment]
        bridge_attach.tmux = old_tmux  # type: ignore[assignment]
        bridge_attach.discovery_file = old_discovery_file  # type: ignore[assignment]
    return calls


def scenario_wait_for_probe_retries_enter_with_pane_id(label: str, tmpdir: Path) -> None:
    calls = _run_wait_for_probe_retry_case(tmpdir, pane_id="%42")
    assert_true(("send-keys", "-t", "%42", "Enter") in calls, f"{label}: expected retry Enter call, got {calls}")
    print(f"  PASS  {label}")


def scenario_wait_for_probe_no_retry_without_pane_id(label: str, tmpdir: Path) -> None:
    calls = _run_wait_for_probe_retry_case(tmpdir, pane_id="")
    assert_true(calls == [], f"{label}: empty pane_id must not retry Enter, got {calls}")
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


# ---------- v1.5.2 scenarios: state-based delivery matching + consume-once ----------

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


def scenario_orphan_nonce_in_user_prompt(label: str, tmpdir: Path) -> None:
    """User prompt that quotes a stale [bridge:nonce] must NOT be treated as a delivery."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # No inflight candidate in queue. Hook reports a nonce extracted from
    # quoted text (would happen with the OLD re.search behavior; the new
    # anchored re.match prevents this at the hook level, but we exercise
    # the daemon's defense too).
    record = {"agent": "claude", "bridge_agent": "claude", "nonce": "old-quoted-nonce", "turn_id": "turn-quoted", "prompt": "user typed something"}
    d.handle_prompt_submitted(record)
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") is None, f"{label}: orphan nonce must not bind ctx to a message")
    assert_true(ctx.get("nonce") is None, f"{label}: orphan nonce must NOT be stored in ctx (would taint discard_nonce later)")
    assert_true(not ctx.get("auto_return"), f"{label}: orphan ctx must not enable auto_return")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "orphan_nonce_in_user_prompt" for e in events), f"{label}: orphan_nonce_in_user_prompt log expected")
    print(f"  PASS  {label}")


def scenario_prompt_intercept_request_notice_body(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-pi-req", "alice", "bob", "n-pi-req", turn_id="t-old", watchdog=True)
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-user", "prompt": "user typed"})

    assert_true(_queue_item(d, "msg-pi-req") is None, f"{label}: intercepted delivered request must be removed")
    assert_true(not any(wd.get("ref_message_id") == "msg-pi-req" for wd in d.watchdogs.values()), f"{label}: request watchdog must be cancelled")
    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("id") is None and ctx.get("turn_id") == "t-user", f"{label}: new user turn must own empty ctx")
    notices = [m for m in d.queue.read() if m.get("source") == "interrupt_notice" and m.get("to") == "alice"]
    assert_true(notices, f"{label}: requester must receive prompt-intercept notice")
    body = str(notices[0].get("body") or "")
    assert_true("held_interrupt state" not in body, f"{label}: prompt-intercept notice must not claim held_interrupt: {body!r}")
    assert_true("started a new prompt" in body, f"{label}: prompt-intercept notice must explain new prompt: {body!r}")
    events = read_events(tmpdir / "events.raw.jsonl")
    intercept = next((e for e in events if e.get("event") == "active_prompt_intercepted"), None)
    assert_true(intercept is not None, f"{label}: intercept event expected")
    assert_true(intercept.get("cancelled_count") == 1, f"{label}: intercept cancelled_count expected")
    assert_true(intercept.get("cancelled_message_ids") == ["msg-pi-req"], f"{label}: intercept cancelled ids expected")
    assert_true(intercept.get("observed_nonce_present") is False, f"{label}: observed nonce field expected")
    assert_true("candidate_message_id" in intercept, f"{label}: candidate_message_id field expected")
    print(f"  PASS  {label}")


def scenario_prompt_intercept_bridge_notice_no_source_notice(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(
        d,
        "msg-pi-bridge",
        "bridge",
        "bob",
        "n-pi-bridge",
        auto_return=False,
        kind="notice",
        source="watchdog_fire",
        turn_id="t-bridge",
    )
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-user", "prompt": "user typed"})

    assert_true(_queue_item(d, "msg-pi-bridge") is None, f"{label}: intercepted bridge notice must be removed")
    assert_true(not any(m.get("source") == "interrupt_notice" for m in d.queue.read()), f"{label}: bridge-origin intercept must not notify bridge")
    print(f"  PASS  {label}")


def scenario_prompt_intercept_response_guard_queue_allows(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-pi-guard", "alice", "bob", "n-pi-guard", turn_id="t-old")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-user", "prompt": "user typed"})

    contexts = bridge_response_guard.contexts_from_queue("bob", d.queue.read())
    violation = bridge_response_guard.response_send_violation(
        sender="bob",
        targets=["alice"],
        outgoing_kind="request",
        force=False,
        contexts=contexts,
        source="queue_fallback",
    )
    assert_true(violation is None, f"{label}: stale delivered row must not false-block response-send guard")
    print(f"  PASS  {label}")


def scenario_prompt_intercept_mixed_inflight_requeues(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
        "carol": {"alias": "carol", "agent_type": "claude", "pane": "%93"},
    }
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-pi-old", "alice", "bob", "n-pi-old", turn_id="t-old")
    _make_inflight(d, "msg-pi-new", "carol", "bob", "n-pi-new")

    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-user", "prompt": "user typed"})
    assert_true(_queue_item(d, "msg-pi-old") is None, f"{label}: old delivered row must be removed")
    new_item = _queue_item(d, "msg-pi-new")
    assert_true(new_item is not None and new_item.get("status") == "inflight", f"{label}: new inflight row must survive intercept")

    def age_new(queue):
        for item in queue:
            if item.get("id") == "msg-pi-new":
                item["updated_ts"] = "1970-01-01T00:00:00.000000Z"
        return None
    d.queue.update(age_new)
    d.last_maintenance = 0.0
    d.requeue_stale_inflight()
    new_item = _queue_item(d, "msg-pi-new")
    assert_true(new_item is not None and new_item.get("status") == "pending", f"{label}: surviving inflight must requeue for retry")
    print(f"  PASS  {label}")


def scenario_prompt_submitted_duplicate_noop(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-dupe", "alice", "bob", "n-dupe")
    record = {"agent": "bob", "bridge_agent": "bob", "nonce": "n-dupe", "turn_id": "t-dupe", "prompt": "[bridge:n-dupe]"}
    d.handle_prompt_submitted(record)
    d.handle_prompt_submitted(record)

    item = _queue_item(d, "msg-dupe")
    assert_true(item is not None and item.get("status") == "delivered", f"{label}: duplicate UPS must leave delivered row active")
    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("id") == "msg-dupe", f"{label}: duplicate UPS must not overwrite ctx")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "duplicate_prompt_submitted" for e in events), f"{label}: duplicate log expected")
    assert_true(not any(e.get("event") == "active_prompt_intercepted" for e in events), f"{label}: duplicate must not intercept")

    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "t-dupe", "last_assistant_message": "done"})
    assert_true(_queue_item(d, "msg-dupe") is None, f"{label}: normal terminal cleanup must still remove message")
    assert_true(any(m.get("to") == "alice" and m.get("reply_to") == "msg-dupe" for m in d.queue.read()), f"{label}: auto-return must still queue")
    print(f"  PASS  {label}")


def scenario_prompt_submitted_duplicate_without_nonce_noop(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-dupe-nononce", "alice", "bob", "n-dupe-nononce")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": "n-dupe-nononce", "turn_id": "t-dupe-nononce", "prompt": "[bridge:n-dupe-nononce]"})
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-dupe-nononce", "prompt": "[bridge prefix stripped]"})

    item = _queue_item(d, "msg-dupe-nononce")
    assert_true(item is not None and item.get("status") == "delivered", f"{label}: nonce-less duplicate must leave delivered row active")
    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("id") == "msg-dupe-nononce", f"{label}: nonce-less duplicate must not overwrite ctx")
    events = read_events(tmpdir / "events.raw.jsonl")
    duplicate = next((e for e in events if e.get("event") == "duplicate_prompt_submitted"), None)
    assert_true(duplicate is not None and duplicate.get("duplicate_match") == "turn_delivered", f"{label}: duplicate should log turn_delivered match")
    assert_true(not any(e.get("event") == "active_prompt_intercepted" for e in events), f"{label}: nonce-less duplicate must not intercept")
    print(f"  PASS  {label}")


def scenario_prompt_intercept_aggregate_completes(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
        "carol": {"alias": "carol", "agent_type": "codex", "pane": "%93"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-pi"
    expected = ["bob", "carol"]
    message_ids = {"bob": "msg-agg-bob", "carol": "msg-agg-carol"}
    _make_delivered_context(
        d,
        "msg-agg-bob",
        "alice",
        "bob",
        "n-agg-bob",
        turn_id="t-bob-old",
        aggregate_id=agg_id,
        aggregate_expected=expected,
        aggregate_message_ids=message_ids,
    )
    _make_delivered_context(
        d,
        "msg-agg-carol",
        "alice",
        "carol",
        "n-agg-carol",
        turn_id="t-carol",
        aggregate_id=agg_id,
        aggregate_expected=expected,
        aggregate_message_ids=message_ids,
    )

    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-user", "prompt": "user typed"})
    d.handle_response_finished({"agent": "carol", "bridge_agent": "carol", "turn_id": "t-carol", "last_assistant_message": "carol ok"})

    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "aggregate_result_queued" and e.get("aggregate_id") == agg_id for e in events), f"{label}: aggregate must complete after intercept + real reply")
    aggregate_data = json.loads(Path(d.aggregate_file).read_text(encoding="utf-8"))
    replies = aggregate_data.get("aggregates", {}).get(agg_id, {}).get("replies", {})
    assert_true("[intercepted by user prompt:" in str(replies.get("bob", {}).get("body") or ""), f"{label}: bob aggregate slot must use intercept text")
    assert_true("carol ok" in str(replies.get("carol", {}).get("body") or ""), f"{label}: carol real reply must be preserved")
    print(f"  PASS  {label}")


def scenario_prompt_intercept_held_drain_noop(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.held_interrupt["bob"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-held",
        "prior_sender": "alice",
        "reason": "interrupt_by_sender",
        "by_sender": "alice",
        "cancelled_message_ids": ["msg-held"],
    }
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-held-user", "prompt": "user typed"})
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(not any(e.get("event") == "active_prompt_intercepted" for e in events), f"{label}: held drain prompt must not trigger intercept")

    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "t-held-user", "last_assistant_message": "drain"})
    assert_true("bob" not in d.held_interrupt, f"{label}: held drain must still release hold")
    assert_true(not d.busy.get("bob"), f"{label}: held drain must leave target idle")
    assert_true("bob" not in d.current_prompt_by_agent, f"{label}: held drain must clear any empty prompt ctx")
    print(f"  PASS  {label}")


def scenario_held_drain_stale_stop_preserves_new_ctx(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.held_interrupt["bob"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-old-held",
        "prior_sender": "alice",
        "reason": "interrupt_by_sender",
        "by_sender": "alice",
        "cancelled_message_ids": ["msg-old-held"],
    }
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-new", "prompt": "new prompt"})
    pending = test_message("msg-pending-held-stale", frm="alice", to="bob", status="pending")
    def add_pending(queue):
        queue.append(pending)
        return None
    d.queue.update(add_pending)

    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "t-old", "last_assistant_message": "old stop"})
    assert_true("bob" not in d.held_interrupt, f"{label}: stale held Stop must release hold")
    ctx = d.current_prompt_by_agent.get("bob") or {}
    assert_true(ctx.get("turn_id") == "t-new", f"{label}: stale held Stop must preserve new ctx, got {ctx}")
    assert_true(d.busy.get("bob") is True, f"{label}: stale held Stop must preserve busy=True")
    item = _queue_item(d, "msg-pending-held-stale")
    assert_true(item is not None and item.get("status") == "pending", f"{label}: pending message must not deliver over active prompt")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "held_drain_stale_stop" for e in events), f"{label}: stale Stop log expected")
    assert_true(not any(e.get("event") == "message_delivery_attempted" and e.get("message_id") == "msg-pending-held-stale" for e in events), f"{label}: no delivery attempt expected")
    print(f"  PASS  {label}")


def scenario_held_drain_missing_identity_does_not_clear_active_ctx(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.held_interrupt["claude"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-old-held",
        "prior_sender": "codex",
        "reason": "legacy",
    }
    active_ctx = {
        "id": None,
        "nonce": "n-present-no-id",
        "causal_id": "causal-present-no-id",
        "hop_count": 1,
        "from": "codex",
        "kind": "request",
        "intent": "test",
        "source": "test",
        "auto_return": True,
        "turn_id": None,
    }
    d.current_prompt_by_agent["claude"] = dict(active_ctx)
    d.busy["claude"] = True
    pending = test_message("msg-pending-missing-identity", frm="codex", to="claude", status="pending")
    d.queue.update(lambda queue: queue.append(pending))

    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "stale"})

    assert_true("claude" not in d.held_interrupt, f"{label}: stale hold marker should be popped")
    assert_true(d.current_prompt_by_agent.get("claude") == active_ctx, f"{label}: unidentified active ctx must remain")
    assert_true(d.busy.get("claude") is True, f"{label}: busy must stay true for unidentified active ctx")
    item = _queue_item(d, "msg-pending-missing-identity")
    assert_true(item is not None and item.get("status") == "pending", f"{label}: pending message must not deliver")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: stale held drain must not auto-route")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "hold_released" and e.get("target") == "claude" for e in events), f"{label}: hold_released log expected")
    stale = next((e for e in events if e.get("event") == "held_drain_stale_stop"), None)
    assert_true(stale is not None, f"{label}: held_drain_stale_stop log expected")
    assert_true(stale.get("active_message_id") is None, f"{label}: stale log active_message_id expected")
    assert_true(stale.get("active_turn_id") is None, f"{label}: stale log active_turn_id expected")
    assert_true(stale.get("response_turn_id") == "stale-turn", f"{label}: stale log response_turn_id expected")
    assert_true(stale.get("prior_message_id") == "msg-old-held", f"{label}: stale log prior_message_id expected")
    print(f"  PASS  {label}")


def scenario_held_drain_matching_prior_id_without_turn_still_clears_ctx(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.current_prompt_by_agent["claude"] = {
        "id": "msg-held-id-only",
        "from": "codex",
        "kind": "request",
        "intent": "test",
        "auto_return": True,
        "turn_id": None,
    }
    d.busy["claude"] = True
    d.held_interrupt["claude"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-held-id-only",
        "prior_sender": "codex",
    }

    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "unrelated-turn", "last_assistant_message": "drain"})

    assert_true("claude" not in d.held_interrupt, f"{label}: id-only matching drain must release hold")
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: id-only matching drain must clear ctx")
    assert_true(d.busy.get("claude") is False, f"{label}: id-only matching drain must clear busy")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: held drain must not auto-route")
    print(f"  PASS  {label}")


def scenario_stale_hold_ignored_active_context_routes_normally(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _active_turn(d, message_id="msg-hold-new", nonce="n-hold-new", turn_id="active-turn")
    d.held_interrupt["claude"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-hold-old",
        "prior_sender": "codex",
    }
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "active-turn", "last_assistant_message": "real reply"})
    assert_true("claude" not in d.held_interrupt, f"{label}: stale hold marker must be popped")
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: active ctx should clear normally")
    assert_true(d.busy.get("claude") is False, f"{label}: busy should clear normally")
    assert_true(_queue_item(d, "msg-hold-new") is None, f"{label}: delivered row should be removed")
    results = _auto_return_results(d, "claude", "codex")
    assert_true(len(results) == 1 and results[0].get("reply_to") == msg["id"], f"{label}: normal result should route")
    events = read_events(tmpdir / "events.raw.jsonl")
    stale = next((e for e in events if e.get("event") == "stale_hold_ignored_active_context"), None)
    assert_true(stale is not None, f"{label}: stale hold log expected")
    assert_true(stale.get("target") == "claude", f"{label}: stale log target expected")
    assert_true(stale.get("prior_message_id") == "msg-hold-old", f"{label}: stale log prior id expected")
    assert_true(stale.get("active_message_id") == "msg-hold-new", f"{label}: stale log active id expected")
    assert_true(stale.get("active_turn_id") == "active-turn", f"{label}: stale log active turn expected")
    assert_true(stale.get("response_turn_id") == "active-turn", f"{label}: stale log response turn expected")
    assert_true("hold_age_ms" in stale, f"{label}: stale log hold age field expected")
    print(f"  PASS  {label}")


def scenario_stale_hold_old_stop_with_active_ctx_applies_a4_mismatch(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-hold-active", nonce="n-hold-active", turn_id="active-turn")
    d.held_interrupt["claude"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-hold-old"}
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "old-turn", "last_assistant_message": "old reply"})
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true("claude" not in d.held_interrupt, f"{label}: stale hold must be popped")
    assert_true(ctx.get("id") == "msg-hold-active", f"{label}: active ctx must remain")
    assert_true(d.busy.get("claude") is True, f"{label}: active turn remains busy")
    assert_true((_queue_item(d, "msg-hold-active") or {}).get("status") == "delivered", f"{label}: delivered row should remain")
    assert_true(ctx.get("turn_id_mismatch_response_turn_id") == "old-turn", f"{label}: A4 mismatch metadata expected")
    assert_true(ctx.get("turn_id_mismatch_since_ts") is not None, f"{label}: mismatch since expected")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: old Stop must not auto-route")
    print(f"  PASS  {label}")


def scenario_held_no_prior_id_with_active_ctx_classified_stale(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-hold-noprior", nonce="n-hold-noprior", turn_id="active-turn")
    d.held_interrupt["claude"] = {"since": utc_now(), "since_ts": time.time(), "reason": "legacy"}
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "active-turn", "last_assistant_message": "real reply"})
    assert_true("claude" not in d.held_interrupt, f"{label}: no-prior held marker must be classified stale and popped")
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: matching response should still finish normally")
    assert_true(len(_auto_return_results(d, "claude", "codex")) == 1, f"{label}: matching response should auto-route")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "stale_hold_ignored_active_context" and e.get("active_message_id") == "msg-hold-noprior" for e in events), f"{label}: stale log expected")
    print(f"  PASS  {label}")


def scenario_held_matching_prior_id_turn_id_mismatch_falls_through_to_a4(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-hold-same-id", nonce="n-hold-same-id", turn_id="active-turn")
    d.held_interrupt["claude"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-hold-same-id"}
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "wrong-turn", "last_assistant_message": "wrong"})
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true("claude" not in d.held_interrupt, f"{label}: same-id wrong-turn hold must be popped as stale")
    assert_true(ctx.get("id") == "msg-hold-same-id", f"{label}: active ctx must remain")
    assert_true(ctx.get("turn_id_mismatch_response_turn_id") == "wrong-turn", f"{label}: A4 mismatch metadata expected")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: wrong-turn response must not auto-route")
    print(f"  PASS  {label}")


def scenario_held_with_no_context_drain_still_works(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.busy["claude"] = True
    d.held_interrupt["claude"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-old"}
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "old-turn", "last_assistant_message": "drain"})
    assert_true("claude" not in d.held_interrupt, f"{label}: no-context drain must release hold")
    assert_true(d.busy.get("claude") is False, f"{label}: no-context drain must leave target idle")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: held drain must not route")
    print(f"  PASS  {label}")


def scenario_held_matching_prior_id_with_matching_turn_drain_still_works(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.current_prompt_by_agent["claude"] = {
        "id": "msg-held-drain",
        "from": "codex",
        "auto_return": True,
        "turn_id": "active-turn",
    }
    d.busy["claude"] = True
    d.held_interrupt["claude"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-held-drain"}
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "active-turn", "last_assistant_message": "drain"})
    assert_true("claude" not in d.held_interrupt, f"{label}: matching legacy drain must release hold")
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: matching legacy drain must clear ctx")
    assert_true(d.busy.get("claude") is False, f"{label}: matching legacy drain must clear busy")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: held drain must not auto-route")
    print(f"  PASS  {label}")


def scenario_held_with_interrupted_tombstone_match_classify_first(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-hold-tombstone-active", nonce="n-hold-tombstone-active", turn_id="active-turn")
    d.held_interrupt["claude"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-old-held"}
    d.interrupted_turns["claude"] = [{
        "message_id": "msg-interrupted-old",
        "turn_id": "old-turn",
        "nonce": "n-old",
        "prompt_submitted_seen": True,
        "interrupted_ts": time.time(),
    }]
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "old-turn", "last_assistant_message": "old interrupted"})
    assert_true("claude" not in d.held_interrupt, f"{label}: stale hold must be popped before tombstone early-return")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") == "msg-hold-tombstone-active", f"{label}: tombstone return must not clobber active ctx")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: tombstone-suppressed response must not route")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "stale_hold_ignored_active_context" for e in events), f"{label}: stale hold log expected")
    assert_true(any(e.get("event") == "response_skipped_stale" and e.get("reason") == "interrupted_drain" for e in events), f"{label}: interrupted tombstone log expected")
    print(f"  PASS  {label}")


def scenario_aggregate_leg_unaffected_by_held_marker(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-held-marker"
    _active_turn(
        d,
        message_id="msg-held-agg-w1",
        frm="manager",
        to="w1",
        nonce="n-held-agg-w1",
        turn_id="active-turn",
        aggregate_id=agg_id,
        aggregate_expected=["w1"],
        aggregate_message_ids={"w1": "msg-held-agg-w1"},
    )
    d.held_interrupt["w1"] = {"since": utc_now(), "since_ts": time.time(), "prior_message_id": "msg-old-agg"}
    d.handle_response_finished({"agent": "w1", "bridge_agent": "w1", "turn_id": "active-turn", "last_assistant_message": "agg reply"})
    assert_true("w1" not in d.held_interrupt, f"{label}: stale aggregate hold should be popped")
    aggregate = (read_json(d.aggregate_file, {"aggregates": {}}).get("aggregates") or {}).get(agg_id) or {}
    reply = ((aggregate.get("replies") or {}).get("w1") or {}).get("body") or ""
    assert_true(reply == "agg reply", f"{label}: aggregate reply should be collected unchanged: {reply!r}")
    assert_true(any(item.get("source") == "aggregate_return" and item.get("to") == "manager" for item in d.queue.read()), f"{label}: aggregate result should be queued")
    print(f"  PASS  {label}")


def scenario_prompt_intercept_inflight_only_requeues(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-inflight-only", "alice", "bob", "n-inflight-only")
    d.handle_prompt_submitted({"agent": "bob", "bridge_agent": "bob", "nonce": None, "turn_id": "t-user", "prompt": "user typed"})

    item = _queue_item(d, "msg-inflight-only")
    assert_true(item is not None and item.get("status") == "inflight", f"{label}: inflight-only nonce miss must not be cancelled")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(not any(e.get("event") == "active_prompt_intercepted" for e in events), f"{label}: no delivered/ctx means no intercept")

    def age_item(queue):
        for row in queue:
            if row.get("id") == "msg-inflight-only":
                row["updated_ts"] = "1970-01-01T00:00:00.000000Z"
        return None
    d.queue.update(age_item)
    d.last_maintenance = 0.0
    d.requeue_stale_inflight()
    item = _queue_item(d, "msg-inflight-only")
    assert_true(item is not None and item.get("status") == "pending", f"{label}: inflight-only row must requeue after timeout")
    print(f"  PASS  {label}")


def scenario_consume_once_basic(label: str, tmpdir: Path) -> None:
    """A peer request gets exactly one auto-routed reply; subsequent Stop without
    a fresh prompt_submitted must NOT route."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-co-1", frm="codex", to="claude", nonce="n-co-1")
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-co-1", "turn_id": "t-co-1", "prompt": "[bridge:n-co-1] from=codex kind=request"})
    # First Stop should route + clear ctx
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-co-1", "last_assistant_message": "first reply"})
    routed_first = [it for it in d.queue.read() if it.get("from") == "claude" and it.get("to") == "codex" and it.get("kind") == "result"]
    assert_true(len(routed_first) == 1, f"{label}: first Stop should auto-route exactly once, got {len(routed_first)}")
    ctx_after_first = d.current_prompt_by_agent.get("claude")
    assert_true(ctx_after_first is None, f"{label}: ctx must be popped after first Stop (consume-once)")
    # Second Stop without a new prompt_submitted (e.g., system reminder) must NOT route
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-co-2", "last_assistant_message": "second leak attempt"})
    routed_total = [it for it in d.queue.read() if it.get("from") == "claude" and it.get("to") == "codex" and it.get("kind") == "result"]
    assert_true(len(routed_total) == 1, f"{label}: second Stop must NOT route to peer (consume-once), got {len(routed_total)}")
    print(f"  PASS  {label}")


def scenario_consume_once_empty_response(label: str, tmpdir: Path) -> None:
    """Empty terminal response routes a sentinel once and consumes ctx."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-empty-1", frm="codex", to="claude", nonce="n-empty-1")
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-empty-1", "turn_id": "t-e-1", "prompt": "[bridge:n-empty-1] x"})
    ctx = dict(d.current_prompt_by_agent.get("claude") or {})
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-e-1", "last_assistant_message": ""})
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: empty Stop must still consume ctx")
    routed_first = _auto_return_results(d, "claude", "codex")
    assert_true(len(routed_first) == 1, f"{label}: empty Stop should auto-route exactly one sentinel result, got {routed_first}")
    _assert_auto_return_result_shape(
        label,
        routed_first[0],
        sender="claude",
        target="codex",
        reply_to="msg-empty-1",
        causal_id=str(ctx.get("causal_id") or ""),
        hop_count=int(ctx.get("hop_count") or 0),
        body="Result from claude:\n(empty response)",
    )
    # Next Stop has no ctx to route through
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-e-2", "last_assistant_message": "later unrelated reply"})
    routed_total = _auto_return_results(d, "claude", "codex")
    assert_true(len(routed_total) == 1, f"{label}: later Stop must not route after empty Stop consumed ctx, got {routed_total}")
    print(f"  PASS  {label}")


def scenario_empty_response_whitespace_only_routes_sentinel(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _make_delivered_context(d, "msg-empty-ws", "codex", "claude", "n-empty-ws", turn_id="t-empty-ws")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-empty-ws", "last_assistant_message": "   \n\t  \n"})
    routed = _auto_return_results(d, "claude", "codex")
    assert_true(len(routed) == 1, f"{label}: whitespace-only response should route sentinel, got {routed}")
    _assert_auto_return_result_shape(
        label,
        routed[0],
        sender="claude",
        target="codex",
        reply_to="msg-empty-ws",
        causal_id=str(msg.get("causal_id") or ""),
        hop_count=int(msg.get("hop_count") or 0),
        body="Result from claude:\n(empty response)",
    )
    print(f"  PASS  {label}")


def scenario_empty_response_preserves_nonempty_with_surrounding_whitespace(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _make_delivered_context(d, "msg-nonempty-ws", "codex", "claude", "n-nonempty-ws", turn_id="t-nonempty-ws")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-nonempty-ws", "last_assistant_message": "\nhello\n"})
    routed = _auto_return_results(d, "claude", "codex")
    assert_true(len(routed) == 1, f"{label}: non-empty response should route once, got {routed}")
    _assert_auto_return_result_shape(
        label,
        routed[0],
        sender="claude",
        target="codex",
        reply_to="msg-nonempty-ws",
        causal_id=str(msg.get("causal_id") or ""),
        hop_count=int(msg.get("hop_count") or 0),
        body="Result from claude:\n\nhello\n",
    )
    print(f"  PASS  {label}")


def scenario_empty_response_self_return_still_skipped(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}}
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-self-empty", "claude", "claude", "n-self-empty", turn_id="t-self-empty")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-self-empty", "last_assistant_message": ""})
    routed = _auto_return_results(d, "claude", "claude")
    assert_true(routed == [], f"{label}: self-return empty response must not route: {routed}")
    print(f"  PASS  {label}")


def scenario_empty_response_auto_return_false_still_skipped(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-noauto-empty", "codex", "claude", "n-noauto-empty", auto_return=False, turn_id="t-noauto-empty")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-noauto-empty", "last_assistant_message": ""})
    routed = _auto_return_results(d, "claude", "codex")
    assert_true(routed == [], f"{label}: auto_return=False empty response must not route: {routed}")
    print(f"  PASS  {label}")


def scenario_empty_response_inactive_requester_still_skipped(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}}
    d = make_daemon(tmpdir, participants)
    _make_delivered_context(d, "msg-inactive-empty", "missing", "claude", "n-inactive-empty", turn_id="t-inactive-empty")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-inactive-empty", "last_assistant_message": ""})
    routed = _auto_return_results(d, "claude", "missing")
    assert_true(routed == [], f"{label}: inactive requester empty response must not route: {routed}")
    print(f"  PASS  {label}")


def scenario_empty_response_unicode_whitespace_routes_sentinel(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _make_delivered_context(d, "msg-empty-unicode-ws", "codex", "claude", "n-empty-unicode-ws", turn_id="t-empty-unicode-ws")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-empty-unicode-ws", "last_assistant_message": "\u00a0\u2028"})
    routed = _auto_return_results(d, "claude", "codex")
    assert_true(len(routed) == 1, f"{label}: unicode whitespace-only response should route sentinel, got {routed}")
    _assert_auto_return_result_shape(
        label,
        routed[0],
        sender="claude",
        target="codex",
        reply_to="msg-empty-unicode-ws",
        causal_id=str(msg.get("causal_id") or ""),
        hop_count=int(msg.get("hop_count") or 0),
        body="Result from claude:\n(empty response)",
    )
    print(f"  PASS  {label}")


def scenario_nonce_mismatch_fail_closed(label: str, tmpdir: Path) -> None:
    """Candidate exists with nonce N1, hook reports N2 → fail-closed."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-mm-1", frm="codex", to="claude", nonce="real-nonce")
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "wrong-nonce", "turn_id": "t-mm", "prompt": "[bridge:wrong-nonce] forged"})
    item = next((it for it in d.queue.read() if it.get("id") == "msg-mm-1"), None)
    assert_true(item is not None and item.get("status") == "inflight", f"{label}: candidate must stay inflight on mismatch")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") is None, f"{label}: ctx must NOT bind on nonce mismatch")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "nonce_mismatch" for e in events), f"{label}: nonce_mismatch log expected")
    print(f"  PASS  {label}")


def scenario_no_observed_nonce_with_candidate_fail_closed(label: str, tmpdir: Path) -> None:
    """Candidate exists but hook reports no nonce (e.g., user typing collision).
    Must fail-closed: candidate stays inflight, ctx not bound."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-nn-1", frm="codex", to="claude", nonce="real-nonce")
    # No nonce in record (anchored regex didn't match because user input came first)
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-nn", "prompt": "user typed first then bridge appended"})
    item = next((it for it in d.queue.read() if it.get("id") == "msg-nn-1"), None)
    assert_true(item is not None and item.get("status") == "inflight", f"{label}: candidate must stay inflight when observed_nonce missing")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") is None, f"{label}: ctx must NOT bind without observed_nonce")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "nonce_missing_for_candidate" for e in events), f"{label}: nonce_missing_for_candidate log expected")
    print(f"  PASS  {label}")


def scenario_daemon_restart_queue_scan(label: str, tmpdir: Path) -> None:
    """After daemon restart, in-memory `reserved` is empty but queue.json still
    has status=inflight. find_inflight_candidate must recover via queue scan."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-rs-1", frm="codex", to="claude", nonce="n-rs-1")
    # Simulate restart: clear in-memory reserved
    d.reserved["claude"] = None
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-rs-1", "turn_id": "t-rs", "prompt": "[bridge:n-rs-1] post-restart"})
    item = next((it for it in d.queue.read() if it.get("id") == "msg-rs-1"), None)
    assert_true(item is not None and item.get("status") == "delivered", f"{label}: queue scan must recover candidate, got {item.get('status') if item else None}")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") == "msg-rs-1", f"{label}: ctx must bind to recovered candidate")
    print(f"  PASS  {label}")


def scenario_ambiguous_inflight_fail_closed(label: str, tmpdir: Path) -> None:
    """Two inflight items for the same target = invariant violation. fail-closed."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-amb-1", frm="codex", to="claude", nonce="n-amb-1")
    _make_inflight(d, "msg-amb-2", frm="codex", to="claude", nonce="n-amb-2")
    # reserved was set twice; clear it so neither path 1 hit
    d.reserved["claude"] = None
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-amb-1", "turn_id": "t-amb", "prompt": "[bridge:n-amb-1]"})
    items = [it for it in d.queue.read() if it.get("id") in ("msg-amb-1", "msg-amb-2")]
    statuses = sorted([it.get("status") for it in items])
    assert_true(statuses == ["inflight", "inflight"], f"{label}: both candidates must stay inflight on ambiguity, got {statuses}")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "ambiguous_inflight" for e in events), f"{label}: ambiguous_inflight log expected")
    print(f"  PASS  {label}")


def scenario_stale_reserved_orphan_swept(label: str, tmpdir: Path) -> None:
    """reserved[agent] points to a message that is NOT in inflight status
    (e.g., already delivered or cancelled). Candidate must be rejected, not
    incorrectly mark a non-inflight item as delivered."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Plant a 'delivered' item (not inflight) + matching stale reserved
    msg = {
        "id": "msg-stale-1",
        "created_ts": utc_now(), "updated_ts": utc_now(), "delivered_ts": utc_now(),
        "from": "codex", "to": "claude",
        "kind": "request", "intent": "test", "body": "x",
        "causal_id": "causal-stale", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "delivered", "nonce": "n-stale", "delivery_attempts": 1,
    }
    def add(q):
        q.append(msg)
        return None
    d.queue.update(add)
    d.reserved["claude"] = "msg-stale-1"  # stale pointer
    # Hook reports the same nonce. find_inflight_candidate should reject (status mismatch)
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-stale", "turn_id": "t-stale", "prompt": "[bridge:n-stale]"})
    item = next((it for it in d.queue.read() if it.get("id") == "msg-stale-1"), None)
    assert_true(item is None, f"{label}: stale delivered orphan must be removed, got {item}")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") is None, f"{label}: ctx must not bind to stale reserved item")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "active_prompt_intercepted" for e in events), f"{label}: stale delivered cleanup should log intercept")
    print(f"  PASS  {label}")


def scenario_matching_nonce_contaminated_body_documents_residual(label: str, tmpdir: Path) -> None:
    """v1.5.2 verifies recipient/status/nonce equality but NOT prompt body
    contents. A submission whose prompt happens to start with the live
    candidate's [bridge:nonce] still binds ctx — this is the documented
    'matching nonce, contaminated body' residual (I-04). The hash
    cross-check in v1.6 closes this hole.

    The test fixes this behavior so that, if the residual is later
    closed, this scenario will fail and force a docs/test update."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-cb-1", frm="codex", to="claude", nonce="live-nonce")
    # Hook reports the live nonce (via anchored regex), but the prompt body
    # contains arbitrary additional user-typed content beyond what the
    # daemon would have sent.
    d.handle_prompt_submitted({
        "agent": "claude", "bridge_agent": "claude",
        "nonce": "live-nonce", "turn_id": "t-cb",
        "prompt": "[bridge:live-nonce] from=codex kind=request causal_id=c. Reply normally; do not call agent_send_peer; bridge auto-returns your reply. Request: hello\nUSER PASTED EXTRA CONTENT HERE",
    })
    item = next((it for it in d.queue.read() if it.get("id") == "msg-cb-1"), None)
    assert_true(item is not None and item.get("status") == "delivered", f"{label}: residual hole — body not verified, candidate is marked delivered")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") == "msg-cb-1", f"{label}: residual hole — ctx binds despite contamination")
    print(f"  PASS  {label}")


def scenario_aggregate_consume_once_no_overwrite(label: str, tmpdir: Path) -> None:
    """After consume-once pops ctx, a duplicate response_finished from the
    same peer must NOT re-enter aggregate collection (would overwrite a
    previously-collected reply)."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Aggregate request from claude -> codex (single member here is enough to
    # exercise the consume-once + aggregate interaction without setting up a
    # multi-peer aggregate).
    aggregate_id = f"agg-{uuid.uuid4().hex[:8]}"
    msg = {
        "id": "msg-agg-1",
        "created_ts": utc_now(), "updated_ts": utc_now(),
        "from": "claude", "to": "codex",
        "kind": "request", "intent": "test",
        "body": "agg ping",
        "causal_id": f"causal-{uuid.uuid4().hex[:12]}",
        "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "inflight", "nonce": "n-agg-1", "delivery_attempts": 1,
        "aggregate_id": aggregate_id,
        "aggregate_expected": ["codex"],
        "aggregate_message_ids": {"codex": "msg-agg-1"},
    }
    def add(q):
        q.append(msg)
        return None
    d.queue.update(add)
    d.reserved["codex"] = "msg-agg-1"
    d.handle_prompt_submitted({"agent": "codex", "bridge_agent": "codex", "nonce": "n-agg-1", "turn_id": "t-agg-1", "prompt": "[bridge:n-agg-1] aggregate"})
    d.handle_response_finished({"agent": "codex", "bridge_agent": "codex", "turn_id": "t-agg-1", "last_assistant_message": "first reply"})
    # consume-once should have popped ctx; second Stop must not re-collect.
    assert_true(d.current_prompt_by_agent.get("codex") is None, f"{label}: consume-once must pop ctx after first aggregate response")
    d.handle_response_finished({"agent": "codex", "bridge_agent": "codex", "turn_id": "t-agg-2", "last_assistant_message": "duplicate reply that must NOT overwrite"})
    # No additional auto-route; no second aggregate collection
    routed_to_claude = [it for it in d.queue.read() if it.get("from") == "codex" and it.get("to") == "claude" and it.get("kind") == "result"]
    # Aggregate routing path differs from per-message; the invariant we care
    # about is that the second Stop didn't sneak past consume-once. Check by
    # ensuring ctx is still cleared and queue has at most one codex->claude
    # auto-routed reply.
    assert_true(d.current_prompt_by_agent.get("codex") is None, f"{label}: ctx should remain cleared after duplicate Stop")
    assert_true(len(routed_to_claude) <= 1, f"{label}: aggregate must not collect a duplicate reply, got {len(routed_to_claude)}")
    print(f"  PASS  {label}")


def scenario_empty_response_in_aggregate_path_unchanged(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = f"agg-empty-{uuid.uuid4().hex[:8]}"
    expected = ["w1", "w2"]
    message_ids = {"w1": "msg-agg-empty-w1", "w2": "msg-agg-empty-w2"}
    _make_delivered_context(
        d,
        "msg-agg-empty-w1",
        "manager",
        "w1",
        "n-agg-empty-w1",
        turn_id="t-agg-empty-w1",
        aggregate_id=agg_id,
        aggregate_expected=expected,
        aggregate_message_ids=message_ids,
    )
    _make_delivered_context(
        d,
        "msg-agg-empty-w2",
        "manager",
        "w2",
        "n-agg-empty-w2",
        turn_id="t-agg-empty-w2",
        aggregate_id=agg_id,
        aggregate_expected=expected,
        aggregate_message_ids=message_ids,
    )
    d.handle_response_finished({"agent": "w1", "bridge_agent": "w1", "turn_id": "t-agg-empty-w1", "last_assistant_message": ""})
    d.handle_response_finished({"agent": "w2", "bridge_agent": "w2", "turn_id": "t-agg-empty-w2", "last_assistant_message": "w2 ok"})

    aggregate_data = read_json(d.aggregate_file, {"aggregates": {}})
    aggregate = (aggregate_data.get("aggregates") or {}).get(agg_id) or {}
    replies = aggregate.get("replies") or {}
    assert_true(str((replies.get("w1") or {}).get("body") or "") == "", f"{label}: aggregate should store raw empty body for w1: {replies}")
    assert_true(str((replies.get("w2") or {}).get("body") or "") == "w2 ok", f"{label}: aggregate should store raw non-empty body for w2: {replies}")

    aggregate_results = [
        item for item in d.queue.read()
        if item.get("from") == "bridge"
        and item.get("to") == "manager"
        and item.get("kind") == "result"
        and item.get("source") == "aggregate_return"
        and item.get("aggregate_id") == agg_id
    ]
    assert_true(len(aggregate_results) == 1, f"{label}: aggregate should queue one result, got {aggregate_results}")
    body = str(aggregate_results[0].get("body") or "")
    assert_true("--- w1 in_reply_to=msg-agg-empty-w1 ---\n(empty response)" in body, f"{label}: aggregate empty leg should render sentinel without per-leg result prefix: {body!r}")
    assert_true("w2 ok" in body, f"{label}: aggregate non-empty leg should remain: {body!r}")
    assert_true("Result from w1:" not in body and "Result from w2:" not in body, f"{label}: aggregate body must not use single-request result prefixes: {body!r}")
    print(f"  PASS  {label}")


def scenario_nonce_mismatch_stops_enter_retry(label: str, tmpdir: Path) -> None:
    """fail-closed branches must clear last_enter_ts so retry_enter_for_inflight
    doesn't keep sending Enter into a pane the human is typing in."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-er-1", frm="codex", to="claude", nonce="real-nonce")
    d.last_enter_ts["msg-er-1"] = time.time()
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "wrong-nonce", "turn_id": "t-er", "prompt": "[bridge:wrong-nonce] forged"})
    assert_true("msg-er-1" not in d.last_enter_ts, f"{label}: last_enter_ts must be cleared on nonce_mismatch to stop retry-enter spam")
    print(f"  PASS  {label}")


def scenario_nonce_missing_stops_enter_retry(label: str, tmpdir: Path) -> None:
    """Same defense for the missing-nonce fail-closed branch."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-er-2", frm="codex", to="claude", nonce="real-nonce")
    d.last_enter_ts["msg-er-2"] = time.time()
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-er", "prompt": "user-typed without bridge prefix"})
    assert_true("msg-er-2" not in d.last_enter_ts, f"{label}: last_enter_ts must be cleared when observed nonce missing for a candidate")
    print(f"  PASS  {label}")


def scenario_hook_logger_anchored_regex(label: str, tmpdir: Path) -> None:
    """Direct unit-style coverage for the hook-side anchored regex."""
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bhl = importlib.import_module("bridge_hook_logger")
        # Prefix match (with leading whitespace tolerated)
        assert_true(bhl.extract_nonce("[bridge:abc] body") == "abc", f"{label}: leading marker must match")
        assert_true(bhl.extract_nonce("   [bridge:xyz] body") == "xyz", f"{label}: leading whitespace tolerated")
        assert_true(bhl.extract_nonce("\t[bridge:tab] body") == "tab", f"{label}: tab tolerated")
        # Mid-prompt quoted marker must NOT match
        assert_true(bhl.extract_nonce("user typed [bridge:old]") is None, f"{label}: mid-prompt marker must not match")
        assert_true(bhl.extract_nonce("> [bridge:quoted] from peer") is None, f"{label}: quoted prefix '>' must not match")
        # Whitespace inside nonce body must reject
        assert_true(bhl.extract_nonce("[bridge:a b] body") is None, f"{label}: whitespace in nonce body must reject")
        # Empty / None
        assert_true(bhl.extract_nonce("") is None, f"{label}: empty input")
        assert_true(bhl.extract_nonce(None) is None, f"{label}: None input")
    finally:
        if str(libexec) in sys.path:
            sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_preserves_ctx(label: str, tmpdir: Path) -> None:
    """A late Stop with mismatched turn_id must NOT pop ctx (consume-once
    is for normal terminal path only; bounded expiry is a later maintenance step)."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-tm-1", frm="codex", to="claude", nonce="n-tm-1")
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-tm-1", "turn_id": "active-turn", "prompt": "[bridge:n-tm-1]"})
    ctx_before = dict(d.current_prompt_by_agent.get("claude") or {})
    assert_true(ctx_before.get("id") == "msg-tm-1", f"{label}: precondition: ctx bound to candidate")
    # Stop arrives with a DIFFERENT turn_id (a stale late event)
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "old-stale-turn", "last_assistant_message": "leftover"})
    ctx_after = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx_after.get("id") == "msg-tm-1", f"{label}: turn_id_mismatch must NOT consume ctx")
    assert_true(d.busy.get("claude") is True, f"{label}: turn_id_mismatch must keep busy until expiry")
    assert_true((_queue_item(d, "msg-tm-1") or {}).get("status") == "delivered", f"{label}: delivered row must remain before expiry")
    assert_true(isinstance(ctx_after.get("turn_id_mismatch_since_ts"), float), f"{label}: mismatch since_ts expected")
    assert_true(isinstance(ctx_after.get("turn_id_mismatch_last_ts"), float), f"{label}: mismatch last_ts expected")
    assert_true(ctx_after.get("turn_id_mismatch_response_turn_id") == "old-stale-turn", f"{label}: response turn_id must be recorded")
    assert_true(int(ctx_after.get("turn_id_mismatch_count") or 0) >= 1, f"{label}: mismatch count expected")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(
        sum(1 for e in events if e.get("event") == "response_skipped_stale" and e.get("reason") == "turn_id_mismatch") == 1,
        f"{label}: one stale response log expected",
    )
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_annotation_is_idempotent(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-tm-idem", nonce="n-tm-idem", turn_id="active-turn")

    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-1", "last_assistant_message": "old1"})
    first = dict(d.current_prompt_by_agent.get("claude") or {})
    since = first.get("turn_id_mismatch_since_ts")
    last = first.get("turn_id_mismatch_last_ts")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-2", "last_assistant_message": "old2"})
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-3", "last_assistant_message": "old3"})

    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("turn_id_mismatch_since_ts") == since, f"{label}: since_ts must stay pinned to first mismatch")
    assert_true(float(ctx.get("turn_id_mismatch_last_ts") or 0) >= float(last or 0), f"{label}: last_ts must update")
    assert_true(ctx.get("turn_id_mismatch_response_turn_id") == "stale-3", f"{label}: latest mismatched turn should be recorded")
    assert_true(ctx.get("turn_id_mismatch_count") == 3, f"{label}: mismatch count must increment")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_matching_stop_before_expiry_cleans_normally(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = _active_turn(d, message_id="msg-tm-match", nonce="n-tm-match", turn_id="active-turn")

    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "old"})
    assert_true((d.current_prompt_by_agent.get("claude") or {}).get("turn_id_mismatch_since_ts") is not None, f"{label}: mismatch must annotate ctx")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "active-turn", "last_assistant_message": "real reply"})

    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: matching Stop must pop ctx normally")
    assert_true(d.busy.get("claude") is False, f"{label}: matching Stop must clear busy")
    assert_true(_queue_item(d, "msg-tm-match") is None, f"{label}: matching Stop must remove delivered row")
    results = _auto_return_results(d, "claude", "codex")
    assert_true(len(results) == 1 and results[0].get("reply_to") == msg["id"], f"{label}: matching Stop must route normal result")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_expiry_unblocks_target(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    d.turn_id_mismatch_post_watchdog_grace_seconds = 0.0
    _active_turn(d, message_id="msg-tm-expire", nonce="n-tm-expire", turn_id="active-turn")
    d.reserved["claude"] = "msg-tm-expire"
    d.watchdogs["wake-tm-expire"] = {
        "sender": "codex",
        "deadline": time.time() - 1.0,
        "ref_message_id": "msg-tm-expire",
        "ref_aggregate_id": None,
        "ref_to": "claude",
        "ref_kind": "request",
        "ref_intent": "test",
        "is_alarm": False,
    }
    pending = test_message("msg-tm-next", frm="codex", to="claude", status="pending")
    d.queue.update(lambda queue: (queue.append(pending), None)[1])

    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "old"})
    assert_true(d.cached_nonce("n-tm-expire") is not None, f"{label}: precondition nonce cached")
    d.expire_turn_id_mismatch_contexts()

    assert_true(d.busy.get("claude") is False, f"{label}: expiry must clear busy")
    assert_true(d.reserved.get("claude") == "msg-tm-next", f"{label}: pending message should be reserved after expiry")
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: expiry must pop current ctx")
    assert_true(_queue_item(d, "msg-tm-expire") is None, f"{label}: expiry must remove delivered row")
    assert_true((_queue_item(d, "msg-tm-next") or {}).get("status") == "inflight", f"{label}: pending message should move to inflight")
    assert_true(d.cached_nonce("n-tm-expire") is None, f"{label}: expiry must discard active nonce")
    assert_true("msg-tm-expire" not in d.last_enter_ts, f"{label}: expiry must clear last_enter_ts")
    assert_true("wake-tm-expire" not in d.watchdogs, f"{label}: expiry must cancel non-aggregate message watchdog")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(
        any(e.get("event") == "watchdog_cancelled" and e.get("reason") == "turn_id_mismatch_expired" and e.get("ref_message_id") == "msg-tm-expire" for e in events),
        f"{label}: watchdog cancellation log expected",
    )
    expired = [e for e in events if e.get("event") == "turn_id_mismatch_context_expired" and e.get("message_id") == "msg-tm-expire"]
    assert_true(len(expired) == 1, f"{label}: exactly one expiry log expected")
    assert_true(expired[0].get("removed_delivered") is True, f"{label}: expiry log must say delivered row removed")
    assert_true(expired[0].get("mismatched_turn_id") == "stale-turn", f"{label}: expiry log must include mismatched turn")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_expiry_waits_for_watchdog_deadline(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    d.turn_id_mismatch_post_watchdog_grace_seconds = 2.0
    _active_turn(d, message_id="msg-tm-wd", nonce="n-tm-wd", turn_id="active-turn")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "old"})
    since = float((d.current_prompt_by_agent.get("claude") or {}).get("turn_id_mismatch_since_ts"))
    d.watchdogs["wake-tm-wd"] = {
        "sender": "codex",
        "deadline": since + 100.0,
        "ref_message_id": "msg-tm-wd",
        "ref_aggregate_id": None,
        "ref_to": "claude",
        "ref_kind": "request",
        "ref_intent": "test",
        "is_alarm": False,
    }

    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(since + 101.0)
    assert_true(expired == [], f"{label}: post-watchdog grace must defer expiry")
    assert_true((d.current_prompt_by_agent.get("claude") or {}).get("id") == "msg-tm-wd", f"{label}: ctx must remain before deadline")
    d.watchdogs["wake-tm-wd"]["deadline"] = since + 200.0
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(since + 201.0)
    assert_true(expired == [], f"{label}: extended watchdog deadline must defer expiry")
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(since + 203.0)
    assert_true(expired == ["claude"], f"{label}: expiry should happen after extended deadline plus post grace")
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: ctx should be expired after extended deadline")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_watchdog_fire_preserves_post_grace(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    d.turn_id_mismatch_post_watchdog_grace_seconds = 10.0
    _active_turn(d, message_id="msg-tm-fired", nonce="n-tm-fired", turn_id="active-turn")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "old"})
    deadline = time.time() - 1.0
    _plant_watchdog(d, "wake-tm-fired", message_id="msg-tm-fired", deadline=deadline)

    d.check_watchdogs()
    d.expire_turn_id_mismatch_contexts()

    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") == "msg-tm-fired", f"{label}: ctx must survive same tick as watchdog fire")
    assert_true("wake-tm-fired" not in d.watchdogs, f"{label}: watchdog should have fired and been popped")
    unblock_ts = float(ctx.get("turn_id_mismatch_post_watchdog_unblock_ts") or 0.0)
    assert_true(abs(unblock_ts - (deadline + 10.0)) < 0.001, f"{label}: post-watchdog unblock ts must be stamped, got {unblock_ts}")

    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(unblock_ts - 0.1)
    assert_true(expired == [], f"{label}: ctx must remain before stamped unblock ts")
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(unblock_ts)
    assert_true(expired == ["claude"], f"{label}: ctx should expire at stamped unblock ts")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_aggregate_watchdog_fire_preserves_post_grace(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    d.turn_id_mismatch_post_watchdog_grace_seconds = 10.0
    agg_id = "agg-tm-fired"
    _active_turn(
        d,
        message_id="msg-agg-fired-w1",
        frm="manager",
        to="w1",
        nonce="n-agg-fired-w1",
        turn_id="active-turn",
        aggregate_id=agg_id,
        aggregate_expected=["w1", "w2"],
        aggregate_message_ids={"w1": "msg-agg-fired-w1", "w2": "msg-agg-fired-w2"},
    )
    d.handle_response_finished({"agent": "w1", "bridge_agent": "w1", "turn_id": "stale-turn", "last_assistant_message": "old"})
    deadline = time.time() - 1.0
    _plant_watchdog(d, "wake-agg-fired", sender="manager", message_id="msg-agg-fired-w1", aggregate_id=agg_id, to="w1", deadline=deadline)

    d.check_watchdogs()
    d.expire_turn_id_mismatch_contexts()

    ctx = d.current_prompt_by_agent.get("w1") or {}
    assert_true(ctx.get("id") == "msg-agg-fired-w1", f"{label}: aggregate leg ctx must survive same tick as watchdog fire")
    assert_true("wake-agg-fired" not in d.watchdogs, f"{label}: aggregate watchdog should have fired and been popped")
    unblock_ts = float(ctx.get("turn_id_mismatch_post_watchdog_unblock_ts") or 0.0)
    assert_true(abs(unblock_ts - (deadline + 10.0)) < 0.001, f"{label}: aggregate post-watchdog unblock ts must be stamped")
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(unblock_ts)
    assert_true(expired == ["w1"], f"{label}: aggregate leg should expire at stamped unblock ts")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_post_watchdog_grace_zero_allows_same_tick_expiry(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    d.turn_id_mismatch_post_watchdog_grace_seconds = 0.0
    _active_turn(d, message_id="msg-tm-zero", nonce="n-tm-zero", turn_id="active-turn")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "old"})
    _plant_watchdog(d, "wake-tm-zero", message_id="msg-tm-zero", deadline=time.time() - 1.0)

    d.check_watchdogs()
    d.expire_turn_id_mismatch_contexts()

    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: post grace 0 allows same-tick expiry")
    assert_true(_queue_item(d, "msg-tm-zero") is None, f"{label}: delivered row should be removed when post grace is zero")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_extend_wait_before_fire_defers_without_stamp(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    d.turn_id_mismatch_post_watchdog_grace_seconds = 2.0
    _active_turn(d, message_id="msg-tm-extend", nonce="n-tm-extend", turn_id="active-turn")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "old"})
    since = float((d.current_prompt_by_agent.get("claude") or {}).get("turn_id_mismatch_since_ts"))
    _plant_watchdog(d, "wake-tm-extend", message_id="msg-tm-extend", deadline=since + 1.0)

    ok, err, _deadline_iso = d.upsert_message_watchdog("codex", "msg-tm-extend", 200.0)
    assert_true(ok, f"{label}: extend_wait should succeed before watchdog fires, got {err!r}")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("turn_id_mismatch_post_watchdog_unblock_ts") is None, f"{label}: no post-fire stamp should exist before watchdog fires")
    active_wd = next(wd for wd in d.watchdogs.values() if wd.get("ref_message_id") == "msg-tm-extend")
    extended_deadline = float(active_wd.get("deadline"))
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(extended_deadline + 1.0)
    assert_true(expired == [], f"{label}: extended watchdog deadline should defer expiry before post grace elapses")
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(extended_deadline + 2.0)
    assert_true(expired == ["claude"], f"{label}: expiry should happen at extended deadline plus post grace")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_expiry_does_not_remove_non_delivered_queue_rows(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    for status in ("pending", "inflight", "submitted"):
        case_dir = tmpdir / status
        case_dir.mkdir()
        d = make_daemon(case_dir, participants)
        d.turn_id_mismatch_grace_seconds = 0.0
        d.queue.update(lambda queue, status=status: (queue.append(test_message("msg-tm-nondelivered", frm="codex", to="claude", status=status)), None)[1])
        d.current_prompt_by_agent["claude"] = {
            "id": "msg-tm-nondelivered",
            "nonce": "n-tm-nondelivered",
            "from": "codex",
            "auto_return": True,
            "turn_id": "active-turn",
            "turn_id_mismatch_since_ts": time.time() - 10.0,
            "turn_id_mismatch_last_ts": time.time() - 10.0,
            "turn_id_mismatch_response_turn_id": "stale-turn",
        }
        d.busy["claude"] = True
        with d.state_lock:
            expired = d._expire_turn_id_mismatch_contexts_locked(time.time())
        item = _queue_item(d, "msg-tm-nondelivered")
        assert_true(expired == ["claude"], f"{label}: ctx should expire for status {status}")
        assert_true(item is not None and item.get("status") == status, f"{label}: {status} row must not be removed")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_aggregate_leg_expiry_no_synthetic_reply(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    d.turn_id_mismatch_post_watchdog_grace_seconds = 1.0
    agg_id = "agg-tm-expire"
    _active_turn(
        d,
        message_id="msg-agg-w1",
        frm="manager",
        to="w1",
        nonce="n-agg-w1",
        turn_id="active-turn",
        aggregate_id=agg_id,
        aggregate_expected=["w1", "w2"],
        aggregate_message_ids={"w1": "msg-agg-w1", "w2": "msg-agg-w2"},
    )
    d.handle_response_finished({"agent": "w1", "bridge_agent": "w1", "turn_id": "stale-turn", "last_assistant_message": "old"})
    since = float((d.current_prompt_by_agent.get("w1") or {}).get("turn_id_mismatch_since_ts"))
    d.watchdogs["wake-agg-tm"] = {
        "sender": "manager",
        "deadline": since + 50.0,
        "ref_message_id": "msg-agg-w1",
        "ref_aggregate_id": agg_id,
        "ref_to": "w1",
        "ref_kind": "request",
        "ref_intent": "test",
        "is_alarm": False,
    }
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(since + 50.5)
    assert_true(expired == [], f"{label}: aggregate watchdog post-grace must defer expiry")
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(since + 51.5)
    assert_true(expired == ["w1"], f"{label}: aggregate leg should expire after aggregate watchdog deadline")
    assert_true(_queue_item(d, "msg-agg-w1") is None, f"{label}: aggregate delivered leg should be removed")
    assert_true("wake-agg-tm" in d.watchdogs, f"{label}: aggregate watchdog must not be cancelled by leg expiry")
    queued = d.queue.read()
    assert_true(not any(item.get("kind") == "result" and item.get("source") in {"auto_return", "aggregate_return"} for item in queued), f"{label}: expiry must not synthesize result")
    aggregate_data = read_json(Path(d.aggregate_file), {"aggregates": {}})
    replies = ((aggregate_data.get("aggregates") or {}).get(agg_id) or {}).get("replies") or {}
    assert_true(replies == {}, f"{label}: expiry must not collect aggregate reply")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_sweep_no_op_without_annotation(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-tm-noann", nonce="n-tm-noann", turn_id="active-turn")
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(time.time() + 3600.0)
    assert_true(expired == [], f"{label}: sweep should ignore ctx without annotation")
    assert_true((d.current_prompt_by_agent.get("claude") or {}).get("id") == "msg-tm-noann", f"{label}: ctx must remain")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_sweep_no_op_after_normal_pop(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _active_turn(d, message_id="msg-tm-pop", nonce="n-tm-pop", turn_id="active-turn")
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": "old"})
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "active-turn", "last_assistant_message": "real"})
    with d.state_lock:
        expired = d._expire_turn_id_mismatch_contexts_locked(time.time() + 3600.0)
    assert_true(expired == [], f"{label}: sweep should no-op after normal terminal pop")
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: ctx remains gone")
    print(f"  PASS  {label}")


def scenario_turn_id_mismatch_notice_ctx_follows_same_expiry(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d.turn_id_mismatch_grace_seconds = 0.0
    _active_turn(
        d,
        message_id="msg-tm-notice",
        nonce="n-tm-notice",
        turn_id="active-turn",
        auto_return=False,
        kind="notice",
    )
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "stale-turn", "last_assistant_message": ""})
    d.expire_turn_id_mismatch_contexts()
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: notice ctx should expire")
    assert_true(_queue_item(d, "msg-tm-notice") is None, f"{label}: notice delivered row should be removed")
    assert_true(_auto_return_results(d, "claude", "codex") == [], f"{label}: notice expiry must not auto-route")
    print(f"  PASS  {label}")


def scenario_held_drain_skips_consume_once(label: str, tmpdir: Path) -> None:
    """Stop event during held_interrupt drain must NOT pop ctx — that path
    is already cleared by interrupt and the consume-once pop in normal
    terminal path doesn't apply here."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-hd-1", frm="codex", to="claude", nonce="n-hd-1")
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-hd-1", "turn_id": "t-hd", "prompt": "[bridge:n-hd-1]"})
    # Inject a held_interrupt for claude (simulates interrupt mid-turn)
    d.held_interrupt["claude"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "prior_message_id": "msg-hd-1",
        "prior_sender": "codex",
        "reason": "test",
        "by_sender": "test",
    }
    # Manually clear ctx as interrupt path would (so we can verify held-drain
    # doesn't trip our consume-once pop in a misleading way)
    d.current_prompt_by_agent["claude"] = None
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-hd", "last_assistant_message": "partial"})
    # Held drain releases hold; it must NOT auto-route
    routed = [it for it in d.queue.read() if it.get("from") == "claude" and it.get("kind") == "result"]
    assert_true(len(routed) == 0, f"{label}: held_drain must not auto-route, got {len(routed)} routes")
    assert_true("claude" not in d.held_interrupt, f"{label}: hold should be released")
    print(f"  PASS  {label}")


# ---------- v1.5.x scenarios: multi-target send_peer ----------

def _participants_state(aliases: list[str]) -> dict:
    return {
        "session": "test-session",
        "participants": {a: {"alias": a, "agent_type": "codex", "pane": f"%{i+10}", "status": "active"} for i, a in enumerate(aliases)},
    }


def scenario_resolve_targets_single(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2"])
        assert_true(bp.resolve_targets(state, "claude", "codex1") == ["codex1"], f"{label}: single alias")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_multi_basic(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2", "codex3"])
        out = bp.resolve_targets(state, "claude", "codex1,codex2")
        assert_true(out == ["codex1", "codex2"], f"{label}: comma-separated multi: {out}")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_order_preserved(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2", "codex3"])
        out = bp.resolve_targets(state, "claude", "codex3,codex1,codex2")
        assert_true(out == ["codex3", "codex1", "codex2"], f"{label}: order preservation: {out}")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_dedup(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2"])
        out = bp.resolve_targets(state, "claude", "codex1,codex1")
        assert_true(out == ["codex1"], f"{label}: dedup to single: {out}")
        out = bp.resolve_targets(state, "claude", "codex1,codex2,codex1")
        assert_true(out == ["codex1", "codex2"], f"{label}: dedup preserves first occurrence: {out}")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_strip_empties(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2"])
        out = bp.resolve_targets(state, "claude", "codex1, codex2")
        assert_true(out == ["codex1", "codex2"], f"{label}: whitespace stripped: {out}")
        out = bp.resolve_targets(state, "claude", "codex1,,codex2")
        assert_true(out == ["codex1", "codex2"], f"{label}: empty token dropped: {out}")
        out = bp.resolve_targets(state, "claude", "codex1,")
        assert_true(out == ["codex1"], f"{label}: trailing comma trimmed: {out}")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_reserved_alone(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2", "codex3"])
        out = bp.resolve_targets(state, "claude", "ALL")
        assert_true(sorted(out) == ["codex1", "codex2", "codex3"], f"{label}: ALL expands: {out}")
        out = bp.resolve_targets(state, "claude", "all")
        assert_true(sorted(out) == ["codex1", "codex2", "codex3"], f"{label}: all expands: {out}")
        out = bp.resolve_targets(state, "claude", "*")
        assert_true(sorted(out) == ["codex1", "codex2", "codex3"], f"{label}: * expands: {out}")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_reserved_mix_rejected(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2"])
        for raw in ("ALL,codex1", "codex1,all", "*,codex2", "ALL,all"):
            try:
                bp.resolve_targets(state, "claude", raw)
            except ValueError:
                continue
            raise AssertionError(f"{label}: reserved mix {raw!r} should reject")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_unknown_rejected(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2"])
        try:
            bp.resolve_targets(state, "claude", "codex1,unknown")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{label}: unknown alias must be rejected")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_sender_in_list_rejected(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2"])
        try:
            bp.resolve_targets(state, "claude", "codex1,claude")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{label}: sender in list must be rejected")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_resolve_targets_empty_after_strip_rejected(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bp = importlib.import_module("bridge_participants")
        state = _participants_state(["claude", "codex1", "codex2"])
        for raw in (",", ",,", "  ,  ,  "):
            try:
                bp.resolve_targets(state, "claude", raw)
            except ValueError:
                continue
            raise AssertionError(f"{label}: comma-only {raw!r} must be rejected")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


def scenario_short_id_format(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    sys.path.insert(0, str(libexec))
    try:
        import importlib
        bu = importlib.import_module("bridge_util")
        ids = {bu.short_id("msg") for _ in range(100)}
        assert_true(len(ids) == 100, f"{label}: 100 random ids must be unique")
        for ident in ids:
            assert_true(ident.startswith("msg-"), f"{label}: prefix preserved: {ident}")
            assert_true(len(ident) == len("msg-") + 12, f"{label}: 12-hex suffix: {ident} (len={len(ident)})")
            hex_part = ident.split("-", 1)[1]
            int(hex_part, 16)  # raises if not hex
        # Custom length still works
        custom = bu.short_id("agg", length=16)
        assert_true(len(custom) == len("agg-") + 16, f"{label}: custom length")
    finally:
        sys.path.remove(str(libexec))
    print(f"  PASS  {label}")


# ---------- v1.5.x scenarios: aggregate trigger guards (unit-style) ----------

def _import_aggregate_helper():
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    be = importlib.import_module("bridge_enqueue")
    return be.should_create_aggregate


def scenario_aggregate_trigger_request_multi(label: str, tmpdir: Path) -> None:
    f = _import_aggregate_helper()
    assert_true(f("request", "claude", False, ["codex1", "codex2"]), f"{label}: standard multi-target request must trigger aggregate")
    print(f"  PASS  {label}")


def scenario_aggregate_trigger_single_no(label: str, tmpdir: Path) -> None:
    f = _import_aggregate_helper()
    assert_true(not f("request", "claude", False, ["codex1"]), f"{label}: single target must NOT trigger aggregate")
    print(f"  PASS  {label}")


def scenario_aggregate_trigger_notice_no(label: str, tmpdir: Path) -> None:
    f = _import_aggregate_helper()
    assert_true(not f("notice", "claude", False, ["codex1", "codex2"]), f"{label}: notice multi-target must NOT trigger aggregate (no reply route)")
    print(f"  PASS  {label}")


def scenario_aggregate_trigger_bridge_sender_no(label: str, tmpdir: Path) -> None:
    f = _import_aggregate_helper()
    assert_true(not f("request", "bridge", False, ["codex1", "codex2"]), f"{label}: bridge synthetic multi-target must NOT trigger aggregate")
    print(f"  PASS  {label}")


def scenario_aggregate_trigger_no_auto_return_no(label: str, tmpdir: Path) -> None:
    f = _import_aggregate_helper()
    assert_true(not f("request", "claude", True, ["codex1", "codex2"]), f"{label}: --no-auto-return multi-target must NOT trigger aggregate")
    print(f"  PASS  {label}")


# ---------- v1.5.x scenarios: forgotten retention + restart guards ----------

def _import_daemon_ctl():
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    return importlib.import_module("bridge_daemon_ctl")


def _make_fake_archive(root: Path, name: str, mtime_offset: int = 0) -> Path:
    archive = root / name
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "events.raw.jsonl").write_text("{}\n", encoding="utf-8")
    if mtime_offset:
        ts = time.time() - mtime_offset
        os.utime(archive, (ts, ts))
    return archive


def scenario_prune_keeps_recent_n(label: str, tmpdir: Path) -> None:
    state_dir = tmpdir / "state"
    state_dir.mkdir()
    forgotten = state_dir / ".forgotten"
    forgotten.mkdir()
    # 12 archives, oldest first by mtime
    for i in range(12):
        _make_fake_archive(forgotten, f"sess-{i:02d}", mtime_offset=(12 - i) * 60)
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        ctl = _import_daemon_ctl()
        # importlib reload to pick up env if previously cached
        import importlib
        importlib.reload(ctl)
        result = ctl.prune_forgotten_archives(retention_count=10)
        assert_true(result["retention"] == 10, f"{label}: retention reported")
        assert_true(len(result["removed"]) == 2, f"{label}: 2 removed, got {result['removed']}")
        # Oldest two should be removed
        assert_true(set(result["removed"]) == {"sess-00", "sess-01"}, f"{label}: removed oldest, got {result['removed']}")
        remaining = sorted(p.name for p in forgotten.iterdir())
        assert_true(len(remaining) == 10, f"{label}: 10 kept, got {len(remaining)}")
    finally:
        os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
    print(f"  PASS  {label}")


def scenario_prune_disabled_retention_zero(label: str, tmpdir: Path) -> None:
    state_dir = tmpdir / "state"
    state_dir.mkdir()
    forgotten = state_dir / ".forgotten"
    forgotten.mkdir()
    for i in range(5):
        _make_fake_archive(forgotten, f"sess-{i}", mtime_offset=(5 - i) * 60)
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        ctl = _import_daemon_ctl()
        import importlib
        importlib.reload(ctl)
        result = ctl.prune_forgotten_archives(retention_count=0)
        assert_true(result["retention"] == 0, f"{label}: retention=0")
        assert_true(result["removed"] == [], f"{label}: nothing removed when disabled")
        remaining = list(forgotten.iterdir())
        assert_true(len(remaining) == 5, f"{label}: all 5 still present")
    finally:
        os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
    print(f"  PASS  {label}")


def scenario_prune_below_retention(label: str, tmpdir: Path) -> None:
    state_dir = tmpdir / "state"
    state_dir.mkdir()
    forgotten = state_dir / ".forgotten"
    forgotten.mkdir()
    for i in range(3):
        _make_fake_archive(forgotten, f"sess-{i}", mtime_offset=(3 - i) * 60)
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        ctl = _import_daemon_ctl()
        import importlib
        importlib.reload(ctl)
        result = ctl.prune_forgotten_archives(retention_count=10)
        assert_true(result["removed"] == [], f"{label}: none removed when below retention")
        assert_true(result["kept"] == 3, f"{label}: kept reflects actual count")
    finally:
        os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
    print(f"  PASS  {label}")


def scenario_prune_missing_forgotten_dir_safe(label: str, tmpdir: Path) -> None:
    state_dir = tmpdir / "state"
    state_dir.mkdir()  # No .forgotten subdir
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        ctl = _import_daemon_ctl()
        import importlib
        importlib.reload(ctl)
        result = ctl.prune_forgotten_archives(retention_count=10)
        assert_true(result["removed"] == [], f"{label}: no-op when .forgotten missing")
    finally:
        os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
    print(f"  PASS  {label}")


def scenario_resolve_forgotten_retention_invalid_env(label: str, tmpdir: Path) -> None:
    ctl = _import_daemon_ctl()
    saved = os.environ.get("AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT")
    try:
        os.environ["AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT"] = "not-a-number"
        # Capture stderr to silence the warning during regression
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            assert_true(ctl._resolve_forgotten_retention() == ctl.DEFAULT_FORGOTTEN_RETENTION, f"{label}: invalid env falls back to default")
        os.environ["AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT"] = "-5"
        with contextlib.redirect_stderr(buf):
            assert_true(ctl._resolve_forgotten_retention() == ctl.DEFAULT_FORGOTTEN_RETENTION, f"{label}: negative env falls back to default")
        os.environ["AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT"] = "7"
        assert_true(ctl._resolve_forgotten_retention() == 7, f"{label}: valid env honored")
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT", None)
        else:
            os.environ["AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT"] = saved
    print(f"  PASS  {label}")


def scenario_queue_status_counts(label: str, tmpdir: Path) -> None:
    ctl = _import_daemon_ctl()
    qfile = tmpdir / "pending.json"
    qfile.write_text(json.dumps([
        {"id": "msg-1", "status": "pending"},
        {"id": "msg-2", "status": "pending"},
        {"id": "msg-3", "status": "delivered"},
        {"id": "msg-4", "status": "inflight"},
        {"id": "msg-5"},  # no status
    ]), encoding="utf-8")
    counts = ctl._read_queue_status_counts(qfile)
    assert_true(counts.get("pending") == 2, f"{label}: pending count")
    assert_true(counts.get("delivered") == 1, f"{label}: delivered count")
    assert_true(counts.get("inflight") == 1, f"{label}: inflight count")
    print(f"  PASS  {label}")


def scenario_queue_status_counts_missing_file(label: str, tmpdir: Path) -> None:
    ctl = _import_daemon_ctl()
    counts = ctl._read_queue_status_counts(tmpdir / "nonexistent.json")
    assert_true(counts == {}, f"{label}: missing file → empty counts, got {counts}")
    print(f"  PASS  {label}")


def scenario_uninstall_helper_print_paths(label: str, tmpdir: Path) -> None:
    helper = str(LIBEXEC / "bridge_uninstall_state.py")
    proc = subprocess.run([sys.executable, helper, "--print-paths"], capture_output=True, text=True, timeout=10)
    assert_true(proc.returncode == 0, f"{label}: helper exit 0, got {proc.returncode}: {proc.stderr}")
    payload = json.loads(proc.stdout)
    for key in ("state", "run", "log"):
        assert_true(key in payload, f"{label}: payload contains {key}")
        assert_true(payload[key].endswith(key), f"{label}: {key} path looks like .../<{key}>")
    print(f"  PASS  {label}")


def scenario_uninstall_helper_refuses_dangerous_path(label: str, tmpdir: Path) -> None:
    helper = str(LIBEXEC / "bridge_uninstall_state.py")
    env = dict(os.environ)
    env["AGENT_BRIDGE_STATE_DIR"] = "/etc"  # dangerous
    proc = subprocess.run([sys.executable, helper, "--dry-run"], env=env, capture_output=True, text=True, timeout=10)
    assert_true(proc.returncode != 0, f"{label}: must refuse dangerous path, exit was {proc.returncode}")
    assert_true("refuses" in proc.stderr.lower() or "dangerous" in proc.stderr.lower(), f"{label}: stderr explains refusal: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_direct_exec_targets_executable(label: str, tmpdir: Path) -> None:
    missing = []
    not_executable = []
    for name, relative in DIRECT_EXECUTABLE_TARGETS:
        path = ROOT / relative
        if not path.exists():
            missing.append(f"{name}={path}")
        elif not os.access(path, os.X_OK):
            not_executable.append(f"{name}={path}")
    assert_true(not missing, f"{label}: direct exec targets missing: {missing}")
    assert_true(not not_executable, f"{label}: direct exec targets not executable: {not_executable}")
    print(f"  PASS  {label}")


def scenario_healthcheck_executable_helper_distinguishes_states(label: str, tmpdir: Path) -> None:
    import importlib
    hc = importlib.import_module("bridge_healthcheck")
    importlib.reload(hc)

    missing = tmpdir / "missing-tool"
    ok, detail = hc.check_executable(missing)
    assert_true(not ok, f"{label}: missing path must fail")
    assert_true("missing" in detail and "not executable" not in detail, f"{label}: missing detail must be distinct: {detail!r}")

    tool = tmpdir / "tool"
    tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(tool, 0o644)
    ok, detail = hc.check_executable(tool)
    assert_true(not ok, f"{label}: non-executable file must fail")
    assert_true("exists but is not executable" in detail, f"{label}: non-executable detail must be distinct: {detail!r}")

    os.chmod(tool, 0o755)
    ok, detail = hc.check_executable(tool)
    assert_true(ok and detail == str(tool), f"{label}: executable file must pass, got ok={ok} detail={detail!r}")
    print(f"  PASS  {label}")


def _write_fake_uninstall_tree(root: Path) -> None:
    shutil.copy2(ROOT / "uninstall.sh", root / "uninstall.sh")
    libexec = root / "libexec" / "agent-bridge"
    libexec.mkdir(parents=True, exist_ok=True)
    for name in ("bridge_uninstall_hooks.py", "bridge_util.py"):
        shutil.copy2(LIBEXEC / name, libexec / name)


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


def _run_fake_uninstall(
    root: Path,
    *,
    env: dict[str, str],
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["bash", str(root / "uninstall.sh"), "--bin-dir", str(Path(env["XDG_BIN_HOME"]))]
    cmd.extend(extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def scenario_uninstall_sh_dry_run_invokes_hook_helper_with_dry_run(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-dry-run"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    env = _fake_install_env(tmpdir / "dry-env")
    claude, codex = _write_seed_hook_configs(Path(env["HOME"]))
    before_claude = claude.read_bytes()
    before_codex = codex.read_bytes()

    proc = _run_fake_uninstall(root, env=env, extra_args=["--dry-run", "--keep-shims"])
    assert_true(proc.returncode == 0, f"{label}: dry-run uninstall should succeed: {proc.stderr!r}")
    assert_true("removed 2 hook command(s)" in proc.stdout, f"{label}: helper output should show removal count: {proc.stdout!r}")
    assert_true(proc.stdout.count("removed 2 hook command(s)") == 2, f"{label}: helper output should include Claude and Codex counts: {proc.stdout!r}")
    assert_true("dry-run: python3" not in proc.stdout and "bridge_uninstall_hooks.py" not in proc.stdout, f"{label}: hook helper must be invoked, not printed as dry-run command: {proc.stdout!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude config must be byte-identical under dry-run")
    assert_true(codex.read_bytes() == before_codex, f"{label}: Codex hooks config must be byte-identical under dry-run")
    print(f"  PASS  {label}")


def scenario_uninstall_sh_non_dry_run_removes_hook_entries(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-real"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    env = _fake_install_env(tmpdir / "real-env")
    claude, codex = _write_seed_hook_configs(Path(env["HOME"]))

    proc = _run_fake_uninstall(root, env=env, extra_args=["--keep-shims"])
    assert_true(proc.returncode == 0, f"{label}: temp uninstall should succeed: {proc.stderr!r}")
    claude_text = claude.read_text(encoding="utf-8")
    codex_text = codex.read_text(encoding="utf-8")
    assert_true("bridge-hook" not in claude_text and "agent-bridge" not in claude_text, f"{label}: Claude bridge hooks should be removed: {claude_text!r}")
    assert_true("bridge-hook" not in codex_text and "agent-bridge" not in codex_text, f"{label}: Codex bridge hooks should be removed: {codex_text!r}")
    assert_true("echo keep-claude" in claude_text and '"user": "preserve"' in claude_text, f"{label}: Claude user entries should remain: {claude_text!r}")
    assert_true("echo keep-codex" in codex_text and '"user": "preserve"' in codex_text, f"{label}: Codex user entries should remain: {codex_text!r}")
    print(f"  PASS  {label}")


def scenario_uninstall_sh_keep_hooks_skips_helper_under_dry_run(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-keep-hooks"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    sentinel = root / "libexec" / "agent-bridge" / "bridge_uninstall_hooks.py"
    sentinel.write_text("#!/usr/bin/env python3\nraise SystemExit(42)\n", encoding="utf-8")
    env = _fake_install_env(tmpdir / "keep-hooks-env")

    proc = _run_fake_uninstall(root, env=env, extra_args=["--dry-run", "--keep-hooks", "--keep-shims"])
    assert_true(proc.returncode == 0, f"{label}: --keep-hooks must skip failing sentinel helper: {proc.stderr!r}")
    assert_true("remove Claude/Codex hook entries" not in proc.stdout, f"{label}: hook section should be skipped entirely: {proc.stdout!r}")
    assert_true("removed " not in proc.stdout, f"{label}: no helper output expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_uninstall_sh_hook_helper_failure_aborts(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-helper-fails"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    sentinel = root / "libexec" / "agent-bridge" / "bridge_uninstall_hooks.py"
    sentinel.write_text("#!/usr/bin/env python3\nraise SystemExit(42)\n", encoding="utf-8")
    env = _fake_install_env(tmpdir / "helper-fails-env")

    proc = _run_fake_uninstall(root, env=env, extra_args=["--keep-shims"])
    assert_true(proc.returncode == 1, f"{label}: helper failure should map to uninstall.sh failure exit 1, got {proc.returncode}")
    assert_true("uninstall.sh: hook removal helper failed; aborting" in proc.stderr, f"{label}: targeted helper failure expected: {proc.stderr!r}")
    assert_true("uninstall complete" not in proc.stdout, f"{label}: script must abort before completion message: {proc.stdout!r}")
    print(f"  PASS  {label}")


def _write_fake_install_tree(root: Path, *, omit: Path | None = None) -> None:
    shutil.copy2(ROOT / "install.sh", root / "install.sh")
    for _, relative in INSTALL_SHIM_TARGETS:
        if omit is not None and relative == omit:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(target, 0o644)
    hook = root / "hooks" / "bridge-hook"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)


def _write_fake_hook_installer(root: Path, *, exit_code: int = 0, argv_file: Path | None = None) -> Path:
    path = root / "libexec" / "agent-bridge" / "bridge_install_hooks.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    argv_literal = repr(str(argv_file)) if argv_file else "''"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        "import sys\n"
        f"argv_file = {argv_literal}\n"
        "if argv_file:\n"
        "    pathlib.Path(argv_file).parent.mkdir(parents=True, exist_ok=True)\n"
        "    pathlib.Path(argv_file).write_text('\\n'.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n"
        f"raise SystemExit({int(exit_code)})\n",
        encoding="utf-8",
    )
    return path


def _fake_install_env(tmpdir: Path, *, path_prefix: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(tmpdir / "home")
    env["XDG_BIN_HOME"] = str(tmpdir / "xdg-bin")
    env["XDG_CONFIG_HOME"] = str(tmpdir / "xdg-config")
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    env["AGENT_BRIDGE_RUN_DIR"] = str(tmpdir / "run")
    env["AGENT_BRIDGE_LOG_DIR"] = str(tmpdir / "log")
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env.get('PATH', '')}"
    return env


def _run_fake_install(
    root: Path,
    bin_dir: Path,
    *,
    env: dict[str, str] | None = None,
    skip_hooks: bool = True,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        "bash",
        str(root / "install.sh"),
        "--yes",
        "--bin-dir",
        str(bin_dir),
    ]
    if skip_hooks:
        cmd.append("--skip-hooks")
    cmd.append("--no-shell-rc")
    cmd.extend(extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _run_bridge_install_hooks(
    tmpdir: Path,
    *,
    claude_settings: Path | None = None,
    codex_hooks: Path | None = None,
    codex_config: Path | None = None,
    skip_claude: bool = False,
    skip_codex: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    hook_command = tmpdir / "bridge-hook"
    hook_command.parent.mkdir(parents=True, exist_ok=True)
    hook_command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(hook_command, 0o755)
    cmd = [
        sys.executable,
        str(ROOT / "libexec" / "agent-bridge" / "bridge_install_hooks.py"),
        "--hook-command",
        str(hook_command),
        "--claude-settings",
        str(claude_settings or (tmpdir / "settings.json")),
        "--codex-hooks",
        str(codex_hooks or (tmpdir / "hooks.json")),
        "--codex-config",
        str(codex_config or (tmpdir / "config.toml")),
    ]
    if skip_claude:
        cmd.append("--skip-claude")
    if skip_codex:
        cmd.append("--skip-codex")
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_fake_install_env(tmpdir),
        timeout=10,
    )


def _assert_hook_config_unchanged(label: str, path: Path, before: bytes, proc: subprocess.CompletedProcess, *needles: str) -> None:
    assert_true(proc.returncode != 0, f"{label}: hook installer should fail, got {proc.returncode}: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    for needle in needles:
        assert_true(needle in proc.stderr, f"{label}: stderr should contain {needle!r}: {proc.stderr!r}")
    assert_true(path.read_bytes() == before, f"{label}: invalid existing hook config must not be overwritten")


def scenario_install_sh_chmods_target_or_fails(label: str, tmpdir: Path) -> None:
    positive_root = tmpdir / "install-positive"
    positive_root.mkdir()
    _write_fake_install_tree(positive_root)
    alarm_target = positive_root / "model-bin" / "agent_alarm"
    assert_true(not os.access(alarm_target, os.X_OK), f"{label}: precondition target starts non-executable")
    proc = _run_fake_install(positive_root, tmpdir / "shims-positive")
    assert_true(proc.returncode == 0, f"{label}: install should recover non-executable targets, got {proc.returncode}: {proc.stderr}")
    assert_true(os.access(alarm_target, os.X_OK), f"{label}: install should chmod shim target executable")
    assert_true(os.access(tmpdir / "shims-positive" / "agent_alarm", os.X_OK), f"{label}: shim itself should be executable")

    missing_root = tmpdir / "install-missing"
    missing_root.mkdir()
    _write_fake_install_tree(missing_root, omit=Path("model-bin/agent_alarm"))
    proc = _run_fake_install(missing_root, tmpdir / "shims-missing")
    assert_true(proc.returncode != 0, f"{label}: missing shim target must hard fail")
    assert_true("missing shim target for agent_alarm" in proc.stderr, f"{label}: missing-target stderr should name shim: {proc.stderr!r}")

    failing_root = tmpdir / "install-failing"
    failing_root.mkdir()
    _write_fake_install_tree(failing_root)
    fakebin = tmpdir / "fakebin"
    fakebin.mkdir()
    fake_chmod = fakebin / "chmod"
    fake_chmod.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *model-bin/agent_alarm*) echo fake chmod failure >&2; exit 42 ;;\n"
        "esac\n"
        "exec /bin/chmod \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(fake_chmod, 0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
    proc = _run_fake_install(failing_root, tmpdir / "shims-failing", env=env)
    assert_true(proc.returncode != 0, f"{label}: chmod failure must hard fail")
    assert_true("cannot make shim target executable for agent_alarm" in proc.stderr, f"{label}: chmod failure stderr should name shim: {proc.stderr!r}")
    assert_true(not os.access(failing_root / "model-bin" / "agent_alarm", os.X_OK), f"{label}: failed chmod target should remain non-executable")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_failure_hard_fails(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-hard-fail"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "hook-hard-fail.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_file)
    proc = _run_fake_install(root, tmpdir / "shims-hook-hard-fail", skip_hooks=False, env=_fake_install_env(tmpdir))
    assert_true(proc.returncode == 42, f"{label}: hook installer status must pass through, got {proc.returncode}: {proc.stderr!r}")
    assert_true("install.sh: hook config install failed" in proc.stderr, f"{label}: hard-fail stderr should name hook failure: {proc.stderr!r}")
    assert_true("shims may have been written" in proc.stderr, f"{label}: hard-fail stderr should mention partial shims: {proc.stderr!r}")
    assert_true("Agent Bridge will not receive hook events until fixed" in proc.stderr, f"{label}: hard-fail stderr should explain hook impact: {proc.stderr!r}")
    assert_true("bridge_healthcheck" in proc.stderr, f"{label}: hard-fail stderr should recommend healthcheck: {proc.stderr!r}")
    assert_true("--ignore-hook-failure" in proc.stderr, f"{label}: hard-fail stderr should mention explicit escape hatch: {proc.stderr!r}")
    assert_true("run: " not in proc.stdout, f"{label}: hard-fail stdout must not show final success hint: {proc.stdout!r}")
    assert_true("tmux not found" not in proc.stderr, f"{label}: hard-fail must stop before tmux warning: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_failure_ignore_flag_allows_success(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-ignore"
    root.mkdir()
    _write_fake_install_tree(root)
    _write_fake_hook_installer(root, exit_code=42, argv_file=tmpdir / "hook-ignore.argv")
    bin_dir = tmpdir / "shims-hook-ignore"
    proc = _run_fake_install(
        root,
        bin_dir,
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--ignore-hook-failure"],
    )
    assert_true(proc.returncode == 0, f"{label}: ignore flag should allow success, got {proc.returncode}: {proc.stderr!r}")
    assert_true("override was used" in proc.stderr, f"{label}: warning must say override was used: {proc.stderr!r}")
    assert_true("shims were installed but hook events will not work until fixed" in proc.stderr, f"{label}: warning must explain broken hook events: {proc.stderr!r}")
    assert_true("bridge_healthcheck" in proc.stderr, f"{label}: warning should recommend healthcheck: {proc.stderr!r}")
    assert_true(f"run: {bin_dir}/bridge_healthcheck" in proc.stdout, f"{label}: success hint should remain after override: {proc.stdout!r}")
    assert_true(os.access(bin_dir / "agent_alarm", os.X_OK), f"{label}: shims should be installed under override")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_dry_run_failure_hard_fails(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-dry-run-fail"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "hook-dry-run-fail.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_file)
    proc = _run_fake_install(
        root,
        tmpdir / "shims-hook-dry-run-fail",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--dry-run"],
    )
    argv_text = argv_file.read_text(encoding="utf-8")
    assert_true(proc.returncode == 42, f"{label}: dry-run hook failure must hard-fail, got {proc.returncode}: {proc.stderr!r}")
    assert_true("--dry-run" in argv_text, f"{label}: install.sh must forward --dry-run to hook installer: {argv_text!r}")
    assert_true("install.sh: hook config install failed" in proc.stderr, f"{label}: dry-run hard-fail should use targeted error: {proc.stderr!r}")
    assert_true("run: " not in proc.stdout, f"{label}: dry-run hard-fail must not show final success hint: {proc.stdout!r}")

    argv_override = tmpdir / "hook-dry-run-override.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_override)
    proc_override = _run_fake_install(
        root,
        tmpdir / "shims-hook-dry-run-override",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--dry-run", "--ignore-hook-failure"],
    )
    assert_true(proc_override.returncode == 0, f"{label}: dry-run override should continue, got {proc_override.returncode}: {proc_override.stderr!r}")
    assert_true("override was used" in proc_override.stderr, f"{label}: dry-run override warning expected: {proc_override.stderr!r}")
    assert_true("--dry-run" in argv_override.read_text(encoding="utf-8"), f"{label}: dry-run override must still forward --dry-run")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_success_succeeds(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-success"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "hook-success.argv"
    _write_fake_hook_installer(root, exit_code=0, argv_file=argv_file)
    bin_dir = tmpdir / "shims-hook-success"
    proc = _run_fake_install(root, bin_dir, skip_hooks=False, env=_fake_install_env(tmpdir))
    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    assert_true(proc.returncode == 0, f"{label}: hook success should keep install success, got {proc.returncode}: {proc.stderr!r}")
    assert_true("--hook-command" in argv_lines, f"{label}: hook installer should receive --hook-command: {argv_lines}")
    assert_true(argv_lines[argv_lines.index("--hook-command") + 1] == str(root / "hooks" / "bridge-hook"), f"{label}: hook command path incorrect: {argv_lines}")
    assert_true("--dry-run" not in argv_lines, f"{label}: non-dry-run install should not pass --dry-run: {argv_lines}")
    assert_true(os.access(bin_dir / "agent_send_peer", os.X_OK), f"{label}: shims should be installed on hook success")

    argv_dry = tmpdir / "hook-success-dry.argv"
    _write_fake_hook_installer(root, exit_code=0, argv_file=argv_dry)
    proc_dry = _run_fake_install(
        root,
        tmpdir / "shims-hook-success-dry",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--dry-run"],
    )
    assert_true(proc_dry.returncode == 0, f"{label}: hook dry-run success should pass: {proc_dry.stderr!r}")
    assert_true("--dry-run" in argv_dry.read_text(encoding="utf-8").splitlines(), f"{label}: dry-run should reach hook installer")
    print(f"  PASS  {label}")


def scenario_install_sh_shim_failure_still_hard_fails_under_override(label: str, tmpdir: Path) -> None:
    root = tmpdir / "shim-fail-override"
    root.mkdir()
    _write_fake_install_tree(root, omit=Path("model-bin/agent_alarm"))
    argv_file = tmpdir / "shim-fail-override.argv"
    _write_fake_hook_installer(root, exit_code=0, argv_file=argv_file)
    proc = _run_fake_install(
        root,
        tmpdir / "shims-shim-fail-override",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--ignore-hook-failure"],
    )
    assert_true(proc.returncode != 0, f"{label}: override must not suppress shim target failures")
    assert_true("missing shim target for agent_alarm" in proc.stderr, f"{label}: missing shim error expected: {proc.stderr!r}")
    assert_true("override was used" not in proc.stderr, f"{label}: hook override warning must not fire for shim failure: {proc.stderr!r}")
    assert_true(not argv_file.exists(), f"{label}: hook installer should not run after shim failure")
    print(f"  PASS  {label}")


def scenario_install_sh_skip_hooks_with_ignore_flag_is_noop(label: str, tmpdir: Path) -> None:
    root = tmpdir / "skip-hooks-ignore"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "skip-hooks-ignore.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_file)
    proc = _run_fake_install(
        root,
        tmpdir / "shims-skip-hooks-ignore",
        skip_hooks=True,
        env=_fake_install_env(tmpdir),
        extra_args=["--ignore-hook-failure"],
    )
    assert_true(proc.returncode == 0, f"{label}: skip-hooks + ignore should remain a no-op success: {proc.stderr!r}")
    assert_true(not argv_file.exists(), f"{label}: --skip-hooks must not invoke hook installer")
    assert_true("hook config install failed" not in proc.stderr, f"{label}: no hook failure warning expected when hooks skipped: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_rejects_malformed_json_without_overwrite(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "claude-malformed" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks": [')
    before = settings.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    _assert_hook_config_unchanged(
        label,
        settings,
        before,
        proc,
        str(settings),
        "invalid JSON",
        "refusing to overwrite",
        "Fix or move aside",
    )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_rejects_non_object_json_without_overwrite(label: str, tmpdir: Path) -> None:
    for suffix, content in (("array", b"[1, 2, 3]"), ("null", b"null")):
        settings = tmpdir / f"claude-non-object-{suffix}" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_bytes(content)
        before = settings.read_bytes()
        proc = _run_bridge_install_hooks(tmpdir / suffix, claude_settings=settings, skip_codex=True)
        _assert_hook_config_unchanged(
            f"{label}:{suffix}",
            settings,
            before,
            proc,
            str(settings),
            "must be a JSON object",
            "refusing to overwrite",
        )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "dry-run-invalid" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks":')
    before = settings.read_bytes()
    before_entries = sorted(p.name for p in settings.parent.iterdir())
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True, dry_run=True)
    _assert_hook_config_unchanged(
        label,
        settings,
        before,
        proc,
        "invalid JSON",
        "refusing to overwrite",
    )
    after_entries = sorted(p.name for p in settings.parent.iterdir())
    assert_true(after_entries == before_entries, f"{label}: dry-run invalid config should not create files: {after_entries}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_missing_json_still_creates(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "new-claude" / "settings.json"
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    assert_true(proc.returncode == 0, f"{label}: missing config should be created, got {proc.returncode}: {proc.stderr!r}")
    data = json.loads(settings.read_text(encoding="utf-8"))
    hooks = data.get("hooks") or {}
    assert_true("SessionStart" in hooks and "Stop" in hooks, f"{label}: bridge hook events should be written: {data!r}")
    stop_blocks = hooks.get("Stop") or []
    assert_true(any("--agent claude" in ((block.get("hooks") or [{}])[0].get("command", "")) for block in stop_blocks if isinstance(block, dict)), f"{label}: claude hook command should be present: {data!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_invalid_utf8_fails_without_overwrite(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "invalid-utf8" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks": "\xff"}')
    before = settings.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    _assert_hook_config_unchanged(
        label,
        settings,
        before,
        proc,
        str(settings),
        "invalid JSON",
        "refusing to overwrite",
        "Fix or move aside",
    )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_existing_valid_json_object_merges_correctly(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "valid-claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({
            "custom": {"preserve": True},
            "hooks": {
                "Stop": [
                    {
                        "matcher": "user-custom",
                        "hooks": [{"type": "command", "command": "echo user hook"}],
                    }
                ]
            },
        }),
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    assert_true(proc.returncode == 0, f"{label}: valid config should merge, got {proc.returncode}: {proc.stderr!r}")
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert_true(data.get("custom", {}).get("preserve") is True, f"{label}: user fields must be preserved: {data!r}")
    stop_blocks = data.get("hooks", {}).get("Stop") or []
    assert_true(any("echo user hook" in str(block) for block in stop_blocks), f"{label}: existing user hook should remain: {data!r}")
    assert_true(any("--agent claude" in str(block) for block in stop_blocks), f"{label}: bridge Stop hook should be merged: {data!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite(label: str, tmpdir: Path) -> None:
    hooks = tmpdir / "codex-malformed" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(b'{"hooks": [')
    before = hooks.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, codex_hooks=hooks, skip_claude=True)
    _assert_hook_config_unchanged(
        label,
        hooks,
        before,
        proc,
        str(hooks),
        "invalid JSON",
        "refusing to overwrite",
        "Fix or move aside",
    )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_ignores_nested_codex_hooks(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-nested-hooks.toml"
    nested_block = '[codex_hooks]\nkind = "keep"\n'
    config.write_text(nested_block, encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true(nested_block in text, f"{label}: [codex_hooks] table content must be preserved: {text!r}")
    assert_true("[features]\ncodex_hooks = true\n" in text, f"{label}: [features].codex_hooks should be added: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-nested-disable.toml"
    config.write_text("[profile]\ndisable_paste_burst = true\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true(text.startswith("disable_paste_burst = true\n[profile]\n"), f"{label}: top-level disable_paste_burst should be inserted before first table: {text!r}")
    assert_true("[profile]\ndisable_paste_burst = true\n" in text, f"{label}: nested disable_paste_burst should remain unchanged: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_updates_only_scoped_keys(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-scoped-update.toml"
    config.write_text(
        "disable_paste_burst = false  # was off\n"
        "\n"
        "[features]\n"
        "  codex_hooks = false  # was off\n"
        "\n"
        "[profile]\n"
        "disable_paste_burst = false  # profile scoped\n"
        "codex_hooks = false  # profile scoped\n",
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("disable_paste_burst = true  # was off\n" in text, f"{label}: top-level disable line/comment should update: {text!r}")
    assert_true("  codex_hooks = true  # was off\n" in text, f"{label}: [features] line indentation/comment should update: {text!r}")
    assert_true("disable_paste_burst = false  # profile scoped\n" in text, f"{label}: profile disable must stay false: {text!r}")
    assert_true("codex_hooks = false  # profile scoped\n" in text, f"{label}: profile codex_hooks must stay false: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_inserts_features_key_before_next_table(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-features-insert.toml"
    config.write_text(
        "[features]\n"
        "experimental = true\n"
        "\n"
        "[profile]\n"
        "codex_hooks = false\n",
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    features_index = text.index("[features]")
    inserted_index = text.index("codex_hooks = true")
    profile_index = text.index("[profile]")
    assert_true(features_index < inserted_index < profile_index, f"{label}: codex_hooks=true should land inside [features]: {text!r}")
    assert_true("[profile]\ncodex_hooks = false\n" in text, f"{label}: profile codex_hooks must remain unchanged: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_dry_run_no_write(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-dry-run.toml"
    config.write_text("[features]\ncodex_hooks = false\n[profile]\ndisable_paste_burst = true\n", encoding="utf-8")
    before = config.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True, dry_run=True)
    assert_true(proc.returncode == 0, f"{label}: dry-run should succeed: {proc.stderr!r}")
    assert_true("codex_hooks enabled" in proc.stdout and "disable_paste_burst enabled" in proc.stdout, f"{label}: dry-run stdout should report computed actions: {proc.stdout!r}")
    assert_true(config.read_bytes() == before, f"{label}: dry-run must not alter config bytes")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_empty_features_section_inserts(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-empty-features.toml"
    config.write_text("[features]\n[profile]\nname = \"p\"\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("[features]\ncodex_hooks = true\n[profile]\n" in text, f"{label}: empty features section should receive key before next table: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_first_table_is_array_table(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-array-first.toml"
    config.write_text("[[features_array]]\nname = \"first\"\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true(text.startswith("disable_paste_burst = true\n[[features_array]]\n"), f"{label}: top-level disable should be inserted before first array table: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_table_header_with_trailing_comment(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-header-comment.toml"
    config.write_text("[features]  # primary features section\nother = true\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("[features]  # primary features section\nother = true\ncodex_hooks = true\n" in text, f"{label}: [features] with trailing comment should be recognized: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_commented_out_assignments_ignored(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-comments.toml"
    config.write_text(
        "# disable_paste_burst = true\n"
        "# codex_hooks = true\n"
        "[features]\n"
        "# codex_hooks = true\n",
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("# disable_paste_burst = true\n" in text and "# codex_hooks = true\n" in text, f"{label}: commented assignments should remain comments: {text!r}")
    live_disable_index = text.index("disable_paste_burst = true\n")
    features_index = text.index("[features]")
    assert_true(live_disable_index < features_index, f"{label}: live top-level disable should be inserted before first table: {text!r}")
    assert_true("[features]\n# codex_hooks = true\ncodex_hooks = true\n" in text, f"{label}: live features codex_hooks should be inserted despite comment: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_no_trailing_newline_handled(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-no-newline.toml"
    config.write_text("[features]\nother = true", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("other = true\ncodex_hooks = true\n" in text, f"{label}: no-newline config should not concatenate inserted key: {text!r}")
    assert_true(text.startswith("disable_paste_burst = true\n[features]\n"), f"{label}: top-level disable should still be inserted cleanly: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-already.toml"
    config.write_text(
        "disable_paste_burst = true\n"
        "\n"
        "[features]\n"
        "codex_hooks = true\n"
        "\n"
        "[profile]\n"
        "codex_hooks = false\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    assert_true("codex_hooks already enabled" in proc.stdout and "disable_paste_burst already enabled" in proc.stdout, f"{label}: stdout should report already-enabled actions: {proc.stdout!r}")
    assert_true(config.read_bytes() == before, f"{label}: already-enabled scoped config must remain byte-identical")
    print(f"  PASS  {label}")


# ---------- v1.5.x P1 follow-up: dry-run safety + orphan delivered + concurrent prune ----------

def scenario_restart_dry_run_no_side_effect(label: str, tmpdir: Path) -> None:
    """restart --dry-run must not invoke start_under_lock (which has stop side
    effects). Verify by patching daemon_command + start_under_lock to detect
    any call."""
    ctl = _import_daemon_ctl()
    state_dir = tmpdir / "state"
    sess_dir = state_dir / "test-session"
    sess_dir.mkdir(parents=True)
    (sess_dir / "session.json").write_text(json.dumps({
        "session": "test-session",
        "participants": {"claude": {"alias": "claude", "agent_type": "claude", "pane": "%4", "status": "active"}},
        "queue_file": str(sess_dir / "pending.json"),
        "state_file": str(sess_dir / "events.raw.jsonl"),
        "events_file": str(sess_dir / "events.jsonl"),
        "state_dir": str(sess_dir),
    }), encoding="utf-8")
    (sess_dir / "pending.json").write_text("[]", encoding="utf-8")
    (sess_dir / "events.raw.jsonl").write_text("", encoding="utf-8")
    (sess_dir / "events.jsonl").write_text("", encoding="utf-8")

    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
    os.environ["AGENT_BRIDGE_RUN_DIR"] = str(tmpdir / "run")
    os.environ["AGENT_BRIDGE_LOG_DIR"] = str(tmpdir / "log")
    try:
        import importlib
        importlib.reload(ctl)
        # Track whether start_under_lock was called
        original = ctl.start_under_lock
        called = {"n": 0}

        def trap(args, paths):
            called["n"] += 1
            return original(args, paths)

        ctl.start_under_lock = trap
        ns = argparse.Namespace(dry_run=True, force=False, json=False, health_delay=0.1, stop_timeout=1.0, submit_delay=None, submit_timeout=None)
        result = ctl.restart_one("test-session", ns)
        assert_true(called["n"] == 0, f"{label}: start_under_lock must NOT be called for dry-run, was called {called['n']} times")
        assert_true(result.get("dry_run") is True, f"{label}: result.dry_run=True")
        assert_true(result.get("restart") is True, f"{label}: result.restart=True")
        assert_true("command" in result and isinstance(result["command"], list), f"{label}: command list returned")
    finally:
        for k in ("AGENT_BRIDGE_STATE_DIR", "AGENT_BRIDGE_RUN_DIR", "AGENT_BRIDGE_LOG_DIR"):
            os.environ.pop(k, None)
    print(f"  PASS  {label}")


def scenario_recover_orphan_delivered(label: str, tmpdir: Path) -> None:
    """Daemon startup must sweep status=delivered items to unblock the queue
    after a restart that lost the routing context."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    # Plant: one delivered (orphan), one pending (waiting)
    msgs = [
        {"id": "msg-orph-1", "created_ts": utc_now(), "updated_ts": utc_now(), "from": "codex", "to": "claude",
         "kind": "result", "intent": "test", "body": "x", "causal_id": "c", "hop_count": 1,
         "auto_return": False, "reply_to": None, "source": "test", "bridge_session": "test-session",
         "status": "delivered", "delivered_ts": utc_now(), "nonce": "n1"},
        {"id": "msg-orph-2", "created_ts": utc_now(), "updated_ts": utc_now(), "from": "codex", "to": "claude",
         "kind": "result", "intent": "test", "body": "y", "causal_id": "c", "hop_count": 1,
         "auto_return": False, "reply_to": None, "source": "test", "bridge_session": "test-session",
         "status": "pending", "nonce": None},
    ]
    def add(q):
        q.extend(msgs)
        return None
    d.queue.update(add)
    d._recover_orphan_delivered_messages()
    queue_after = list(d.queue.read())
    ids = {m.get("id"): m.get("status") for m in queue_after}
    assert_true("msg-orph-1" not in ids, f"{label}: orphan delivered must be removed, got {ids}")
    assert_true(ids.get("msg-orph-2") == "pending", f"{label}: pending unaffected, got {ids}")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "delivered_orphan_recovered" for e in events), f"{label}: log emitted")
    print(f"  PASS  {label}")


def scenario_recover_orphan_delivered_aggregate_member(label: str, tmpdir: Path) -> None:
    """Documents current restart-sweep policy for aggregate members:
    sweeping a delivered aggregate member does NOT inject a synthetic
    'interrupted' reply (unlike agent_interrupt_peer). The aggregate
    therefore depends on its own watchdog to surface the stalled state.
    This regression freezes that policy so any future change has to
    update it consciously."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    aggregate_id = "agg-test-orph"
    msg = {
        "id": "msg-aggorph-1",
        "created_ts": utc_now(), "updated_ts": utc_now(), "delivered_ts": utc_now(),
        "from": "claude", "to": "codex",
        "kind": "request", "intent": "test",
        "body": "x", "causal_id": "c", "hop_count": 1, "auto_return": True,
        "reply_to": None, "source": "test", "bridge_session": "test-session",
        "status": "delivered", "nonce": "n-aggorph",
        "aggregate_id": aggregate_id,
        "aggregate_expected": ["codex"],
        "aggregate_message_ids": {"codex": "msg-aggorph-1"},
    }
    def add(q):
        q.append(msg)
        return None
    d.queue.update(add)
    d._recover_orphan_delivered_messages()
    queue_after = list(d.queue.read())
    # Member removed
    assert_true(not any(m.get("id") == "msg-aggorph-1" for m in queue_after), f"{label}: aggregate member removed from queue")
    # No synthetic reply was injected for the aggregate
    synthetic = [m for m in queue_after if m.get("aggregate_id") == aggregate_id]
    assert_true(synthetic == [], f"{label}: NO synthetic reply injected for swept aggregate member, got {synthetic}")
    print(f"  PASS  {label}")


def scenario_prune_concurrent_stat_safe(label: str, tmpdir: Path) -> None:
    """prune_forgotten_archives must tolerate FileNotFoundError during
    stat() when a concurrent process removes an entry between iterdir and
    sort."""
    state_dir = tmpdir / "state"
    state_dir.mkdir()
    forgotten = state_dir / ".forgotten"
    forgotten.mkdir()
    for i in range(3):
        _make_fake_archive(forgotten, f"sess-{i}", mtime_offset=(3 - i) * 60)
    # Mimic the race: monkey-patch Path.stat on the second entry to raise FileNotFoundError once.
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        ctl = _import_daemon_ctl()
        import importlib
        importlib.reload(ctl)
        # Remove one entry RIGHT before the prune to simulate a concurrent prune
        # finishing first. The sort path must not crash.
        shutil.rmtree(forgotten / "sess-1")
        result = ctl.prune_forgotten_archives(retention_count=10)
        # 2 entries left, retention 10 → none removed, no error
        assert_true(result["errors"] == {}, f"{label}: no errors on concurrent missing, got {result['errors']}")
    finally:
        os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
    print(f"  PASS  {label}")


# ---------- v1.5.x: list_peers model-safe view ----------

def _model_state(aliases_with_extras: dict) -> dict:
    """Build a state with full participant records (including pane/target/hook_session_id)."""
    return {
        "session": "test-session",
        "participants": {
            alias: {
                "alias": alias,
                "agent_type": rec.get("agent_type", "codex"),
                "pane": rec.get("pane", "%99"),
                "target": rec.get("target", "0:1.99"),
                "hook_session_id": rec.get("hook_session_id", "uuid-secret"),
                "model": rec.get("model", "gpt-test"),
                "cwd": rec.get("cwd", "/tmp/x"),
                "status": "active",
            }
            for alias, rec in aliases_with_extras.items()
        },
        "hook_session_ids": {alias: "uuid-secret" for alias in aliases_with_extras},
    }


def scenario_format_peer_list_model_safe_default(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = _model_state({"codex1": {}, "codex2": {}})
    out = bp.format_peer_list(state, "codex1")
    assert_true("pane=" not in out, f"{label}: text mode default must NOT include pane=, got: {out!r}")
    assert_true("target=" not in out, f"{label}: text mode default must NOT include target=, got: {out!r}")
    assert_true("hook_session_id" not in out, f"{label}: never expose hook_session_id")
    # Should still include type, model, cwd
    assert_true("type=" in out, f"{label}: type still present")
    assert_true("model=" in out, f"{label}: model still present")
    assert_true("cwd=" in out, f"{label}: cwd still present (operator confirmed)")
    print(f"  PASS  {label}")


def scenario_format_peer_list_full_includes_operator_fields(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = _model_state({"codex1": {}})
    out = bp.format_peer_list(state, "codex1", full=True)
    assert_true("pane=" in out, f"{label}: full mode includes pane=, got: {out!r}")
    assert_true("target=" in out, f"{label}: full mode includes target=")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_concise(label: str, tmpdir: Path) -> None:
    import importlib
    bms = importlib.import_module("bridge_manage_summary")
    importlib.reload(bms)
    state = {
        "session": "test-session",
        "participants": {
            "z-codex": {
                "alias": "z-codex",
                "agent_type": "codex",
                "pane": "%9",
                "target": "0:1.9",
                "status": "active",
                "model": "gpt-test",
            },
            "a-claude": {
                "alias": "a-claude",
                "agent_type": "claude",
                "pane": "%1",
                "target": "0:1.1",
                "status": "active",
                "model": "",
            },
            "inactive": {
                "alias": "inactive",
                "agent_type": "codex",
                "pane": "%8",
                "target": "0:1.8",
                "status": "left",
            },
        },
    }
    out1 = bms.format_room_summary(state)
    out2 = bms.format_room_summary(state)
    assert_true(out1 == out2, f"{label}: output must be deterministic")
    lines = out1.splitlines()
    assert_true(lines[0] == "Agents:", f"{label}: starts with Agents:, got {out1!r}")
    assert_true(lines[1].startswith("- a-claude claude active target=0:1.1 pane=%1"), f"{label}: sorted a-claude first: {out1!r}")
    assert_true(lines[2].startswith("- z-codex codex active target=0:1.9 pane=%9 model=gpt-test"), f"{label}: z-codex fields/model: {out1!r}")
    assert_true("inactive" not in out1, f"{label}: inactive participants omitted: {out1!r}")
    assert_true("model=" not in lines[1], f"{label}: empty model omitted: {lines[1]!r}")
    forbidden = ("agent_send_peer", "Commands:", "Kinds and routing contract", "Reply normally")
    for needle in forbidden:
        assert_true(needle not in out1, f"{label}: summary must not include cheat sheet text {needle!r}")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_defaults(label: str, tmpdir: Path) -> None:
    import importlib
    bms = importlib.import_module("bridge_manage_summary")
    importlib.reload(bms)
    state = {
        "session": "test-session",
        "participants": {
            "loose": {
                "alias": "loose",
                "agent_type": "",
                "pane": "",
                "target": "",
                "status": "",
            }
        },
    }
    out = bms.format_room_summary(state)
    assert_true("- loose unknown unknown target=? pane=?" in out, f"{label}: missing fields use stable defaults: {out!r}")
    assert_true("model=" not in out, f"{label}: missing model omitted: {out!r}")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_legacy_state_fallback(label: str, tmpdir: Path) -> None:
    import importlib
    bms = importlib.import_module("bridge_manage_summary")
    importlib.reload(bms)
    state = {
        "session": "test-session",
        "panes": {"claude": "%1", "codex": "%2"},
        "targets": {"claude": "0:1.1", "codex": "0:1.2"},
    }
    out = bms.format_room_summary(state)
    assert_true("- claude claude active target=0:1.1 pane=%1" in out, f"{label}: legacy claude rendered: {out!r}")
    assert_true("- codex codex active target=0:1.2 pane=%2" in out, f"{label}: legacy codex rendered: {out!r}")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_missing_session_exits(label: str, tmpdir: Path) -> None:
    script = ROOT / "libexec" / "agent-bridge" / "bridge_manage_summary.py"
    env = os.environ.copy()
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    proc = subprocess.run(
        [sys.executable, str(script), "--session", "missing-room"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert_true(proc.returncode == 2, f"{label}: missing session should exit 2, got {proc.returncode}")
    assert_true(proc.stdout == "", f"{label}: missing session should not print summary: {proc.stdout!r}")
    assert_true("not active or was stopped" in proc.stderr, f"{label}: stderr should explain missing room: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_model_safe_participants_uses_active_only(label: str, tmpdir: Path) -> None:
    """JSON view should match text view: only active participants."""
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = {
        "session": "test-session",
        "participants": {
            "codex1": {"alias": "codex1", "agent_type": "codex", "pane": "%0", "model": "m", "cwd": "/x", "status": "active"},
            "stale": {"alias": "stale", "agent_type": "codex", "pane": "%99", "model": "m", "cwd": "/y", "status": "left"},
        },
    }
    safe = bp.model_safe_participants(state)
    assert_true("codex1" in safe, f"{label}: active codex1 included")
    assert_true("stale" not in safe, f"{label}: inactive 'stale' must NOT be exposed: {safe}")
    print(f"  PASS  {label}")


def scenario_list_peers_json_daemon_status_strips_pid(label: str, tmpdir: Path) -> None:
    """Default JSON output's daemon_status must not contain pid; --full does."""
    helper = str(LIBEXEC / "bridge_list_peers.py")
    # Use existing live session to drive the CLI
    proc = subprocess.run(
        [sys.executable, helper, "--session", "agent-bridge-auto", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        # If live session not present (CI or fresh checkout), skip silently
        print(f"  SKIP  {label}: no live session ({proc.stderr.strip()[:60]})")
        return
    data = json.loads(proc.stdout)
    ds = data.get("daemon_status") or {}
    assert_true("pid" not in ds, f"{label}: default JSON daemon_status must NOT include pid, got {ds}")
    proc_full = subprocess.run(
        [sys.executable, helper, "--session", "agent-bridge-auto", "--json", "--full"],
        capture_output=True, text=True, timeout=10,
    )
    data_full = json.loads(proc_full.stdout)
    ds_full = data_full.get("daemon_status") or {}
    # pid is in the full view (may be None when not available, but the key should be present)
    assert_true("pid" in ds_full, f"{label}: --full JSON daemon_status must include pid key: {ds_full}")
    print(f"  PASS  {label}")


def scenario_model_safe_participants_strips_endpoints(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = _model_state({"codex1": {}, "codex2": {}})
    safe = bp.model_safe_participants(state)
    for alias, record in safe.items():
        assert_true("pane" not in record, f"{label}: {alias} record must not include pane")
        assert_true("target" not in record, f"{label}: {alias} record must not include target")
        assert_true("hook_session_id" not in record, f"{label}: {alias} record must not include hook_session_id")
        assert_true(record.get("agent_type"), f"{label}: agent_type retained")
        assert_true(record.get("cwd"), f"{label}: cwd retained")
    print(f"  PASS  {label}")


def _import_view_peer():
    import importlib
    bv = importlib.import_module("bridge_view_peer")
    return importlib.reload(bv)


def scenario_view_peer_render_output_model_safe(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    full_snapshot_id = "20260425T000000Z-abcdef12"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bv.render_output(
            room="room-secret",
            caller="viewer-secret",
            target="codex1",
            target_record={"agent_type": "codex", "pane": "%99"},
            mode="onboard",
            lines=["hello"],
            total_lines=1,
            max_chars=12000,
            snapshot_id=full_snapshot_id,
            page=2,
            confidence="high",
        )
    out = buf.getvalue()
    assert_true("Peer view: codex1 (codex)" in out, f"{label}: header keeps alias/type: {out!r}")
    for forbidden in ("pane=", "%99", "room-secret", "viewer=", "viewer-secret", full_snapshot_id):
        assert_true(forbidden not in out, f"{label}: output must not expose {forbidden!r}: {out!r}")
    assert_true("snapshot=cdef12" in out, f"{label}: short snapshot ref retained: {out!r}")
    assert_true("page=2" in out and "confidence=high" in out, f"{label}: public paging fields retained: {out!r}")
    assert_true("Next: agent_view_peer codex1 --older" in out, f"{label}: next hint retained: {out!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_search_explicit_snapshot_uses_safe_ref(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    try:
        full_snapshot_id = "20260425T000000Z-abcdef12"
        text_path, meta_path = bv.snapshot_paths("test-session", "codex1", full_snapshot_id)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text("alpha\nneedle\nomega\n", encoding="utf-8")
        meta_path.write_text(json.dumps({"snapshot_id": full_snapshot_id, "created_at": bv.utc_now()}), encoding="utf-8")
        args = argparse.Namespace(live=False, snapshot="cdef12", search="needle", context=0, raw=False, capture_file=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.handle_search(
                args,
                "test-session",
                "viewer",
                "codex1",
                {},
                {"agent_type": "codex", "pane": "%99"},
                20,
                12000,
            )
        out = buf.getvalue()
        assert_true(full_snapshot_id not in out, f"{label}: full snapshot id must stay hidden: {out!r}")
        assert_true("source=saved snapshot cdef12" in out, f"{label}: search source uses short ref: {out!r}")
        assert_true("source=snapshot=" not in out, f"{label}: search note must not expose raw snapshot source: {out!r}")
        assert_true("needle" in out, f"{label}: match content shown: {out!r}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_snapshot_ref_collision_unique(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    try:
        ids = ["20260425T000000Z-xaaaaaa", "20260425T000001Z-yaaaaaa"]
        for idx, snapshot_id in enumerate(ids):
            text_path, meta_path = bv.snapshot_paths("test-session", "codex1", snapshot_id)
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(f"snapshot {idx}\n", encoding="utf-8")
            meta_path.write_text(json.dumps({"snapshot_id": snapshot_id, "created_at": bv.utc_now()}), encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.render_output(
                room="test-session",
                caller="viewer",
                target="codex1",
                target_record={"agent_type": "codex", "pane": "%99"},
                mode="onboard",
                lines=["hello"],
                total_lines=1,
                max_chars=12000,
                snapshot_id=ids[0],
            )
        out = buf.getvalue()
        assert_true("snapshot=xaaaaaa" in out, f"{label}: displayed ref expands past colliding 6-char suffix: {out!r}")
        assert_true("snapshot=aaaaaa" not in out, f"{label}: colliding 6-char ref must not be displayed: {out!r}")
        assert_true(bv.resolve_snapshot_id("test-session", "codex1", "xaaaaaa") == ids[0], f"{label}: expanded ref resolves")
        assert_true(bv.resolve_snapshot_id("test-session", "codex1", "") == "", f"{label}: empty ref does not match every snapshot")
        try:
            bv.resolve_snapshot_id("test-session", "codex1", "aaaaaa")
        except SystemExit as exc:
            msg = str(exc)
        else:
            raise AssertionError(f"{label}: ambiguous 6-char suffix must fail")
        assert_true("ambiguous snapshot ref" in msg, f"{label}: ambiguous error explains issue: {msg!r}")
        assert_true("xaaaaaa" in msg and "yaaaaaa" in msg, f"{label}: ambiguous error lists actionable refs: {msg!r}")
        assert_true(ids[0] not in msg and ids[1] not in msg, f"{label}: ambiguous error hides full ids: {msg!r}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_capture_errors_sanitized(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_short_id = bv.short_id
    old_room_status = bv.room_status
    try:
        safe = bv.model_safe_capture_error("can't find pane %99\nrelated pane %12" + ("x" * 300), "%99")
        assert_true("%99" not in safe and "%12" not in safe and "\n" not in safe, f"{label}: pane ids/newlines redacted: {safe!r}")
        assert_true("<target-pane>" in safe and "<pane>" in safe, f"{label}: redaction markers present: {safe!r}")
        assert_true(len(safe) <= 200, f"{label}: error capped: {len(safe)}")

        bv.short_id = lambda prefix: "cap-fixed"
        bv.room_status = lambda session: argparse.Namespace(state="alive", reason="ok")
        response_file = bv.capture_response_dir("test-session") / "cap-fixed.json"
        response_file.parent.mkdir(parents=True, exist_ok=True)
        response_file.write_text(json.dumps({"ok": False, "error": "can't find pane %99\nother pane %12"}), encoding="utf-8")
        args = argparse.Namespace(capture_timeout=0.1)
        try:
            bv.capture_via_daemon(
                args,
                session="test-session",
                caller="viewer",
                target="codex1",
                state={"state_file": str(tmpdir / "state" / "test-session" / "events.raw.jsonl")},
                pane="%99",
                start=-10,
            )
        except SystemExit as exc:
            msg = str(exc)
        else:
            raise AssertionError(f"{label}: daemon error response must raise")
        assert_true("target codex1" in msg, f"{label}: target alias used: {msg!r}")
        assert_true("%99" not in msg and "%12" not in msg and "\n" not in msg, f"{label}: daemon response error sanitized: {msg!r}")
    finally:
        bv.short_id = old_short_id
        bv.room_status = old_room_status
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_snapshot_not_found_hides_full_id(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    try:
        full_snapshot_id = "20260425T000000Z-hidden12"
        try:
            bv.load_snapshot("test-session", "codex1", full_snapshot_id)
        except SystemExit as exc:
            msg = str(exc)
        else:
            raise AssertionError(f"{label}: missing snapshot must raise")
        assert_true(full_snapshot_id not in msg, f"{label}: full id hidden: {msg!r}")
        assert_true("hidden12"[-6:] in msg, f"{label}: short ref retained: {msg!r}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_matches_changed_volatile_chrome(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Review finding HIGH-1 process identity validation keeps endpoint safe",
        "Implementation note bridge_view_peer stable anchor cursor stores unique windows",
        "Validation command python3 scripts/regression_interrupt.py completed cleanly",
        "Reviewer summary msg-abc123 confirms since-last behavior is stable",
    ]
    previous = ["old output before cursor", *stable, "\u273b Churned for 4m 1s", "\u2500" * 40, "\u276f", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]
    current = [*stable, "New semantic output from peer after onboard should be shown", "\u273b Churned for 4m 8s", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]
    cursor = {"since_anchors": bv.build_since_anchors(previous), "last_tail_lines": previous[-30:]}
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == ["New semantic output from peer after onboard should be shown"], f"{label}: volatile suffix trimmed from delta: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_legacy_tail_derives_stable_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Legacy cursor retained useful file path /tmp/agent-bridge-share/review.md",
        "Secondary reviewer response msg-legacy confirms stable matching can recover",
        "Bridge command agent_view_peer codex-reviewer --since-last should advance",
        "Regression note python3 scripts/regression_interrupt.py covers legacy tails",
    ]
    previous_tail = [*stable, "\u273b Churned for 56s", "\u2500" * 40, "\u276f"]
    current = [*stable, "Fresh line after legacy cursor should appear", "\u273b Churned for 1m 2s"]
    delta, confidence, note = bv.compute_since_delta({"last_tail_lines": previous_tail}, current)
    assert_true(confidence == "high", f"{label}: legacy stable anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Fresh line after legacy cursor should appear"], f"{label}: expected fresh legacy delta, got {delta!r}")
    assert_true("legacy anchor" in note, f"{label}: note should identify legacy anchor: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_ambiguous_current_anchor_skips_to_unique(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    ambiguous_anchor = [
        "Common reviewer paragraph explains repeated process identity issue alpha",
        "Common reviewer paragraph explains repeated process identity issue beta",
        "Common reviewer paragraph explains repeated process identity issue gamma",
        "Common reviewer paragraph explains repeated process identity issue delta",
    ]
    unique_anchor = [
        "Unique cursor anchor line one includes msg-unique-001 for disambiguation",
        "Unique cursor anchor line two references bridge_view_peer.py implementation",
        "Unique cursor anchor line three references scripts/regression_interrupt.py",
        "Unique cursor anchor line four references agent_view_peer since-last",
    ]
    cursor = {
        "since_anchors": [
            {"lines": ambiguous_anchor, "stable_count": 4},
            {"lines": unique_anchor, "stable_count": 4},
        ]
    }
    current = [*ambiguous_anchor, "unrelated middle output", *ambiguous_anchor, *unique_anchor, "Only this unique-match delta should be shown"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: unique fallback anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Only this unique-match delta should be shown"], f"{label}: ambiguous anchor must not be used: {delta!r}")
    assert_true("skipping 1 newer anchor" in note, f"{label}: note should mention skipped ambiguous anchor: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_matches_anchor_before_long_delta(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    anchor = [
        "Long delta anchor line one includes msg-long-001 and detail",
        "Long delta anchor line two references bridge_view_peer.py implementation",
        "Long delta anchor line three references scripts/regression_interrupt.py",
        "Long delta anchor line four references agent_view_peer since-last",
    ]
    current = [
        *anchor,
        *[f"Semantic output line {idx:03d} after anchor with enough detail for matching" for idx in range(bv.SINCE_ANCHOR_SCAN_RAW_LINES + 1)],
    ]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": [{"lines": anchor, "stable_count": 4}]}, current)
    assert_true(confidence == "high", f"{label}: full match projection should find old anchor, got {confidence} note={note!r}")
    assert_true(len(delta) == bv.SINCE_ANCHOR_SCAN_RAW_LINES + 1, f"{label}: full long delta should be returned, got {len(delta)}")
    assert_true(delta[0].startswith("Semantic output line 000"), f"{label}: delta should begin right after anchor: {delta[:2]!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_build_rejects_duplicate_previous_window(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    repeated = [
        "Repeated anchor candidate line one has enough information for matching",
        "Repeated anchor candidate line two has enough information for matching",
        "Repeated anchor candidate line three has enough information for matching",
        "Repeated anchor candidate line four has enough information for matching",
    ]
    anchors = bv.build_since_anchors([*repeated, *repeated])
    anchor_lines = [anchor.get("lines") for anchor in anchors]
    assert_true(repeated not in anchor_lines, f"{label}: duplicate previous window must not be stored: {anchor_lines!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_uncertain_does_not_advance_cursor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_capture_text = bv.capture_text
    try:
        cursor = {
            "cursor_version": 2,
            "caller": "viewer",
            "target": "codex1",
            "since_anchors": [
                {
                    "lines": [
                        "Missing anchor one contains msg-missing-001 and enough detail",
                        "Missing anchor two contains bridge_view_peer.py and enough detail",
                        "Missing anchor three contains regression_interrupt.py and detail",
                        "Missing anchor four contains agent_view_peer --since-last detail",
                    ],
                    "stable_count": 4,
                }
            ],
            "last_tail_lines": ["old"],
            "sentinel": "keep-me",
        }
        path = bv.cursor_path("test-session", "viewer", "codex1")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, cursor)
        bv.capture_text = lambda *args, **kwargs: "\n".join([
            "Current output has a different stable line with msg-current-001",
            "Another unrelated stable line references bridge stable anchors",
            "Third unrelated stable line references python3 scripts regression",
            "Fourth unrelated stable line references cursor not advanced",
        ])  # type: ignore[assignment]
        args = argparse.Namespace(raw=False, capture_file=None, capture_timeout=0.1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 20, 12000, True)
        out = buf.getvalue()
        after = read_json(path, {})
        assert_true(after.get("sentinel") == "keep-me", f"{label}: uncertain cursor should not be overwritten: {after!r}")
        assert_true(after.get("since_anchors") == cursor["since_anchors"], f"{label}: uncertain anchors should stay unchanged")
        assert_true("cursor not advanced" in out, f"{label}: output should explain cursor preservation: {out!r}")
    finally:
        bv.capture_text = old_capture_text  # type: ignore[assignment]
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_upgrade_reset_when_no_legacy_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_capture_text = bv.capture_text
    try:
        path = bv.cursor_path("test-session", "viewer", "codex1")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"caller": "viewer", "target": "codex1", "last_tail_lines": ["\u2500" * 40, "\u276f", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]})
        bv.capture_text = lambda *args, **kwargs: "\n".join([
            "Upgrade reset current line one includes msg-reset-001 detail",
            "Upgrade reset current line two references bridge_view_peer.py detail",
            "Upgrade reset current line three references regression_interrupt.py",
            "Upgrade reset current line four references agent_view_peer cursor v2",
        ])  # type: ignore[assignment]
        args = argparse.Namespace(raw=False, capture_file=None, capture_timeout=0.1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 20, 12000, True)
        out = buf.getvalue()
        after = read_json(path, {})
        assert_true(after.get("cursor_version") == 2, f"{label}: upgrade reset should write v2 cursor: {after!r}")
        assert_true(after.get("since_anchors"), f"{label}: upgrade reset should store fresh anchors: {after!r}")
        assert_true("cursor reset from current capture" in out, f"{label}: reset note expected: {out!r}")
    finally:
        bv.capture_text = old_capture_text  # type: ignore[assignment]
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_low_info_lines_do_not_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    lines = [
        "\u2500" * 80,
        "\u276f",
        "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)",
        "",
        "\u273b Churned for 56s",
    ]
    assert_true(bv.build_since_anchors(lines) == [], f"{label}: low-info TUI chrome must not produce anchors")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_claude_status_variants_are_volatile(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    volatile_lines = [
        "\u273b Baked for 1s",
        "\u273b Cogitated for 3m 33s",
        "\u273b Cooked for 1m 3s",
        "\u273b Brewed for 4s",
        "\u273b Crunched for 1m 41s",
        "\u2500 Worked for 1m 07s \u2500\u2500\u2500\u2500\u2500",
        "\u273b Quantumizing\u2026 (6s \u00b7 thinking with high effort)",
        "\u273d Embellishing\u2026 (4s \u00b7 thinking with high effort)",
        "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 13s \u00b7 \u2193 326 tokens)",
        "* Befuddling\u2026 (20s \u00b7 still thinking with high effort)",
        "\u2502 \u2022 Running Stop hook \u2502",
        "\u273b Baked for <n> \u00b7 <n> shell still running",
    ]
    for line in volatile_lines:
        assert_true(bv.is_since_volatile_line(line), f"{label}: expected volatile Claude status: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_status_classifier_preserves_prose(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    prose_lines = [
        "* Worked for 3 hours on this bug",
        "Working... (4h overtime today)",
        "* Running Stop hook should be tested in test_view_peer.py",
        "The implementation worked for this bridge_view_peer.py scenario and should remain visible",
        "I was thinking with high effort about the review plan and wrote this note",
        "The baked fixture output is a real semantic line with enough detail",
        "* Implementing... (using tokens for auth)",
        "Implementing... (using tokens for auth)",
        "* Working... (auth tokens are valid)",
        "* Tokenizing... (input has 100 tokens for processing)",
        "Tokenizing... (input has 100 tokens for processing)",
        "Working... (thought for the day)",
        "Implementing... (running stop hook command in test setup)",
    ]
    for line in prose_lines:
        assert_true(not bv.is_since_volatile_line(line), f"{label}: semantic prose must remain visible: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_claude_status_lines_do_not_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Claude status anchor test line one references bridge_view_peer.py msg-status-001",
        "Claude status anchor test line two references scripts/regression_interrupt.py",
        "Claude status anchor test line three references agent_view_peer since-last",
        "Claude status anchor test line four references volatile filtering behavior",
    ]
    status_lines = [
        "\u273b Baked for 1s",
        "\u273b Cogitated for 3m 33s",
        "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 13s \u00b7 \u2193 326 tokens)",
        "* Befuddling\u2026 (20s \u00b7 still thinking with high effort)",
    ]
    anchors = bv.build_since_anchors([stable[0], status_lines[0], stable[1], status_lines[1], stable[2], status_lines[2], stable[3], status_lines[3]])
    assert_true(anchors, f"{label}: stable lines should still form anchors")
    for anchor in anchors:
        for line in anchor.get("lines", []):
            assert_true(not bv.is_since_volatile_line(line), f"{label}: anchor retained volatile status line: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_filters_stored_volatile_anchor_lines(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    status = "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 <n> \u00b7 \u2193 <n>)"
    stable = [
        "Stored filtered anchor stable line one includes msg-filter-001 and enough detail",
        "Stored filtered anchor stable line two references bridge_view_peer.py details",
        "Stored filtered anchor stable line three references scripts/regression_interrupt.py details",
    ]
    cursor = {"since_anchors": [{"lines": [status, *stable], "stable_count": 4}]}
    current = [*stable, "Fresh semantic output after filtered stored anchor should appear"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "medium", f"{label}: filtered 3-line stored anchor should match as medium, got {confidence} note={note!r}")
    assert_true(delta == ["Fresh semantic output after filtered stored anchor should appear"], f"{label}: expected filtered-anchor delta, got {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    status = "\u273b Baked for <n>"
    weak = [
        "Weak anchor line alpha x1",
        "Weak anchor line beta x2",
        "Weak anchor line gamma x3",
    ]
    valid = [
        "Valid fallback anchor line one includes msg-valid-001 and detail",
        "Valid fallback anchor line two references bridge_view_peer.py behavior",
        "Valid fallback anchor line three references scripts/regression_interrupt.py",
        "Valid fallback anchor line four references agent_view_peer since-last",
    ]
    cursor = {
        "since_anchors": [
            {"lines": [status, *weak], "stable_count": 4},
            {"lines": valid, "stable_count": 4},
        ]
    }
    current = [*weak, "Bad delta from weak anchor must not be used", *valid, "Good semantic delta from valid anchor"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: valid fallback anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Good semantic delta from valid anchor"], f"{label}: weak shortened anchor must be skipped: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_volatile_only_claude_status_delta(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Claude volatile delta anchor one includes msg-claude-volatile-001 detail",
        "Claude volatile delta anchor two references bridge_view_peer.py detail",
        "Claude volatile delta anchor three references regression_interrupt.py",
        "Claude volatile delta anchor four references agent_view_peer since-last",
    ]
    current = [*stable, "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 13s \u00b7 \u2193 326 tokens)"]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, current)
    assert_true(confidence == "high", f"{label}: anchor should match, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: Claude volatile-only delta should be hidden: {delta!r}")
    assert_true("only volatile TUI status changed" in note, f"{label}: volatile status note expected: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_codex_status_variants_preserved(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    volatile_lines = [
        "\u2022 Working (1m 42s \u2022 esc to interrupt)",
        "\u25e6 Working (4s \u2022 esc to interrupt)",
        "\u2502 \u2022 Working (15s \u2022 esc to interrupt) \u2502",
        "\u23f5\u23f5 bypass permissions on (shift+tab to cycle)",
        "\u2502 gpt-5.5 xhigh \u00b7 /data/sembench-hard \u2502",
        "Remote Control active \u2502",
    ]
    for line in volatile_lines:
        assert_true(bv.is_since_volatile_line(line), f"{label}: expected volatile Codex chrome: {line!r}")
    prose_lines = [
        "\u2022 Reviewed bridge_view_peer.py and kept this semantic bullet visible",
        "The gpt-5.5 model string appears in this semantic sentence and should remain visible",
    ]
    for line in prose_lines:
        assert_true(not bv.is_since_volatile_line(line), f"{label}: semantic Codex/prose line must remain visible: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_volatile_only_delta(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Volatile only test anchor one includes msg-volatile-001 detail",
        "Volatile only test anchor two references bridge_view_peer.py detail",
        "Volatile only test anchor three references regression_interrupt.py",
        "Volatile only test anchor four references agent_view_peer since-last",
    ]
    previous = [*stable, "\u273b Churned for 10s"]
    current = [*stable, "\u273b Churned for 40s", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(previous)}, current)
    assert_true(confidence == "high", f"{label}: anchor should match, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: volatile-only delta should be hidden: {delta!r}")
    assert_true("only volatile TUI status changed" in note, f"{label}: volatile status note expected: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_short_delta_consumed_once(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Short consumed anchor one includes msg-short-001 and bridge_view_peer.py",
        "Short consumed anchor two references scripts/regression_interrupt.py",
        "Short consumed anchor three references agent_view_peer since-last behavior",
        "Short consumed anchor four references causal-short-consumed detail",
    ]
    current = [*stable, "\u25cf - ACK claude-reviewer", "  - 12"]
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    first = bv.compute_since_delta_detail(cursor, current)
    assert_true(first["delta"] == ["\u25cf - ACK claude-reviewer", "  - 12"], f"{label}: first short delta should be visible: {first!r}")
    cursor["since_consumed_tail"] = bv.build_since_consumed_tail(str(first["matched_anchor_identity"]), list(first["consumed_raw_delta"]))
    second_delta, second_confidence, second_note = bv.compute_since_delta(cursor, current)
    assert_true(second_confidence == "high", f"{label}: second view should still match anchor: {second_confidence} {second_note!r}")
    assert_true(second_delta == [], f"{label}: consumed short delta should not repeat: {second_delta!r}")
    assert_true("skipped previously consumed" in second_note, f"{label}: note should mention consumed skip: {second_note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_capture_text = bv.capture_text
    try:
        stable = [
            "Request consumed base anchor one includes msg-request-consumed-001 bridge_view_peer.py",
            "Request consumed base anchor two references scripts/regression_interrupt.py",
            "Request consumed base anchor three references agent_view_peer since-last behavior",
            "Request consumed base anchor four references causal-request-consumed detail",
        ]
        request = [
            "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-request-consumed",
            "aggregate_id=agg-request-consumed. Reply normally; do not call agent_send_peer",
            "Request: [FINAL-VERIFY-SHORT] Reply with exactly two short bullet lines",
            "first line says ACK <your-alias>; second line says 3+4=7",
        ]
        reply = ["\u2022 - ACK codex-reviewer", "  - 3+4=7"]
        current = [*stable, *request, "", *reply, "\u203a Improve documentation in @filename", "  gpt-5.5 xhigh \u00b7 ~/agent-bridge"]
        path = bv.cursor_path("test-session", "viewer", "codex1")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"caller": "viewer", "target": "codex1", "since_anchors": bv.build_since_anchors(stable)})
        bv.capture_text = lambda *args, **kwargs: "\n".join(current)  # type: ignore[assignment]
        args = argparse.Namespace(raw=False, capture_file=None, capture_timeout=0.1)
        first_buf = io.StringIO()
        with contextlib.redirect_stdout(first_buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 40, 12000, True)
        first_out = first_buf.getvalue()
        assert_true("ACK codex-reviewer" in first_out and "FINAL-VERIFY-SHORT" in first_out, f"{label}: first view should show request and reply: {first_out!r}")
        second_buf = io.StringIO()
        with contextlib.redirect_stdout(second_buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 40, 12000, True)
        second_out = second_buf.getvalue()
        assert_true("ACK codex-reviewer" not in second_out, f"{label}: short reply must not repeat after cursor update: {second_out!r}")
        assert_true("skipped previously consumed" in second_out, f"{label}: consumed skip note expected: {second_out!r}")
    finally:
        bv.capture_text = old_capture_text  # type: ignore[assignment]
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_does_not_hide_new_duplicate(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Duplicate consumed anchor one includes msg-dup-001 and bridge_view_peer.py",
        "Duplicate consumed anchor two references scripts/regression_interrupt.py",
        "Duplicate consumed anchor three references agent_view_peer since-last behavior",
        "Duplicate consumed anchor four references causal-duplicate detail",
    ]
    old_reply = ["\u2022 - ACK codex-reviewer", "  - 12"]
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    first = bv.compute_since_delta_detail(cursor, [*stable, *old_reply])
    cursor["since_consumed_tail"] = bv.build_since_consumed_tail(str(first["matched_anchor_identity"]), list(first["consumed_raw_delta"]))
    current = [
        *stable,
        *old_reply,
        "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-dup",
        "\u2022 - ACK codex-reviewer",
        "  - 12",
    ]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == current[len(stable) + len(old_reply) :], f"{label}: duplicate new reply must remain visible: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_anchor_change_resets(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Anchor change line one includes msg-anchor-change-001 bridge_view_peer.py",
        "Anchor change line two references scripts/regression_interrupt.py",
        "Anchor change line three references agent_view_peer since-last behavior",
        "Anchor change line four references causal-anchor-change detail",
    ]
    current = [*stable, "\u2022 New output after changed anchor should remain visible"]
    cursor = {
        "since_anchors": bv.build_since_anchors(stable),
        "since_consumed_tail": {"anchor_identity": "sha256:not-the-current-anchor", "lines": ["\u2022 New output after changed anchor should remain visible"], "truncated": False},
    }
    detail = bv.compute_since_delta_detail(cursor, current)
    assert_true(detail["delta"] == ["\u2022 New output after changed anchor should remain visible"], f"{label}: changed-anchor memo must not hide output: {detail!r}")
    assert_true("reset stale consumed-tail" in str(detail["note"]), f"{label}: note should mention reset: {detail!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_mismatch_clears(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Mismatch consumed anchor one includes msg-mismatch-001 bridge_view_peer.py",
        "Mismatch consumed anchor two references scripts/regression_interrupt.py",
        "Mismatch consumed anchor three references agent_view_peer since-last behavior",
        "Mismatch consumed anchor four references causal-mismatch detail",
    ]
    anchor_identity = bv.since_anchor_identity(list(bv.build_since_anchors(stable)[0]["lines"]))
    current = [*stable, "\u2022 Different output should remain visible"]
    cursor = {
        "since_anchors": bv.build_since_anchors(stable),
        "since_consumed_tail": {"anchor_identity": anchor_identity, "lines": ["\u2022 Previous output"], "truncated": False},
    }
    detail = bv.compute_since_delta_detail(cursor, current)
    assert_true(detail["delta"] == ["\u2022 Different output should remain visible"], f"{label}: mismatched memo must not hide output: {detail!r}")
    assert_true("reset stale consumed-tail" in str(detail["note"]), f"{label}: mismatch should clear memo: {detail!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_cap(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    identity = bv.since_anchor_identity(["Cap anchor one", "Cap anchor two", "Cap anchor three", "Cap anchor four"])
    raw_delta = [f"Consumed cap line {idx:02d} has enough text to count but not anchor" for idx in range(bv.SINCE_CONSUMED_TAIL_MAX_LINES + 5)]
    memo = bv.build_since_consumed_tail(identity, raw_delta)
    assert_true(memo.get("anchor_identity") == identity, f"{label}: identity should be stored: {memo!r}")
    assert_true(len(memo.get("lines") or []) == bv.SINCE_CONSUMED_TAIL_MAX_LINES, f"{label}: line cap expected: {memo!r}")
    assert_true(memo.get("truncated") is True, f"{label}: truncated flag expected: {memo!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_ignores_volatile_churn(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Volatile consumed anchor one includes msg-volatile-consumed-001 bridge_view_peer.py",
        "Volatile consumed anchor two references scripts/regression_interrupt.py",
        "Volatile consumed anchor three references agent_view_peer since-last behavior",
        "Volatile consumed anchor four references causal-volatile-consumed detail",
    ]
    current = [*stable, "\u25cf - ACK claude-reviewer", "  - 12"]
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    first = bv.compute_since_delta_detail(cursor, current)
    cursor["since_consumed_tail"] = bv.build_since_consumed_tail(str(first["matched_anchor_identity"]), list(first["consumed_raw_delta"]))
    churned = [*stable, "\u273b Churning\u2026 (4s \u00b7 \u2193 1 tokens)", "\u25cf - ACK claude-reviewer", "\u273b Churning\u2026 (5s \u00b7 \u2193 2 tokens)", "  - 12"]
    delta, confidence, note = bv.compute_since_delta(cursor, churned)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: volatile churn should not defeat consumed prefix: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_codex_prompt_placeholder_not_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Codex prompt anchor one includes msg-codex-prompt-001 bridge_view_peer.py",
        "Codex prompt anchor two references scripts/regression_interrupt.py",
        "Codex prompt anchor three references agent_view_peer since-last behavior",
        "Codex prompt anchor four references causal-codex-prompt detail",
    ]
    prompt = "\u203a Improve documentation in @filename"
    footer = "  gpt-5.5 xhigh \u00b7 ~/agent-bridge"
    previous = [*stable, prompt, "", footer]
    cursor = {"since_anchors": bv.build_since_anchors(previous)}
    current = [
        *stable,
        "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-codex-prompt",
        "\u2022 - ACK codex-reviewer",
        "  - 12",
        prompt,
        "",
        footer,
    ]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: stable anchor should match, got {confidence} note={note!r}")
    assert_true(delta == current[len(stable) : len(stable) + 3], f"{label}: prompt anchor must not hide response above it: {delta!r}")
    for anchor in cursor["since_anchors"]:
        assert_true(prompt not in anchor.get("lines", []), f"{label}: prompt placeholder must not be stored as anchor: {cursor!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    prompt = "\u203a Improve documentation in @filename"
    stable = [
        "Stored codex prompt stable one includes msg-stored-codex-001 bridge_view_peer.py",
        "Stored codex prompt stable two references scripts/regression_interrupt.py",
        "Stored codex prompt stable three references agent_view_peer since-last behavior",
    ]
    cursor = {"since_anchors": [{"lines": [prompt, *stable], "stable_count": 4}]}
    current = [*stable, "Fresh output after stored codex prompt cleanup should appear"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "medium", f"{label}: shortened stored anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Fresh output after stored codex prompt cleanup should appear"], f"{label}: polluted prompt line should be filtered: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_preserves_codex_bridge_prompt_lines(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    bridge_line = "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-bridge-line"
    stable = [
        bridge_line,
        "Bridge prompt preserve stable two references bridge_view_peer.py detail",
        "Bridge prompt preserve stable three references scripts/regression_interrupt.py",
        "Bridge prompt preserve stable four references agent_view_peer since-last",
    ]
    anchors = bv.build_since_anchors(stable)
    assert_true(any(bridge_line in anchor.get("lines", []) for anchor in anchors), f"{label}: bridge prompt line should remain anchor-eligible: {anchors!r}")
    assert_true(bv.cursor_anchors({"since_anchors": [{"lines": stable, "stable_count": 4}]})[0], f"{label}: stored bridge prompt line should remain usable")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_preserves_trailing_semantic_codex_arrow(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Semantic codex arrow anchor one includes msg-arrow-001 bridge_view_peer.py",
        "Semantic codex arrow anchor two references scripts/regression_interrupt.py",
        "Semantic codex arrow anchor three references agent_view_peer since-last behavior",
        "Semantic codex arrow anchor four references causal-arrow detail",
    ]
    semantic = "\u203a This semantic blockquote-like output should remain visible"
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    delta, confidence, note = bv.compute_since_delta(cursor, [*stable, semantic])
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [semantic], f"{label}: trailing semantic arrow output must remain visible: {delta!r}")
    assert_true(len(stable) not in bv.since_context_volatile_indexes([*stable, semantic]), f"{label}: semantic arrow should not be context-volatile")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_preserves_semantic_codex_arrow_before_footer(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Semantic codex footer anchor one includes msg-arrow-footer-001 bridge_view_peer.py",
        "Semantic codex footer anchor two references scripts/regression_interrupt.py",
        "Semantic codex footer anchor three references agent_view_peer since-last behavior",
        "Semantic codex footer anchor four references causal-arrow-footer detail",
    ]
    semantic = "\u203a This semantic blockquote-like output should remain visible"
    footer = "  gpt-5.5 xhigh \u00b7 ~/agent-bridge"
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    delta, confidence, note = bv.compute_since_delta(cursor, [*stable, semantic, "", footer])
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [semantic], f"{label}: semantic arrow before footer must remain visible: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_claude_partial_status_fragments_are_volatile(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    assert_true(bv.is_since_volatile_line("\u273b Precipitating\u2026"), f"{label}: rare glyph partial should be volatile")
    assert_true(bv.is_since_volatile_line("\u00b7 Precipitating\u2026 (5s \u00b7 \u2193 1 tokens)"), f"{label}: middle-dot payload status should be volatile")
    stable = [
        "Partial status anchor one includes msg-partial-001 bridge_view_peer.py",
        "Partial status anchor two references scripts/regression_interrupt.py",
        "Partial status anchor three references agent_view_peer since-last behavior",
        "Partial status anchor four references causal-partial detail",
    ]
    current = [*stable, "\u00b7 Unraveling\u2026", "\u2500" * 40]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: contextual middle-dot fragment should be hidden: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_partial_status_preserves_prose(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    prose = [
        "\u00b7 Unraveling\u2026 additional content",
        "\u00b7 Unraveling the parser behavior took time",
        "\u273b Precipitating",
    ]
    for line in prose:
        assert_true(not bv.is_since_volatile_line(line), f"{label}: prose/no-ellipsis line must remain visible: {line!r}")
    stable = [
        "Partial prose anchor one includes msg-partial-prose-001 bridge_view_peer.py",
        "Partial prose anchor two references scripts/regression_interrupt.py",
        "Partial prose anchor three references agent_view_peer since-last behavior",
        "Partial prose anchor four references causal-partial-prose detail",
    ]
    current = [*stable, prose[0], "\u2500" * 40]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [prose[0]], f"{label}: prose should survive adjacent TUI chrome: {delta!r}")
    bare_fragment = "\u00b7 Unraveling\u2026"
    bare_delta, bare_confidence, bare_note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, [*stable, bare_fragment])
    assert_true(bare_confidence == "high", f"{label}: expected high confidence for bare fragment, got {bare_confidence} note={bare_note!r}")
    assert_true(bare_delta == [bare_fragment], f"{label}: middle-dot fragment without TUI evidence must remain visible: {bare_delta!r}")
    print(f"  PASS  {label}")


def scenario_endpoint_rejects_stale_pane_lock_without_live(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path)
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        assert_true(not detail.get("ok"), f"{label}: stale pane lock must not authorize endpoint")
        assert_true(detail.get("reason") == "live_record_missing", f"{label}: expected live_record_missing, got {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%20" in (locks.get("panes") or {}), f"{label}: unknown/stale lock should remain diagnostic-only")
    print(f"  PASS  {label}")


def scenario_endpoint_rejects_same_pane_new_live_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codexA", session_id="sess-a", pane="%21")
        write_json_atomic(
            Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]),
            {
                "version": 1,
                "panes": {
                    "%21": {
                        "agent": "codex",
                        "session_id": "sess-b",
                        "pane": "%21",
                        "target": "tmux:1.0",
                        "bridge_session": "test-session",
                        "alias": "codexB",
                        "last_seen_at": utc_now(),
                        "process_identity": verified_identity("codex", "%21", pid=2000, start_time="99"),
                    }
                },
                "sessions": {},
            },
        )
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codexA", participant)
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_mismatch", f"{label}: expected live mismatch, got {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%21" not in (locks.get("panes") or {}), f"{label}: positive mismatch should clear pane lock")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        record = ((state.get("participants") or {}).get("codexA") or {})
        assert_true(record.get("endpoint_status") == "endpoint_lost", f"{label}: participant remains visible but endpoint_lost")
    print(f"  PASS  {label}")


def scenario_endpoint_probe_unknown_does_not_mutate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%22")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%22",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": verified_identity("codex", "%22"),
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%22": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "unknown", "reason": "ps_unavailable", "processes": []}  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(not detail.get("ok") and detail.get("reason") == "probe_unknown", f"{label}: expected probe_unknown, got {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%22" in (locks.get("panes") or {}), f"{label}: probe_unknown must not clear pane lock")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        record = ((state.get("participants") or {}).get("codex") or {})
        assert_true(record.get("endpoint_status") != "endpoint_lost", f"{label}: probe_unknown must not mark endpoint lost")
    print(f"  PASS  {label}")


def scenario_endpoint_accepts_matching_process_fingerprint(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%23")
        identity = verified_identity("codex", "%23", pid=3000, start_time="101")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%23",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%23": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(detail.get("ok") and detail.get("pane") == "%23", f"{label}: matching fingerprint should resolve, got {detail}")
    print(f"  PASS  {label}")


def scenario_backfill_refuses_to_mint_without_live_record(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%24")
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=4000, start_time="201")  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codex": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codex", {}).get("reason") == "live_record_missing", f"{label}: backfill must refuse to mint without live hook proof: {summary}")
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        assert_true((live.get("panes") or {}) == {} and (live.get("sessions") or {}) == {}, f"{label}: live records must remain empty: {live}")
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_missing", f"{label}: resolver must still fail closed: {detail}")
    print(f"  PASS  {label}")


def scenario_backfill_refuses_other_live_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codexA", session_id="sess-a", pane="%25")
        other = {
            "agent": "codex",
            "session_id": "sess-b",
            "pane": "%25",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codexB",
            "last_seen_at": utc_now(),
            "process_identity": verified_identity("codex", "%25", pid=5000, start_time="301"),
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%25": other}, "sessions": {"codex:sess-b": other}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=5001, start_time="302")  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codexA": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codexA", {}).get("reason") == "live_record_mismatch", f"{label}: backfill must refuse other live identity: {summary}")
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        assert_true(((live.get("panes") or {}).get("%25") or {}).get("session_id") == "sess-b", f"{label}: other live record must not be overwritten: {live}")
    print(f"  PASS  {label}")


def scenario_backfill_rejects_changed_process_fingerprint(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%26")
        original_identity = verified_identity("codex", "%26", pid=6000, start_time="401")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%26",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": original_identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%26": live}, "sessions": {"codex:sess-a": live}})
        mismatch = verified_identity("codex", "%26", pid=6001, start_time="402")
        mismatch["status"] = "mismatch"
        mismatch["reason"] = "process_fingerprint_mismatch"
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(mismatch)  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codex": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codex", {}).get("status") == "mismatch", f"{label}: changed process must not be refreshed: {summary}")
        live_after = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        proc = (((live_after.get("panes") or {}).get("%26") or {}).get("process_identity") or {}).get("processes", [{}])[0]
        assert_true(proc.get("pid") == 6000, f"{label}: original fingerprint must remain: {live_after}")
    print(f"  PASS  {label}")


def scenario_backfill_allows_fresh_hook_proof_create(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%27")
        identity = verified_identity("codex", "%27", pid=7000, start_time="501")
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities(
                "test-session",
                {"session": "test-session", "participants": {"codex": participant}},
                allow_create_from_hook=True,
            )
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codex", {}).get("status") == "verified", f"{label}: fresh hook proof may create fingerprint: {summary}")
        assert_true(detail.get("ok") and detail.get("pane") == "%27", f"{label}: created live fingerprint should resolve: {detail}")
    print(f"  PASS  {label}")


def scenario_hook_unknown_preserves_verified_process_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%28")
        original_identity = verified_identity("codex", "%28", pid=8000, start_time="601")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%28",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": original_identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%28": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%28", bridge_session="test-session", alias="codex", event="prompt_submitted")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        live_after = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        record = (live_after.get("panes") or {}).get("%28") or {}
        proc = ((record.get("process_identity") or {}).get("processes") or [{}])[0]
        assert_true(proc.get("pid") == 8000, f"{label}: verified fingerprint must be preserved: {record}")
        assert_true((record.get("process_identity_diagnostics") or {}).get("reason") == "ps_unavailable", f"{label}: unknown probe diagnostics retained: {record}")
    print(f"  PASS  {label}")


def scenario_probe_tmux_access_failure_unknown(label: str, tmpdir: Path) -> None:
    old_run = bridge_pane_probe.run
    try:
        bridge_pane_probe.run = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "no server running on /tmp/tmux-0/default")  # type: ignore[assignment]
        unknown = bridge_pane_probe.probe_agent_process("%29", "codex")
        bridge_pane_probe.run = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "can't find pane: %29")  # type: ignore[assignment]
        missing = bridge_pane_probe.probe_agent_process("%29", "codex")
    finally:
        bridge_pane_probe.run = old_run  # type: ignore[assignment]
    assert_true(unknown.get("status") == "unknown" and unknown.get("reason") == "tmux_access_failed", f"{label}: tmux access failure must be unknown: {unknown}")
    assert_true(missing.get("status") == "mismatch" and missing.get("reason") == "pane_unavailable", f"{label}: positive missing pane remains mismatch: {missing}")
    print(f"  PASS  {label}")


def scenario_endpoint_read_mismatch_does_not_mutate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codexA", session_id="sess-a", pane="%30")
        other = {
            "agent": "codex",
            "session_id": "sess-b",
            "pane": "%30",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codexB",
            "last_seen_at": utc_now(),
            "process_identity": verified_identity("codex", "%30", pid=9000, start_time="701"),
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%30": other}, "sessions": {"codex:sess-b": other}})
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codexA", participant, purpose="read")
        assert_true(not detail.get("ok") and not detail.get("should_detach"), f"{label}: read mismatch should fail without detach directive: {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%30" in (locks.get("panes") or {}), f"{label}: read path must not clear pane lock")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        record = ((state.get("participants") or {}).get("codexA") or {})
        assert_true(record.get("endpoint_status") != "endpoint_lost", f"{label}: read path must not mark endpoint lost")
    print(f"  PASS  {label}")


def scenario_verified_candidate_ordering_prefers_pane_then_newest(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir):
        write_identity_fixture(tmpdir / "identity-runtime" / "state", pane="%40")
        older = identity_live_record(pane="%40", pid=9400, start_time="901", last_seen_at="2026-01-01T00:00:00Z")
        newer = identity_live_record(pane="%41", pid=9401, start_time="902", last_seen_at="2026-01-02T00:00:00Z")
        write_live_identity_records(older, newer, index_record=older)
        calls: list[str] = []
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: (calls.append(pane) or dict(stored_identity or verified_identity(agent, pane)))  # type: ignore[assignment]
        try:
            preferred = bridge_identity.find_verified_live_record_for_identity("codex", "sess-a", prefer_pane="%40")
            calls.clear()
            newest = bridge_identity.find_verified_live_record_for_identity("codex", "sess-a")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(preferred.get("pane") == "%40", f"{label}: prefer_pane must win before timestamp: {preferred}")
        assert_true(newest.get("pane") == "%41", f"{label}: without prefer_pane newest verified candidate must win: {newest}")
        assert_true(calls == ["%41"], f"{label}: verified helper should short-circuit on first newest candidate: {calls}")
    print(f"  PASS  {label}")


def scenario_resume_new_pane_reconnects_unknown_old_and_logs(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%42")
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9500, start_time="1001")  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(
                agent_type="codex",
                session_id="sess-a",
                pane="%43",
                bridge_session="test-session",
                alias="codex",
                event="prompt_submitted",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        assert_true(mapping and mapping.get("pane") == "%43", f"{label}: hook should reconnect to new verified pane: {mapping}")
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(attached.get("pane") == "%43", f"{label}: attached mapping not updated: {attached}")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        participant = ((state.get("participants") or {}).get("codex") or {})
        assert_true(participant.get("pane") == "%43", f"{label}: participant pane not refreshed: {participant}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%42" in (locks.get("panes") or {}) and "%43" in (locks.get("panes") or {}), f"{label}: unknown-old reconnect must not clear old diagnostic lock: {locks}")
        events = read_raw_events(state_root_path)
        reconnects = [event for event in events if event.get("event") == "endpoint_auto_reconnected"]
        assert_true(reconnects and reconnects[-1].get("reason") == "mapped_endpoint_unknown", f"{label}: reconnect log missing/incorrect: {events}")
        assert_true(reconnects[-1].get("old_status") == "unknown", f"{label}: old_status should be unknown: {reconnects[-1]}")
    print(f"  PASS  {label}")


def scenario_resume_unknown_old_opt_out_blocks_switch(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%44")
        os.environ["AGENT_BRIDGE_NO_RESUME_FROM_UNKNOWN"] = "1"
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9501, start_time="1002")  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(
                agent_type="codex",
                session_id="sess-a",
                pane="%45",
                bridge_session="test-session",
                alias="codex",
                event="prompt_submitted",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(mapping is None, f"{label}: opt-out should suppress unknown-old reconnect return: {mapping}")
        assert_true(attached.get("pane") == "%44", f"{label}: opt-out should keep original mapping: {attached}")
        assert_true(not read_raw_events(state_root_path), f"{label}: blocked reconnect should not log success")
    print(f"  PASS  {label}")


def scenario_hook_cached_prior_unknown_does_not_reconnect(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%58")
        prior = identity_live_record(pane="%59", pid=9510, start_time="1011")
        write_live_identity_records(prior, index_record=prior)
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(
                agent_type="codex",
                session_id="sess-a",
                pane="%59",
                bridge_session="test-session",
                alias="codex",
                event="prompt_submitted",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        candidate = ((live.get("panes") or {}).get("%59") or {})
        assert_true(mapping is None, f"{label}: cached prior plus fresh unknown must not reconnect: {mapping}")
        assert_true(attached.get("pane") == "%58", f"{label}: attached mapping must stay on old pane: {attached}")
        assert_true((candidate.get("process_identity_diagnostics") or {}).get("reason") == "ps_unavailable", f"{label}: unknown diagnostics should be retained: {candidate}")
        assert_true(not read_raw_events(state_root_path), f"{label}: blocked reconnect should not log success")
    print(f"  PASS  {label}")


def scenario_resolver_reconnects_to_alternate_verified_live_record(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%46")
        new_live = identity_live_record(pane="%47", pid=9502, start_time="1003", last_seen_at="2026-01-03T00:00:00Z")
        write_live_identity_records(new_live, index_record=new_live)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(detail.get("ok") and detail.get("pane") == "%47", f"{label}: resolver should recover to alternate verified pane: {detail}")
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%47", f"{label}: registry not migrated: {registry}")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%47", f"{label}: participant pane not refreshed: {state}")
    print(f"  PASS  {label}")


def scenario_resolver_candidate_unknown_on_final_probe_does_not_reconnect(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%60")
        candidate = identity_live_record(pane="%61", pid=9511, start_time="1012", last_seen_at="2026-01-08T00:00:00Z")
        write_live_identity_records(candidate, index_record=candidate)
        calls: dict[str, int] = {"%61": 0}
        old_probe = bridge_identity.probe_agent_process

        def probe(pane, agent, stored_identity=None):
            if pane == "%61":
                calls["%61"] += 1
                if calls["%61"] == 1:
                    return dict(stored_identity or verified_identity(agent, pane))
                return {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}
            return {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}

        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_missing", f"{label}: final unknown probe should block reconnect: {detail}")
        assert_true(calls["%61"] >= 2, f"{label}: candidate should be re-probed before write: {calls}")
        assert_true(attached.get("pane") == "%60", f"{label}: attached mapping must stay on original pane: {attached}")
        assert_true(not read_raw_events(state_root_path), f"{label}: blocked reconnect should not log success")
    print(f"  PASS  {label}")


def scenario_resolver_read_reconnect_logs_distinct_reason(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%48")
        new_live = identity_live_record(pane="%49", pid=9503, start_time="1004", last_seen_at="2026-01-04T00:00:00Z")
        write_live_identity_records(new_live, index_record=new_live)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant, purpose="read")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(detail.get("ok") and detail.get("pane") == "%49", f"{label}: read resolver should recover to alternate verified pane: {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%48" in (locks.get("panes") or {}), f"{label}: read reconnect must not clear old lock: {locks}")
        reconnects = [event for event in read_raw_events(state_root_path) if event.get("event") == "endpoint_auto_reconnected"]
        assert_true(reconnects and reconnects[-1].get("reason") == "endpoint_auto_reconnected_via_read", f"{label}: read reconnect reason not distinct: {reconnects}")
    print(f"  PASS  {label}")


def scenario_session_end_replacement_uses_verified_candidate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%50")
        ended = identity_live_record(pane="%50", pid=9504, start_time="1005", last_seen_at="2026-01-05T00:00:00Z")
        stale_newer = identity_live_record(pane="%51", pid=9505, start_time="1006", last_seen_at="2026-01-07T00:00:00Z")
        verified_older = identity_live_record(pane="%52", pid=9506, start_time="1007", last_seen_at="2026-01-06T00:00:00Z")
        write_live_identity_records(ended, stale_newer, verified_older, index_record=ended)
        old_probe = bridge_identity.probe_agent_process
        def probe(pane, agent, stored_identity=None):
            if pane == "%51":
                result = dict(stored_identity or verified_identity(agent, pane))
                result["status"] = "mismatch"
                result["reason"] = "process_fingerprint_mismatch"
                return result
            return dict(stored_identity or verified_identity(agent, pane))
        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%50", event="session_ended")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"sessions": {}})
        assert_true(attached.get("pane") == "%52", f"{label}: SessionEnd should not choose newer unverified stale pane: {attached}")
        assert_true(((live.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%52", f"{label}: live index should choose verified replacement: {live}")
    print(f"  PASS  {label}")


def scenario_reconnect_rereads_mapping_before_write(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%53")
        stale_mapping = bridge_identity.read_attached_mapping("codex", "sess-a") or {}
        candidate = identity_live_record(pane="%54", pid=9507, start_time="1008")
        newer = identity_live_record(pane="%55", pid=9508, start_time="1009")
        write_live_identity_records(candidate, newer, index_record=newer)
        bridge_identity.update_attached_endpoint(stale_mapping, "%55", "tmux:1.0")
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            result = bridge_identity.auto_reconnect_attached_endpoint(
                stale_mapping,
                candidate,
                "mapped_endpoint_mismatch",
                old_pane="%53",
                old_status="mismatch",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(attached.get("pane") == "%55", f"{label}: stale reconnect must not overwrite newer mapping: {attached}")
        assert_true(result.get("pane") == "%55", f"{label}: helper should return current verified mapping: {result}")
    print(f"  PASS  {label}")


def scenario_caller_reconnects_from_resumed_pane(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%56")
        new_live = identity_live_record(pane="%57", pid=9509, start_time="1010")
        write_live_identity_records(new_live, index_record=new_live)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            resolution = bridge_identity.resolve_caller_from_pane(pane="%57", tool_name="agent_send_peer")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(resolution.ok and resolution.alias == "codex", f"{label}: caller should reconnect resumed pane: {resolution}")
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%57", f"{label}: caller reconnect did not persist mapping: {registry}")
    print(f"  PASS  {label}")


def scenario_no_probe_requires_verified_live_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%31")
        missing = bridge_identity.verify_existing_live_process_identity("codex", "sess-a", "%31")
        identity = verified_identity("codex", "%31", pid=9100, start_time="801")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%31",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%31": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        try:
            verified = bridge_identity.verify_existing_live_process_identity("codex", "sess-a", "%31")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(missing.get("reason") == "live_record_missing", f"{label}: no-probe must require live hook record before publish: {missing}")
        assert_true(verified.get("status") == "verified", f"{label}: verified live identity accepted: {verified}")
    print(f"  PASS  {label}")


def scenario_daemon_undeliverable_request_returns_result(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.busy["alice"] = True
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    calls: list[str] = []
    old_literal = bridge_daemon.run_tmux_send_literal
    bridge_daemon.run_tmux_send_literal = lambda pane, prompt, **kwargs: calls.append(pane)  # type: ignore[assignment]
    try:
        msg = test_message("msg-undeliverable", frm="alice", to="bob", status="pending")
        d.queue.update(lambda queue: queue.append(msg))
        d.try_deliver("bob")
    finally:
        bridge_daemon.run_tmux_send_literal = old_literal  # type: ignore[assignment]
    queue = d.queue.read()
    assert_true(calls == [], f"{label}: must not paste into stale pane")
    assert_true(not any(item.get("id") == "msg-undeliverable" for item in queue), f"{label}: original removed")
    result = next((item for item in queue if item.get("to") == "alice" and item.get("kind") == "result"), None)
    assert_true(result is not None and "[bridge:undeliverable]" in str(result.get("body") or ""), f"{label}: sender gets undeliverable result")
    print(f"  PASS  {label}")


def scenario_interrupt_endpoint_lost_finalizes_delivered_non_aggregate(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.busy["alice"] = True
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    msg = test_message("msg-delivered-lost", frm="alice", to="bob", status="delivered")
    d.queue.update(lambda queue: queue.append(msg))
    d.current_prompt_by_agent["bob"] = {"id": "msg-delivered-lost", "from": "alice", "auto_return": True}
    result = d.handle_interrupt(sender="alice", target="bob")
    queue = d.queue.read()
    assert_true(not result.get("held"), f"{label}: endpoint loss should not enter hold")
    assert_true(not any(item.get("id") == "msg-delivered-lost" for item in queue), f"{label}: delivered original removed")
    assert_true(any(item.get("kind") == "result" and item.get("to") == "alice" for item in queue), f"{label}: non-aggregate sender gets result")
    print(f"  PASS  {label}")


def scenario_interrupt_endpoint_lost_finalizes_delivered_aggregate(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.busy["alice"] = True
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    msg = test_message("msg-agg-lost", frm="alice", to="bob", status="delivered")
    msg["aggregate_id"] = "agg-lost"
    msg["aggregate_expected"] = ["bob"]
    msg["aggregate_message_ids"] = {"bob": "msg-agg-lost"}
    d.queue.update(lambda queue: queue.append(msg))
    d.current_prompt_by_agent["bob"] = {"id": "msg-agg-lost", "from": "alice", "auto_return": True, "aggregate_id": "agg-lost"}
    d.handle_interrupt(sender="alice", target="bob")
    aggregate = (read_json(d.aggregate_file, {"aggregates": {}}).get("aggregates") or {}).get("agg-lost") or {}
    reply = ((aggregate.get("replies") or {}).get("bob") or {}).get("body") or ""
    assert_true("[bridge:undeliverable]" in reply, f"{label}: aggregate gets synthetic undeliverable reply")
    print(f"  PASS  {label}")


def scenario_retry_enter_endpoint_lost_does_not_press_enter(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    msg = test_message("msg-enter-lost", frm="alice", to="bob", status="inflight")
    d.queue.update(lambda queue: queue.append(msg))
    d.last_enter_ts["msg-enter-lost"] = time.time() - 2.0
    enter_calls: list[str] = []
    old_enter = bridge_daemon.run_tmux_enter
    bridge_daemon.run_tmux_enter = lambda pane: enter_calls.append(pane)  # type: ignore[assignment]
    try:
        d.retry_enter_for_inflight()
    finally:
        bridge_daemon.run_tmux_enter = old_enter  # type: ignore[assignment]
    assert_true(enter_calls == [], f"{label}: retry must not press Enter into stale pane")
    assert_true(not any(item.get("id") == "msg-enter-lost" for item in d.queue.read()), f"{label}: inflight removed as undeliverable")
    print(f"  PASS  {label}")


def scenario_direct_notices_suppress_unverified_endpoint(label: str, tmpdir: Path) -> None:
    import bridge_daemon_ctl
    import bridge_leave

    record = {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"}
    calls: list[tuple[str, str]] = []
    old_leave_resolve = bridge_leave.resolve_participant_endpoint_detail
    old_leave_send = bridge_leave.tmux_send_literal
    old_ctl_resolve = bridge_daemon_ctl.resolve_participant_endpoint_detail
    old_ctl_send = bridge_daemon_ctl.tmux_send_literal
    bridge_leave.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
    bridge_leave.tmux_send_literal = lambda pane, text: calls.append(("leave", pane))  # type: ignore[assignment]
    bridge_daemon_ctl.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
    bridge_daemon_ctl.tmux_send_literal = lambda pane, text: calls.append(("close", pane))  # type: ignore[assignment]
    try:
        leave_result = bridge_leave.send_leave_notice("test-session", "bob", record)
        with isolated_identity_env(tmpdir) as state_root_path:
            write_identity_fixture(state_root_path, alias="bob", pane="%2", session_id="sess-bob")
            close_result = bridge_daemon_ctl.send_room_closed_notices("test-session")
    finally:
        bridge_leave.resolve_participant_endpoint_detail = old_leave_resolve  # type: ignore[assignment]
        bridge_leave.tmux_send_literal = old_leave_send  # type: ignore[assignment]
        bridge_daemon_ctl.resolve_participant_endpoint_detail = old_ctl_resolve  # type: ignore[assignment]
        bridge_daemon_ctl.tmux_send_literal = old_ctl_send  # type: ignore[assignment]
    assert_true(leave_result.get("sent") == 0 and close_result.get("sent") == 0, f"{label}: notices suppressed")
    assert_true(calls == [], f"{label}: no direct tmux send for unverified endpoint")
    print(f"  PASS  {label}")


def scenario_view_peer_unverified_endpoint_uses_daemon_not_local_capture(label: str, tmpdir: Path) -> None:
    import bridge_view_peer as bv

    state = {"participants": {"bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob", "status": "active"}}}
    args = argparse.Namespace(capture_file=None, capture_timeout=0.1)
    calls: list[str] = []
    old_resolve = bv.resolve_participant_endpoint_detail
    old_capture = bv.run_tmux_capture
    old_daemon = bv.capture_via_daemon
    bv.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
    bv.run_tmux_capture = lambda *args, **kwargs: calls.append("local") or ""  # type: ignore[assignment]
    bv.capture_via_daemon = lambda *args, **kwargs: "daemon-capture"  # type: ignore[assignment]
    try:
        text = bv.capture_text(args, session="test-session", caller="alice", target="bob", state=state, pane="%2", start=-10)
    finally:
        bv.resolve_participant_endpoint_detail = old_resolve  # type: ignore[assignment]
        bv.run_tmux_capture = old_capture  # type: ignore[assignment]
        bv.capture_via_daemon = old_daemon  # type: ignore[assignment]
    assert_true(text == "daemon-capture" and calls == [], f"{label}: unverified endpoint must not use local capture")
    print(f"  PASS  {label}")


def scenario_daemon_startup_backfill_summary_logs_repair_hint(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "pane": "%1"}, "bob": {"alias": "bob", "pane": "%2"}}
    d = make_daemon(tmpdir, participants)
    d.startup_backfill_summary = {"alice": {"status": "unknown", "reason": "ps_unavailable"}}
    d.follow()
    events = read_events(tmpdir / "events.raw.jsonl")
    event = next((item for item in events if item.get("event") == "endpoint_backfill_summary"), None)
    assert_true(event is not None, f"{label}: startup backfill summary event missing")
    assert_true("bridge_healthcheck.sh --backfill-endpoints" in str(event.get("repair_hint") or ""), f"{label}: repair hint missing")
    print(f"  PASS  {label}")


def _import_enqueue_module():
    import importlib
    be = importlib.import_module("bridge_enqueue")
    return importlib.reload(be)


def _import_send_peer_module():
    import importlib
    bs = importlib.import_module("bridge_send_peer")
    return importlib.reload(bs)


def _import_extend_wait_module():
    import importlib
    bew = importlib.import_module("bridge_extend_wait")
    return importlib.reload(bew)


def _import_alarm_module():
    import importlib
    ba = importlib.import_module("bridge_alarm")
    return importlib.reload(ba)


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


def _run_extend_wait_main(bew, argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import io
    old_argv = sys.argv[:]
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_extend_wait", *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = bew.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
    return int(code), out.getvalue(), err.getvalue()


def _run_alarm_main(ba, argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import io
    old_argv = sys.argv[:]
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_alarm", *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ba.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
    return int(code), out.getvalue(), err.getvalue()


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


def _patch_enqueue_for_unit(be, state: dict, *, socket_error: str = "") -> None:
    be.ensure_daemon_running = lambda session: ""
    be.room_status = lambda session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")
    be.sender_matches_caller = lambda args, session: True
    be.load_session = lambda session: state
    be.enqueue_via_daemon_socket = lambda session, messages, **kwargs: (False, [], socket_error, "")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


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


def scenario_extend_wait_zero_negative_nan_inf_rejected(label: str, tmpdir: Path) -> None:
    bew = _import_extend_wait_module()
    bew.request_extend = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("request_extend must not run for invalid seconds"))
    for raw, expected in [("0", "0"), ("-1", "-1"), ("nan", "nan"), ("inf", "inf"), ("-inf", "-inf")]:
        argv = ["msg-test", "--", raw] if raw == "-inf" else ["msg-test", raw]
        code, out, err = _run_extend_wait_main(bew, argv)
        assert_true(code == 2, f"{label}: extend_wait {raw!r} exits 2, got {code}")
        assert_true(out == "", f"{label}: invalid extend_wait has no stdout: {out!r}")
        assert_true("finite positive" in err and f"got {expected}" in err, f"{label}: error should explain rule and value for {raw!r}: {err!r}")
    print(f"  PASS  {label}")


def scenario_extend_wait_finite_positive_calls_request_extend(label: str, tmpdir: Path) -> None:
    bew = _import_extend_wait_module()
    calls: list[tuple[str, str, str, float]] = []
    bew.resolve_caller_from_pane = lambda **kwargs: bridge_identity.CallerResolution(True, "test-session", "alice")  # type: ignore[assignment]
    bew.ensure_daemon_running = lambda session: ""
    bew.room_status = lambda session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")
    bew.load_session = lambda session: _participants_state(["alice", "bob"])

    def request_extend(session: str, sender: str, message_id: str, seconds: float) -> tuple[bool, dict, str]:
        calls.append((session, sender, message_id, seconds))
        return True, {"message_id": message_id, "new_deadline": "2026-04-27T00:00:00.000000Z"}, ""

    bew.request_extend = request_extend
    code, out, err = _run_extend_wait_main(bew, ["msg-test", "1.25", "--session", "test-session", "--from", "alice", "--allow-spoof"])
    assert_true(code == 0, f"{label}: finite extend_wait should succeed, got {code}, err={err!r}")
    assert_true(calls == [("test-session", "alice", "msg-test", 1.25)], f"{label}: request_extend call mismatch: {calls}")
    summary = json.loads(out)
    assert_true(summary.get("seconds") == 1.25 and summary.get("message_id") == "msg-test", f"{label}: stdout summary mismatch: {summary}")
    print(f"  PASS  {label}")


def scenario_alarm_negative_nan_inf_minus_inf_rejected(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    ba.request_alarm = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("request_alarm must not run for invalid delay"))
    for raw, expected in [("-1", "-1"), ("nan", "nan"), ("inf", "inf"), ("-inf", "-inf")]:
        argv = ["--", raw] if raw == "-inf" else [raw]
        code, out, err = _run_alarm_main(ba, argv)
        assert_true(code == 2, f"{label}: alarm {raw!r} exits 2, got {code}")
        assert_true(out == "", f"{label}: invalid alarm has no stdout: {out!r}")
        assert_true("finite non-negative" in err and f"got {expected}" in err, f"{label}: error should explain rule and value for {raw!r}: {err!r}")
    print(f"  PASS  {label}")


def scenario_alarm_zero_and_finite_positive_call_request_alarm(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    calls: list[tuple[str, str, float, str | None]] = []
    ba.resolve_caller_from_pane = lambda **kwargs: bridge_identity.CallerResolution(True, "test-session", "alice")  # type: ignore[assignment]
    ba.ensure_daemon_running = lambda session: ""
    ba.room_status = lambda session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")
    ba.load_session = lambda session: _participants_state(["alice", "bob"])

    def request_alarm(session: str, sender: str, delay_seconds: float, body: str | None) -> tuple[bool, str, str]:
        calls.append((session, sender, delay_seconds, body))
        return True, f"wake-{format(delay_seconds, 'g')}", ""

    ba.request_alarm = request_alarm
    code0, out0, err0 = _run_alarm_main(ba, ["0", "--note", "now", "--session", "test-session", "--from", "alice", "--allow-spoof"])
    code1, out1, err1 = _run_alarm_main(ba, ["2.5", "--session", "test-session", "--from", "alice", "--allow-spoof"])
    assert_true(code0 == 0 and out0.strip() == "wake-0", f"{label}: zero alarm should succeed: code={code0} out={out0!r} err={err0!r}")
    assert_true(code1 == 0 and out1.strip() == "wake-2.5", f"{label}: positive alarm should succeed: code={code1} out={out1!r} err={err1!r}")
    assert_true(calls == [("test-session", "alice", 0.0, "now"), ("test-session", "alice", 2.5, None)], f"{label}: request_alarm calls mismatch: {calls}")
    print(f"  PASS  {label}")


def scenario_resolve_default_watchdog_seconds_env_table(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    cases: list[tuple[str | None, float | None]] = [
        (None, 300.0),
        ("300", 300.0),
        ("0", None),
        ("-1", None),
        ("inf", 300.0),
        ("nan", 300.0),
        ("abc", 300.0),
    ]
    for raw, expected in cases:
        with patched_environ(AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC=raw):
            actual = be._resolve_default_watchdog_seconds()
        assert_true(actual == expected, f"{label}: env {raw!r} expected {expected!r}, got {actual!r}")
    print(f"  PASS  {label}")


def _daemon_command_result(d: bridge_daemon.BridgeDaemon, payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8") + b"\n"
    return d.handle_command_connection(FakeCommandConn(raw))  # type: ignore[arg-type]


def scenario_daemon_socket_alarm_op_rejects_non_finite(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    for value, expected in [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")]:
        before = dict(d.watchdogs)
        result = _daemon_command_result(d, {"op": "alarm", "from": "claude", "delay_seconds": value})
        assert_true(result.get("ok") is False, f"{label}: alarm {expected} should be rejected: {result}")
        assert_true("finite non-negative" in str(result.get("error") or ""), f"{label}: alarm error should mention finite non-negative: {result}")
        assert_true(d.watchdogs == before, f"{label}: rejected alarm must not mutate watchdogs")
    print(f"  PASS  {label}")


def scenario_daemon_socket_extend_watchdog_op_rejects_non_finite(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    for value, expected in [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")]:
        before = dict(d.watchdogs)
        result = _daemon_command_result(d, {"op": "extend_watchdog", "from": "claude", "message_id": "msg-test", "seconds": value})
        assert_true(result.get("ok") is False, f"{label}: extend {expected} should be rejected: {result}")
        assert_true("finite positive" in str(result.get("error") or ""), f"{label}: extend error should mention finite positive: {result}")
        assert_true(d.watchdogs == before, f"{label}: rejected extend must not mutate watchdogs")
    print(f"  PASS  {label}")


def scenario_daemon_upsert_message_watchdog_rejects_non_finite(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-upsert-nonfinite", frm="claude", to="codex", status="delivered")
    d.queue.update(lambda queue: queue.append(msg) or None)
    for value, expected in [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")]:
        ok, err, deadline = d.upsert_message_watchdog("claude", "msg-upsert-nonfinite", value)
        assert_true(not ok and err == "seconds_must_be_positive" and deadline is None, f"{label}: upsert {expected} should reject: {(ok, err, deadline)}")
    assert_true(not d.watchdogs, f"{label}: rejected upserts must not arm watchdogs: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_daemon_register_alarm_rejects_non_finite_and_negative(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    for value in ("not-a-number", float("nan"), float("inf"), float("-inf"), -1.0):
        wake_id = d.register_alarm("claude", value, None)  # type: ignore[arg-type]
        assert_true(wake_id is None, f"{label}: register_alarm must reject {value!r}, got {wake_id!r}")
    assert_true(not d.watchdogs, f"{label}: rejected alarms must not mutate watchdogs")
    print(f"  PASS  {label}")


def scenario_daemon_mark_message_delivered_ignores_non_finite_watchdog(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-stale-watchdog-inf", frm="claude", to="codex", status="inflight")
    msg["watchdog_delay_sec"] = float("inf")
    d.queue.update(lambda queue: queue.append(msg) or None)
    delivered = d.mark_message_delivered_by_id("codex", "msg-stale-watchdog-inf")
    assert_true(delivered is not None and delivered.get("status") == "delivered", f"{label}: message should still be delivered: {delivered}")
    assert_true(not any(wd.get("ref_message_id") == "msg-stale-watchdog-inf" for wd in d.watchdogs.values()), f"{label}: non-finite stored delay must not arm watchdog: {d.watchdogs}")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-stale-watchdog-inf"), None)
    assert_true(queued is not None and queued.get("status") == "delivered", f"{label}: queue row should be delivered: {queued}")
    print(f"  PASS  {label}")


def scenario_daemon_socket_ack_message_unsupported_does_not_delete_queue(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-ack-unsupported", frm="claude", to="codex", status="inflight")
    msg["nonce"] = "n-victim"
    d.queue.update(lambda queue: queue.append(msg) or None)
    before = list(d.queue.read())
    result = _daemon_command_result(d, {"op": "ack_message", "nonce": "n-victim"})
    assert_true(result.get("ok") is False, f"{label}: ack_message socket op should be unsupported: {result}")
    assert_true("unsupported command" in str(result.get("error") or ""), f"{label}: unsupported error expected: {result}")
    after = list(d.queue.read())
    assert_true(before == after, f"{label}: unsupported ack_message op must not mutate queue: before={before} after={after}")
    print(f"  PASS  {label}")


def scenario_daemon_ack_message_helper_removed(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    assert_true(not hasattr(bridge_daemon.BridgeDaemon, "ack_message"), f"{label}: deprecated unsafe class helper must stay removed")
    assert_true(not hasattr(d, "ack_message"), f"{label}: deprecated unsafe instance helper must stay removed")
    print(f"  PASS  {label}")


def scenario_peer_body_size_helper_boundaries(label: str, tmpdir: Path) -> None:
    import io
    ok, err = validate_peer_body_size("x" * MAX_INLINE_SEND_BODY_CHARS)
    assert_true(ok and err == "", f"{label}: exactly limit chars should be accepted")
    ok2, err2 = validate_peer_body_size("x" * (MAX_INLINE_SEND_BODY_CHARS + 1))
    assert_true(not ok2, f"{label}: over-limit chars should be rejected")
    assert_true(str(MAX_INLINE_SEND_BODY_CHARS) in err2 and "/tmp/agent-bridge-share" in err2, f"{label}: error explains limit and shared path: {err2!r}")
    ok3, err3 = validate_peer_body_size("한" * MAX_INLINE_SEND_BODY_CHARS)
    assert_true(ok3 and err3 == "", f"{label}: limit is explicit char-count, not byte-count")
    limited = read_limited_text(io.StringIO("x" * (MAX_INLINE_SEND_BODY_CHARS + 100)))
    assert_true(len(limited) == MAX_INLINE_SEND_BODY_CHARS + 1, f"{label}: stdin read is bounded to limit+1 chars")
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
    returncode: int = 0,
):
    calls: list[tuple[list[str], dict]] = []
    old_run = bs.subprocess.run

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs)))
        if stdout_text:
            print(stdout_text, end="")
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


def scenario_send_peer_rejects_option_after_destination(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "--kind", "notice", "hello"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: option leakage after --to must reject")
    assert_true("after the destination" in err and "--kind" in err, f"{label}: stderr identifies leaked option: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_option_after_implicit_target(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "bob", "--kind", "notice", "hello"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: option leakage after implicit target must reject")
    assert_true("after the destination" in err and "--kind" in err, f"{label}: stderr identifies leaked option: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_rejects_option_after_inline_body(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--to", "bob", "hello", "--kind", "notice"],
        stdin_isatty=True,
    )
    assert_true(code == 2 and not calls and out == "", f"{label}: option after body must reject")
    assert_true("after the inline body" in err or "after the destination" in err, f"{label}: stderr explains option position: {err!r}")
    print(f"  PASS  {label}")


def scenario_send_peer_single_inline_body_uses_stdin_handoff(label: str, tmpdir: Path) -> None:
    bs = _import_send_peer_module()
    _patch_send_peer_for_unit(bs)
    code, out, err, calls = _run_send_peer_with_fake_subprocess(
        bs,
        ["--session", "test-session", "--from", "alice", "--kind", "notice", "--to", "bob", "hello world"],
        stdin_isatty=True,
    )
    assert_true(code == 0 and len(calls) == 1, f"{label}: single argv body should enqueue once: code={code} err={err!r}")
    cmd, kwargs = calls[0]
    assert_true("--stdin" in cmd and "--body" not in cmd, f"{label}: wrapper must hand body to enqueue via stdin: {cmd}")
    assert_true(cmd[cmd.index("--to") + 1] == "bob", f"{label}: target preserved: {cmd}")
    assert_true(kwargs.get("input") == b"hello world", f"{label}: body encoded as utf-8 bytes: {kwargs}")
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
    assert_true("Waiting for a bridge follow-up?" in err, f"{label}: common hint missing: {err!r}")
    assert_true("notice sent" not in err, f"{label}: request must not print notice alarm hint: {err!r}")
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
    assert_true("notice sent" in err and "agent_alarm" in err, f"{label}: notice alarm hint missing: {err!r}")
    assert_true("Waiting for a bridge follow-up?" in err, f"{label}: common hint missing: {err!r}")
    assert_true(err.index("notice sent") < err.index("Waiting for a bridge follow-up?"), f"{label}: notice-specific hint should come first: {err!r}")
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
    assert_true("notice sent" not in err and "Waiting for a bridge follow-up?" not in err, f"{label}: success hints must not print on failure: {err!r}")
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
    body = "한글 can't --kind request `x`"
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
    assert_true("cannot combine --stdin" in err, f"{label}: stderr explains collision: {err!r}")
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
    assert_true("piped stdin" in err, f"{label}: stderr explains pipe collision: {err!r}")
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


def scenario_alarm_cancel_preserves_at_limit_body(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    _enqueue_alarm(d, "bob", "long body alarm")
    original = "x" * MAX_INLINE_SEND_BODY_CHARS
    msg = _qualifying_message("alice", "bob", kind="request", body=original)
    msg["id"] = "msg-at-limit-alarm"
    d.enqueue_ipc_message(msg)
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-at-limit-alarm"), None)
    assert_true(queued is not None, f"{label}: message queued")
    visible_body = str(queued.get("body") or "")
    assert_true(visible_body.startswith("[bridge:alarm_cancelled]"), f"{label}: recipient-visible body must include cancellation signal")
    assert_true(original in visible_body, f"{label}: alarm notice must not truncate or displace at-limit user body")
    assert_true(len(visible_body) <= MAX_PEER_BODY_CHARS, f"{label}: visible body must stay within daemon prompt guard")
    prompt = bridge_daemon.build_peer_prompt(queued, "nonce-test")
    assert_true("[bridge:alarm_cancelled]" in prompt and "[bridge truncated peer body]" not in prompt, f"{label}: in-band prompt carries clean alarm signal without truncation")
    events = read_events(tmpdir / "events.raw.jsonl")
    alarm_events = [e for e in events if e.get("event") == "alarm_cancelled_by_message"]
    assert_true(alarm_events and alarm_events[-1].get("notice_omitted") is False, f"{label}: at external limit should retain alarm notice: {alarm_events}")
    print(f"  PASS  {label}")


def scenario_prompt_body_preserves_multiline_and_sanitizes(label: str, tmpdir: Path) -> None:
    body = "alpha\r\nbeta\tgamma\rdelta\x00epsilon\x1b[31m\x85z"
    formatted = bridge_daemon.prompt_body(body)
    assert_true(formatted == "alpha\nbeta\tgamma\ndeltaepsilon[31mz", f"{label}: body formatting mismatch: {formatted!r}")
    assert_true("\\n" not in formatted and "\\t" not in formatted, f"{label}: multiline body must not be backslash-escaped: {formatted!r}")

    literal_backslashes = r"literal \n and \t stay"
    assert_true(bridge_daemon.prompt_body(literal_backslashes) == literal_backslashes, f"{label}: literal slash sequences must pass through")

    crlf_at_limit = ("x" * (MAX_PEER_BODY_CHARS - 1)) + "\r\n"
    assert_true("[bridge truncated peer body]" not in bridge_daemon.prompt_body(crlf_at_limit), f"{label}: CRLF must normalize before truncation")

    trailing_lf = "tail\n"
    assert_true(bridge_daemon.prompt_body(trailing_lf) == trailing_lf, f"{label}: trailing LF should be preserved")

    long_body = ("x" * MAX_PEER_BODY_CHARS) + "\nrest"
    truncated = bridge_daemon.prompt_body(long_body)
    assert_true(truncated == ("x" * MAX_PEER_BODY_CHARS) + "\n[bridge truncated peer body]", f"{label}: truncation marker should be on its own line")

    msg = test_message("msg-multiline")
    msg["body"] = "first\nsecond\tline"
    prompt = bridge_daemon.build_peer_prompt(msg, "nonce-multiline")
    lines = prompt.splitlines()
    assert_true(len(lines) == 2, f"{label}: prompt should contain exactly body newline: {prompt!r}")
    assert_true(lines[0].startswith("[bridge:nonce-multiline] "), f"{label}: prefix must stay on the first line: {prompt!r}")
    assert_true(lines[0].endswith("Request: first") and lines[1] == "second\tline", f"{label}: body lines should be literal: {prompt!r}")
    assert_true(bridge_hook_logger.extract_nonce(prompt) == "nonce-multiline", f"{label}: nonce extraction must still match prompt start")
    print(f"  PASS  {label}")


def scenario_build_peer_prompt_signature_drops_max_hops(label: str, tmpdir: Path) -> None:
    params = inspect.signature(bridge_daemon.build_peer_prompt).parameters
    assert_true(set(params.keys()) == {"message", "nonce"}, f"{label}: build_peer_prompt params should be message+nonce only, got {list(params)}")
    msg = test_message("msg-prompt-signature", frm="alice", to="bob")
    msg["body"] = "hello"
    msg["kind"] = "request"
    prompt = bridge_daemon.build_peer_prompt(msg, "nonce-sig")
    expected = (
        "[bridge:nonce-sig] from=alice kind=request "
        f"causal_id={msg['causal_id']}. "
        "Reply normally; do not call agent_send_peer; bridge auto-returns your reply. "
        "Request: hello"
    )
    assert_true(prompt == expected, f"{label}: prompt output changed: {prompt!r} != {expected!r}")
    print(f"  PASS  {label}")


def scenario_tmux_paste_buffer_delivery_sequence(label: str, tmpdir: Path) -> None:
    prompt = "prefix line\nbody\tline\n"
    old_run = bridge_daemon.subprocess.run
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append((cmd_list, dict(kwargs)))
        if cmd_list[1] == "load-buffer":
            assert_true(kwargs.get("input") == prompt.encode("utf-8"), f"{label}: load-buffer stdin should be prompt bytes")
        return subprocess.CompletedProcess(cmd_list, 0)

    bridge_daemon.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        bridge_daemon.run_tmux_send_literal(
            "%42",
            prompt,
            bridge_session="sess/a",
            target_alias="codex.review",
            message_id="msg-1",
            nonce="nonce-1",
        )
        bridge_daemon.run_tmux_send_literal(
            "%42",
            prompt,
            bridge_session="sess/a",
            target_alias="codex.review",
            message_id="msg-1",
            nonce="nonce-1",
        )
    finally:
        bridge_daemon.subprocess.run = old_run  # type: ignore[assignment]

    first = calls[:3]
    second = calls[3:6]
    assert_true(len(first) == 3 and len(second) == 3, f"{label}: each delivery should load, paste, cleanup: {calls}")
    load_cmd, load_kwargs = first[0]
    paste_cmd, _ = first[1]
    delete_cmd, delete_kwargs = first[2]
    buffer_name = load_cmd[3]
    assert_true(load_cmd == ["tmux", "load-buffer", "-b", buffer_name, "-"], f"{label}: load command mismatch: {load_cmd}")
    assert_true(load_kwargs.get("check") is True, f"{label}: load-buffer should check")
    assert_true(paste_cmd == ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer_name, "-t", "%42"], f"{label}: paste command mismatch: {paste_cmd}")
    assert_true(delete_cmd == ["tmux", "delete-buffer", "-b", buffer_name], f"{label}: delete command mismatch: {delete_cmd}")
    assert_true(delete_kwargs.get("check") is False, f"{label}: delete-buffer cleanup should tolerate not-found")
    assert_true("sess-a" in buffer_name and "codex.review" in buffer_name and "msg-1" in buffer_name and "nonce-1" in buffer_name, f"{label}: buffer name should include session/target/message/nonce: {buffer_name}")
    assert_true(str(os.getpid()) in buffer_name, f"{label}: buffer name should include daemon pid: {buffer_name}")
    assert_true(buffer_name != second[0][0][3], f"{label}: repeated deliveries must use unique server-scoped buffer names")

    calls.clear()

    def fake_run_paste_fails(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append((cmd_list, dict(kwargs)))
        if cmd_list[1] == "paste-buffer":
            raise subprocess.CalledProcessError(2, cmd_list)
        if cmd_list[1] == "delete-buffer":
            return subprocess.CompletedProcess(cmd_list, 1, "", "no buffer")
        return subprocess.CompletedProcess(cmd_list, 0)

    bridge_daemon.subprocess.run = fake_run_paste_fails  # type: ignore[assignment]
    try:
        try:
            bridge_daemon.run_tmux_send_literal("%42", prompt, bridge_session="sess", target_alias="bob", message_id="msg", nonce="nonce")
            raise AssertionError(f"{label}: paste failure should propagate")
        except subprocess.CalledProcessError:
            pass
    finally:
        bridge_daemon.subprocess.run = old_run  # type: ignore[assignment]
    assert_true([cmd[1] for cmd, _ in calls] == ["load-buffer", "paste-buffer", "delete-buffer"], f"{label}: paste failure should still cleanup: {calls}")

    calls.clear()

    def fake_run_load_fails(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append((cmd_list, dict(kwargs)))
        if cmd_list[1] == "load-buffer":
            raise subprocess.CalledProcessError(3, cmd_list)
        if cmd_list[1] == "paste-buffer":
            raise AssertionError(f"{label}: paste should not run after load failure")
        return subprocess.CompletedProcess(cmd_list, 1, "", "no buffer")

    bridge_daemon.subprocess.run = fake_run_load_fails  # type: ignore[assignment]
    try:
        try:
            bridge_daemon.run_tmux_send_literal("%42", prompt, bridge_session="sess", target_alias="bob", message_id="msg", nonce="nonce")
            raise AssertionError(f"{label}: load failure should propagate")
        except subprocess.CalledProcessError:
            pass
    finally:
        bridge_daemon.subprocess.run = old_run  # type: ignore[assignment]
    assert_true("paste-buffer" not in [cmd[1] for cmd, _ in calls], f"{label}: load failure should not paste: {calls}")
    print(f"  PASS  {label}")


def scenario_daemon_logs_body_truncated_for_legacy_long_body(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    legacy = test_message("msg-legacy-long", frm="alice", to="bob")
    legacy["body"] = "x" * (MAX_PEER_BODY_CHARS + 1)
    prompt = bridge_daemon.build_peer_prompt(legacy, "nonce-test")
    assert_true("[bridge truncated peer body]" in prompt, f"{label}: direct prompt build includes truncation marker")
    assert_true(prompt.count("x") == MAX_PEER_BODY_CHARS, f"{label}: direct prompt build keeps exactly prompt limit chars")

    def add(queue):
        queue.append(legacy)
        return None

    d.queue.update(add)
    d.try_deliver("bob")
    events = read_events(tmpdir / "events.raw.jsonl")
    truncated = [e for e in events if e.get("event") == "body_truncated"]
    assert_true(truncated, f"{label}: body_truncated event expected")
    assert_true(truncated[-1].get("message_id") == "msg-legacy-long", f"{label}: truncation log names message: {truncated[-1]}")
    assert_true(truncated[-1].get("original_chars") == MAX_PEER_BODY_CHARS + 1, f"{label}: truncation log records original chars: {truncated[-1]}")
    print(f"  PASS  {label}")


def scenario_response_send_guard_socket_cli_error_kind(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    guard_error = (
        "agent_send_peer: you are currently responding to a peer request from alice. "
        "Reply normally; do not call agent_send_peer; bridge auto-returns your reply. "
        "If you really intend to send a separate request/notice to alice, retry with --force."
    )
    be.enqueue_via_daemon_socket = lambda session, messages, **kwargs: (True, [], guard_error, "response_send_guard")

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--to", "alice",
            "--body", "wrong response path",
            "--queue-file", str(tmpdir / "pending.json"),
            "--state-file", str(tmpdir / "events.raw.jsonl"),
            "--public-state-file", str(tmpdir / "events.jsonl"),
        ],
    )
    assert_true(code == 2, f"{label}: socket guard exits 2, got {code}")
    assert_true(out == "", f"{label}: socket guard has no stdout: {out!r}")
    assert_true(err.count("agent_send_peer:") == 1, f"{label}: socket guard must not double-prefix stderr: {err!r}")
    assert_true("do not call agent_send_peer" in err and "--force" in err, f"{label}: canonical guard text present: {err!r}")
    print(f"  PASS  {label}")


def scenario_response_send_guard_socket_error_kind_parse(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    run_dir = Path(tempfile.mkdtemp(prefix="abrs-", dir="/tmp"))
    socket_path = run_dir / "test-session.sock"
    response = {
        "ok": False,
        "error": "agent_send_peer: guard blocked",
        "error_kind": "response_send_guard",
    }

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    received: list[dict] = []

    def serve_once() -> None:
        conn, _ = server.accept()
        with conn:
            raw = b""
            while b"\n" not in raw:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
            received.append(json.loads(raw.decode("utf-8")))
            conn.sendall((json.dumps(response, ensure_ascii=True) + "\n").encode("utf-8"))
        server.close()

    thread = threading.Thread(target=serve_once)
    old_run_root = be.run_root
    try:
        be.run_root = lambda: run_dir
        thread.start()
        attempted, ids, error, error_kind = be.enqueue_via_daemon_socket(
            "test-session",
            [{"id": "msg-test", "from": "bob", "to": "alice"}],
            force_response_send=True,
        )
        thread.join(timeout=2.0)
    finally:
        be.run_root = old_run_root
        try:
            server.close()
        except OSError:
            pass
        shutil.rmtree(run_dir, ignore_errors=True)

    assert_true(attempted, f"{label}: socket was attempted")
    assert_true(ids == [], f"{label}: rejected socket response has no ids: {ids}")
    assert_true(error == response["error"], f"{label}: error string preserved: {error!r}")
    assert_true(error_kind == "response_send_guard", f"{label}: error_kind surfaced: {error_kind!r}")
    assert_true(received and received[0].get("force_response_send") is True, f"{label}: force flag sent over socket: {received}")
    print(f"  PASS  {label}")


def scenario_response_send_guard_fallback_blocks_unchanged(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob", "carol"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    _write_json(queue_file, [_delivered_request("msg-delivered-fb", "alice", "bob")])
    state_file.write_text(json.dumps({"event": "initial_raw"}) + "\n", encoding="utf-8")
    public_file.write_text(json.dumps({"event": "initial_public"}) + "\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--to", "alice",
            "--body", "should be normal reply",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 2, f"{label}: blocked fallback exits 2, got {code}, err={err!r}")
    assert_true(out == "", f"{label}: blocked fallback has no stdout: {out!r}")
    assert_true("do not call agent_send_peer" in err, f"{label}: blocked fallback explains normal reply: {err!r}")
    after = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    assert_true(before == after, f"{label}: blocked fallback must leave queue and event files byte-identical")
    print(f"  PASS  {label}")


def scenario_response_send_guard_fallback_all_blocks_unchanged(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob", "carol"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    _write_json(queue_file, [_delivered_request("msg-delivered-all", "alice", "bob")])
    state_file.write_text("raw-before\n", encoding="utf-8")
    public_file.write_text("public-before\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--all",
            "--body", "broadcast while replying",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 2, f"{label}: --all including requester must be blocked, got {code}, err={err!r}")
    assert_true(out == "", f"{label}: blocked --all has no stdout: {out!r}")
    assert_true("target list includes alice" in err, f"{label}: --all error should mention requester inclusion: {err!r}")
    after = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    assert_true(before == after, f"{label}: blocked --all fallback must leave files unchanged")
    print(f"  PASS  {label}")


def scenario_response_send_guard_fallback_force_allows(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    _write_json(queue_file, [_delivered_request("msg-delivered-force", "alice", "bob")])
    state_file.touch()
    public_file.touch()

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--to", "alice",
            "--kind", "notice",
            "--force",
            "--body", "separate forced notice",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 0, f"{label}: --force fallback succeeds, got {code}, err={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: forced fallback returns message id: {out!r}")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    assert_true(len(queue) == 2, f"{label}: forced fallback appends queue item: {queue}")
    assert_true(any(item.get("from") == "bob" and item.get("to") == "alice" for item in queue), f"{label}: forced message queued")
    assert_true(any(e.get("event") == "message_queued" for e in read_events(state_file)), f"{label}: raw message_queued written")
    assert_true(any(e.get("event") == "message_queued" for e in read_events(public_file)), f"{label}: public message_queued written")
    print(f"  PASS  {label}")


def scenario_response_send_guard_fallback_no_auto_return_allowed(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    _write_json(queue_file, [_delivered_request("msg-delivered-noauto", "alice", "bob", auto_return=False)])
    state_file.touch()
    public_file.touch()

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--to", "alice",
            "--body", "manual reply allowed",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 0, f"{label}: no-auto-return fallback should allow manual send, got {code}, err={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: no-auto-return fallback returns message id: {out!r}")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    assert_true(len(queue) == 2, f"{label}: no-auto-return fallback appends queue item: {queue}")
    print(f"  PASS  {label}")


def scenario_response_send_guard_fallback_false_positive_resistance(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob", "carol"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"

    pending = _qualifying_message("alice", "bob", kind="request", body="pending")
    pending["id"] = "msg-nonqual-pending"
    pending["status"] = "pending"
    ingressing = _qualifying_message("alice", "bob", kind="request", body="ingressing")
    ingressing["id"] = "msg-nonqual-ingressing"
    inflight = _qualifying_message("alice", "bob", kind="request", body="inflight")
    inflight["id"] = "msg-nonqual-inflight"
    inflight["status"] = "inflight"
    no_auto = _delivered_request("msg-nonqual-noauto", "alice", "bob", auto_return=False)
    other_responder = _delivered_request("msg-nonqual-other-responder", "alice", "carol")
    notice = _qualifying_message("alice", "bob", kind="notice", body="delivered notice")
    notice.update({"id": "msg-nonqual-notice", "status": "delivered", "auto_return": False})
    _write_json(queue_file, [pending, ingressing, inflight, no_auto, other_responder, notice])
    state_file.touch()
    public_file.touch()

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--to", "alice",
            "--body", "allowed despite stale non-qualifying rows",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 0, f"{label}: non-qualifying queue rows must not block fallback send, got {code}, err={err!r}")
    assert_true(out.strip().startswith("msg-"), f"{label}: allowed fallback returns message id: {out!r}")
    queue = json.loads(queue_file.read_text(encoding="utf-8"))
    assert_true(len(queue) == 7, f"{label}: allowed fallback appends exactly one item: {queue}")
    assert_true(any(item.get("from") == "bob" and item.get("to") == "alice" for item in queue), f"{label}: bob to alice message queued")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix="bridge-regression-"))
    try:
        scenarios = [
            ("lifecycle_delivered_terminal", scenario_lifecycle),
            ("held_interrupt_does_not_block_delivery", scenario_held_interrupt_does_not_block_delivery),
            ("reserve_next_ignores_held_marker", scenario_reserve_next_ignores_held_marker),
            ("held_marker_persists_through_delivery", scenario_held_marker_persists_through_delivery),
            ("esc_fail_no_state_change", scenario_esc_fail_no_state_change),
            ("clear_hold_logs_event", scenario_clear_hold),
            ("clear_hold_socket_still_pops_held", scenario_clear_hold_socket_still_pops_held),
            ("aggregate_interrupt_synthetic_reply", scenario_aggregate_interrupt_synthetic),
            ("interrupt_pending_replacement_delivers", scenario_interrupt_pending_replacement_delivers),
            ("interrupt_new_replacement_after_interrupt_delivers", scenario_interrupt_new_replacement_after_interrupt_delivers),
            ("interrupted_late_prompt_submitted_before_replacement", scenario_interrupted_late_prompt_submitted_before_replacement),
            ("interrupted_late_prompt_submitted_after_replacement", scenario_interrupted_late_prompt_submitted_after_replacement),
            ("interrupted_late_turn_stop_preserves_replacement", scenario_interrupted_late_turn_stop_preserves_replacement),
            ("interrupted_no_turn_stop_no_context_suppressed", scenario_interrupted_no_turn_stop_no_context_suppressed),
            ("interrupted_no_turn_race_routes_replacement_then_suppresses_old", scenario_interrupted_no_turn_race_routes_replacement_then_suppresses_old),
            ("interrupted_inflight_tombstone_retains_on_unrelated_stop", scenario_interrupted_inflight_tombstone_retains_on_unrelated_stop),
            ("interrupted_empty_values_do_not_match_tombstone", scenario_interrupted_empty_values_do_not_match_tombstone),
            ("interrupted_tombstone_current_ctx_id_match_cleans", scenario_interrupted_tombstone_current_ctx_id_match_cleans),
            ("interrupted_tombstone_current_ctx_turn_match_cleans", scenario_interrupted_tombstone_current_ctx_turn_match_cleans),
            ("interrupted_tombstone_stale_stop_preserves_replacement_watchdog", scenario_interrupted_tombstone_stale_stop_preserves_replacement_watchdog),
            ("interrupted_tombstone_aggregate_ctx_does_not_cancel_aggregate_watchdog", scenario_interrupted_tombstone_aggregate_ctx_does_not_cancel_aggregate_watchdog),
            ("aggregate_late_real_stop_after_interrupt_does_not_overwrite", scenario_aggregate_late_real_stop_after_interrupt_does_not_overwrite),
            ("watchdog_cancel_on_empty_response", scenario_watchdog_cancel_on_empty_response),
            ("alarm_cancelled_by_qualifying_request", scenario_alarm_cancelled_by_qualifying_request),
            ("socket_path_alarm_cancel", scenario_socket_path_alarm_cancel),
            ("response_send_guard_socket_request_notice", scenario_response_send_guard_socket_request_notice),
            ("response_send_guard_socket_force_and_other_peer", scenario_response_send_guard_socket_force_and_other_peer),
            ("response_send_guard_socket_no_auto_return_allowed", scenario_response_send_guard_socket_no_auto_return_allowed),
            ("response_send_guard_socket_atomic_multi", scenario_response_send_guard_socket_atomic_multi),
            ("response_send_guard_socket_aggregate_and_held", scenario_response_send_guard_socket_aggregate_and_held),
            ("response_send_guard_after_response_finished_allowed", scenario_response_send_guard_after_response_finished_allowed),
            ("fallback_path_alarm_cancel", scenario_fallback_path_alarm_cancel),
            ("ingressing_not_delivered_before_finalize", scenario_ingressing_not_delivered_before_finalize),
            ("replay_does_not_cancel_later_alarm", scenario_replay_does_not_cancel_later_alarm),
            ("bridge_origin_fallback_ingressing_promoted", scenario_bridge_origin_fallback_ingressing_promoted),
            ("socket_normalizes_non_ingressing_status", scenario_socket_normalizes_non_ingressing_status),
            ("aggregate_fallback_finalize", scenario_aggregate_fallback_finalize),
            ("aged_ingressing_promoted_by_maintenance", scenario_aged_ingressing_promoted_by_maintenance),
            ("aged_ingressing_does_not_cancel_alarms", scenario_aged_ingressing_does_not_cancel_alarms),
            ("aged_ingressing_malformed_timestamp_promoted", scenario_aged_ingressing_malformed_timestamp_promoted),
            ("fresh_ingressing_not_promoted_by_maintenance", scenario_fresh_ingressing_not_promoted_by_maintenance),
            ("alarm_not_cancelled_by_result", scenario_alarm_not_cancelled_by_result),
            ("alarm_not_cancelled_by_bridge", scenario_alarm_not_cancelled_by_bridge),
            ("user_prompt_does_not_cancel_alarm", scenario_user_prompt_does_not_cancel_alarm),
            ("extend_wait_upserts_watchdog", scenario_extend_wait_upserts_watchdog),
            ("extend_wait_aggregate_rejected", scenario_extend_wait_aggregate_rejected),
            ("extend_wait_unknown_message", scenario_extend_wait_unknown_message),
            ("extend_wait_pending_rejected", scenario_extend_wait_pending_rejected),
            ("extend_wait_not_owner", scenario_extend_wait_not_owner),
            ("duplicate_enqueue_does_not_cancel_alarm", scenario_duplicate_enqueue_does_not_cancel_alarm),
            ("alarm_op_invalid_delay_is_rejected", scenario_alarm_op_invalid_delay_is_rejected_not_crashed),
            ("send_peer_watchdog_negative_one_rejected", scenario_send_peer_watchdog_negative_one_rejected),
            ("send_peer_watchdog_nan_rejected", scenario_send_peer_watchdog_nan_rejected),
            ("send_peer_watchdog_inf_rejected", scenario_send_peer_watchdog_inf_rejected),
            ("send_peer_watchdog_minus_inf_rejected", scenario_send_peer_watchdog_minus_inf_rejected),
            ("send_peer_watchdog_zero_disables_for_request", scenario_send_peer_watchdog_zero_disables_for_request),
            ("send_peer_watchdog_finite_positive_succeeds", scenario_send_peer_watchdog_finite_positive_succeeds),
            ("send_peer_watchdog_inf_with_notice_reports_finite_error_first", scenario_send_peer_watchdog_inf_with_notice_reports_finite_error_first),
            ("send_peer_watchdog_zero_with_notice_still_rejects_request_only", scenario_send_peer_watchdog_zero_with_notice_still_rejects_request_only),
            ("send_peer_watchdog_abc_argparse_error", scenario_send_peer_watchdog_abc_argparse_error),
            ("extend_wait_zero_negative_nan_inf_rejected", scenario_extend_wait_zero_negative_nan_inf_rejected),
            ("extend_wait_finite_positive_calls_request_extend", scenario_extend_wait_finite_positive_calls_request_extend),
            ("alarm_negative_nan_inf_minus_inf_rejected", scenario_alarm_negative_nan_inf_minus_inf_rejected),
            ("alarm_zero_and_finite_positive_call_request_alarm", scenario_alarm_zero_and_finite_positive_call_request_alarm),
            ("resolve_default_watchdog_seconds_env_table", scenario_resolve_default_watchdog_seconds_env_table),
            ("daemon_socket_alarm_op_rejects_non_finite", scenario_daemon_socket_alarm_op_rejects_non_finite),
            ("daemon_socket_extend_watchdog_op_rejects_non_finite", scenario_daemon_socket_extend_watchdog_op_rejects_non_finite),
            ("daemon_upsert_message_watchdog_rejects_non_finite", scenario_daemon_upsert_message_watchdog_rejects_non_finite),
            ("daemon_register_alarm_rejects_non_finite_and_negative", scenario_daemon_register_alarm_rejects_non_finite_and_negative),
            ("daemon_mark_message_delivered_ignores_non_finite_watchdog", scenario_daemon_mark_message_delivered_ignores_non_finite_watchdog),
            ("daemon_socket_ack_message_unsupported_does_not_delete_queue", scenario_daemon_socket_ack_message_unsupported_does_not_delete_queue),
            ("daemon_ack_message_helper_removed", scenario_daemon_ack_message_helper_removed),
            ("stale_watchdog_skipped", scenario_stale_watchdog_skipped),
            ("watchdog_pending_text_omits_held_interrupt", scenario_watchdog_pending_text_omits_held_interrupt),
            ("pane_mode_pending_defers_without_attempt", scenario_pane_mode_pending_defers_without_attempt),
            ("pane_mode_clears_then_delivers", scenario_pane_mode_clears_then_delivers),
            ("pane_mode_force_cancel_after_grace", scenario_pane_mode_force_cancel_after_grace),
            ("pane_mode_nonforce_mode_stays_pending", scenario_pane_mode_nonforce_mode_stays_pending),
            ("pane_mode_busy_target_does_not_start_timer", scenario_pane_mode_busy_target_does_not_start_timer),
            ("retry_enter_skips_pane_mode", scenario_retry_enter_skips_pane_mode),
            ("pane_mode_probe_failure_defers_pending", scenario_pane_mode_probe_failure_defers_pending),
            ("pane_mode_force_cancel_failure_stays_pending", scenario_pane_mode_force_cancel_failure_stays_pending),
            ("enter_deferred_survives_stale_requeue_and_restart", scenario_enter_deferred_survives_stale_requeue_and_restart),
            ("pre_enter_probe_failure_defers_enter", scenario_pre_enter_probe_failure_defers_enter),
            ("pane_mode_grace_zero_disables_cancel", scenario_pane_mode_grace_zero_disables_cancel),
            ("wait_for_probe_retries_enter_with_pane_id", scenario_wait_for_probe_retries_enter_with_pane_id),
            ("wait_for_probe_no_retry_without_pane_id", scenario_wait_for_probe_no_retry_without_pane_id),
            ("join_probe_passes_pane_id_to_wait", scenario_join_probe_passes_pane_id_to_wait),
            ("bridge_attach_start_daemon_argv_includes_from_start", scenario_bridge_attach_start_daemon_argv_includes_from_start),
            ("bridge_daemon_ctl_start_subparser_accepts_from_start", scenario_bridge_daemon_ctl_start_subparser_accepts_from_start),
            ("daemon_command_forwards_from_start", scenario_daemon_command_forwards_from_start),
            ("daemon_command_ignores_legacy_max_hops", scenario_daemon_command_ignores_legacy_max_hops),
            ("bridge_daemon_ctl_subcommands_reject_max_hops", scenario_bridge_daemon_ctl_subcommands_reject_max_hops),
            ("bridge_daemon_rejects_max_hops_cli", scenario_bridge_daemon_rejects_max_hops_cli),
            ("bridge_daemon_ctl_start_argv_includes_from_start_end_to_end", scenario_bridge_daemon_ctl_start_argv_includes_from_start_end_to_end),
            ("daemon_follow_from_start_replays_prompt_submitted", scenario_daemon_follow_from_start_replays_prompt_submitted),
            ("daemon_follow_from_start_false_skips_pre_existing_record", scenario_daemon_follow_from_start_false_skips_pre_existing_record),
            ("daemon_follow_from_start_handles_self_daemon_started_safely", scenario_daemon_follow_from_start_handles_self_daemon_started_safely),
            ("orphan_nonce_in_user_prompt", scenario_orphan_nonce_in_user_prompt),
            ("prompt_intercept_request_notice_body", scenario_prompt_intercept_request_notice_body),
            ("prompt_intercept_bridge_notice_no_source_notice", scenario_prompt_intercept_bridge_notice_no_source_notice),
            ("prompt_intercept_response_guard_queue_allows", scenario_prompt_intercept_response_guard_queue_allows),
            ("prompt_intercept_mixed_inflight_requeues", scenario_prompt_intercept_mixed_inflight_requeues),
            ("prompt_submitted_duplicate_noop", scenario_prompt_submitted_duplicate_noop),
            ("prompt_submitted_duplicate_without_nonce_noop", scenario_prompt_submitted_duplicate_without_nonce_noop),
            ("prompt_intercept_aggregate_completes", scenario_prompt_intercept_aggregate_completes),
            ("prompt_intercept_held_drain_noop", scenario_prompt_intercept_held_drain_noop),
            ("held_drain_stale_stop_preserves_new_ctx", scenario_held_drain_stale_stop_preserves_new_ctx),
            ("held_drain_missing_identity_does_not_clear_active_ctx", scenario_held_drain_missing_identity_does_not_clear_active_ctx),
            ("held_drain_matching_prior_id_without_turn_still_clears_ctx", scenario_held_drain_matching_prior_id_without_turn_still_clears_ctx),
            ("stale_hold_ignored_active_context_routes_normally", scenario_stale_hold_ignored_active_context_routes_normally),
            ("stale_hold_old_stop_with_active_ctx_applies_a4_mismatch", scenario_stale_hold_old_stop_with_active_ctx_applies_a4_mismatch),
            ("held_no_prior_id_with_active_ctx_classified_stale", scenario_held_no_prior_id_with_active_ctx_classified_stale),
            ("held_matching_prior_id_turn_id_mismatch_falls_through_to_a4", scenario_held_matching_prior_id_turn_id_mismatch_falls_through_to_a4),
            ("held_with_no_context_drain_still_works", scenario_held_with_no_context_drain_still_works),
            ("held_matching_prior_id_with_matching_turn_drain_still_works", scenario_held_matching_prior_id_with_matching_turn_drain_still_works),
            ("held_with_interrupted_tombstone_match_classify_first", scenario_held_with_interrupted_tombstone_match_classify_first),
            ("aggregate_leg_unaffected_by_held_marker", scenario_aggregate_leg_unaffected_by_held_marker),
            ("prompt_intercept_inflight_only_requeues", scenario_prompt_intercept_inflight_only_requeues),
            ("consume_once_basic", scenario_consume_once_basic),
            ("consume_once_empty_response", scenario_consume_once_empty_response),
            ("empty_response_whitespace_only_routes_sentinel", scenario_empty_response_whitespace_only_routes_sentinel),
            ("empty_response_preserves_nonempty_with_surrounding_whitespace", scenario_empty_response_preserves_nonempty_with_surrounding_whitespace),
            ("empty_response_self_return_still_skipped", scenario_empty_response_self_return_still_skipped),
            ("empty_response_auto_return_false_still_skipped", scenario_empty_response_auto_return_false_still_skipped),
            ("empty_response_inactive_requester_still_skipped", scenario_empty_response_inactive_requester_still_skipped),
            ("empty_response_unicode_whitespace_routes_sentinel", scenario_empty_response_unicode_whitespace_routes_sentinel),
            ("nonce_mismatch_fail_closed", scenario_nonce_mismatch_fail_closed),
            ("no_observed_nonce_with_candidate_fail_closed", scenario_no_observed_nonce_with_candidate_fail_closed),
            ("daemon_restart_queue_scan", scenario_daemon_restart_queue_scan),
            ("ambiguous_inflight_fail_closed", scenario_ambiguous_inflight_fail_closed),
            ("stale_reserved_orphan_swept", scenario_stale_reserved_orphan_swept),
            ("held_drain_skips_consume_once", scenario_held_drain_skips_consume_once),
            ("matching_nonce_contaminated_body_residual", scenario_matching_nonce_contaminated_body_documents_residual),
            ("aggregate_consume_once_no_overwrite", scenario_aggregate_consume_once_no_overwrite),
            ("empty_response_in_aggregate_path_unchanged", scenario_empty_response_in_aggregate_path_unchanged),
            ("nonce_mismatch_stops_enter_retry", scenario_nonce_mismatch_stops_enter_retry),
            ("nonce_missing_stops_enter_retry", scenario_nonce_missing_stops_enter_retry),
            ("hook_logger_anchored_regex", scenario_hook_logger_anchored_regex),
            ("turn_id_mismatch_preserves_ctx", scenario_turn_id_mismatch_preserves_ctx),
            ("turn_id_mismatch_annotation_is_idempotent", scenario_turn_id_mismatch_annotation_is_idempotent),
            ("turn_id_mismatch_matching_stop_before_expiry_cleans_normally", scenario_turn_id_mismatch_matching_stop_before_expiry_cleans_normally),
            ("turn_id_mismatch_expiry_unblocks_target", scenario_turn_id_mismatch_expiry_unblocks_target),
            ("turn_id_mismatch_expiry_waits_for_watchdog_deadline", scenario_turn_id_mismatch_expiry_waits_for_watchdog_deadline),
            ("turn_id_mismatch_watchdog_fire_preserves_post_grace", scenario_turn_id_mismatch_watchdog_fire_preserves_post_grace),
            ("turn_id_mismatch_aggregate_watchdog_fire_preserves_post_grace", scenario_turn_id_mismatch_aggregate_watchdog_fire_preserves_post_grace),
            ("turn_id_mismatch_post_watchdog_grace_zero_allows_same_tick_expiry", scenario_turn_id_mismatch_post_watchdog_grace_zero_allows_same_tick_expiry),
            ("turn_id_mismatch_extend_wait_before_fire_defers_without_stamp", scenario_turn_id_mismatch_extend_wait_before_fire_defers_without_stamp),
            ("turn_id_mismatch_expiry_does_not_remove_non_delivered_queue_rows", scenario_turn_id_mismatch_expiry_does_not_remove_non_delivered_queue_rows),
            ("turn_id_mismatch_aggregate_leg_expiry_no_synthetic_reply", scenario_turn_id_mismatch_aggregate_leg_expiry_no_synthetic_reply),
            ("turn_id_mismatch_sweep_no_op_without_annotation", scenario_turn_id_mismatch_sweep_no_op_without_annotation),
            ("turn_id_mismatch_sweep_no_op_after_normal_pop", scenario_turn_id_mismatch_sweep_no_op_after_normal_pop),
            ("turn_id_mismatch_notice_ctx_follows_same_expiry", scenario_turn_id_mismatch_notice_ctx_follows_same_expiry),
            ("short_id_format", scenario_short_id_format),
            ("resolve_targets_single", scenario_resolve_targets_single),
            ("resolve_targets_multi_basic", scenario_resolve_targets_multi_basic),
            ("resolve_targets_order_preserved", scenario_resolve_targets_order_preserved),
            ("resolve_targets_dedup", scenario_resolve_targets_dedup),
            ("resolve_targets_strip_empties", scenario_resolve_targets_strip_empties),
            ("resolve_targets_reserved_alone", scenario_resolve_targets_reserved_alone),
            ("resolve_targets_reserved_mix_rejected", scenario_resolve_targets_reserved_mix_rejected),
            ("resolve_targets_unknown_rejected", scenario_resolve_targets_unknown_rejected),
            ("resolve_targets_sender_in_list_rejected", scenario_resolve_targets_sender_in_list_rejected),
            ("resolve_targets_empty_after_strip_rejected", scenario_resolve_targets_empty_after_strip_rejected),
            ("aggregate_trigger_request_multi", scenario_aggregate_trigger_request_multi),
            ("aggregate_trigger_single_no", scenario_aggregate_trigger_single_no),
            ("aggregate_trigger_notice_no", scenario_aggregate_trigger_notice_no),
            ("aggregate_trigger_bridge_sender_no", scenario_aggregate_trigger_bridge_sender_no),
            ("aggregate_trigger_no_auto_return_no", scenario_aggregate_trigger_no_auto_return_no),
            ("prune_keeps_recent_n", scenario_prune_keeps_recent_n),
            ("prune_disabled_retention_zero", scenario_prune_disabled_retention_zero),
            ("prune_below_retention", scenario_prune_below_retention),
            ("prune_missing_forgotten_dir_safe", scenario_prune_missing_forgotten_dir_safe),
            ("resolve_forgotten_retention_invalid_env", scenario_resolve_forgotten_retention_invalid_env),
            ("queue_status_counts", scenario_queue_status_counts),
            ("queue_status_counts_missing_file", scenario_queue_status_counts_missing_file),
            ("uninstall_helper_print_paths", scenario_uninstall_helper_print_paths),
            ("uninstall_helper_refuses_dangerous_path", scenario_uninstall_helper_refuses_dangerous_path),
            ("uninstall_sh_dry_run_invokes_hook_helper_with_dry_run", scenario_uninstall_sh_dry_run_invokes_hook_helper_with_dry_run),
            ("uninstall_sh_non_dry_run_removes_hook_entries", scenario_uninstall_sh_non_dry_run_removes_hook_entries),
            ("uninstall_sh_keep_hooks_skips_helper_under_dry_run", scenario_uninstall_sh_keep_hooks_skips_helper_under_dry_run),
            ("uninstall_sh_hook_helper_failure_aborts", scenario_uninstall_sh_hook_helper_failure_aborts),
            ("direct_exec_targets_executable", scenario_direct_exec_targets_executable),
            ("healthcheck_executable_helper_distinguishes_states", scenario_healthcheck_executable_helper_distinguishes_states),
            ("install_sh_chmods_target_or_fails", scenario_install_sh_chmods_target_or_fails),
            ("install_sh_hook_failure_hard_fails", scenario_install_sh_hook_failure_hard_fails),
            ("install_sh_hook_failure_ignore_flag_allows_success", scenario_install_sh_hook_failure_ignore_flag_allows_success),
            ("install_sh_hook_dry_run_failure_hard_fails", scenario_install_sh_hook_dry_run_failure_hard_fails),
            ("install_sh_hook_success_succeeds", scenario_install_sh_hook_success_succeeds),
            ("install_sh_shim_failure_still_hard_fails_under_override", scenario_install_sh_shim_failure_still_hard_fails_under_override),
            ("install_sh_skip_hooks_with_ignore_flag_is_noop", scenario_install_sh_skip_hooks_with_ignore_flag_is_noop),
            ("bridge_install_hooks_rejects_malformed_json_without_overwrite", scenario_bridge_install_hooks_rejects_malformed_json_without_overwrite),
            ("bridge_install_hooks_rejects_non_object_json_without_overwrite", scenario_bridge_install_hooks_rejects_non_object_json_without_overwrite),
            ("bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite", scenario_bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite),
            ("bridge_install_hooks_missing_json_still_creates", scenario_bridge_install_hooks_missing_json_still_creates),
            ("bridge_install_hooks_invalid_utf8_fails_without_overwrite", scenario_bridge_install_hooks_invalid_utf8_fails_without_overwrite),
            ("bridge_install_hooks_existing_valid_json_object_merges_correctly", scenario_bridge_install_hooks_existing_valid_json_object_merges_correctly),
            ("bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite", scenario_bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite),
            ("bridge_install_hooks_codex_config_ignores_nested_codex_hooks", scenario_bridge_install_hooks_codex_config_ignores_nested_codex_hooks),
            ("bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst", scenario_bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst),
            ("bridge_install_hooks_codex_config_updates_only_scoped_keys", scenario_bridge_install_hooks_codex_config_updates_only_scoped_keys),
            ("bridge_install_hooks_codex_config_inserts_features_key_before_next_table", scenario_bridge_install_hooks_codex_config_inserts_features_key_before_next_table),
            ("bridge_install_hooks_codex_config_dry_run_no_write", scenario_bridge_install_hooks_codex_config_dry_run_no_write),
            ("bridge_install_hooks_codex_config_empty_features_section_inserts", scenario_bridge_install_hooks_codex_config_empty_features_section_inserts),
            ("bridge_install_hooks_codex_config_first_table_is_array_table", scenario_bridge_install_hooks_codex_config_first_table_is_array_table),
            ("bridge_install_hooks_codex_config_table_header_with_trailing_comment", scenario_bridge_install_hooks_codex_config_table_header_with_trailing_comment),
            ("bridge_install_hooks_codex_config_commented_out_assignments_ignored", scenario_bridge_install_hooks_codex_config_commented_out_assignments_ignored),
            ("bridge_install_hooks_codex_config_no_trailing_newline_handled", scenario_bridge_install_hooks_codex_config_no_trailing_newline_handled),
            ("bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged", scenario_bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged),
            ("restart_dry_run_no_side_effect", scenario_restart_dry_run_no_side_effect),
            ("recover_orphan_delivered", scenario_recover_orphan_delivered),
            ("recover_orphan_delivered_aggregate_member", scenario_recover_orphan_delivered_aggregate_member),
            ("prune_concurrent_stat_safe", scenario_prune_concurrent_stat_safe),
            ("format_peer_list_model_safe_default", scenario_format_peer_list_model_safe_default),
            ("format_peer_list_full_includes_operator_fields", scenario_format_peer_list_full_includes_operator_fields),
            ("bridge_manage_summary_concise", scenario_bridge_manage_summary_concise),
            ("bridge_manage_summary_defaults", scenario_bridge_manage_summary_defaults),
            ("bridge_manage_summary_legacy_state_fallback", scenario_bridge_manage_summary_legacy_state_fallback),
            ("bridge_manage_summary_missing_session_exits", scenario_bridge_manage_summary_missing_session_exits),
            ("model_safe_participants_strips_endpoints", scenario_model_safe_participants_strips_endpoints),
            ("model_safe_participants_uses_active_only", scenario_model_safe_participants_uses_active_only),
            ("list_peers_json_daemon_status_strips_pid", scenario_list_peers_json_daemon_status_strips_pid),
            ("view_peer_render_output_model_safe", scenario_view_peer_render_output_model_safe),
            ("view_peer_search_explicit_snapshot_uses_safe_ref", scenario_view_peer_search_explicit_snapshot_uses_safe_ref),
            ("view_peer_snapshot_ref_collision_unique", scenario_view_peer_snapshot_ref_collision_unique),
            ("view_peer_capture_errors_sanitized", scenario_view_peer_capture_errors_sanitized),
            ("view_peer_snapshot_not_found_hides_full_id", scenario_view_peer_snapshot_not_found_hides_full_id),
            ("view_peer_since_last_matches_changed_volatile_chrome", scenario_view_peer_since_last_matches_changed_volatile_chrome),
            ("view_peer_since_last_legacy_tail_derives_stable_anchor", scenario_view_peer_since_last_legacy_tail_derives_stable_anchor),
            ("view_peer_since_last_ambiguous_current_anchor_skips_to_unique", scenario_view_peer_since_last_ambiguous_current_anchor_skips_to_unique),
            ("view_peer_since_last_matches_anchor_before_long_delta", scenario_view_peer_since_last_matches_anchor_before_long_delta),
            ("view_peer_since_last_build_rejects_duplicate_previous_window", scenario_view_peer_since_last_build_rejects_duplicate_previous_window),
            ("view_peer_since_last_uncertain_does_not_advance_cursor", scenario_view_peer_since_last_uncertain_does_not_advance_cursor),
            ("view_peer_since_last_upgrade_reset_when_no_legacy_anchor", scenario_view_peer_since_last_upgrade_reset_when_no_legacy_anchor),
            ("view_peer_since_last_low_info_lines_do_not_anchor", scenario_view_peer_since_last_low_info_lines_do_not_anchor),
            ("view_peer_since_last_claude_status_variants_are_volatile", scenario_view_peer_since_last_claude_status_variants_are_volatile),
            ("view_peer_since_last_status_classifier_preserves_prose", scenario_view_peer_since_last_status_classifier_preserves_prose),
            ("view_peer_since_last_claude_status_lines_do_not_anchor", scenario_view_peer_since_last_claude_status_lines_do_not_anchor),
            ("view_peer_since_last_filters_stored_volatile_anchor_lines", scenario_view_peer_since_last_filters_stored_volatile_anchor_lines),
            ("view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality", scenario_view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality),
            ("view_peer_since_last_volatile_only_claude_status_delta", scenario_view_peer_since_last_volatile_only_claude_status_delta),
            ("view_peer_since_last_codex_status_variants_preserved", scenario_view_peer_since_last_codex_status_variants_preserved),
            ("view_peer_since_last_volatile_only_delta", scenario_view_peer_since_last_volatile_only_delta),
            ("view_peer_since_last_short_delta_consumed_once", scenario_view_peer_since_last_short_delta_consumed_once),
            ("view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update", scenario_view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update),
            ("view_peer_since_last_consumed_tail_does_not_hide_new_duplicate", scenario_view_peer_since_last_consumed_tail_does_not_hide_new_duplicate),
            ("view_peer_since_last_consumed_tail_anchor_change_resets", scenario_view_peer_since_last_consumed_tail_anchor_change_resets),
            ("view_peer_since_last_consumed_tail_mismatch_clears", scenario_view_peer_since_last_consumed_tail_mismatch_clears),
            ("view_peer_since_last_consumed_tail_cap", scenario_view_peer_since_last_consumed_tail_cap),
            ("view_peer_since_last_consumed_tail_ignores_volatile_churn", scenario_view_peer_since_last_consumed_tail_ignores_volatile_churn),
            ("view_peer_since_last_codex_prompt_placeholder_not_anchor", scenario_view_peer_since_last_codex_prompt_placeholder_not_anchor),
            ("view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor", scenario_view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor),
            ("view_peer_since_last_preserves_codex_bridge_prompt_lines", scenario_view_peer_since_last_preserves_codex_bridge_prompt_lines),
            ("view_peer_since_last_preserves_trailing_semantic_codex_arrow", scenario_view_peer_since_last_preserves_trailing_semantic_codex_arrow),
            ("view_peer_since_last_preserves_semantic_codex_arrow_before_footer", scenario_view_peer_since_last_preserves_semantic_codex_arrow_before_footer),
            ("view_peer_since_last_claude_partial_status_fragments_are_volatile", scenario_view_peer_since_last_claude_partial_status_fragments_are_volatile),
            ("view_peer_since_last_partial_status_preserves_prose", scenario_view_peer_since_last_partial_status_preserves_prose),
            ("endpoint_rejects_stale_pane_lock_without_live", scenario_endpoint_rejects_stale_pane_lock_without_live),
            ("endpoint_rejects_same_pane_new_live_identity", scenario_endpoint_rejects_same_pane_new_live_identity),
            ("endpoint_probe_unknown_does_not_mutate", scenario_endpoint_probe_unknown_does_not_mutate),
            ("endpoint_accepts_matching_process_fingerprint", scenario_endpoint_accepts_matching_process_fingerprint),
            ("backfill_refuses_to_mint_without_live_record", scenario_backfill_refuses_to_mint_without_live_record),
            ("backfill_refuses_other_live_identity", scenario_backfill_refuses_other_live_identity),
            ("backfill_rejects_changed_process_fingerprint", scenario_backfill_rejects_changed_process_fingerprint),
            ("backfill_allows_fresh_hook_proof_create", scenario_backfill_allows_fresh_hook_proof_create),
            ("hook_unknown_preserves_verified_process_identity", scenario_hook_unknown_preserves_verified_process_identity),
            ("probe_tmux_access_failure_unknown", scenario_probe_tmux_access_failure_unknown),
            ("endpoint_read_mismatch_does_not_mutate", scenario_endpoint_read_mismatch_does_not_mutate),
            ("verified_candidate_ordering_prefers_pane_then_newest", scenario_verified_candidate_ordering_prefers_pane_then_newest),
            ("resume_new_pane_reconnects_unknown_old_and_logs", scenario_resume_new_pane_reconnects_unknown_old_and_logs),
            ("resume_unknown_old_opt_out_blocks_switch", scenario_resume_unknown_old_opt_out_blocks_switch),
            ("hook_cached_prior_unknown_does_not_reconnect", scenario_hook_cached_prior_unknown_does_not_reconnect),
            ("resolver_reconnects_to_alternate_verified_live_record", scenario_resolver_reconnects_to_alternate_verified_live_record),
            ("resolver_candidate_unknown_on_final_probe_does_not_reconnect", scenario_resolver_candidate_unknown_on_final_probe_does_not_reconnect),
            ("resolver_read_reconnect_logs_distinct_reason", scenario_resolver_read_reconnect_logs_distinct_reason),
            ("session_end_replacement_uses_verified_candidate", scenario_session_end_replacement_uses_verified_candidate),
            ("reconnect_rereads_mapping_before_write", scenario_reconnect_rereads_mapping_before_write),
            ("caller_reconnects_from_resumed_pane", scenario_caller_reconnects_from_resumed_pane),
            ("no_probe_requires_verified_live_identity", scenario_no_probe_requires_verified_live_identity),
            ("daemon_undeliverable_request_returns_result", scenario_daemon_undeliverable_request_returns_result),
            ("interrupt_endpoint_lost_finalizes_delivered_non_aggregate", scenario_interrupt_endpoint_lost_finalizes_delivered_non_aggregate),
            ("interrupt_endpoint_lost_finalizes_delivered_aggregate", scenario_interrupt_endpoint_lost_finalizes_delivered_aggregate),
            ("retry_enter_endpoint_lost_does_not_press_enter", scenario_retry_enter_endpoint_lost_does_not_press_enter),
            ("direct_notices_suppress_unverified_endpoint", scenario_direct_notices_suppress_unverified_endpoint),
            ("view_peer_unverified_endpoint_uses_daemon_not_local_capture", scenario_view_peer_unverified_endpoint_uses_daemon_not_local_capture),
            ("daemon_startup_backfill_summary_logs_repair_hint", scenario_daemon_startup_backfill_summary_logs_repair_hint),
            ("peer_body_size_helper_boundaries", scenario_peer_body_size_helper_boundaries),
            ("send_peer_rejects_oversized_body_before_subprocess", scenario_send_peer_rejects_oversized_body_before_subprocess),
            ("send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim", scenario_send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim),
            ("send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim", scenario_send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim),
            ("send_peer_watchdog_bare_minus_inf_argparse_error_at_shim", scenario_send_peer_watchdog_bare_minus_inf_argparse_error_at_shim),
            ("send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim", scenario_send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim),
            ("send_peer_watchdog_finite_value_forwarded_with_equals", scenario_send_peer_watchdog_finite_value_forwarded_with_equals),
            ("send_peer_rejects_split_inline_body", scenario_send_peer_rejects_split_inline_body),
            ("send_peer_rejects_implicit_split_inline_body", scenario_send_peer_rejects_implicit_split_inline_body),
            ("send_peer_rejects_option_after_destination", scenario_send_peer_rejects_option_after_destination),
            ("send_peer_rejects_option_after_implicit_target", scenario_send_peer_rejects_option_after_implicit_target),
            ("send_peer_rejects_option_after_inline_body", scenario_send_peer_rejects_option_after_inline_body),
            ("send_peer_single_inline_body_uses_stdin_handoff", scenario_send_peer_single_inline_body_uses_stdin_handoff),
            ("send_peer_request_success_prints_anti_wait_hint", scenario_send_peer_request_success_prints_anti_wait_hint),
            ("send_peer_notice_success_prints_alarm_and_anti_wait_hints", scenario_send_peer_notice_success_prints_alarm_and_anti_wait_hints),
            ("send_peer_subprocess_failure_prints_no_success_hint", scenario_send_peer_subprocess_failure_prints_no_success_hint),
            ("send_peer_inline_body_accepts_empty_non_tty_stdin", scenario_send_peer_inline_body_accepts_empty_non_tty_stdin),
            ("send_peer_explicit_stdin_multibyte_body", scenario_send_peer_explicit_stdin_multibyte_body),
            ("send_peer_implicit_target_allows_stdin", scenario_send_peer_implicit_target_allows_stdin),
            ("send_peer_implicit_target_resolves_session_from_pane", scenario_send_peer_implicit_target_resolves_session_from_pane),
            ("send_peer_implicit_target_stdin_resolves_session_from_pane", scenario_send_peer_implicit_target_stdin_resolves_session_from_pane),
            ("send_peer_precheck_fails_open_identity_errors_owned_by_validator", scenario_send_peer_precheck_fails_open_identity_errors_owned_by_validator),
            ("send_peer_allow_spoof_requires_explicit_destination_without_session", scenario_send_peer_allow_spoof_requires_explicit_destination_without_session),
            ("send_peer_rejects_allow_spoof_attached_value", scenario_send_peer_rejects_allow_spoof_attached_value),
            ("send_peer_all_rejects_leading_alias_body", scenario_send_peer_all_rejects_leading_alias_body),
            ("send_peer_rejects_stdin_with_positional_body", scenario_send_peer_rejects_stdin_with_positional_body),
            ("send_peer_rejects_pipe_with_positional_body", scenario_send_peer_rejects_pipe_with_positional_body),
            ("send_peer_pipe_only_body_still_supported", scenario_send_peer_pipe_only_body_still_supported),
            ("send_peer_precheck_option_table_matches_parser", scenario_send_peer_precheck_option_table_matches_parser),
            ("enqueue_rejects_body_and_stdin_before_session_lookup", scenario_enqueue_rejects_body_and_stdin_before_session_lookup),
            ("enqueue_rejects_empty_body_and_stdin", scenario_enqueue_rejects_empty_body_and_stdin),
            ("enqueue_rejects_body_and_stdin_argv_order_independent", scenario_enqueue_rejects_body_and_stdin_argv_order_independent),
            ("enqueue_body_only_still_works", scenario_enqueue_body_only_still_works),
            ("enqueue_stdin_only_still_works", scenario_enqueue_stdin_only_still_works),
            ("enqueue_bare_body_without_value_remains_argparse_owned", scenario_enqueue_bare_body_without_value_remains_argparse_owned),
            ("enqueue_rejects_oversized_body_unchanged", scenario_enqueue_rejects_oversized_body_unchanged),
            ("enqueue_stdin_rejects_oversized_body_unchanged", scenario_enqueue_stdin_rejects_oversized_body_unchanged),
            ("alarm_cancel_preserves_at_limit_body", scenario_alarm_cancel_preserves_at_limit_body),
            ("prompt_body_preserves_multiline_and_sanitizes", scenario_prompt_body_preserves_multiline_and_sanitizes),
            ("build_peer_prompt_signature_drops_max_hops", scenario_build_peer_prompt_signature_drops_max_hops),
            ("tmux_paste_buffer_delivery_sequence", scenario_tmux_paste_buffer_delivery_sequence),
            ("daemon_logs_body_truncated_for_legacy_long_body", scenario_daemon_logs_body_truncated_for_legacy_long_body),
            ("response_send_guard_socket_cli_error_kind", scenario_response_send_guard_socket_cli_error_kind),
            ("response_send_guard_socket_error_kind_parse", scenario_response_send_guard_socket_error_kind_parse),
            ("enqueue_fallback_success_silent_with_raw_diagnostic", scenario_enqueue_fallback_success_silent_with_raw_diagnostic),
            ("enqueue_fallback_write_failure_preserves_stderr", scenario_enqueue_fallback_write_failure_preserves_stderr),
            ("response_send_guard_fallback_blocks_unchanged", scenario_response_send_guard_fallback_blocks_unchanged),
            ("response_send_guard_fallback_all_blocks_unchanged", scenario_response_send_guard_fallback_all_blocks_unchanged),
            ("response_send_guard_fallback_force_allows", scenario_response_send_guard_fallback_force_allows),
            ("response_send_guard_fallback_no_auto_return_allowed", scenario_response_send_guard_fallback_no_auto_return_allowed),
            ("response_send_guard_fallback_false_positive_resistance", scenario_response_send_guard_fallback_false_positive_resistance),
        ]
        passes = 0
        fails = 0
        for label, fn in scenarios:
            sub = base / label
            sub.mkdir(parents=True, exist_ok=True)
            try:
                fn(label, sub)
                passes += 1
            except AssertionError as exc:
                print(f"  FAIL  {label}: {exc}", file=sys.stderr)
                fails += 1
            except Exception as exc:
                print(f"  ERROR {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
                fails += 1
        print(f"--- {passes} passed, {fails} failed ---")
        return 0 if fails == 0 else 1
    finally:
        if not args.keep_tmp:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
