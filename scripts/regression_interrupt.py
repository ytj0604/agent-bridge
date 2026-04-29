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
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import errno
import inspect
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from regression.harness import (
    DIRECT_EXECUTABLE_TARGETS,
    INSTALL_SHIM_TARGETS,
    LIBEXEC,
    ROOT,
    _delivered_request,
    _import_enqueue_module,
    _participants_state,
    _patch_enqueue_for_unit,
    _qualifying_message,
    _run_enqueue_main,
    _write_json,
    FakeCommandConn,
    assert_true,
    identity_live_record,
    isolated_identity_env,
    make_daemon,
    patched_environ,
    patched_redirect_root,
    read_events,
    read_raw_events,
    set_identity_target,
    test_message,
    verified_identity,
    write_identity_fixture,
    write_live_identity_records,
)

import bridge_daemon  # noqa: E402
import bridge_attach  # noqa: E402
import bridge_hook_logger  # noqa: E402
import bridge_identity  # noqa: E402
import bridge_instructions  # noqa: E402
import bridge_interrupt_peer  # noqa: E402
import bridge_clear_peer  # noqa: E402
import bridge_clear_guard  # noqa: E402
import bridge_clear_marker  # noqa: E402
import bridge_cancel_message  # noqa: E402
import bridge_wait_status  # noqa: E402
import bridge_aggregate_status  # noqa: E402
import bridge_join  # noqa: E402
import bridge_leave  # noqa: E402
import bridge_pane_probe  # noqa: E402
import bridge_response_guard  # noqa: E402
import bridge_codex_config  # noqa: E402
from bridge_util import MAX_INLINE_SEND_BODY_CHARS, MAX_PEER_BODY_CHARS, RESTART_PRESERVED_INFLIGHT_KEY, locked_json, read_json, read_limited_text, validate_peer_body_size, utc_now, write_json_atomic  # noqa: E402


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


def scenario_interrupt_claude_with_active_sends_esc_then_cc(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    records: list[dict] = []
    _record_interrupt_keys(d, records)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    result = d.handle_interrupt(sender="alice", target="bob")

    assert_true([record["key"] for record in records] == ["Escape", "C-c"], f"{label}: expected ESC then C-c, got {records}")
    assert_true(result.get("interrupt_ok") is True, f"{label}: interrupt should complete: {result}")
    assert_true(result.get("cc_sent") is True, f"{label}: C-c should be reported as sent: {result}")
    assert_true(result.get("interrupt_keys") == ["Escape", "C-c"], f"{label}: response should list actual keys: {result}")
    assert_true(result.get("cancelled_message_ids") == ["msg-old"], f"{label}: active message should be cancelled: {result}")
    assert_true(_queue_item(d, "msg-old") is None, f"{label}: old message should be removed")
    print(f"  PASS  {label}")


def scenario_interrupt_codex_sends_only_esc(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "codex")
    records: list[dict] = []
    _record_interrupt_keys(d, records)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    result = d.handle_interrupt(sender="alice", target="bob")

    assert_true([record["key"] for record in records] == ["Escape"], f"{label}: codex should only receive ESC, got {records}")
    assert_true(result.get("interrupt_ok") is True, f"{label}: ESC-only interrupt should complete: {result}")
    assert_true(result.get("cc_sent") is None, f"{label}: codex interrupt should not report C-c: {result}")
    print(f"  PASS  {label}")


def scenario_interrupt_unknown_type_falls_back_to_esc(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "codex")
    d.reload_participants = lambda: None  # type: ignore[method-assign]
    d.participants["bob"]["agent_type"] = "other"
    records: list[dict] = []
    _record_interrupt_keys(d, records)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    result = d.handle_interrupt(sender="alice", target="bob")

    assert_true([record["key"] for record in records] == ["Escape"], f"{label}: unknown type should fall back to ESC, got {records}")
    assert_true(result.get("interrupt_ok") is True, f"{label}: ESC fallback should complete: {result}")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "interrupt_unknown_agent_type" and e.get("target") == "bob" for e in events), f"{label}: unknown type should be logged")
    print(f"  PASS  {label}")


def scenario_interrupt_claude_idle_skips_cc(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    records: list[dict] = []
    _record_interrupt_keys(d, records)

    result = d.handle_interrupt(sender="alice", target="bob")

    assert_true([record["key"] for record in records] == ["Escape"], f"{label}: idle claude should not receive C-c, got {records}")
    assert_true(result.get("interrupt_ok") is True, f"{label}: idle ESC should complete: {result}")
    assert_true(result.get("cc_sent") is None, f"{label}: idle claude should not report C-c: {result}")
    assert_true(result.get("cancelled_message_ids") == [], f"{label}: idle interrupt should not cancel rows: {result}")
    print(f"  PASS  {label}")


def scenario_interrupt_claude_cc_failure_does_not_revert_state(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    records: list[dict] = []
    _record_interrupt_keys(d, records, fail_key="C-c")
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    result = d.handle_interrupt(sender="alice", target="bob")

    assert_true([record["key"] for record in records] == ["Escape", "C-c"], f"{label}: C-c should be attempted after ESC, got {records}")
    assert_true(result.get("esc_sent") is True, f"{label}: ESC should still be successful: {result}")
    assert_true(result.get("interrupt_ok") is False, f"{label}: partial key failure should be visible: {result}")
    assert_true(result.get("cc_sent") is False, f"{label}: failed C-c should be reported: {result}")
    assert_true("C-c failed" in str(result.get("cc_error") or ""), f"{label}: C-c error should be surfaced: {result}")
    assert_true(_queue_item(d, "msg-old") is None, f"{label}: cancelled queue row should not be restored")
    assert_true("bob" not in d.current_prompt_by_agent, f"{label}: active context should stay cleared")
    assert_true("bob" in d.interrupt_partial_failure_blocks, f"{label}: partial C-c failure should gate future delivery")

    replacement = test_message("msg-after-partial", frm="alice", to="bob", status="pending")
    d.queue.update(lambda queue: (queue.append(replacement), None)[1])
    delivered: list[dict] = []
    d.deliver_reserved = lambda message: delivered.append(dict(message))  # type: ignore[method-assign]
    d.try_deliver("bob")
    replacement_after = _queue_item(d, "msg-after-partial")
    assert_true(delivered == [], f"{label}: periodic try_deliver must not deliver through partial-failure gate: {delivered}")
    assert_true(replacement_after is not None and replacement_after.get("status") == "pending", f"{label}: replacement should stay pending behind gate: {replacement_after}")

    _record_interrupt_keys(d, records)
    recovery = d.handle_interrupt(sender="alice", target="bob")
    assert_true(recovery.get("interrupt_ok") is True and recovery.get("cc_sent") is True, f"{label}: follow-up interrupt should clear dirty-buffer gate: {recovery}")
    assert_true("bob" not in d.interrupt_partial_failure_blocks, f"{label}: successful follow-up interrupt should clear partial gate")
    assert_true(delivered and delivered[-1].get("id") == "msg-after-partial", f"{label}: pending replacement should deliver after successful recovery interrupt: {delivered}")
    print(f"  PASS  {label}")


def scenario_interrupt_holds_state_lock_through_sequence(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    records: list[dict] = []
    _record_interrupt_keys(d, records)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    d.handle_interrupt(sender="alice", target="bob")

    cc_records = [record for record in records if record.get("key") == "C-c"]
    assert_true(cc_records, f"{label}: C-c should be sent for active claude interrupt")
    assert_true(all(record.get("lock_held") is True for record in records), f"{label}: key dispatch must stay under state_lock: {records}")
    print(f"  PASS  {label}")


def scenario_interrupt_no_try_deliver_between_keys(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    calls: list[str] = []

    def recorder(_pane: str, key: str) -> tuple[bool, str]:
        calls.append(f"key:{key}")
        return True, ""

    d.send_interrupt_key = recorder  # type: ignore[method-assign]
    d.try_deliver = lambda target=None: calls.append(f"try_deliver:{target}")  # type: ignore[method-assign]
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    d.handle_interrupt(sender="alice", target="bob")

    assert_true(calls == ["key:Escape", "key:C-c", "try_deliver:bob"], f"{label}: try_deliver must not interleave between keys: {calls}")
    print(f"  PASS  {label}")


def scenario_interrupt_env_override_disables_cc(label: str, tmpdir: Path) -> None:
    with patched_environ(AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS="esc"):
        d = _make_interrupt_key_daemon(tmpdir, "claude")
    records: list[dict] = []
    _record_interrupt_keys(d, records)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    result = d.handle_interrupt(sender="alice", target="bob")

    assert_true(d.claude_interrupt_keys == ("Escape",), f"{label}: env override should configure ESC-only: {d.claude_interrupt_keys}")
    assert_true([record["key"] for record in records] == ["Escape"], f"{label}: env override should disable C-c, got {records}")
    assert_true(result.get("cc_sent") is None, f"{label}: disabled C-c should not be reported: {result}")
    print(f"  PASS  {label}")


def scenario_interrupt_key_delay_env_nonfinite_uses_default(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "claude", "pane": "%92"},
    }
    with patched_environ(AGENT_BRIDGE_INTERRUPT_KEY_DELAY_SEC="nan"):
        d = make_daemon(tmpdir, participants)

    assert_true(
        d.interrupt_key_delay_seconds == bridge_daemon.INTERRUPT_KEY_DELAY_DEFAULT_SECONDS,
        f"{label}: non-finite delay should use default, got {d.interrupt_key_delay_seconds}",
    )
    assert_true(
        any("non-finite" in warning for warning in d.interrupt_config_warnings),
        f"{label}: non-finite delay should produce warning: {d.interrupt_config_warnings}",
    )
    print(f"  PASS  {label}")


def scenario_interrupt_key_delay_env_clamps_out_of_range(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "claude", "pane": "%92"},
    }
    with patched_environ(AGENT_BRIDGE_INTERRUPT_KEY_DELAY_SEC="2.0"):
        d = make_daemon(tmpdir, participants)

    assert_true(
        d.interrupt_key_delay_seconds == bridge_daemon.INTERRUPT_KEY_DELAY_MAX_SECONDS,
        f"{label}: out-of-range delay should clamp to max, got {d.interrupt_key_delay_seconds}",
    )
    assert_true(
        any("outside" in warning and "using 1" in warning for warning in d.interrupt_config_warnings),
        f"{label}: clamped delay should produce warning: {d.interrupt_config_warnings}",
    )
    print(f"  PASS  {label}")


def scenario_claude_interrupt_keys_invalid_uses_default(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "claude", "pane": "%92"},
    }
    with patched_environ(AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS="bogus"):
        d = make_daemon(tmpdir, participants)

    assert_true(
        d.claude_interrupt_keys == bridge_daemon.CLAUDE_INTERRUPT_KEYS_DEFAULT,
        f"{label}: invalid keys should use default, got {d.claude_interrupt_keys}",
    )
    assert_true(
        any("invalid AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS" in warning for warning in d.interrupt_config_warnings),
        f"{label}: invalid keys should produce warning: {d.interrupt_config_warnings}",
    )
    print(f"  PASS  {label}")


def scenario_claude_interrupt_keys_empty_uses_default(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "claude", "pane": "%92"},
    }
    with patched_environ(AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS=", ,"):
        d = make_daemon(tmpdir, participants)

    assert_true(
        d.claude_interrupt_keys == bridge_daemon.CLAUDE_INTERRUPT_KEYS_DEFAULT,
        f"{label}: empty key list should use default, got {d.claude_interrupt_keys}",
    )
    assert_true(
        any("has no keys" in warning for warning in d.interrupt_config_warnings),
        f"{label}: empty key list should produce specific warning: {d.interrupt_config_warnings}",
    )
    print(f"  PASS  {label}")


def scenario_send_interrupt_key_timeout_returns_false(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    old_run = bridge_daemon.subprocess.run
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        raise subprocess.TimeoutExpired(argv, timeout=kwargs.get("timeout"))

    try:
        bridge_daemon.subprocess.run = fake_run  # type: ignore[assignment]
        ok, error = d.send_interrupt_key("%92", "Escape")
    finally:
        bridge_daemon.subprocess.run = old_run  # type: ignore[assignment]

    assert_true(ok is False and error == "timeout", f"{label}: timeout should return false/timeout, got {(ok, error)!r}")
    assert_true(calls and calls[0].get("timeout") == bridge_daemon.INTERRUPT_SEND_KEY_TIMEOUT_SECONDS, f"{label}: send-keys should use timeout: {calls}")
    print(f"  PASS  {label}")


def scenario_clear_hold_clears_interrupt_partial_failure_gate(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    d.interrupt_partial_failure_blocks["bob"] = {
        "since": utc_now(),
        "since_ts": time.time(),
        "by_sender": "alice",
        "prior_message_id": "msg-old",
        "cc_error": "timeout",
    }
    info = d.release_hold("bob", reason="manual_clear_by_alice", by_sender="alice")

    assert_true(info is not None and "interrupt_partial_failure" in info, f"{label}: clear_hold should return partial-failure info: {info}")
    assert_true("bob" not in d.interrupt_partial_failure_blocks, f"{label}: clear_hold should clear partial-failure gate")
    events = read_events(Path(d.state_file))
    assert_true(any(e.get("event") == "interrupt_partial_failure_block_cleared" for e in events), f"{label}: clear event should be logged")
    print(f"  PASS  {label}")


def scenario_interrupt_double_within_no_active_skips_cc(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    records: list[dict] = []
    _record_interrupt_keys(d, records)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    first = d.handle_interrupt(sender="alice", target="bob")
    second = d.handle_interrupt(sender="alice", target="bob")

    assert_true(first.get("cc_sent") is True, f"{label}: first active interrupt should send C-c: {first}")
    assert_true(second.get("cc_sent") is None, f"{label}: second idle interrupt should skip C-c: {second}")
    assert_true([record["key"] for record in records] == ["Escape", "C-c", "Escape"], f"{label}: second interrupt should be ESC-only: {records}")
    print(f"  PASS  {label}")


def scenario_interrupt_double_with_fresh_delivery_sends_cc_again(label: str, tmpdir: Path) -> None:
    d = _make_interrupt_key_daemon(tmpdir, "claude")
    records: list[dict] = []
    _record_interrupt_keys(d, records)
    _make_delivered_context(d, "msg-old", "alice", "bob", "n-old", turn_id="turn-old")

    first = d.handle_interrupt(sender="alice", target="bob")
    _make_delivered_context(d, "msg-new", "alice", "bob", "n-new", turn_id="turn-new")
    second = d.handle_interrupt(sender="alice", target="bob")

    assert_true(first.get("cc_sent") is True and second.get("cc_sent") is True, f"{label}: fresh active delivery should send C-c again: {first} / {second}")
    assert_true([record["key"] for record in records] == ["Escape", "C-c", "Escape", "C-c"], f"{label}: fresh delivery should reset natural dedup: {records}")
    assert_true(second.get("cancelled_message_ids") == ["msg-new"], f"{label}: second interrupt should cancel fresh delivery: {second}")
    print(f"  PASS  {label}")


def _run_interrupt_peer_cli(
    argv: list[str],
    response: dict,
    *,
    ok: bool = True,
    error: str = "",
    state: dict | None = None,
) -> tuple[int, str, str, list[dict]]:
    import contextlib
    import io

    if state is None:
        state = {
            "session": "test-session",
            "participants": {
                "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
                "bob": {"alias": "bob", "agent_type": "claude", "pane": "%92"},
            },
        }
    calls: list[dict] = []
    old_argv = sys.argv[:]
    old_resolve = bridge_interrupt_peer.resolve_caller_from_pane
    old_ensure = bridge_interrupt_peer.ensure_daemon_running
    old_room_status = bridge_interrupt_peer.room_status
    old_load_session = bridge_interrupt_peer.load_session
    old_send_command = bridge_interrupt_peer.send_command
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_interrupt_peer", *argv]
        bridge_interrupt_peer.resolve_caller_from_pane = lambda **_kwargs: argparse.Namespace(ok=True, session="test-session", alias="alice", error="")  # type: ignore[assignment]
        bridge_interrupt_peer.ensure_daemon_running = lambda _session: ""  # type: ignore[assignment]
        bridge_interrupt_peer.room_status = lambda _session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")  # type: ignore[assignment]
        bridge_interrupt_peer.load_session = lambda _session: state  # type: ignore[assignment]

        def fake_send_command(_session: str, payload: dict):
            calls.append(dict(payload))
            return ok, dict(response), error

        bridge_interrupt_peer.send_command = fake_send_command  # type: ignore[assignment]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bridge_interrupt_peer.main()
    finally:
        sys.argv = old_argv
        bridge_interrupt_peer.resolve_caller_from_pane = old_resolve  # type: ignore[assignment]
        bridge_interrupt_peer.ensure_daemon_running = old_ensure  # type: ignore[assignment]
        bridge_interrupt_peer.room_status = old_room_status  # type: ignore[assignment]
        bridge_interrupt_peer.load_session = old_load_session  # type: ignore[assignment]
        bridge_interrupt_peer.send_command = old_send_command  # type: ignore[assignment]
    return code, out.getvalue(), err.getvalue(), calls


def scenario_interrupt_peer_cli_exit_nonzero_on_partial_key_failure(label: str, tmpdir: Path) -> None:
    response = {
        "ok": True,
        "esc_sent": True,
        "interrupt_ok": False,
        "interrupt_keys": ["Escape", "C-c"],
        "cc_sent": False,
        "cc_error": "C-c failed",
        "held": False,
        "cancelled_message_ids": ["msg-old"],
    }
    code, out, err, calls = _run_interrupt_peer_cli(
        ["bob", "--session", "test-session", "--from", "alice", "--allow-spoof", "--json"],
        response,
    )
    summary = json.loads(out)
    expected = {
        "target": "bob",
        "esc_sent": True,
        "interrupt_ok": False,
        "interrupt_keys": ["Escape", "C-c"],
        "cc_sent": False,
        "held": False,
        "cancelled_message_ids": ["msg-old"],
        "prior_active_message_id": None,
        "cc_error": "C-c failed",
    }
    assert_true(code == 1, f"{label}: --json CLI should exit non-zero on partial interrupt failure, got {code}, stderr={err!r}")
    assert_true(summary == expected, f"{label}: --json summary should preserve compatibility shape: {summary}")
    assert_true(calls == [{"op": "interrupt", "from": "alice", "target": "bob"}], f"{label}: interrupt payload mismatch: {calls}")

    code, out, err, calls = _run_interrupt_peer_cli(
        ["bob", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        response,
    )
    assert_true(code == 1, f"{label}: default text CLI should exit non-zero on partial interrupt failure, got {code}")
    assert_true(out == "", f"{label}: default failure must not leak JSON/stdout: {out!r}")
    for token in ("agent_interrupt_peer:", "(interrupt_partial_failure)", "cancelled=1", "C-c failed", "Hint:", "--status", "--clear-hold"):
        assert_true(token in err, f"{label}: default failure stderr missing {token!r}: {err!r}")
    assert_true("{" not in out and "{" not in err, f"{label}: default failure should not print JSON: out={out!r} err={err!r}")
    print(f"  PASS  {label}")


def scenario_interrupt_peer_action_text_and_json_modes(label: str, tmpdir: Path) -> None:
    success_response = {
        "ok": True,
        "esc_sent": True,
        "interrupt_ok": True,
        "interrupt_keys": ["Escape"],
        "cc_sent": None,
        "held": False,
        "cancelled_message_ids": ["msg-active"],
        "prior_active_message_id": "msg-active",
    }
    code, out, err, calls = _run_interrupt_peer_cli(
        ["bob", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        success_response,
    )
    assert_true(code == 0 and calls, f"{label}: default interrupt should succeed: code={code} err={err!r}")
    for token in ("agent_interrupt_peer:", "(interrupted)", "bob", "cancelled=1", "prior_active_message_id=msg-active"):
        assert_true(token in out, f"{label}: default success stdout missing {token!r}: {out!r}")
    assert_true("Hint:" in err and "agent_cancel_message <msg_id>" in err, f"{label}: default success hint missing: {err!r}")
    assert_true("{" not in out, f"{label}: default success stdout must be text, not JSON: {out!r}")

    code, out, err, _calls = _run_interrupt_peer_cli(
        ["bob", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        {**success_response, "cancelled_message_ids": [], "prior_active_message_id": None},
    )
    assert_true(code == 0 and "cancelled=0" in out and "no-op" in err, f"{label}: no-op text should be clear: out={out!r} err={err!r}")

    code, out, err, _calls = _run_interrupt_peer_cli(
        ["bob", "--session", "test-session", "--from", "alice", "--allow-spoof", "--json"],
        success_response,
    )
    expected = {
        "target": "bob",
        "esc_sent": True,
        "interrupt_ok": True,
        "interrupt_keys": ["Escape"],
        "cc_sent": None,
        "held": False,
        "cancelled_message_ids": ["msg-active"],
        "prior_active_message_id": "msg-active",
    }
    assert_true(code == 0 and err == "" and json.loads(out) == expected, f"{label}: --json interrupt should preserve summary: code={code} out={out!r} err={err!r}")
    print(f"  PASS  {label}")


def scenario_interrupt_peer_clear_hold_text_and_json_modes(label: str, tmpdir: Path) -> None:
    warning = "clear carefully"
    response = {"ok": True, "had_hold": True, "info": {"reason": "held_interrupt"}, "warning": warning}
    code, out, err, calls = _run_interrupt_peer_cli(
        ["bob", "--clear-hold", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        response,
    )
    assert_true(code == 0 and calls == [{"op": "clear_hold", "from": "alice", "target": "bob"}], f"{label}: clear-hold should call daemon: {calls}")
    assert_true("(hold_cleared)" in out and "bob" in out and "{" not in out, f"{label}: clear-hold stdout should be text: {out!r}")
    assert_true("Hint:" in err and warning in err, f"{label}: clear-hold warning should be visible on stderr: {err!r}")

    code, out, err, _calls = _run_interrupt_peer_cli(
        ["bob", "--clear-hold", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        {**response, "had_hold": False, "info": {}},
    )
    assert_true(code == 0 and "(no_hold)" in out and "Hint:" in err, f"{label}: no-hold text should be explicit: out={out!r} err={err!r}")

    code, out, err, _calls = _run_interrupt_peer_cli(
        ["bob", "--clear-hold", "--session", "test-session", "--from", "alice", "--allow-spoof", "--json"],
        response,
    )
    expected = {"target": "bob", "had_hold": True, "info": {"reason": "held_interrupt"}, "warning": warning}
    assert_true(code == 0 and err == "" and json.loads(out) == expected, f"{label}: --json clear-hold should preserve summary: code={code} out={out!r} err={err!r}")
    print(f"  PASS  {label}")


def scenario_interrupt_peer_status_json_unchanged_and_json_rejected(label: str, tmpdir: Path) -> None:
    peers = [{"target": "bob", "busy": False, "held": False}]
    code, out, err, calls = _run_interrupt_peer_cli(
        ["--status", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        {"ok": True, "peers": peers},
    )
    assert_true(code == 0 and err == "" and json.loads(out) == {"peers": peers}, f"{label}: --status JSON should stay unchanged: code={code} out={out!r} err={err!r}")
    assert_true(calls == [{"op": "status", "from": "alice", "target": ""}], f"{label}: status payload mismatch: {calls}")

    code, out, err, calls = _run_interrupt_peer_cli(
        ["--status", "--json", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        {"ok": True, "peers": peers},
    )
    assert_true(code == 2 and out == "" and not calls, f"{label}: --status --json should reject before daemon call: code={code} out={out!r} calls={calls}")
    assert_true("--json is redundant with --status" in err, f"{label}: --status --json diagnostic should be clear: {err!r}")
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
    # Watchdog still does not arm at enqueue. Once a request is active, even
    # an empty/no-text response must cancel the response watchdog and
    # auto-route an explicit sentinel result.
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
    assert_true(not any(wd.get("ref_message_id") == "msg-wd-1" for wd in d.watchdogs.values()), f"{label}: watchdog must NOT be registered at enqueue")
    def assign(queue):
        for it in queue:
            if it.get("id") == "msg-wd-1":
                it["nonce"] = "wd-nonce"
                it["status"] = "inflight"
        return None
    d.queue.update(assign)
    d.handle_prompt_submitted({"agent": "codex", "bridge_agent": "codex", "nonce": "wd-nonce", "turn_id": "t-wd", "prompt": ""})
    assert_true(any(wd.get("ref_message_id") == "msg-wd-1" and wd.get("watchdog_phase") == "response" for wd in d.watchdogs.values()), f"{label}: response watchdog must be registered after prompt_submitted")
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
    error_text = str(result.get("error") or "")
    for token in ("current_prompt.from=alice", "separate agent_send_peer", "blocked/rejected", "requester", "third-party", "review/collaboration", "other validations still apply"):
        assert_true(token in error_text, f"{label}: error missing token {token!r}: {error_text!r}")
    assert_true("do not call agent_send_peer" not in error_text, f"{label}: error must not imply all send_peer is forbidden: {error_text!r}")

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
        "carol": {"alias": "carol", "agent_type": "codex", "pane": "%93", "status": "active"},
    }
    d = make_daemon(tmpdir, participants)
    _set_response_context(d, "bob", "alice", aggregate_id="agg-active")
    aggregate_result = d.handle_enqueue_command([_qualifying_message("bob", "alice", kind="request", body="agg follow-up")])
    assert_true(not aggregate_result.get("ok"), f"{label}: aggregate response context must block send to requester")
    third_party_result = d.handle_enqueue_command([_qualifying_message("bob", "carol", kind="request", body="agg review")])
    assert_true(third_party_result.get("ok"), f"{label}: aggregate context must not block third-party peer send: {third_party_result}")

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
    assert_true(not ok and err == "message_unknown", f"{label}: unknown message id should error message_unknown, got ok={ok}, err={err!r}")
    result = _daemon_command_result(d, {"op": "extend_watchdog", "from": "claude", "message_id": "msg-does-not-exist", "seconds": 60.0})
    assert_true(result.get("error") == "message_unknown" and "old or restart-lost ids cannot be extended" in str(result.get("hint") or ""), f"{label}: daemon should return stable unknown hint: {result}")
    print(f"  PASS  {label}")


def scenario_extend_wait_terminal_tombstone_classification(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d._record_message_tombstone(
        "codex",
        {"id": "msg-recent-response", "from": "claude"},
        by_sender="bridge",
        reason="terminal_response",
        suppress_late_hooks=False,
        prompt_submitted_seen=True,
    )
    d._record_message_tombstone(
        "codex",
        {"id": "msg-already-cancelled", "from": "claude"},
        by_sender="claude",
        reason="cancelled_by_sender",
        suppress_late_hooks=False,
        prompt_submitted_seen=False,
    )
    d._record_message_tombstone(
        "codex",
        {"id": "msg-unknown-terminal-reason", "from": "claude"},
        by_sender="bridge",
        reason="new_future_terminal_reason",
        suppress_late_hooks=False,
        prompt_submitted_seen=True,
    )

    ok_recent, err_recent, _ = d.upsert_message_watchdog("claude", "msg-recent-response", 60.0)
    ok_done, err_done, _ = d.upsert_message_watchdog("claude", "msg-already-cancelled", 60.0)
    ok_unknown_reason, err_unknown_reason, _ = d.upsert_message_watchdog("claude", "msg-unknown-terminal-reason", 60.0)
    ok_foreign, err_foreign, _ = d.upsert_message_watchdog("codex", "msg-recent-response", 60.0)
    assert_true(not ok_recent and err_recent == "message_recently_responded", f"{label}: response tombstone classification wrong: {ok_recent}, {err_recent!r}")
    assert_true(not ok_done and err_done == "message_already_terminal", f"{label}: cancelled tombstone classification wrong: {ok_done}, {err_done!r}")
    assert_true(not ok_unknown_reason and err_unknown_reason == "message_already_terminal", f"{label}: unknown tombstone reason must default terminal: {ok_unknown_reason}, {err_unknown_reason!r}")
    assert_true(not ok_foreign and err_foreign == "not_owner", f"{label}: foreign tombstone owner should be rejected: {ok_foreign}, {err_foreign!r}")

    result = _daemon_command_result(d, {"op": "extend_watchdog", "from": "claude", "message_id": "msg-recent-response", "seconds": 60.0})
    assert_true(result.get("error") == "message_recently_responded" and "[bridge:result]" in str(result.get("hint") or ""), f"{label}: daemon should return recently-responded hint: {result}")
    print(f"  PASS  {label}")


def scenario_extend_wait_pending_rejected(label: str, tmpdir: Path) -> None:
    # Pending rows have no active delivery attempt yet, so extend_wait still
    # rejects them. Once a row is inflight/submitted, the delivery watchdog is
    # active and may be extended.
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
    assert_true(not ok and err == "message_not_extendable_state", f"{label}: pending message must be rejected, got ok={ok}, err={err!r}")
    # Inflight delivery wait is extendable.
    def to_inflight(queue):
        for it in queue:
            if it.get("id") == "msg-pending-1":
                it["status"] = "inflight"
                it["inflight_ts"] = utc_now()
        return None
    d.queue.update(to_inflight)
    ok2, err2, _ = d.upsert_message_watchdog("claude", "msg-pending-1", 60.0)
    assert_true(ok2, f"{label}: inflight delivery wait should extend, got ok={ok2}, err={err2!r}")
    # Submitted is the same delivery phase: prompt submission is still not
    # fully delivered until the bridge nonce is matched.
    def to_submitted(queue):
        for it in queue:
            if it.get("id") == "msg-pending-1":
                it["status"] = "submitted"
        return None
    d.queue.update(to_submitted)
    ok3, err3, _ = d.upsert_message_watchdog("claude", "msg-pending-1", 60.0)
    assert_true(ok3, f"{label}: submitted delivery wait should extend, got ok={ok3}, err={err3!r}")
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


def _watchdogs_for_message(d, message_id: str, phase: str | None = None) -> list[tuple[str, dict]]:
    return [
        (wake_id, wd)
        for wake_id, wd in d.watchdogs.items()
        if wd.get("ref_message_id") == message_id
        and not wd.get("is_alarm")
        and (phase is None or wd.get("watchdog_phase") == phase)
    ]


def _add_watchdog_request(d, message_id: str, *, frm: str = "claude", to: str = "codex", aggregate_id: str = "") -> None:
    msg = test_message(message_id, frm=frm, to=to, status="pending")
    msg["watchdog_delay_sec"] = 60.0
    if aggregate_id:
        msg["aggregate_id"] = aggregate_id
        msg["aggregate_expected"] = ["codex", "bob"]
        msg["aggregate_message_ids"] = {"codex": message_id, "bob": "msg-agg-bob"}
    d.enqueue_ipc_message(msg)


def _run_cancel_message_cli(
    argv: list[str],
    response: dict,
    *,
    ok: bool = True,
    error: str = "",
    state: dict | None = None,
) -> tuple[int, str, str, list[dict]]:
    import contextlib
    import io

    if state is None:
        state = {
            "session": "test-session",
            "participants": {
                "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
                "bob": {"alias": "bob", "agent_type": "claude", "pane": "%92"},
            },
        }
    calls: list[dict] = []
    old_argv = sys.argv[:]
    old_resolve = bridge_cancel_message.resolve_caller_from_pane
    old_ensure = bridge_cancel_message.ensure_daemon_running
    old_room_status = bridge_cancel_message.room_status
    old_load_session = bridge_cancel_message.load_session
    old_send_command = bridge_cancel_message.send_command
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_cancel_message", *argv]
        bridge_cancel_message.resolve_caller_from_pane = lambda **_kwargs: argparse.Namespace(ok=True, session="test-session", alias="alice", error="")  # type: ignore[assignment]
        bridge_cancel_message.ensure_daemon_running = lambda _session: ""  # type: ignore[assignment]
        bridge_cancel_message.room_status = lambda _session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")  # type: ignore[assignment]
        bridge_cancel_message.load_session = lambda _session: state  # type: ignore[assignment]

        def fake_send_command(_session: str, payload: dict):
            calls.append(dict(payload))
            return ok, dict(response), error

        bridge_cancel_message.send_command = fake_send_command  # type: ignore[assignment]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bridge_cancel_message.main()
    finally:
        sys.argv = old_argv
        bridge_cancel_message.resolve_caller_from_pane = old_resolve  # type: ignore[assignment]
        bridge_cancel_message.ensure_daemon_running = old_ensure  # type: ignore[assignment]
        bridge_cancel_message.room_status = old_room_status  # type: ignore[assignment]
        bridge_cancel_message.load_session = old_load_session  # type: ignore[assignment]
        bridge_cancel_message.send_command = old_send_command  # type: ignore[assignment]
    return code, out.getvalue(), err.getvalue(), calls


def scenario_cancel_message_pending_removes_row(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-pending", frm="claude", to="codex", status="pending")
    d.queue.update(lambda queue: queue.append(msg))
    wake_id = _plant_watchdog(d, "wake-cancel-pending", sender="claude", message_id="msg-cancel-pending", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "delivery"

    result = d.cancel_message("claude", "msg-cancel-pending")

    assert_true(result.get("ok") and result.get("cancelled") and result.get("status_before") == "pending", f"{label}: cancel should ack pending row: {result}")
    assert_true(_queue_item(d, "msg-cancel-pending") is None, f"{label}: pending row should be removed")
    assert_true(_watchdogs_for_message(d, "msg-cancel-pending") == [], f"{label}: watchdogs should be cancelled: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_cancel_message_inflight_pre_paste(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-inflight", frm="claude", to="codex", status="inflight")
    msg["nonce"] = "nonce-cancel-inflight"
    d.queue.update(lambda queue: queue.append(msg))
    d.reserved["codex"] = "msg-cancel-inflight"
    d.remember_nonce("nonce-cancel-inflight", msg)
    wake_id = _plant_watchdog(d, "wake-cancel-inflight", sender="claude", message_id="msg-cancel-inflight", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "delivery"

    result = d.cancel_message("claude", "msg-cancel-inflight")

    assert_true(result.get("ok") and result.get("cancelled") and result.get("status_before") == "inflight", f"{label}: cancel should ack inflight row: {result}")
    assert_true(result.get("input_clear_attempted") is False, f"{label}: pre-paste cancel should not press Escape: {result}")
    assert_true(_queue_item(d, "msg-cancel-inflight") is None, f"{label}: inflight row should be removed")
    assert_true(d.reserved.get("codex") is None, f"{label}: reserved pointer should be cleared: {d.reserved}")
    assert_true(d.cached_nonce("nonce-cancel-inflight") is None, f"{label}: nonce cache should be cleared")
    assert_true("msg-cancel-inflight" not in d.last_enter_ts, f"{label}: last_enter_ts should be clear: {d.last_enter_ts}")
    assert_true(_watchdogs_for_message(d, "msg-cancel-inflight") == [], f"{label}: watchdogs should be cancelled: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_cancel_message_pane_mode_deferred_inflight_cancellable(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-pane-mode", frm="claude", to="codex", status="inflight")
    msg["nonce"] = "nonce-cancel-pane-mode"
    d.queue.update(lambda queue: queue.append(msg))
    d.reserved["codex"] = "msg-cancel-pane-mode"
    d.remember_nonce("nonce-cancel-pane-mode", msg)
    d.last_enter_ts["msg-cancel-pane-mode"] = time.time()
    d.mark_enter_deferred_for_pane_mode("msg-cancel-pane-mode", "codex", "copy-mode")
    wake_id = _plant_watchdog(d, "wake-cancel-pane-mode", sender="claude", message_id="msg-cancel-pane-mode", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "delivery"

    result = d.cancel_message("claude", "msg-cancel-pane-mode")

    assert_true(result.get("ok") and result.get("cancelled"), f"{label}: pane-mode deferred inflight should cancel: {result}")
    assert_true(result.get("input_clear_attempted") is False, f"{label}: enter-deferred pane-mode cancel should not press Escape: {result}")
    assert_true(_queue_item(d, "msg-cancel-pane-mode") is None, f"{label}: cancelled row and pane-mode metadata should be removed")
    assert_true(d.reserved.get("codex") is None, f"{label}: reserved pointer should be cleared: {d.reserved}")
    assert_true(d.cached_nonce("nonce-cancel-pane-mode") is None, f"{label}: nonce cache should be cleared")
    assert_true("msg-cancel-pane-mode" not in d.last_enter_ts, f"{label}: enter retry state should be cleared: {d.last_enter_ts}")
    assert_true(_watchdogs_for_message(d, "msg-cancel-pane-mode") == [], f"{label}: delivery watchdog should be cancelled: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_cancel_message_inflight_after_enter_rejects_with_interrupt_guidance(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-active-inflight", frm="claude", to="codex", status="inflight")
    msg["nonce"] = "nonce-cancel-active-inflight"
    d.queue.update(lambda queue: queue.append(msg))
    d.last_enter_ts["msg-cancel-active-inflight"] = time.time()
    wake_id = _plant_watchdog(d, "wake-cancel-active-inflight", sender="claude", message_id="msg-cancel-active-inflight", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "delivery"

    result = d.cancel_message("claude", "msg-cancel-active-inflight")
    text = bridge_cancel_message.cancel_error_message("msg-cancel-active-inflight", str(result.get("error") or ""), result)

    assert_true(not result.get("ok") and result.get("error") == "message_active_use_interrupt", f"{label}: post-enter inflight cancel should be rejected: {result}")
    assert_true("Use agent_interrupt_peer codex" in text, f"{label}: CLI guidance should point to interrupt_peer: {text!r}")
    assert_true((_queue_item(d, "msg-cancel-active-inflight") or {}).get("status") == "inflight", f"{label}: inflight row should remain")
    assert_true(wake_id in d.watchdogs and _watchdogs_for_message(d, "msg-cancel-active-inflight", "delivery"), f"{label}: delivery watchdog should remain: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_cancel_message_submitted_rejects_with_interrupt_guidance(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-submitted", frm="claude", to="codex", status="submitted")
    d.queue.update(lambda queue: queue.append(msg))
    wake_id = _plant_watchdog(d, "wake-cancel-submitted", sender="claude", message_id="msg-cancel-submitted", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "delivery"

    result = d.cancel_message("claude", "msg-cancel-submitted")
    text = bridge_cancel_message.cancel_error_message("msg-cancel-submitted", str(result.get("error") or ""), result)

    assert_true(not result.get("ok") and result.get("error") == "message_active_use_interrupt", f"{label}: submitted cancel should be rejected: {result}")
    assert_true("Use agent_interrupt_peer codex" in text, f"{label}: CLI guidance should point to interrupt_peer: {text!r}")
    assert_true((_queue_item(d, "msg-cancel-submitted") or {}).get("status") == "submitted", f"{label}: submitted row should remain")
    assert_true(wake_id in d.watchdogs and _watchdogs_for_message(d, "msg-cancel-submitted", "delivery"), f"{label}: watchdog should remain: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_cancel_message_delivered_rejects_with_interrupt_guidance(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-delivered", frm="claude", to="codex", status="delivered")
    d.queue.update(lambda queue: queue.append(msg))
    wake_id = _plant_watchdog(d, "wake-cancel-delivered", sender="claude", message_id="msg-cancel-delivered", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "response"

    result = d.cancel_message("claude", "msg-cancel-delivered")
    text = bridge_cancel_message.cancel_error_message("msg-cancel-delivered", str(result.get("error") or ""), result)

    assert_true(not result.get("ok") and result.get("error") == "message_active_use_interrupt", f"{label}: delivered cancel should be rejected: {result}")
    assert_true("Use agent_interrupt_peer codex" in text, f"{label}: CLI guidance should point to interrupt_peer: {text!r}")
    assert_true((_queue_item(d, "msg-cancel-delivered") or {}).get("status") == "delivered", f"{label}: delivered row should remain")
    assert_true(wake_id in d.watchdogs and _watchdogs_for_message(d, "msg-cancel-delivered", "response"), f"{label}: response watchdog should remain: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_cancel_message_ownership_violation(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-owner", frm="claude", to="codex", status="pending")
    d.queue.update(lambda queue: queue.append(msg))
    wake_id = _plant_watchdog(d, "wake-cancel-owner", sender="claude", message_id="msg-cancel-owner", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "delivery"

    result = d.cancel_message("codex", "msg-cancel-owner")
    text = bridge_cancel_message.cancel_error_message("msg-cancel-owner", str(result.get("error") or ""), result)

    assert_true(not result.get("ok") and result.get("error") == "not_owner", f"{label}: non-owner cancel should be rejected: {result}")
    assert_true("Only the original sender can cancel" in text, f"{label}: CLI text should explain ownership: {text!r}")
    assert_true((_queue_item(d, "msg-cancel-owner") or {}).get("status") == "pending", f"{label}: pending row should remain")
    assert_true(wake_id in d.watchdogs and _watchdogs_for_message(d, "msg-cancel-owner", "delivery"), f"{label}: delivery watchdog should remain: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_cancel_message_idempotent_after_terminal(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-cancel-idem", frm="claude", to="codex", status="pending")
    d.queue.update(lambda queue: queue.append(msg))

    first = d.cancel_message("claude", "msg-cancel-idem")
    second = d.cancel_message("claude", "msg-cancel-idem")
    foreign = d.cancel_message("codex", "msg-cancel-idem")
    d._record_message_tombstone(
        "codex",
        {"id": "msg-cancel-responded", "from": "claude"},
        by_sender="bridge",
        reason="terminal_response",
        suppress_late_hooks=False,
        prompt_submitted_seen=True,
    )
    responded = d.cancel_message("claude", "msg-cancel-responded")
    responded_foreign = d.cancel_message("codex", "msg-cancel-responded")

    assert_true(first.get("ok") and first.get("cancelled"), f"{label}: first cancel should remove row: {first}")
    assert_true(second.get("ok") and second.get("already_terminal") and second.get("terminal_reason") == "cancelled_by_sender", f"{label}: repeat owner cancel should be idempotent: {second}")
    assert_true(not foreign.get("ok") and foreign.get("error") == "not_owner", f"{label}: foreign repeat cancel should preserve owner: {foreign}")
    assert_true(responded.get("ok") and responded.get("already_terminal") and responded.get("terminal_reason") == "terminal_response", f"{label}: responded row should report terminal idempotency: {responded}")
    assert_true(not responded_foreign.get("ok") and responded_foreign.get("error") == "not_owner", f"{label}: foreign terminal cancel should preserve owner: {responded_foreign}")
    assert_true(_queue_item(d, "msg-cancel-idem") is None and _queue_item(d, "msg-cancel-responded") is None, f"{label}: terminal rows should remain absent")
    print(f"  PASS  {label}")


def scenario_cancel_message_aggregate_per_leg_keeps_other_legs(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-cancel-leg"
    msg_w1 = test_message("msg-cancel-agg-w1", frm="manager", to="w1", status="pending")
    msg_w2 = test_message("msg-cancel-agg-w2", frm="manager", to="w2", status="pending")
    for msg in (msg_w1, msg_w2):
        msg["watchdog_delay_sec"] = 60.0
        msg["aggregate_id"] = agg_id
        msg["aggregate_expected"] = ["w1", "w2"]
        msg["aggregate_message_ids"] = {"w1": "msg-cancel-agg-w1", "w2": "msg-cancel-agg-w2"}
        d.queue.update(lambda queue, item=msg: queue.append(item))
    w1_wake = _plant_watchdog(d, "wake-cancel-agg-w1", sender="manager", message_id="msg-cancel-agg-w1", aggregate_id=agg_id, to="w1")
    w2_wake = _plant_watchdog(d, "wake-cancel-agg-w2", sender="manager", message_id="msg-cancel-agg-w2", aggregate_id=agg_id, to="w2")
    agg_wake = _plant_watchdog(d, "wake-cancel-agg-response", sender="manager", aggregate_id=agg_id, to="w1")
    d.watchdogs[w1_wake]["watchdog_phase"] = "delivery"
    d.watchdogs[w2_wake]["watchdog_phase"] = "delivery"
    d.watchdogs[agg_wake]["watchdog_phase"] = "response"

    cancelled = d.cancel_message("manager", "msg-cancel-agg-w1")
    d.collect_aggregate_response("w2", "real reply", {
        "aggregate_id": agg_id,
        "from": "manager",
        "aggregate_expected": ["w1", "w2"],
        "aggregate_message_ids": {"w1": "msg-cancel-agg-w1", "w2": "msg-cancel-agg-w2"},
        "causal_id": "causal-cancel-agg",
        "intent": "test",
        "hop_count": 1,
        "id": "msg-cancel-agg-w2",
    })

    assert_true(cancelled.get("ok") and cancelled.get("cancelled") and cancelled.get("aggregate_id") == agg_id, f"{label}: aggregate leg should cancel: {cancelled}")
    assert_true(_queue_item(d, "msg-cancel-agg-w1") is None, f"{label}: cancelled leg row should be removed")
    assert_true((_queue_item(d, "msg-cancel-agg-w2") or {}).get("status") == "pending", f"{label}: other leg should remain queued")
    assert_true(w1_wake not in d.watchdogs, f"{label}: cancelled leg delivery watchdog should be removed: {d.watchdogs}")
    assert_true(w2_wake in d.watchdogs, f"{label}: surviving leg delivery watchdog should remain: {d.watchdogs}")
    assert_true(agg_wake not in d.watchdogs, f"{label}: aggregate response watchdog should be cancelled only after aggregate completion: {d.watchdogs}")
    aggregate = (read_json(Path(d.aggregate_file), {"aggregates": {}}).get("aggregates") or {}).get(agg_id) or {}
    replies = aggregate.get("replies") or {}
    assert_true(aggregate.get("status") == "complete" and {"w1", "w2"}.issubset(replies), f"{label}: aggregate should complete with cancelled + real replies: {aggregate}")
    result = next((item for item in d.queue.read() if item.get("source") == "aggregate_return" and item.get("aggregate_id") == agg_id), None)
    assert_true(result is not None and "cancelled by manager" in str(result.get("body") or "") and "real reply" in str(result.get("body") or ""), f"{label}: aggregate result should include both leg outcomes: {result}")
    print(f"  PASS  {label}")


def scenario_cancel_message_action_text_and_json_modes(label: str, tmpdir: Path) -> None:
    cancelled_response = {
        "ok": True,
        "message_id": "msg-cancel-cli",
        "cancelled": True,
        "already_terminal": False,
        "target": "bob",
        "status_before": "pending",
        "aggregate_id": "agg-cancel-cli",
    }
    code, out, err, calls = _run_cancel_message_cli(
        ["msg-cancel-cli", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        cancelled_response,
    )
    assert_true(code == 0 and calls == [{"op": "cancel_message", "from": "alice", "message_id": "msg-cancel-cli"}], f"{label}: cancel should call daemon: {calls}")
    for token in ("agent_cancel_message:", "(cancelled)", "target=bob", "status_before=pending"):
        assert_true(token in out, f"{label}: cancel text stdout missing {token!r}: {out!r}")
    assert_true("Hint:" in err and "No further action" in err, f"{label}: cancel success hint missing: {err!r}")
    assert_true("{" not in out, f"{label}: default cancel stdout must be text, not JSON: {out!r}")

    terminal_response = {
        "ok": True,
        "message_id": "msg-cancel-terminal",
        "cancelled": False,
        "already_terminal": True,
        "target": "bob",
        "terminal_reason": "terminal_response",
    }
    code, out, err, _calls = _run_cancel_message_cli(
        ["msg-cancel-terminal", "--session", "test-session", "--from", "alice", "--allow-spoof"],
        terminal_response,
    )
    assert_true(code == 0 and "(terminal_response)" in out and "do not retry" in err, f"{label}: terminal idempotent text wrong: out={out!r} err={err!r}")

    code, out, err, _calls = _run_cancel_message_cli(
        ["msg-cancel-cli", "--session", "test-session", "--from", "alice", "--allow-spoof", "--json"],
        cancelled_response,
    )
    expected_cancelled = {
        "message_id": "msg-cancel-cli",
        "cancelled": True,
        "already_terminal": False,
        "target": "bob",
        "status_before": "pending",
        "aggregate_id": "agg-cancel-cli",
    }
    assert_true(code == 0 and err == "" and json.loads(out) == expected_cancelled, f"{label}: --json cancel summary mismatch: code={code} out={out!r} err={err!r}")

    code, out, err, _calls = _run_cancel_message_cli(
        ["msg-cancel-terminal", "--session", "test-session", "--from", "alice", "--allow-spoof", "--json"],
        terminal_response,
    )
    expected_terminal = {
        "message_id": "msg-cancel-terminal",
        "cancelled": False,
        "already_terminal": True,
        "target": "bob",
        "status_before": None,
        "aggregate_id": None,
        "terminal_reason": "terminal_response",
    }
    assert_true(code == 0 and err == "" and json.loads(out) == expected_terminal, f"{label}: --json terminal summary mismatch: code={code} out={out!r} err={err!r}")
    print(f"  PASS  {label}")


def scenario_cancel_message_action_error_text_modes(label: str, tmpdir: Path) -> None:
    cases = [
        (
            "message_active_use_interrupt",
            {"error": "message_active_use_interrupt", "message_id": "msg-active", "target": "bob", "status": "delivered"},
            ("(message_active_use_interrupt)", "agent_interrupt_peer bob", "Hint:"),
        ),
        (
            "not_owner",
            {"error": "not_owner", "message_id": "msg-owner", "owner": "carol"},
            ("(not_owner)", "Only the original sender", "Hint:"),
        ),
        (
            "message_not_found",
            {"error": "message_not_found", "message_id": "msg-missing"},
            ("(message_not_found)", "may already have responded", "Hint:"),
        ),
        (
            "unsupported command",
            {"error": "unsupported command"},
            ("(unsupported_command)", "Reload/restart", "Hint:"),
        ),
    ]
    for error, response, tokens in cases:
        code, out, err, _calls = _run_cancel_message_cli(
            ["msg-error", "--session", "test-session", "--from", "alice", "--allow-spoof"],
            response,
            ok=False,
            error=error,
        )
        assert_true(code == 1 and out == "", f"{label}: {error} should exit 1 with no stdout: code={code} out={out!r} err={err!r}")
        for token in tokens:
            assert_true(token in err, f"{label}: {error} stderr missing {token!r}: {err!r}")
        assert_true("{" not in err, f"{label}: {error} stderr should be text, not JSON: {err!r}")
    print(f"  PASS  {label}")


def _wait_status_participants() -> dict[str, dict]:
    return {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
        "worker": {"alias": "worker", "agent_type": "claude", "pane": "%93", "status": "active"},
    }


def _wait_status_section_items(result: dict, section: str) -> list[dict]:
    payload = result.get(section) or {}
    return list(payload.get("items") or [])


def _aggregate_status_participants() -> dict[str, dict]:
    return {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%90", "status": "active"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%91", "status": "active"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%92", "status": "active"},
        "w3": {"alias": "w3", "agent_type": "codex", "pane": "%93", "status": "active"},
        "intruder": {"alias": "intruder", "agent_type": "codex", "pane": "%94", "status": "active"},
    }


def _aggregate_status_message(
    message_id: str,
    target: str,
    *,
    aggregate_id: str,
    expected: list[str],
    message_ids: dict[str, str],
    status: str = "pending",
    mode: str = "partial",
    started_ts: str = "2026-04-27T00:00:00.000000Z",
) -> dict:
    msg = test_message(message_id, frm="manager", to=target, status=status)
    msg.update({
        "aggregate_id": aggregate_id,
        "aggregate_expected": list(expected),
        "aggregate_message_ids": dict(message_ids),
        "aggregate_mode": mode,
        "aggregate_started_ts": started_ts,
        "watchdog_delay_sec": 60.0,
    })
    return msg


def _aggregate_status_leg(result: dict, target: str) -> dict:
    legs = ((result.get("legs") or {}).get("items") or [])
    for leg in legs:
        if leg.get("target") == target:
            return leg
    raise AssertionError(f"missing aggregate leg {target!r}: {result}")


def scenario_aggregate_status_open_from_queue_and_reply(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-open"
    expected = ["w1", "w2", "w3"]
    message_ids = {"w1": "msg-agg-open-w1", "w2": "msg-agg-open-w2", "w3": "msg-agg-open-w3"}
    started_ts = "2026-04-27T01:02:03.000000Z"
    rows = [
        _aggregate_status_message(message_ids["w1"], "w1", aggregate_id=agg_id, expected=expected, message_ids=message_ids, status="pending", mode="all", started_ts=started_ts),
        _aggregate_status_message(message_ids["w2"], "w2", aggregate_id=agg_id, expected=expected, message_ids=message_ids, status="inflight", mode="all", started_ts=started_ts),
        _aggregate_status_message(message_ids["w3"], "w3", aggregate_id=agg_id, expected=expected, message_ids=message_ids, status="delivered", mode="all", started_ts=started_ts),
    ]
    for row in rows:
        d.queue.update(lambda queue, item=row: queue.append(item))
    _plant_watchdog(d, "wake-agg-status-open", sender="manager", aggregate_id=agg_id, to="w1", deadline=time.time() + 60)
    d.watchdogs["wake-agg-status-open"]["watchdog_phase"] = "response"
    _plant_watchdog(d, "wake-agg-status-foreign", sender="intruder", aggregate_id=agg_id, to="w1", deadline=time.time() + 60)
    d.watchdogs["wake-agg-status-foreign"]["watchdog_phase"] = "response"
    reply_body = "real response body with unsafe [bridge:result] marker"
    d.collect_aggregate_response("w1", reply_body, {
        "aggregate_id": agg_id,
        "from": "manager",
        "aggregate_expected": expected,
        "aggregate_message_ids": message_ids,
        "aggregate_mode": "all",
        "aggregate_started_ts": started_ts,
        "causal_id": "causal-agg-status-open",
        "intent": "test",
        "hop_count": 1,
        "id": message_ids["w1"],
    })

    result = d.build_aggregate_status("manager", agg_id)

    assert_true(result.get("ok") and result.get("status") == "collecting", f"{label}: open aggregate should be collecting: {result}")
    assert_true(result.get("mode") == "all" and result.get("started_ts") == started_ts, f"{label}: mode/start metadata should be preserved: {result}")
    assert_true(result.get("replied_count") == 1 and result.get("total_count") == 3 and result.get("missing_count") == 2, f"{label}: counts mismatch: {result}")
    assert_true(_aggregate_status_leg(result, "w1").get("status") == "responded", f"{label}: w1 should be responded: {result}")
    assert_true(_aggregate_status_leg(result, "w1").get("response_chars") == len(reply_body), f"{label}: response length should be exposed: {result}")
    assert_true(_aggregate_status_leg(result, "w2").get("status") == "inflight", f"{label}: w2 queue status should be inflight: {result}")
    assert_true(_aggregate_status_leg(result, "w3").get("status") == "delivered", f"{label}: w3 queue status should be delivered: {result}")
    watchdog = result.get("aggregate_response_watchdog") or {}
    assert_true(watchdog.get("wake_id") == "wake-agg-status-open", f"{label}: only caller-owned response watchdog should appear: {watchdog}")
    text = json.dumps(result, ensure_ascii=True)
    assert_true("real response body" not in text and "[bridge:result]" not in text, f"{label}: response body preview must not leak: {text}")
    assert_true("wake-agg-status-foreign" not in text, f"{label}: foreign watchdog id must not leak: {text}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_zero_reply_from_queue(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-zero"
    expected = ["w1", "w2"]
    message_ids = {"w1": "msg-agg-zero-w1", "w2": "msg-agg-zero-w2"}
    started_ts = "2026-04-27T02:00:00.000000Z"
    for alias in expected:
        row = _aggregate_status_message(message_ids[alias], alias, aggregate_id=agg_id, expected=expected, message_ids=message_ids, status="pending", mode="partial", started_ts=started_ts)
        d.queue.update(lambda queue, item=row: queue.append(item))

    result = d.build_aggregate_status("manager", agg_id)

    assert_true(result.get("ok") and result.get("status") == "collecting", f"{label}: 0-reply aggregate should resolve from queue rows: {result}")
    assert_true(result.get("mode") == "partial" and result.get("started_ts") == started_ts, f"{label}: queue metadata should be used: {result}")
    assert_true(result.get("replied_count") == 0 and result.get("total_count") == 2, f"{label}: 0-reply counts mismatch: {result}")
    assert_true([leg.get("status") for leg in (result.get("legs") or {}).get("items", [])] == ["pending", "pending"], f"{label}: queue legs should be pending: {result}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_cancelled_and_timeout_synthetic_reasons(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-synth"
    expected = ["w1", "w2", "w3", "intruder"]
    message_ids = {
        "w1": "msg-agg-synth-w1",
        "w2": "msg-agg-synth-w2",
        "w3": "msg-agg-synth-w3",
        "intruder": "msg-agg-synth-intruder",
    }
    for alias in expected:
        row = _aggregate_status_message(message_ids[alias], alias, aggregate_id=agg_id, expected=expected, message_ids=message_ids)
        d.queue.update(lambda queue, item=row: queue.append(item))

    cancelled = d.cancel_message("manager", message_ids["w1"])
    endpoint_lost_msg = dict(_queue_item(d, message_ids["w2"]) or {})
    d._record_aggregate_interrupted_reply(endpoint_lost_msg, by_sender="bridge", reason="endpoint_lost")
    prompt_intercepted_msg = dict(_queue_item(d, message_ids["w3"]) or {})
    d._record_aggregate_interrupted_reply(prompt_intercepted_msg, by_sender="bridge", reason="prompt_intercepted")
    d.collect_aggregate_response("intruder", "real final response", {
        "aggregate_id": agg_id,
        "from": "manager",
        "aggregate_expected": expected,
        "aggregate_message_ids": message_ids,
        "causal_id": "causal-agg-status-synth",
        "intent": "test",
        "hop_count": 1,
        "id": message_ids["intruder"],
    })

    result = d.build_aggregate_status("manager", agg_id)

    assert_true(cancelled.get("ok") and cancelled.get("cancelled"), f"{label}: fixture cancel failed: {cancelled}")
    w1 = _aggregate_status_leg(result, "w1")
    w2 = _aggregate_status_leg(result, "w2")
    w3 = _aggregate_status_leg(result, "w3")
    w4 = _aggregate_status_leg(result, "intruder")
    assert_true(w1.get("status") == "cancelled" and w1.get("synthetic") and w1.get("terminal_reason") == "cancelled_by_sender", f"{label}: cancelled synthetic leg wrong: {w1}")
    assert_true(w2.get("status") == "timeout" and w2.get("synthetic") and w2.get("terminal_reason") == "endpoint_lost", f"{label}: endpoint_lost synthetic leg wrong: {w2}")
    assert_true(w3.get("status") == "cancelled" and w3.get("synthetic") and w3.get("terminal_reason") == "prompt_intercepted", f"{label}: prompt_intercepted synthetic leg wrong: {w3}")
    assert_true(w4.get("status") == "responded" and w4.get("response_received") is True, f"{label}: real response leg wrong: {w4}")
    assert_true(result.get("status") == "complete" and result.get("replied_count") == 4, f"{label}: aggregate should complete with structured synthetic replies: {result}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_legacy_synthetic_reply_uses_tombstone(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-legacy-synth"
    with locked_json(Path(d.aggregate_file), {"version": 1, "aggregates": {}}) as data:
        data.setdefault("aggregates", {})[agg_id] = {
            "id": agg_id,
            "created_ts": "2026-04-27T04:00:00.000000Z",
            "requester": "manager",
            "expected": ["w1"],
            "message_ids": {"w1": "msg-legacy-synth-w1"},
            "replies": {
                "w1": {
                    "from": "w1",
                    "body": "[cancelled by manager: legacy synthetic text without structured fields]",
                    "reply_to": "msg-legacy-synth-w1",
                    "received_ts": "2026-04-27T04:01:00.000000Z",
                }
            },
            "status": "complete",
            "delivered": True,
        }
    d._record_message_tombstone(
        "w1",
        {"id": "msg-legacy-synth-w1", "from": "manager"},
        by_sender="manager",
        reason="cancelled_by_sender",
        suppress_late_hooks=True,
        prompt_submitted_seen=False,
    )

    result = d.build_aggregate_status("manager", agg_id)
    leg = _aggregate_status_leg(result, "w1")

    assert_true(leg.get("status") == "cancelled", f"{label}: legacy synthetic reply should classify from tombstone: {leg}")
    assert_true(leg.get("synthetic") is True and leg.get("terminal_reason") == "cancelled_by_sender", f"{label}: synthetic metadata should be synthesized from tombstone: {leg}")
    assert_true(leg.get("response_received") is False, f"{label}: legacy synthetic should not look like real response: {leg}")
    assert_true(leg.get("response_chars") > 0, f"{label}: body length can still be exposed without preview: {leg}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_foreign_and_unknown_indistinguishable(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-private"
    d.collect_aggregate_response("w1", "private reply", {
        "aggregate_id": agg_id,
        "from": "manager",
        "aggregate_expected": ["w1", "w2"],
        "aggregate_message_ids": {"w1": "msg-private-w1", "w2": "msg-private-w2"},
        "causal_id": "causal-private",
        "intent": "test",
        "hop_count": 1,
        "id": "msg-private-w1",
    })

    foreign = _daemon_command_result(d, {"op": "aggregate_status", "from": "intruder", "aggregate_id": agg_id})
    unknown = _daemon_command_result(d, {"op": "aggregate_status", "from": "intruder", "aggregate_id": "agg-status-missing"})

    assert_true(foreign.get("ok") is False and foreign.get("error") == "aggregate_not_found", f"{label}: foreign probe should look not found: {foreign}")
    assert_true(unknown.get("ok") is False and unknown.get("error") == "aggregate_not_found", f"{label}: unknown probe should be same error class: {unknown}")
    assert_true(set(foreign.keys()) == set(unknown.keys()), f"{label}: foreign and unknown daemon shapes should match: foreign={foreign}, unknown={unknown}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_source_conflict_fails_closed(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-conflict"
    with locked_json(Path(d.aggregate_file), {"version": 1, "aggregates": {}}) as data:
        data.setdefault("aggregates", {})[agg_id] = {
            "id": agg_id,
            "requester": "manager",
            "expected": ["w1"],
            "message_ids": {"w1": "msg-conflict-w1"},
            "replies": {},
            "status": "collecting",
            "delivered": False,
        }
    foreign_row = test_message("msg-conflict-foreign", frm="intruder", to="w2", status="pending")
    foreign_row.update({"aggregate_id": agg_id, "aggregate_expected": ["w2"], "aggregate_message_ids": {"w2": "msg-conflict-foreign"}})
    d.queue.update(lambda queue: queue.append(foreign_row))

    result = d.build_aggregate_status("manager", agg_id)

    assert_true(result.get("ok") is False and result.get("error") == "aggregate_not_found", f"{label}: conflicting owner sources must fail closed: {result}")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "aggregate_status_not_found" and e.get("reason") == "source_owner_conflict" for e in events), f"{label}: conflict should be logged for operator debugging: {events[-5:]}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_legacy_fallback_and_cap(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-legacy"
    expected = [f"w{i}" for i in range(101)]
    message_ids = {alias: f"msg-{alias}" for alias in expected}
    created_ts = "2026-04-27T03:00:00.000000Z"
    with locked_json(Path(d.aggregate_file), {"version": 1, "aggregates": {}}) as data:
        data.setdefault("aggregates", {})[agg_id] = {
            "id": agg_id,
            "created_ts": created_ts,
            "requester": "manager",
            "causal_id": "causal-legacy",
            "intent": "test",
            "expected": expected,
            "message_ids": message_ids,
            "replies": {},
            "status": "collecting",
            "delivered": False,
        }

    result = d.build_aggregate_status("manager", agg_id)
    legs = result.get("legs") or {}

    assert_true(result.get("mode") == "unknown" and result.get("started_ts") == created_ts, f"{label}: legacy mode/start fallback wrong: {result}")
    assert_true(legs.get("total_count") == 101 and legs.get("returned_count") == bridge_daemon.AGGREGATE_STATUS_LEG_LIMIT and legs.get("truncated") is True, f"{label}: leg cap mismatch: {legs}")
    assert_true("expected" not in result and "message_ids" not in result and "missing_peers" not in result, f"{label}: top-level uncapped lists/maps must not be returned: {result.keys()}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_watchdog_only_anchor(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _aggregate_status_participants())
    agg_id = "agg-status-watchdog-only"
    wake_id = _plant_watchdog(d, "wake-agg-only", sender="manager", aggregate_id=agg_id, to="w1", deadline=time.time() + 60)
    d.watchdogs[wake_id]["watchdog_phase"] = "response"
    d.watchdogs[wake_id]["ref_aggregate_expected"] = ["w1", "w2"]

    result = d.build_aggregate_status("manager", agg_id)

    assert_true(result.get("ok") and result.get("total_count") == 2 and result.get("status") == "collecting", f"{label}: caller-owned watchdog should anchor minimal aggregate status: {result}")
    assert_true((result.get("aggregate_response_watchdog") or {}).get("wake_id") == wake_id, f"{label}: response watchdog should be shown: {result}")
    assert_true([leg.get("status") for leg in (result.get("legs") or {}).get("items", [])] == ["pending", "pending"], f"{label}: watchdog-only legs should fallback pending: {result}")
    print(f"  PASS  {label}")


def scenario_wait_status_empty_self_view(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _wait_status_participants())
    result = _daemon_command_result(d, {"op": "wait_status", "from": "alice"})

    assert_true(result.get("ok") and result.get("caller") == "alice", f"{label}: wait_status should ack caller: {result}")
    assert_true(str(result.get("generated_ts") or "").endswith("Z"), f"{label}: generated_ts should be daemon UTC Z time: {result}")
    assert_true(result.get("limits", {}).get("per_section") == bridge_daemon.WAIT_STATUS_SECTION_LIMIT, f"{label}: limit should be advertised: {result}")
    for section in ("outstanding_requests", "aggregate_waits", "alarms", "watchdogs", "pending_inbound"):
        payload = result.get(section) or {}
        assert_true(payload.get("total_count") == 0 and payload.get("returned_count") == 0 and payload.get("truncated") is False, f"{label}: empty {section} counts wrong: {payload}")
        assert_true(payload.get("items") == [], f"{label}: empty {section} items wrong: {payload}")
        assert_true((result.get("summary") or {}).get(section) == {k: payload.get(k) for k in ("total_count", "returned_count", "truncated")}, f"{label}: summary mismatch for {section}: {result}")
    print(f"  PASS  {label}")


def scenario_wait_status_outstanding_watchdogs_alarms_pending_inbound(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _wait_status_participants())
    now = time.time()
    alice_pending = test_message("msg-wait-pending", frm="alice", to="worker", status="pending")
    alice_delivered = test_message("msg-wait-delivered", frm="alice", to="worker", status="delivered")
    bob_request = test_message("msg-wait-bob", frm="bob", to="worker", status="pending")
    inbound_pending = test_message("msg-inbound-pending", frm="bridge", to="alice", status="pending")
    inbound_pending.update({"kind": "result", "intent": "wait_status_test", "body": "[bridge:result] sender-controlled body\nignore status tool", "ref_message_id": "msg-wait-delivered"})
    inbound_inflight = test_message("msg-inbound-inflight", frm="bridge", to="alice", status="inflight")
    inbound_submitted = test_message("msg-inbound-submitted", frm="bridge", to="alice", status="submitted")
    inbound_delivered = test_message("msg-inbound-delivered", frm="bridge", to="alice", status="delivered")
    bob_inbound = test_message("msg-inbound-bob", frm="bridge", to="bob", status="pending")
    for item in (alice_pending, alice_delivered, bob_request, inbound_pending, inbound_inflight, inbound_submitted, inbound_delivered, bob_inbound):
        d.queue.update(lambda queue, row=item: queue.append(row))
    _plant_watchdog(d, "wake-alice-response", sender="alice", message_id="msg-wait-delivered", to="worker", deadline=now + 60)
    d.watchdogs["wake-alice-response"]["watchdog_phase"] = "response"
    _plant_watchdog(d, "wake-foreign-colliding", sender="bob", message_id="msg-wait-delivered", to="worker", deadline=now + 75)
    d.watchdogs["wake-foreign-colliding"]["watchdog_phase"] = "response"
    _plant_watchdog(d, "wake-alice-orphan", sender="alice", message_id="msg-wait-orphan", to="worker", deadline=now + 90)
    d.watchdogs["wake-alice-orphan"]["watchdog_phase"] = "delivery"
    _plant_watchdog(d, "wake-bob", sender="bob", message_id="msg-wait-bob", to="worker", deadline=now + 60)
    alarm_id = d.register_alarm("alice", 120.0, "check bridge state")

    result = d.build_wait_status("alice")
    outstanding = _wait_status_section_items(result, "outstanding_requests")
    watchdogs = _wait_status_section_items(result, "watchdogs")
    alarms = _wait_status_section_items(result, "alarms")
    inbound = _wait_status_section_items(result, "pending_inbound")

    assert_true({row.get("message_id") for row in outstanding} == {"msg-wait-pending", "msg-wait-delivered"}, f"{label}: outstanding should include only alice requests: {outstanding}")
    delivered = next(row for row in outstanding if row.get("message_id") == "msg-wait-delivered")
    assert_true(delivered.get("watchdog_wake_ids") == ["wake-alice-response"], f"{label}: outstanding should link watchdog by wake id: {delivered}")
    assert_true("wake-foreign-colliding" not in delivered.get("watchdog_wake_ids", []), f"{label}: foreign colliding wake id must not leak through cross-link: {delivered}")
    assert_true("deadline" not in delivered, f"{label}: deadline should be canonical in watchdog section only: {delivered}")
    assert_true({row.get("wake_id") for row in watchdogs} == {"wake-alice-response", "wake-alice-orphan"}, f"{label}: watchdogs should include alice live and orphan timers only: {watchdogs}")
    assert_true(alarm_id and [row.get("wake_id") for row in alarms] == [alarm_id], f"{label}: alarm should appear with wake id only: {alarms}")
    assert_true("scheduled_at" not in alarms[0] and alarms[0].get("note") == "check bridge state", f"{label}: alarm metadata should match stored contract: {alarms}")
    assert_true([row.get("message_id") for row in inbound] == ["msg-inbound-pending"], f"{label}: pending inbound should exclude inflight/submitted/delivered/other target: {inbound}")
    assert_true("body_preview" not in inbound[0] and inbound[0].get("body_chars") == len(inbound_pending["body"]), f"{label}: inbound must expose body length, not sender-controlled preview: {inbound}")
    assert_true("[bridge:result]" not in json.dumps(result, ensure_ascii=True), f"{label}: sender-controlled body must not leak into output: {result}")
    print(f"  PASS  {label}")


def scenario_wait_status_aggregate_waits_privacy_and_completed_result(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _wait_status_participants())
    with locked_json(Path(d.aggregate_file), {"version": 1, "aggregates": {}}) as data:
        data.setdefault("aggregates", {})["agg-alice-open"] = {
            "id": "agg-alice-open",
            "requester": "alice",
            "expected": ["worker", "bob"],
            "replies": {"worker": {"body": "done"}},
            "message_ids": {"worker": "msg-agg-worker", "bob": "msg-agg-bob"},
            "intent": "test",
            "causal_id": "causal-agg-alice-open",
            "status": "collecting",
            "delivered": False,
            "updated_ts": utc_now(),
        }
        data["aggregates"]["agg-bob-open"] = {
            "id": "agg-bob-open",
            "requester": "bob",
            "expected": ["alice", "worker"],
            "replies": {},
            "message_ids": {"alice": "msg-bob-alice", "worker": "msg-bob-worker"},
            "status": "collecting",
            "delivered": False,
        }
        data["aggregates"]["agg-alice-complete"] = {
            "id": "agg-alice-complete",
            "requester": "alice",
            "expected": ["worker"],
            "replies": {"worker": {"body": "done"}},
            "message_ids": {"worker": "msg-agg-complete-worker"},
            "status": "complete",
            "delivered": True,
        }
    pending_result = test_message("msg-agg-result-pending", frm="bridge", to="alice", status="pending")
    pending_result.update({"kind": "result", "source": "aggregate_return", "ref_aggregate_id": "agg-alice-complete", "aggregate_id": "agg-alice-complete", "body": "aggregate complete"})
    d.queue.update(lambda queue: queue.append(pending_result))

    result = d.build_wait_status("alice")
    aggregates = _wait_status_section_items(result, "aggregate_waits")
    inbound = _wait_status_section_items(result, "pending_inbound")

    assert_true([row.get("aggregate_id") for row in aggregates] == ["agg-alice-open"], f"{label}: only caller-owned incomplete aggregate should appear: {aggregates}")
    assert_true(aggregates[0].get("replied_count") == 1 and aggregates[0].get("expected_count") == 2, f"{label}: aggregate progress mismatch: {aggregates}")
    assert_true(aggregates[0].get("missing_peers") == ["bob"], f"{label}: missing peers should be computed: {aggregates}")
    assert_true(not any(row.get("aggregate_id") == "agg-bob-open" for row in aggregates), f"{label}: being expected must not expose another requester's aggregate: {aggregates}")
    assert_true(not any(row.get("aggregate_id") == "agg-alice-complete" for row in aggregates), f"{label}: completed aggregate should not remain in waits: {aggregates}")
    assert_true(any(row.get("ref_aggregate_id") == "agg-alice-complete" and row.get("source") == "aggregate_return" for row in inbound), f"{label}: completed aggregate result should appear as pending inbound metadata: {inbound}")
    print(f"  PASS  {label}")


def scenario_wait_status_caps_and_summary_counts(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _wait_status_participants())
    limit = bridge_daemon.WAIT_STATUS_SECTION_LIMIT
    for idx in range(limit + 1):
        item = test_message(f"msg-wait-inbound-{idx}", frm="bridge", to="alice", status="pending")
        item.update({"kind": "notice", "body": f"notice {idx}"})
        d.queue.update(lambda queue, row=item: queue.append(row))

    result = d.build_wait_status("alice")
    section = result.get("pending_inbound") or {}
    summary = (result.get("summary") or {}).get("pending_inbound") or {}
    expected = {"total_count": limit + 1, "returned_count": limit, "truncated": True}

    assert_true({k: section.get(k) for k in expected} == expected, f"{label}: capped section counts wrong: {section}")
    assert_true({k: summary.get(k) for k in expected} == expected, f"{label}: summary should use total counts and truncation: {summary}")
    assert_true(len(section.get("items") or []) == limit, f"{label}: returned items should be capped at {limit}: {section}")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_arms_on_reserve(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _add_watchdog_request(d, "msg-delivery-arm")

    d.try_deliver("codex")

    item = _queue_item(d, "msg-delivery-arm")
    assert_true(item is not None and item.get("status") == "inflight", f"{label}: delivery should reserve message: {item}")
    matches = _watchdogs_for_message(d, "msg-delivery-arm", "delivery")
    assert_true(len(matches) == 1, f"{label}: exactly one delivery watchdog expected, got {d.watchdogs}")
    wd = matches[0][1]
    assert_true(wd.get("ref_to") == "codex" and wd.get("sender") == "claude", f"{label}: watchdog should point to sender/target: {wd}")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_fire_inflight_notice(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _add_watchdog_request(d, "msg-delivery-fire")
    d.try_deliver("codex")
    wake_id, wd = _watchdogs_for_message(d, "msg-delivery-fire", "delivery")[0]
    wd["deadline"] = time.time() - 1.0
    d.watchdogs[wake_id] = wd

    d.check_watchdogs()

    assert_true(wake_id not in d.watchdogs, f"{label}: delivery watchdog should fire once")
    notices = [it for it in d.queue.read() if it.get("from") == "bridge" and it.get("to") == "claude" and it.get("intent") == "watchdog_wake"]
    assert_true(len(notices) == 1, f"{label}: one watchdog notice expected, got {notices}")
    assert_true(notices[0].get("ref_message_id") == "msg-delivery-fire", f"{label}: watchdog notice should carry ref_message_id: {notices[0]}")
    assert_true(not notices[0].get("ref_aggregate_id"), f"{label}: non-aggregate watchdog notice should not carry ref_aggregate_id: {notices[0]}")
    body = notices[0].get("body") or ""
    for token in ("not completed bridge delivery", "agent_extend_wait msg-delivery-fire <sec>", "agent_interrupt_peer codex", "agent_view_peer codex"):
        assert_true(token in body, f"{label}: delivery watchdog body missing {token!r}: {body!r}")
    assert_true("held_interrupt" not in body and "--clear-hold" not in body, f"{label}: delivery watchdog body should not mention hold internals: {body!r}")
    print(f"  PASS  {label}")


def scenario_watchdog_fire_after_terminal_suppresses_notice(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    d._record_message_tombstone(
        "codex",
        {"id": "msg-terminal-watchdog", "from": "claude"},
        by_sender="bridge",
        reason="terminal_response",
        suppress_late_hooks=False,
        prompt_submitted_seen=True,
    )
    wake_id = _plant_watchdog(
        d,
        "wake-terminal-watchdog",
        sender="claude",
        message_id="msg-terminal-watchdog",
        to="codex",
        deadline=time.time() - 1.0,
    )
    d.watchdogs[wake_id]["watchdog_phase"] = "response"

    d.check_watchdogs()

    assert_true(wake_id not in d.watchdogs, f"{label}: stale terminal watchdog should be consumed")
    notices = [it for it in d.queue.read() if it.get("intent") == "watchdog_wake"]
    assert_true(not notices, f"{label}: terminal stale watchdog must not enqueue notice: {notices}")
    events = read_events(tmpdir / "events.raw.jsonl")
    skipped = [e for e in events if e.get("event") == "watchdog_skipped_stale" and e.get("wake_id") == wake_id]
    assert_true(skipped and "terminal_response" in str(skipped[-1].get("reason") or ""), f"{label}: terminal skip reason should be logged: {skipped}")
    print(f"  PASS  {label}")


def scenario_pending_watchdog_wake_removed_on_terminal(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%99"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%98"},
    }
    d = make_daemon(tmpdir, participants)
    d.try_deliver = lambda *args, **kwargs: None  # type: ignore[assignment]
    msg = _delivered_request("msg-terminal-suppress", "alice", "bob")
    msg["causal_id"] = "causal-terminal-suppress"
    msg["intent"] = "review"
    msg["hop_count"] = 2

    def add(queue):
        queue.extend([
            msg,
            {
                "id": "msg-stale-wake",
                "created_ts": utc_now(),
                "updated_ts": utc_now(),
                "from": "bridge",
                "to": "alice",
                "kind": "notice",
                "intent": "watchdog_wake",
                "body": "stale wake",
                "causal_id": "causal-terminal-suppress",
                "hop_count": 0,
                "auto_return": False,
                "reply_to": "msg-terminal-suppress",
                "ref_message_id": "msg-terminal-suppress",
                "source": "watchdog_fire",
                "bridge_session": "test-session",
                "status": "pending",
                "nonce": None,
                "delivery_attempts": 0,
            },
            {
                "id": "msg-unrelated-wake",
                "created_ts": utc_now(),
                "updated_ts": utc_now(),
                "from": "bridge",
                "to": "alice",
                "kind": "notice",
                "intent": "watchdog_wake",
                "body": "unrelated wake",
                "causal_id": "causal-other",
                "hop_count": 0,
                "auto_return": False,
                "reply_to": "msg-other",
                "ref_message_id": "msg-other",
                "source": "watchdog_fire",
                "bridge_session": "test-session",
                "status": "pending",
                "nonce": None,
                "delivery_attempts": 0,
            },
            {
                "id": "msg-delivered-wake",
                "created_ts": utc_now(),
                "updated_ts": utc_now(),
                "from": "bridge",
                "to": "alice",
                "kind": "notice",
                "intent": "watchdog_wake",
                "body": "already delivered wake",
                "causal_id": "causal-terminal-suppress",
                "hop_count": 0,
                "auto_return": False,
                "reply_to": "msg-terminal-suppress",
                "ref_message_id": "msg-terminal-suppress",
                "source": "watchdog_fire",
                "bridge_session": "test-session",
                "status": "delivered",
                "nonce": None,
                "delivery_attempts": 1,
            },
            {
                "id": "msg-inflight-wake",
                "created_ts": utc_now(),
                "updated_ts": utc_now(),
                "from": "bridge",
                "to": "alice",
                "kind": "notice",
                "intent": "watchdog_wake",
                "body": "inflight wake",
                "causal_id": "causal-terminal-suppress",
                "hop_count": 0,
                "auto_return": False,
                "reply_to": "msg-terminal-suppress",
                "ref_message_id": "msg-terminal-suppress",
                "source": "watchdog_fire",
                "bridge_session": "test-session",
                "status": "inflight",
                "nonce": "wake-nonce",
                "delivery_attempts": 1,
            },
        ])
        return None

    d.queue.update(add)
    d.current_prompt_by_agent["bob"] = {
        "id": "msg-terminal-suppress",
        "nonce": msg.get("nonce"),
        "causal_id": msg.get("causal_id"),
        "hop_count": 2,
        "from": "alice",
        "kind": "request",
        "intent": "review",
        "auto_return": True,
        "turn_id": "t-terminal-suppress",
    }
    d.busy["bob"] = True

    d.handle_response_finished({"agent": "bob", "bridge_agent": "bob", "turn_id": "t-terminal-suppress", "last_assistant_message": "done"})

    queue = d.queue.read()
    ids = {item.get("id") for item in queue}
    assert_true("msg-stale-wake" not in ids, f"{label}: matching pending wake should be suppressed: {queue}")
    assert_true({"msg-unrelated-wake", "msg-delivered-wake", "msg-inflight-wake"}.issubset(ids), f"{label}: unrelated/non-pending wakes must remain: {queue}")
    result = next((it for it in queue if it.get("kind") == "result" and it.get("reply_to") == "msg-terminal-suppress"), None)
    assert_true(result is not None and result.get("source") == "auto_return", f"{label}: real result should remain queued: {queue}")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_extend_replaces_wake(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _add_watchdog_request(d, "msg-delivery-extend")
    d.try_deliver("codex")
    first_wake = _watchdogs_for_message(d, "msg-delivery-extend", "delivery")[0][0]

    ok, err, _deadline = d.upsert_message_watchdog("claude", "msg-delivery-extend", 120.0)

    assert_true(ok, f"{label}: inflight delivery watchdog should extend, got {err!r}")
    matches = _watchdogs_for_message(d, "msg-delivery-extend", "delivery")
    assert_true(len(matches) == 1, f"{label}: exactly one delivery watchdog after extend, got {d.watchdogs}")
    assert_true(matches[0][0] != first_wake, f"{label}: extend should replace wake id")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_submitted_extend_and_text(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _add_watchdog_request(d, "msg-delivery-submitted")
    d.try_deliver("codex")
    d.mark_message_submitted("msg-delivery-submitted")

    ok, err, _deadline = d.upsert_message_watchdog("claude", "msg-delivery-submitted", 120.0)
    assert_true(ok, f"{label}: submitted delivery watchdog should extend, got {err!r}")
    wd = _watchdogs_for_message(d, "msg-delivery-submitted", "delivery")[0][1]
    text = d.build_watchdog_fire_text(wd)
    assert_true("status=submitted" in text and "agent_extend_wait msg-delivery-submitted <sec>" in text, f"{label}: submitted delivery text should be actionable: {text!r}")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_aggregate_pending_extend_rejected(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
    }
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-agg-pending", frm="manager", to="w1", status="pending")
    msg["watchdog_delay_sec"] = 60.0
    msg["aggregate_id"] = "agg-pending"
    msg["aggregate_expected"] = ["w1"]
    msg["aggregate_message_ids"] = {"w1": "msg-agg-pending"}
    d.enqueue_ipc_message(msg)

    ok, err, _deadline = d.upsert_message_watchdog("manager", "msg-agg-pending", 120.0)

    assert_true(not ok and err == "message_not_extendable_state", f"{label}: pending aggregate leg should be rejected, got ok={ok}, err={err!r}")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_replaced_by_response_on_delivered(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _add_watchdog_request(d, "msg-delivery-replace")
    d.try_deliver("codex")
    delivery_wake = _watchdogs_for_message(d, "msg-delivery-replace", "delivery")[0][0]
    item = _queue_item(d, "msg-delivery-replace") or {}

    d.handle_prompt_submitted({"agent": "codex", "bridge_agent": "codex", "nonce": item.get("nonce"), "turn_id": "t-replace", "prompt": ""})

    delivery_matches = _watchdogs_for_message(d, "msg-delivery-replace", "delivery")
    response_matches = _watchdogs_for_message(d, "msg-delivery-replace", "response")
    assert_true(delivery_matches == [], f"{label}: delivery watchdog should be removed after delivered: {delivery_matches}")
    assert_true(len(response_matches) == 1, f"{label}: response watchdog should replace delivery watchdog: {d.watchdogs}")
    assert_true(response_matches[0][0] != delivery_wake, f"{label}: response watchdog should have a new wake id")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_requeue_cancels(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _add_watchdog_request(d, "msg-delivery-requeue")
    d.try_deliver("codex")
    assert_true(_watchdogs_for_message(d, "msg-delivery-requeue", "delivery"), f"{label}: precondition delivery watchdog missing")
    first_wake = _watchdogs_for_message(d, "msg-delivery-requeue", "delivery")[0][0]
    def age(queue):
        for it in queue:
            if it.get("id") == "msg-delivery-requeue":
                it["updated_ts"] = "1970-01-01T00:00:00.000000Z"
        return None
    d.queue.update(age)

    d._requeue_stale_inflight_locked(time.time())

    item = _queue_item(d, "msg-delivery-requeue")
    assert_true(item is not None and item.get("status") == "inflight" and item.get("delivery_attempts") == 2, f"{label}: stale inflight should requeue then immediately retry delivery: {item}")
    assert_true(first_wake not in d.watchdogs, f"{label}: requeue should cancel stale delivery watchdog wake id: {d.watchdogs}")
    fresh = _watchdogs_for_message(d, "msg-delivery-requeue", "delivery")
    assert_true(len(fresh) == 1 and fresh[0][0] != first_wake, f"{label}: immediate retry should arm a fresh delivery watchdog: {d.watchdogs}")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(
        any(e.get("event") == "watchdog_cancelled" and e.get("wake_id") == first_wake and e.get("reason") == "prompt_submit_timeout" for e in events),
        f"{label}: stale delivery watchdog cancellation should be logged: {events}",
    )
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_mark_pending_cancels(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _add_watchdog_request(d, "msg-delivery-pending")
    d.try_deliver("codex")
    first_wake = _watchdogs_for_message(d, "msg-delivery-pending", "delivery")[0][0]

    d.mark_message_pending("msg-delivery-pending", "tmux paste failed")

    item = _queue_item(d, "msg-delivery-pending")
    assert_true(item is not None and item.get("status") == "pending", f"{label}: mark_message_pending should revert row to pending: {item}")
    assert_true(first_wake not in d.watchdogs, f"{label}: pending rollback should cancel delivery watchdog: {d.watchdogs}")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(
        any(e.get("event") == "watchdog_cancelled" and e.get("wake_id") == first_wake and e.get("reason") == "delivery_requeued" for e in events),
        f"{label}: pending rollback watchdog cancellation should be logged: {events}",
    )
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_pane_mode_reverts_cancel(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)

    msg_mode = test_message("msg-delivery-mode", frm="claude", to="codex", status="pending")
    msg_mode["watchdog_delay_sec"] = 60.0
    d.enqueue_ipc_message(msg_mode)
    d.try_deliver("codex")
    mode_item = _queue_item(d, "msg-delivery-mode") or {}
    mode_wake = _watchdogs_for_message(d, "msg-delivery-mode", "delivery")[0][0]
    result_mode = d.defer_inflight_for_pane_mode(mode_item, "copy-mode")
    assert_true(result_mode is not None and result_mode.get("status") == "pending", f"{label}: pane-mode defer should return pending row: {result_mode}")
    assert_true(mode_wake not in d.watchdogs, f"{label}: pane-mode defer should cancel delivery watchdog: {d.watchdogs}")

    probe_dir = tmpdir / "probe-failure"
    d_probe = make_daemon(probe_dir, participants)
    msg_probe = test_message("msg-delivery-probe", frm="claude", to="codex", status="pending")
    msg_probe["watchdog_delay_sec"] = 60.0
    d_probe.enqueue_ipc_message(msg_probe)
    d_probe.try_deliver("codex")
    probe_item = _queue_item(d_probe, "msg-delivery-probe") or {}
    probe_wake = _watchdogs_for_message(d_probe, "msg-delivery-probe", "delivery")[0][0]
    result_probe = d_probe.defer_inflight_for_pane_mode_probe_failed(probe_item, "probe failed")
    assert_true(result_probe is not None and result_probe.get("status") == "pending", f"{label}: probe failure defer should return pending row: {result_probe}")
    assert_true(probe_wake not in d_probe.watchdogs, f"{label}: probe failure defer should cancel delivery watchdog: {d_probe.watchdogs}")
    events = read_events(tmpdir / "events.raw.jsonl") + read_events(probe_dir / "events.raw.jsonl")
    cancelled_reasons = {
        e.get("reason")
        for e in events
        if e.get("event") == "watchdog_cancelled" and e.get("watchdog_phase") == "delivery"
    }
    assert_true({"pane_mode_deferred", "pane_mode_probe_failed"}.issubset(cancelled_reasons), f"{label}: expected pane-mode cancellation reasons, got {cancelled_reasons}")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_phase_mismatch_skipped(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-delivery-mismatch", frm="claude", to="codex", status="delivered")
    d.queue.update(lambda queue: queue.append(msg))
    wake_id = _plant_watchdog(d, "wake-delivery-mismatch", sender="claude", message_id="msg-delivery-mismatch", to="codex")
    d.watchdogs[wake_id]["watchdog_phase"] = "delivery"

    d.fire_watchdog(wake_id, dict(d.watchdogs[wake_id]))

    assert_true(wake_id not in d.watchdogs, f"{label}: mismatched delivery watchdog should be removed")
    notices = [it for it in d.queue.read() if it.get("intent") == "watchdog_wake"]
    assert_true(not notices, f"{label}: mismatched delivery watchdog should not wake sender: {notices}")
    events = read_events(tmpdir / "events.raw.jsonl")
    skipped = [e for e in events if e.get("event") == "watchdog_skipped_stale" and e.get("wake_id") == wake_id]
    assert_true(skipped and skipped[0].get("reason") == "delivery_status_delivered", f"{label}: skip reason should record phase mismatch: {skipped}")
    print(f"  PASS  {label}")


def scenario_watchdog_phase_legacy_default_response(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    legacy = {
        "sender": "claude",
        "deadline": time.time(),
        "ref_message_id": "msg-legacy-watchdog",
        "ref_aggregate_id": None,
        "ref_to": "codex",
        "ref_kind": "request",
        "ref_intent": "test",
        "is_alarm": False,
    }

    assert_true(d.normalize_watchdog_phase(legacy) == "response", f"{label}: missing watchdog_phase should default to response")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_aggregate_leg_coexists_with_response(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-delivery-watchdog"
    msg1 = test_message("msg-agg-w1-delivery", frm="manager", to="w1", status="pending")
    msg2 = test_message("msg-agg-w2-delivery", frm="manager", to="w2", status="pending")
    for msg in (msg1, msg2):
        msg["watchdog_delay_sec"] = 60.0
        msg["aggregate_id"] = agg_id
        msg["aggregate_expected"] = ["w1", "w2"]
        msg["aggregate_message_ids"] = {"w1": "msg-agg-w1-delivery", "w2": "msg-agg-w2-delivery"}
        d.enqueue_ipc_message(msg)
    d.try_deliver("w1")
    d.try_deliver("w2")
    w1_item = _queue_item(d, "msg-agg-w1-delivery") or {}
    d.handle_prompt_submitted({"agent": "w1", "bridge_agent": "w1", "nonce": w1_item.get("nonce"), "turn_id": "t-agg-w1", "prompt": ""})

    assert_true(not _watchdogs_for_message(d, "msg-agg-w1-delivery", "delivery"), f"{label}: delivered aggregate leg should not keep delivery watchdog")
    assert_true(_watchdogs_for_message(d, "msg-agg-w2-delivery", "delivery"), f"{label}: stuck aggregate leg should keep per-leg delivery watchdog")
    aggregate_response = [
        wd for wd in d.watchdogs.values()
        if wd.get("ref_aggregate_id") == agg_id and wd.get("watchdog_phase") == "response"
    ]
    assert_true(len(aggregate_response) == 1, f"{label}: aggregate response watchdog should coexist with per-leg delivery watchdog: {d.watchdogs}")
    w2_wake, w2_wd = _watchdogs_for_message(d, "msg-agg-w2-delivery", "delivery")[0]
    w2_wd["deadline"] = time.time() - 1.0
    d.watchdogs[w2_wake] = w2_wd
    d.check_watchdogs()
    notices = [it for it in d.queue.read() if it.get("from") == "bridge" and it.get("to") == "manager" and it.get("intent") == "watchdog_wake"]
    assert_true(len(notices) == 1, f"{label}: aggregate delivery leg should wake requester once: {notices}")
    body = str(notices[0].get("body") or "")
    assert_true("msg-agg-w2-delivery" in body and "not completed bridge delivery" in body, f"{label}: aggregate delivery wake should be leg-specific delivery text: {body!r}")
    assert_true("aggregate " not in body.lower(), f"{label}: aggregate delivery wake must not use aggregate-response wording: {body!r}")
    print(f"  PASS  {label}")


def scenario_delivery_watchdog_aggregate_interrupt_cancels_leg_only(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-delivery-interrupt"
    msg_w1 = test_message("msg-agg-int-w1", frm="manager", to="w1", status="pending")
    msg_w2 = test_message("msg-agg-int-w2", frm="manager", to="w2", status="pending")
    for msg in (msg_w1, msg_w2):
        msg["watchdog_delay_sec"] = 60.0
        msg["aggregate_id"] = agg_id
        msg["aggregate_expected"] = ["w1", "w2"]
        msg["aggregate_message_ids"] = {"w1": "msg-agg-int-w1", "w2": "msg-agg-int-w2"}
        d.enqueue_ipc_message(msg)
    d.try_deliver("w1")
    d.try_deliver("w2")
    w1_item = _queue_item(d, "msg-agg-int-w1") or {}
    d.handle_prompt_submitted({"agent": "w1", "bridge_agent": "w1", "nonce": w1_item.get("nonce"), "turn_id": "t-agg-int-w1", "prompt": ""})
    aggregate_wake = next(
        wake_id for wake_id, wd in d.watchdogs.items()
        if wd.get("ref_aggregate_id") == agg_id and wd.get("watchdog_phase") == "response"
    )
    w2_wake = _watchdogs_for_message(d, "msg-agg-int-w2", "delivery")[0][0]

    result = d.handle_interrupt(sender="manager", target="w2")

    assert_true(result.get("esc_sent") is True, f"{label}: interrupt should succeed")
    assert_true(w2_wake not in d.watchdogs, f"{label}: interrupted delivery leg watchdog should be cancelled: {d.watchdogs}")
    assert_true(aggregate_wake in d.watchdogs, f"{label}: aggregate response watchdog should remain after cancelling one delivery leg: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_aggregate_response_watchdog_text_uses_progress(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-response-text"
    with locked_json(Path(d.aggregate_file), {"version": 1, "aggregates": {}}) as data:
        data.setdefault("aggregates", {})[agg_id] = {
            "id": agg_id,
            "requester": "manager",
            "expected": ["w1", "w2"],
            "message_ids": {"w1": "msg-agg-text-w1", "w2": "msg-agg-text-w2"},
            "replies": {"w1": {"from": "w1", "body": "ok", "reply_to": "msg-agg-text-w1"}},
            "status": "collecting",
            "delivered": False,
        }
    wd = {
        "sender": "manager",
        "deadline": time.time(),
        "watchdog_phase": "response",
        "ref_message_id": "msg-agg-text-w1",
        "ref_aggregate_id": agg_id,
        "ref_to": "w1",
        "ref_kind": "request",
        "ref_intent": "test",
        "ref_causal_id": "causal-agg-text",
        "ref_aggregate_expected": ["w1", "w2"],
        "is_alarm": False,
    }

    text = d.build_watchdog_fire_text(wd)

    assert_true(f"aggregate {agg_id} has not completed" in text, f"{label}: aggregate response text should identify aggregate: {text!r}")
    assert_true("Replied: 1/2" in text and "Missing peers: w2" in text, f"{label}: aggregate response text should include progress: {text!r}")
    assert_true("not completed bridge delivery" not in text, f"{label}: aggregate response text should not use delivery wording: {text!r}")
    print(f"  PASS  {label}")


def scenario_aggregate_completion_suppresses_pending_watchdog_wake(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%90"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%91"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.try_deliver = lambda *args, **kwargs: None  # type: ignore[assignment]
    agg_id = "agg-suppress-wake"
    context = {
        "aggregate_id": agg_id,
        "from": "manager",
        "aggregate_expected": ["w1", "w2"],
        "aggregate_message_ids": {"w1": "msg-agg-suppress-w1", "w2": "msg-agg-suppress-w2"},
        "causal_id": "causal-agg-suppress",
        "intent": "review",
        "hop_count": 1,
    }
    d.collect_aggregate_response("w1", "w1 ok", {**context, "id": "msg-agg-suppress-w1"})

    def add_wakes(queue):
        queue.extend([
            {
                "id": "msg-agg-stale-wake",
                "created_ts": utc_now(),
                "updated_ts": utc_now(),
                "from": "bridge",
                "to": "manager",
                "kind": "notice",
                "intent": "watchdog_wake",
                "body": "aggregate stale wake",
                "causal_id": "causal-agg-suppress",
                "hop_count": 0,
                "auto_return": False,
                "reply_to": "",
                "ref_aggregate_id": agg_id,
                "source": "watchdog_fire",
                "bridge_session": "test-session",
                "status": "pending",
                "nonce": None,
                "delivery_attempts": 0,
            },
            {
                "id": "msg-agg-delivered-wake",
                "created_ts": utc_now(),
                "updated_ts": utc_now(),
                "from": "bridge",
                "to": "manager",
                "kind": "notice",
                "intent": "watchdog_wake",
                "body": "aggregate delivered wake",
                "causal_id": "causal-agg-suppress",
                "hop_count": 0,
                "auto_return": False,
                "reply_to": "",
                "ref_aggregate_id": agg_id,
                "source": "watchdog_fire",
                "bridge_session": "test-session",
                "status": "delivered",
                "nonce": None,
                "delivery_attempts": 1,
            },
        ])
        return None

    d.queue.update(add_wakes)
    d.collect_aggregate_response("w2", "w2 ok", {**context, "id": "msg-agg-suppress-w2"})

    queue = d.queue.read()
    ids = {item.get("id") for item in queue}
    assert_true("msg-agg-stale-wake" not in ids, f"{label}: pending aggregate wake should be suppressed: {queue}")
    assert_true("msg-agg-delivered-wake" in ids, f"{label}: delivered aggregate wake should remain: {queue}")
    result = next((it for it in queue if it.get("source") == "aggregate_return" and it.get("aggregate_id") == agg_id), None)
    assert_true(result is not None and result.get("to") == "manager", f"{label}: aggregate result should remain queued: {queue}")
    ok, err, _ = d.upsert_message_watchdog("manager", "msg-agg-suppress-w2", 60.0)
    assert_true(not ok and err == "message_recently_responded", f"{label}: completed aggregate leg should not be unknown: ok={ok}, err={err!r}")
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


def scenario_alarm_fire_text_includes_rearm_hint(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    hint = "Re-arm via agent_alarm <sec> if still waiting."

    no_note = d.build_watchdog_fire_text({"is_alarm": True, "alarm_body": ""})
    assert_true(no_note == f"[bridge:alarm] Wake elapsed. {hint}", f"{label}: alarm text must be compact and actionable: {no_note!r}")

    with_note = d.build_watchdog_fire_text({"is_alarm": True, "alarm_body": "check build"})
    assert_true("Note: check build" in with_note, f"{label}: alarm text must preserve custom note: {with_note!r}")
    assert_true(hint in with_note, f"{label}: alarm note text must include re-arm hint: {with_note!r}")
    assert_true(with_note.index("Note: check build") < with_note.index(hint), f"{label}: note should precede re-arm hint: {with_note!r}")
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
    d = make_daemon(tmpdir, participants, queue_items=[msg])
    queued_startup = next((it for it in d.queue.read() if it.get("id") == "msg-mode-restart"), None)
    assert_true(queued_startup is not None and not queued_startup.get(RESTART_PRESERVED_INFLIGHT_KEY), f"{label}: enter-deferred restart row must not be frozen by restart-preserved marker")
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


def _make_interrupt_key_daemon(tmpdir: Path, target_agent_type: str = "claude"):
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": target_agent_type, "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.interrupt_key_delay_seconds = 0.0

    def fake_resolve(target: str, purpose: str = "write") -> dict:
        pane = str((d.participants.get(target) or {}).get("pane") or "")
        return {
            "ok": bool(pane),
            "pane": pane,
            "reason": "" if pane else "no_pane",
            "probe_status": "verified" if pane else "",
            "detail": "",
            "should_detach": False,
        }

    d.resolve_endpoint_detail = fake_resolve  # type: ignore[method-assign]
    return d


def _record_interrupt_keys(d, records: list[dict], *, fail_key: str = "") -> None:
    def recorder(pane: str, key: str) -> tuple[bool, str]:
        records.append({"pane": pane, "key": key, "lock_held": d.state_lock._is_owned()})
        if fail_key and key == fail_key:
            return False, f"{key} failed"
        return True, ""

    d.send_interrupt_key = recorder  # type: ignore[method-assign]


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


def scenario_daemon_reload_inflight_idempotent(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    stale_iso = "2000-01-01T00:00:00.000000Z"
    inflight = test_message("msg-reload-inflight", frm="alice", to="bob", status="inflight")
    inflight.update({
        "nonce": "n-reload-inflight",
        "delivery_attempts": 1,
        "created_ts": stale_iso,
        "updated_ts": stale_iso,
    })
    followup = test_message("msg-reload-followup", frm="alice", to="bob", status="pending")
    d = make_daemon(tmpdir, participants, queue_items=[inflight, followup])

    row = _queue_item(d, "msg-reload-inflight") or {}
    assert_true(row.get(RESTART_PRESERVED_INFLIGHT_KEY) is True, f"{label}: startup must mark pre-existing inflight rows: {row}")
    assert_true(d._classify_prior_for_hint(row) == "interrupt", f"{label}: preserved inflight must guide operators to interrupt, not cancel: {row}")
    cancel_result = d.cancel_message("alice", "msg-reload-inflight")
    assert_true(cancel_result.get("error") == "message_active_use_interrupt", f"{label}: preserved inflight must not be pre-paste cancellable: {cancel_result}")

    delivered_calls: list[str] = []
    d.deliver_reserved = lambda message: delivered_calls.append(str(message.get("id") or ""))  # type: ignore[method-assign]
    d.try_deliver("bob")
    assert_true(delivered_calls == [], f"{label}: restart must not re-paste preserved inflight rows: {delivered_calls}")
    row = _queue_item(d, "msg-reload-inflight") or {}
    follow = _queue_item(d, "msg-reload-followup") or {}
    assert_true(row.get("status") == "inflight" and row.get("delivery_attempts") == 1, f"{label}: inflight row must remain untouched: {row}")
    assert_true(follow.get("status") == "pending", f"{label}: follow-up must stay queued behind inflight: {follow}")

    def age(queue):
        for item in queue:
            if item.get("id") == "msg-reload-inflight":
                item["updated_ts"] = stale_iso
        return None

    d.queue.update(age)
    d.last_maintenance = 0.0
    d._requeue_stale_inflight_locked(time.time())
    row = _queue_item(d, "msg-reload-inflight") or {}
    assert_true(row.get("status") == "inflight" and row.get("delivery_attempts") == 1, f"{label}: stale maintenance must not requeue preserved inflight: {row}")
    assert_true(delivered_calls == [], f"{label}: stale maintenance must not retry delivery: {delivered_calls}")

    resolve_calls: list[str] = []
    d.resolve_endpoint_detail = lambda target, purpose="write": resolve_calls.append(str(target)) or {"ok": True, "pane": "%92", "reason": ""}  # type: ignore[method-assign]
    d.retry_enter_for_inflight()
    assert_true(resolve_calls == [], f"{label}: normal preserved inflight must not retry Enter after restart: {resolve_calls}")

    d.handle_prompt_submitted({
        "agent": "bob",
        "bridge_agent": "bob",
        "nonce": "n-reload-inflight",
        "turn_id": "turn-reload",
        "prompt": "[bridge:n-reload-inflight] from=alice kind=request",
    })
    row = _queue_item(d, "msg-reload-inflight") or {}
    follow = _queue_item(d, "msg-reload-followup") or {}
    assert_true(row.get("status") == "delivered", f"{label}: prompt_submitted should recover by queue scan: {row}")
    assert_true(RESTART_PRESERVED_INFLIGHT_KEY not in row, f"{label}: delivery binding must clear restart marker: {row}")
    assert_true(follow.get("status") == "pending", f"{label}: follow-up must wait until terminal response: {follow}")

    d.deliver_reserved = bridge_daemon.BridgeDaemon.deliver_reserved.__get__(d, bridge_daemon.BridgeDaemon)  # type: ignore[method-assign]
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": True, "pane": "%92" if target == "bob" else "%91", "reason": ""}  # type: ignore[method-assign]
    d.handle_response_finished({
        "agent": "bob",
        "bridge_agent": "bob",
        "turn_id": "turn-reload",
        "last_assistant_message": "reply after restart",
    })
    assert_true(_queue_item(d, "msg-reload-inflight") is None, f"{label}: terminal response removes original request")
    follow = _queue_item(d, "msg-reload-followup") or {}
    assert_true(follow.get("status") == "inflight" and follow.get("delivery_attempts") == 1, f"{label}: follow-up advances only after terminal response: {follow}")
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


def scenario_backfill_fresh_probe_repairs_unscoped_live_mismatch(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codex1", session_id="sess-a", pane="%27a")
        polluted = identity_live_record(
            pane="%27a",
            session_id="sess-b",
            alias="",
            pid=7001,
            start_time="502",
            process_identity=verified_identity("codex", "%27a", pid=7001, start_time="502"),
        )
        polluted["bridge_session"] = ""
        write_live_identity_records(polluted, index_record=polluted)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=7002, start_time="503")  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities(
                "test-session",
                {"session": "test-session", "participants": {"codex1": participant}},
                allow_create_from_hook=True,
            )
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex1", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        assert_true(summary.get("codex1", {}).get("status") == "verified", f"{label}: fresh probe should repair mismatch: {summary}")
        assert_true(((live.get("panes") or {}).get("%27a") or {}).get("session_id") == "sess-a", f"{label}: stable id must replace polluted live record: {live}")
        assert_true("codex:sess-b" not in (live.get("sessions") or {}), f"{label}: polluted session index must be removed: {live}")
        assert_true(detail.get("ok") and detail.get("pane") == "%27a", f"{label}: repaired endpoint should resolve: {detail}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_canonicalizes_via_pane_lock(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%60")
        stable = identity_live_record(pane="%60", session_id="sess-a", pid=9600, start_time="1600")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9600, start_time="1600"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%60", event="prompt_submitted", target="tmux:1.0")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        record = (live.get("panes") or {}).get("%60") or {}
        lock = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(record.get("session_id") == "sess-a", f"{label}: locked session id must be preserved: {record}")
        assert_true(record.get("hook_payload_session_id") == "sess-b", f"{label}: payload id should be diagnostic: {record}")
        assert_true(record.get("canonicalized_from") == "pane_lock", f"{label}: source should be pane_lock: {record}")
        assert_true(((lock.get("panes") or {}).get("%60") or {}).get("hook_session_id") == "sess-a", f"{label}: pane lock changed: {lock}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("hook_session_id")) == "sess-a", f"{label}: participant hook id changed: {state}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalized" for event in events), f"{label}: canonicalization event missing: {events}")
        assert_true(detail.get("ok") and detail.get("pane") == "%60", f"{label}: endpoint should remain routable: {detail}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_canonicalizes_during_attach_gap(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%61")
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"version": 1, "sessions": {}})
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"version": 1, "panes": {}})
        stable = identity_live_record(pane="%61", session_id="sess-a", pid=9610, start_time="1610")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9610, start_time="1610"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%61", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        record = (live.get("panes") or {}).get("%61") or {}
        assert_true(record.get("session_id") == "sess-a", f"{label}: scoped prior live should close attach gap: {record}")
        assert_true(record.get("canonicalized_from") == "scoped_live_prior", f"{label}: source should be scoped prior: {record}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_canonicalizes_via_attached_registry(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir):
        write_identity_fixture(tmpdir / "identity-runtime" / "state", pane="%62")
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"version": 1, "panes": {}})
        stable = identity_live_record(pane="%62", session_id="sess-a", pid=9620, start_time="1620")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9620, start_time="1620"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%62", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        record = (live.get("panes") or {}).get("%62") or {}
        assert_true(record.get("session_id") == "sess-a", f"{label}: registry authority should preserve stable id: {record}")
        assert_true(record.get("canonicalized_from") == "attached_registry", f"{label}: source should be attached_registry: {record}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_new_process_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%63")
        stable = identity_live_record(pane="%63", session_id="sess-a", pid=9630, start_time="1630")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        def probe(pane, agent, stored_identity=None):
            if stored_identity:
                return {"status": "mismatch", "reason": "process_fingerprint_mismatch", "pane": pane, "agent": agent, "processes": []}
            return verified_identity(agent, pane, pid=9631, start_time="1631")
        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%63", event="prompt_submitted", target="tmux:1.0")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(((live.get("panes") or {}).get("%63") or {}).get("session_id") == "sess-b", f"{label}: existing takeover semantics should write new live id: {live}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: participant should be endpoint_lost: {state}")
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_mismatch", f"{label}: old alias must not route to new process: {detail}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalize_blocked" and event.get("reason") == "process_fingerprint_mismatch" for event in events), f"{label}: blocked event missing: {events}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_cross_reboot_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%64")
        identity = verified_identity("codex", "%64", pid=9640, start_time="1640")
        identity["boot_id"] = "boot-old"
        stable = identity_live_record(pane="%64", session_id="sess-a", process_identity=identity)
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: (
            {"status": "mismatch", "reason": "boot_id_mismatch", "pane": pane, "agent": agent, "processes": []}
            if stored_identity else verified_identity(agent, pane, pid=9641, start_time="1641")
        )  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%64", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: reboot mismatch must fail closed: {state}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalize_blocked" and event.get("reason") == "boot_id_mismatch" for event in events), f"{label}: boot mismatch event missing: {events}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_missing_prior_fingerprint_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%65")
        weak = identity_live_record(pane="%65", session_id="sess-a", process_identity={"status": "unknown", "reason": "ps_unavailable", "processes": []})
        write_live_identity_records(weak, index_record=weak)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9651, start_time="1651")  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%65", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: missing prior fingerprint must fail closed: {state}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalize_blocked" and event.get("reason") == "missing_prior_verified_fingerprint" for event in events), f"{label}: missing fingerprint event absent: {events}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_different_agent_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%66")
        stable = identity_live_record(pane="%66", session_id="sess-a", pid=9660, start_time="1660")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9661, start_time="1661")  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="claude", session_id="sess-b", pane="%66", event="prompt_submitted", target="tmux:1.0")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: different agent must mark old endpoint lost: {state}")
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_mismatch", f"{label}: codex alias must not route to claude: {detail}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_same_session_no_canonicalization_event(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%67")
        stable = identity_live_record(pane="%67", session_id="sess-a", pid=9670, start_time="1670")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9670, start_time="1670"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%67", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        events = read_raw_events(state_root_path)
        assert_true(not any(str(event.get("event") or "").startswith("unscoped_hook_canonical") for event in events), f"{label}: same-session update should not canonicalize: {events}")
    print(f"  PASS  {label}")


def scenario_scoped_different_session_no_canonicalization(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%68")
        stable = identity_live_record(pane="%68", session_id="sess-a", pid=9680, start_time="1680")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9681, start_time="1681")  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%68", bridge_session="other-session", alias="codex2", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        events = read_raw_events(state_root_path)
        assert_true(not any(event.get("event") == "unscoped_hook_canonicalized" for event in events), f"{label}: scoped handoff must not use unscoped canonicalization: {events}")
    print(f"  PASS  {label}")


def scenario_session_ended_payload_mismatch_no_canonicalization(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%69")
        stable = identity_live_record(pane="%69", session_id="sess-a", pid=9690, start_time="1690")
        write_live_identity_records(stable, index_record=stable)
        bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%69", event="session_ended", target="tmux:1.0")
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        events = read_raw_events(state_root_path)
        assert_true(((live.get("panes") or {}).get("%69") or {}).get("session_id") == "sess-a", f"{label}: mismatched session_ended should be no-op: {live}")
        assert_true(not any(str(event.get("event") or "").startswith("unscoped_hook_canonical") for event in events), f"{label}: session_ended must bypass canonicalization: {events}")
    print(f"  PASS  {label}")


def scenario_resolver_reconnects_exact_mismatch_shape(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%70")
        old_other = identity_live_record(pane="%70", session_id="sess-b", alias="", pid=9700, start_time="1700")
        old_other["bridge_session"] = ""
        candidate = identity_live_record(pane="%71", session_id="sess-a", pid=9701, start_time="1701")
        write_live_identity_records(old_other, candidate, index_record=candidate)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(detail.get("ok") and detail.get("pane") == "%71", f"{label}: resolver should recover exact mismatch shape: {detail}")
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%71", f"{label}: registry not moved: {registry}")
        assert_true("%70" not in (locks.get("panes") or {}) and ((locks.get("panes") or {}).get("%71") or {}).get("hook_session_id") == "sess-a", f"{label}: locks not moved: {locks}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%71", f"{label}: participant pane not moved: {state}")
    print(f"  PASS  {label}")


def scenario_hook_reconnects_exact_mismatch_shape(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%72")
        old_other = identity_live_record(pane="%72", session_id="sess-b", alias="", pid=9720, start_time="1720")
        old_other["bridge_session"] = ""
        write_live_identity_records(old_other, index_record=old_other)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9721, start_time="1721")  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%73", bridge_session="test-session", alias="codex", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(mapping and mapping.get("pane") == "%73", f"{label}: hook path should reconnect to new stable pane: {mapping}")
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%73", f"{label}: registry not moved: {registry}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%73", f"{label}: participant pane not moved: {state}")
    print(f"  PASS  {label}")


def scenario_cross_pane_candidate_mismatch_blocks_reconnect(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%74")
        candidate = identity_live_record(pane="%75", session_id="sess-a", pid=9750, start_time="1750")
        write_live_identity_records(candidate, index_record=candidate)
        old_probe = bridge_identity.probe_agent_process
        def probe(pane, agent, stored_identity=None):
            if pane == "%75":
                return {"status": "mismatch", "reason": "process_fingerprint_mismatch", "pane": pane, "agent": agent, "processes": []}
            return {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}
        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_missing", f"{label}: dead candidate must not reconnect: {detail}")
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%74", f"{label}: mapping should stay old: {registry}")
    print(f"  PASS  {label}")


def scenario_codex1_incident_replay_canonicalizes_repeated_unscoped_hooks(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codex1", session_id="019dce7c", pane="%76")
        stable = identity_live_record(pane="%76", session_id="019dce7c", alias="codex1", pid=9760, start_time="1760")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9760, start_time="1760"))  # type: ignore[assignment]
        try:
            for suffix in range(5):
                bridge_identity.update_live_session(agent_type="codex", session_id=f"019dceb{suffix}", pane="%76", event="response_finished", target="tmux:1.0", model="gpt-5.4-mini")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex1", participant)
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codex1": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        events = read_raw_events(state_root_path)
        record = (live.get("panes") or {}).get("%76") or {}
        assert_true(record.get("session_id") == "019dce7c", f"{label}: incident replay should keep stable id: {record}")
        assert_true(sum(1 for event in events if event.get("event") == "unscoped_hook_canonicalized") == 5, f"{label}: expected 5 canonicalization events: {events}")
        assert_true(detail.get("ok") and detail.get("pane") == "%76", f"{label}: alias should remain routable: {detail}")
        assert_true(summary.get("codex1", {}).get("status") == "verified", f"{label}: healthcheck backfill should report healthy: {summary}")
    print(f"  PASS  {label}")


def _target_recovery_fixture(
    state_root_path: Path,
    *,
    session_id: str = "019dce7c-3126-7983-88c0-a3d80cb6f556",
    old_pane: str = "%80",
    target: str = "0:1.2",
) -> dict:
    participant = write_identity_fixture(state_root_path, session_id=session_id, pane=old_pane)
    set_identity_target(state_root_path, session_id=session_id, pane=old_pane, target=target)
    stable = identity_live_record(pane=old_pane, session_id=session_id, pid=9800, start_time="1800")
    stable["target"] = target
    write_live_identity_records(stable, index_record=stable)
    return participant


def _patch_target_recovery(
    *,
    target: str = "0:1.2",
    new_pane: str = "%81",
    session_id: str = "019dce7c-3126-7983-88c0-a3d80cb6f556",
    proof_session: str | None = "019dce7c-3126-7983-88c0-a3d80cb6f556",
    proof_pid: int = 9801,
    probe_verified: bool = True,
    owner_pid: int | None = None,
):
    old_probe = bridge_identity.probe_agent_process
    old_pane_for_target = bridge_identity.tmux_pane_for_target
    old_display = bridge_identity.tmux_display_pane
    old_transcript = bridge_identity.transcript_session_id_for_pid
    old_owners = bridge_identity.transcript_owners_for_session

    def probe(pane, agent, stored_identity=None):
        if pane == new_pane and probe_verified:
            identity = verified_identity(agent, pane, pid=proof_pid, start_time="1801")
            identity["target"] = target
            return identity
        if pane == new_pane:
            return {"status": "mismatch", "reason": "agent_process_not_found", "pane": pane, "agent": agent, "processes": []}
        return {"status": "mismatch", "reason": "pane_unavailable", "pane": pane, "agent": agent, "processes": []}

    def transcript(agent, pid):
        if int(str(pid)) == proof_pid and proof_session:
            return {
                "session_id": proof_session,
                "path": f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18-28-58-{proof_session}.jsonl",
                "pid": proof_pid,
            }
        return {}

    def owners(agent, wanted_session):
        if owner_pid:
            return [
                {
                    "session_id": wanted_session,
                    "path": f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18-28-58-{wanted_session}.jsonl",
                    "pid": owner_pid,
                }
            ]
        return []

    bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
    bridge_identity.tmux_pane_for_target = lambda candidate: new_pane if candidate == target else ""  # type: ignore[assignment]
    bridge_identity.tmux_display_pane = lambda pane: (  # type: ignore[assignment]
        {"pane_id": new_pane, "pane_pid": "200", "target": target, "command": "node", "cwd": "/data/sembench-hard"}
        if pane == new_pane else {"error": "pane_unavailable", "detail": "stale"}
    )
    bridge_identity.transcript_session_id_for_pid = transcript  # type: ignore[assignment]
    bridge_identity.transcript_owners_for_session = owners  # type: ignore[assignment]

    def restore():
        bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        bridge_identity.tmux_pane_for_target = old_pane_for_target  # type: ignore[assignment]
        bridge_identity.tmux_display_pane = old_display  # type: ignore[assignment]
        bridge_identity.transcript_session_id_for_pid = old_transcript  # type: ignore[assignment]
        bridge_identity.transcript_owners_for_session = old_owners  # type: ignore[assignment]

    return restore


def scenario_target_recovery_reconnects_stale_pane_by_codex_transcript(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%80", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=session_id, new_pane="%81", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(detail.get("ok") and detail.get("pane") == "%81", f"{label}: resolver should recover target pane: {detail}")
        assert_true(((live.get("panes") or {}).get("%81") or {}).get("session_id") == session_id, f"{label}: live not moved to new pane: {live}")
        assert_true(((registry.get("sessions") or {}).get(f"codex:{session_id}") or {}).get("pane") == "%81", f"{label}: registry not moved: {registry}")
        assert_true("%80" not in (locks.get("panes") or {}) and ((locks.get("panes") or {}).get("%81") or {}).get("hook_session_id") == session_id, f"{label}: pane lock not moved: {locks}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("hook_session_id")) == session_id, f"{label}: hook id changed: {state}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%81", f"{label}: participant pane not moved: {state}")
        assert_true(any(event.get("event") == "target_recovery_reconnected" and event.get("transcript_path") for event in events), f"{label}: recovery event missing: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_transcript_session_mismatch(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%82", target="0:1.2")
        restore = _patch_target_recovery(proof_session="019dceff-0000-7000-8000-000000000000", new_pane="%83", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(not detail.get("ok"), f"{label}: mismatch proof must not recover: {detail}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: endpoint_lost expected: {state}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "transcript_session_mismatch" for event in events), f"{label}: blocked event missing: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_missing_transcript(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%84", target="0:1.2")
        restore = _patch_target_recovery(proof_session=None, new_pane="%85", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: missing transcript must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "transcript_proof_missing" for event in events), f"{label}: missing transcript event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_wrong_pid_transcript(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%86", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=None, proof_pid=9801, owner_pid=9900, new_pane="%87", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: wrong pid proof must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "wrong_pid_owns_transcript" for event in events), f"{label}: wrong pid event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_different_agent_at_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%88", target="0:1.2")
        restore = _patch_target_recovery(probe_verified=False, new_pane="%89", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: different agent/no codex must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "probe_not_verified" for event in events), f"{label}: probe_not_verified absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_same_pane_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%90", target="0:1.2")
        old_probe = bridge_identity.probe_agent_process
        old_pane_for_target = bridge_identity.tmux_pane_for_target
        old_display = bridge_identity.tmux_display_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: (  # type: ignore[assignment]
            {"status": "mismatch", "reason": "pane_unavailable", "pane": pane, "agent": agent, "processes": []}
            if stored_identity else verified_identity(agent, pane, pid=9901, start_time="1901")
        )
        bridge_identity.tmux_pane_for_target = lambda target: "%90"  # type: ignore[assignment]
        bridge_identity.tmux_display_pane = lambda pane: {"pane_id": "%90", "pane_pid": "200", "target": "0:1.2", "command": "node", "cwd": "/data/sembench-hard"}  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_pane_for_target = old_pane_for_target  # type: ignore[assignment]
            bridge_identity.tmux_display_pane = old_display  # type: ignore[assignment]
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: same-pane target must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "target_resolves_same_pane" for event in events), f"{label}: same pane event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_unresolvable_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%91", target="0:1.2")
        restore = _patch_target_recovery(new_pane="", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: unresolvable target must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "target_unresolvable" for event in events), f"{label}: unresolvable event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_skips_without_participant_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%92", target="")
        restore = _patch_target_recovery(new_pane="%93", target="")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: empty target should preserve failure: {detail}")
        assert_true(not any(str(event.get("event") or "").startswith("target_recovery") for event in events), f"{label}: no target recovery event expected: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_read_purpose_does_not_mutate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%94", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=session_id, new_pane="%95", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant, purpose="read")
        finally:
            restore()
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: read purpose should not write recovery: {detail}")
        assert_true(((registry.get("sessions") or {}).get(f"codex:{session_id}") or {}).get("pane") == "%94", f"{label}: registry mutated on read: {registry}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%94", f"{label}: participant mutated on read: {state}")
        assert_true(not any(event.get("event") == "target_recovery_reconnected" for event in events), f"{label}: reconnected event on read: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_env_disable_blocks(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%99", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=session_id, new_pane="%100", target="0:1.2")
        try:
            with patched_environ(AGENT_BRIDGE_NO_TARGET_RECOVERY="1"):
                detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(not detail.get("ok"), f"{label}: env disabled recovery should not recover: {detail}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%99", f"{label}: participant mutated despite env disable: {state}")
        assert_true(not any(event.get("event") == "target_recovery_reconnected" for event in events), f"{label}: reconnected event despite env disable: {events}")
    print(f"  PASS  {label}")


def scenario_tmux_display_pane_empty_metadata_is_unavailable(label: str, tmpdir: Path) -> None:
    old_run = bridge_pane_probe.run
    bridge_pane_probe.run = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "\t\t:.\t\t\n", "")  # type: ignore[assignment]
    try:
        pane = bridge_pane_probe.tmux_display_pane("%dead")
    finally:
        bridge_pane_probe.run = old_run  # type: ignore[assignment]
    assert_true(pane.get("error") == "pane_unavailable", f"{label}: empty pane metadata should be unavailable: {pane}")
    print(f"  PASS  {label}")


def scenario_codex_rollout_path_regex_is_strict(label: str, tmpdir: Path) -> None:
    session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
    valid = f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18-28-58-{session_id}.jsonl"
    assert_true(bridge_pane_probe.codex_session_id_from_rollout_path(valid) == session_id, f"{label}: valid rollout not parsed")
    loose = f"/root/.codex/sessions/2026/04/27/not-a-rollout-{session_id}.jsonl"
    assert_true(bridge_pane_probe.codex_session_id_from_rollout_path(loose) == "", f"{label}: loose substring matched")
    malformed = f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18:28:58-{session_id}.jsonl"
    assert_true(bridge_pane_probe.codex_session_id_from_rollout_path(malformed) == "", f"{label}: malformed timestamp matched")
    print(f"  PASS  {label}")


def scenario_target_recovery_only_matching_alias_recovers(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_a = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        session_b = "019dceff-0000-7000-8000-000000000000"
        state_dir = state_root_path / "test-session"
        participant_a = {"alias": "codex", "agent_type": "codex", "pane": "%96", "target": "0:1.2", "hook_session_id": session_a, "status": "active"}
        participant_b = {"alias": "codex2", "agent_type": "codex", "pane": "%97", "target": "0:1.2", "hook_session_id": session_b, "status": "active"}
        write_json_atomic(
            state_dir / "session.json",
            {
                "session": "test-session",
                "participants": {"codex": participant_a, "codex2": participant_b},
                "panes": {"codex": "%96", "codex2": "%97"},
                "targets": {"codex": "0:1.2", "codex2": "0:1.2"},
            },
        )
        write_json_atomic(
            Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]),
            {
                "version": 1,
                "sessions": {
                    f"codex:{session_a}": {"agent": "codex", "alias": "codex", "session_id": session_a, "bridge_session": "test-session", "pane": "%96", "target": "0:1.2"},
                    f"codex:{session_b}": {"agent": "codex", "alias": "codex2", "session_id": session_b, "bridge_session": "test-session", "pane": "%97", "target": "0:1.2"},
                },
            },
        )
        write_json_atomic(
            Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]),
            {
                "version": 1,
                "panes": {
                    "%96": {"bridge_session": "test-session", "agent": "codex", "alias": "codex", "target": "0:1.2", "hook_session_id": session_a},
                    "%97": {"bridge_session": "test-session", "agent": "codex", "alias": "codex2", "target": "0:1.2", "hook_session_id": session_b},
                },
            },
        )
        stable_a = identity_live_record(pane="%96", session_id=session_a, alias="codex", pid=9890, start_time="1890")
        stable_a["target"] = "0:1.2"
        stable_b = identity_live_record(pane="%97", session_id=session_b, alias="codex2", pid=9900, start_time="1900")
        stable_b["target"] = "0:1.2"
        write_live_identity_records(stable_a, stable_b, index_record=stable_a)
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        live.setdefault("sessions", {})[f"codex:{session_b}"] = stable_b
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), live)
        restore = _patch_target_recovery(session_id=session_a, proof_session=session_a, new_pane="%98", target="0:1.2")
        try:
            detail_a = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant_a)
            detail_b = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex2", participant_b)
        finally:
            restore()
        assert_true(detail_a.get("ok") and detail_a.get("pane") == "%98", f"{label}: matching alias should recover: {detail_a}")
        assert_true(not detail_b.get("ok"), f"{label}: nonmatching alias must not recover: {detail_b}")
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
    body = str((result or {}).get("body") or "")
    assert_true(result is not None and "[bridge:undeliverable]" in body, f"{label}: sender gets undeliverable result")
    assert_true("If the agent was resumed in another pane" in body, f"{label}: resume guidance included")
    assert_true("submit any short prompt" in body and "bridge hook can re-attach" in body, f"{label}: hook reattach guidance included")
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






def _import_extend_wait_module():
    import importlib
    bew = importlib.import_module("bridge_extend_wait")
    return importlib.reload(bew)


def _import_wait_status_module():
    import importlib
    bws = importlib.import_module("bridge_wait_status")
    return importlib.reload(bws)


def _import_aggregate_status_module():
    import importlib
    bas = importlib.import_module("bridge_aggregate_status")
    return importlib.reload(bas)


def _import_alarm_module():
    import importlib
    ba = importlib.import_module("bridge_alarm")
    return importlib.reload(ba)




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


def _run_wait_status_main(bws, argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import io
    old_argv = sys.argv[:]
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_wait_status", *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = bws.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
    return int(code), out.getvalue(), err.getvalue()


def _run_aggregate_status_main(bas, argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import io
    old_argv = sys.argv[:]
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_aggregate_status", *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = bas.main()
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


def scenario_extend_wait_terminal_error_texts(label: str, tmpdir: Path) -> None:
    bew = _import_extend_wait_module()
    bew.resolve_caller_from_pane = lambda **kwargs: bridge_identity.CallerResolution(True, "test-session", "alice")  # type: ignore[assignment]
    bew.ensure_daemon_running = lambda session: ""
    bew.room_status = lambda session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")
    bew.load_session = lambda session: _participants_state(["alice", "bob"])

    def normalized_sentence_fragments(text: str) -> list[str]:
        fragments = re.split(r"\.\s+", text)
        normalized: list[str] = []
        for fragment in fragments:
            cleaned = " ".join(fragment.strip().lower().split())
            if len(cleaned) >= 30:
                normalized.append(cleaned)
        return normalized

    daemon_hints = {
        "message_recently_responded": "A [bridge:result] may already be queued or arriving; do not keep extending this id.",
        "message_already_terminal": "No active watchdog remains; do not retry this id.",
        "message_unknown": "Verify the id; old or restart-lost ids cannot be extended.",
        "message_not_found": "A [bridge:result] may already be queued or arriving; do not keep extending this id.",
        "not_owner": "Only the original sender can extend; check the id you intended.",
        "aggregate_extend_not_supported": "Per-message extend is not supported for delivered aggregate members; wait for the broadcast result.",
        "watchdog_requires_auto_return": "This request has no automatic return route; the bridge cannot extend its watchdog.",
        "message_not_in_delivered_state": "Only inflight/submitted delivery and delivered response waits can be extended.",
        "message_not_extendable_state": "Only inflight/submitted delivery and delivered response waits can be extended.",
    }
    cases = [
        (
            "message_recently_responded",
            ("already reached a terminal response", "[bridge:result]", "queued", "do not keep extending"),
        ),
        (
            "message_already_terminal",
            ("already terminal", "No active watchdog remains", "do not retry"),
        ),
        (
            "message_unknown",
            ("unknown to the daemon", "Verify the id", "cannot be extended"),
        ),
        (
            "message_not_found",
            ("not found by the daemon", "[bridge:result]", "queued", "do not keep extending"),
        ),
        (
            "not_owner",
            ("not sent by you", "Only the original sender", "check the id"),
        ),
        (
            "aggregate_extend_not_supported",
            ("delivered aggregate broadcast member", "Per-message extend", "broadcast result"),
        ),
        (
            "watchdog_requires_auto_return",
            ("no automatic return route", "cannot extend its watchdog"),
        ),
        (
            "message_not_in_delivered_state",
            ("not in an extendable watchdog state", "inflight/submitted delivery", "delivered response"),
        ),
        (
            "message_not_extendable_state",
            ("not in an extendable watchdog state", "inflight/submitted delivery", "delivered response"),
        ),
    ]
    assert_true(daemon_hints == bew.EXTEND_WAIT_FALLBACK_HINTS, f"{label}: CLI fallback hints must match expected canonical hints")
    assert_true(daemon_hints == bridge_daemon.EXTEND_WATCHDOG_HINTS, f"{label}: daemon hints must match expected canonical hints")
    legacy_verbose_fragments = [
        "A [bridge:result] may already be queued or may arrive as a separate prompt",
        "It may already have responded, and a [bridge:result] may already be queued",
        "Pending messages have no active delivery attempt yet",
    ]
    for error, tokens in cases:
        for mode in ("new-daemon", "old-daemon"):
            response_extra = {"hint": daemon_hints[error]} if mode == "new-daemon" else {}

            def request_extend(_session: str, _sender: str, message_id: str, _seconds: float, *, error=error, response_extra=response_extra):
                return False, {"error": error, "message_id": message_id, **response_extra}, error

            bew.request_extend = request_extend
            code, out, err = _run_extend_wait_main(bew, ["msg-cli-terminal", "1", "--session", "test-session", "--from", "alice", "--allow-spoof"])
            assert_true(code == 1 and out == "", f"{label}: {error} {mode} should exit 1 with no stdout: code={code} out={out!r} err={err!r}")
            assert_true(f"({error})." in err, f"{label}: {error} {mode} stderr missing stable code parenthetical: {err!r}")
            assert_true("Hint: " in err, f"{label}: {error} {mode} stderr missing literal Hint prefix: {err!r}")
            body, hint = err.split("Hint: ", 1)
            body_fragments = set(normalized_sentence_fragments(body))
            hint_fragments = set(normalized_sentence_fragments(hint))
            duplicates = body_fragments & hint_fragments
            assert_true(not duplicates, f"{label}: {error} {mode} duplicated body/Hint fragments >=30 chars: {duplicates!r} in {err!r}")
            for token in tokens:
                assert_true(token in err, f"{label}: {error} {mode} stderr missing {token!r}: {err!r}")
            for fragment in legacy_verbose_fragments:
                assert_true(fragment not in body, f"{label}: {error} {mode} body kept legacy verbose recovery wording {fragment!r}: {err!r}")
    print(f"  PASS  {label}")


def _patch_wait_status_for_unit(bws, *, response: dict | None = None, error: str = "", state: dict | None = None) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    bws.resolve_caller_from_pane = lambda **kwargs: bridge_identity.CallerResolution(True, "test-session", "alice")  # type: ignore[assignment]
    bws.ensure_daemon_running = lambda session: ""
    bws.room_status = lambda session: argparse.Namespace(active_enough_for_read=True, reason="ok")
    bws.load_session = lambda session: state if state is not None else _participants_state(["alice", "bob"])

    def send_command(session: str, payload: dict):
        calls.append((session, payload))
        if error:
            return False, {"error": error}, error
        return True, response if response is not None else {
            "ok": True,
            "bridge_session": "test-session",
            "caller": "alice",
            "generated_ts": "2026-04-27T00:00:00.000000Z",
            "limits": {"per_section": 50},
            "summary": {"watchdogs": {"total_count": 1, "returned_count": 1, "truncated": False}},
            "watchdogs": {"total_count": 1, "returned_count": 1, "truncated": False, "items": [{"wake_id": "wake-1"}]},
        }, ""

    bws.send_command = send_command
    return calls


def _patch_aggregate_status_for_unit(bas, *, response: dict | None = None, error: str = "", state: dict | None = None) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    bas.resolve_caller_from_pane = lambda **kwargs: bridge_identity.CallerResolution(True, "test-session", "manager")  # type: ignore[assignment]
    bas.ensure_daemon_running = lambda session: ""
    bas.room_status = lambda session: argparse.Namespace(active_enough_for_read=True, reason="ok")
    bas.load_session = lambda session: state if state is not None else _participants_state(["manager", "w1"])

    def send_command(session: str, payload: dict):
        calls.append((session, payload))
        if error:
            return False, {"error": error}, error
        return True, response if response is not None else {
            "ok": True,
            "bridge_session": "test-session",
            "caller": "manager",
            "aggregate_id": "agg-cli",
            "status": "collecting",
            "mode": "partial",
            "replied_count": 1,
            "total_count": 2,
            "missing_count": 1,
            "legs": {
                "total_count": 2,
                "returned_count": 2,
                "truncated": False,
                "items": [
                    {
                        "target": "w1",
                        "msg_id": "msg-w1",
                        "status": "responded",
                        "response_received": True,
                        "response_chars": 20,
                        "received_ts": "2026-04-27T00:00:00.000000Z",
                        "synthetic": False,
                        "terminal_reason": "",
                    },
                    {
                        "target": "w2",
                        "msg_id": "msg-w2",
                        "status": "pending",
                        "response_received": False,
                        "response_chars": 0,
                        "received_ts": "",
                        "synthetic": False,
                        "terminal_reason": "",
                    },
                ],
            },
            "aggregate_response_watchdog": {
                "wake_id": "wake-cli",
                "phase": "response",
                "deadline": "2026-04-27T00:05:00.000000Z",
            },
        }, ""

    bas.send_command = send_command
    return calls


def scenario_wait_status_cli_summary_and_json(label: str, tmpdir: Path) -> None:
    bws = _import_wait_status_module()
    response = {
        "ok": True,
        "bridge_session": "test-session",
        "caller": "alice",
        "generated_ts": "2026-04-27T00:00:00.000000Z",
        "limits": {"per_section": 50},
        "summary": {"pending_inbound": {"total_count": 2, "returned_count": 2, "truncated": False}},
        "pending_inbound": {"total_count": 2, "returned_count": 2, "truncated": False, "items": [{"message_id": "msg-1"}, {"message_id": "msg-2"}]},
    }
    calls = _patch_wait_status_for_unit(bws, response=response)

    code_summary, out_summary, err_summary = _run_wait_status_main(bws, ["--summary", "--session", "test-session", "--from", "alice", "--allow-spoof"])
    summary = json.loads(out_summary)
    assert_true(code_summary == 0 and err_summary == "", f"{label}: --summary should succeed cleanly: code={code_summary} err={err_summary!r}")
    assert_true(summary.get("summary") == response["summary"] and "pending_inbound" not in summary, f"{label}: --summary should omit full sections: {summary}")

    code_json, out_json, err_json = _run_wait_status_main(bws, ["--json", "--session", "test-session", "--from", "alice", "--allow-spoof"])
    full = json.loads(out_json)
    assert_true(code_json == 0 and err_json == "", f"{label}: --json should succeed cleanly: code={code_json} err={err_json!r}")
    assert_true(full.get("pending_inbound", {}).get("items") == [{"message_id": "msg-1"}, {"message_id": "msg-2"}], f"{label}: --json should preserve full response: {full}")
    assert_true(calls == [("test-session", {"op": "wait_status", "from": "alice"}), ("test-session", {"op": "wait_status", "from": "alice"})], f"{label}: daemon command calls mismatch: {calls}")
    print(f"  PASS  {label}")


def scenario_wait_status_cli_unsupported_old_daemon(label: str, tmpdir: Path) -> None:
    bws = _import_wait_status_module()
    _patch_wait_status_for_unit(bws, error="unsupported command")

    code, out, err = _run_wait_status_main(bws, ["--session", "test-session", "--from", "alice", "--allow-spoof"])

    assert_true(code == 1 and out == "", f"{label}: unsupported daemon should exit 1 with no stdout: code={code} out={out!r}")
    assert_true("does not support wait_status yet" in err and "Reload/restart" in err, f"{label}: unsupported daemon guidance missing: {err!r}")
    print(f"  PASS  {label}")


def scenario_wait_status_cli_rejects_inactive_sender(label: str, tmpdir: Path) -> None:
    bws = _import_wait_status_module()
    calls = _patch_wait_status_for_unit(bws, state=_participants_state(["bob"]))

    code, out, err = _run_wait_status_main(bws, ["--session", "test-session", "--from", "alice", "--allow-spoof"])

    assert_true(code == 2 and out == "", f"{label}: inactive sender should be rejected before socket call: code={code} out={out!r}")
    assert_true("sender 'alice' is not active" in err, f"{label}: inactive sender error should name alias: {err!r}")
    assert_true(calls == [], f"{label}: daemon should not be contacted after local identity rejection: {calls}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_cli_summary_and_json(label: str, tmpdir: Path) -> None:
    bas = _import_aggregate_status_module()
    calls = _patch_aggregate_status_for_unit(bas)

    code_summary, out_summary, err_summary = _run_aggregate_status_main(bas, ["agg-cli", "--summary", "--session", "test-session", "--from", "manager", "--allow-spoof"])
    summary = json.loads(out_summary)
    assert_true(code_summary == 0 and err_summary == "", f"{label}: --summary should succeed cleanly: code={code_summary} err={err_summary!r}")
    summary_text = json.dumps(summary, ensure_ascii=True)
    assert_true("response_chars" not in summary_text and "received_ts" not in summary_text and "deadline" not in summary_text, f"{label}: summary should omit detail timestamps/sizes: {summary}")
    assert_true(summary.get("aggregate_response_watchdog") == {"present": True}, f"{label}: summary should expose watchdog presence only: {summary}")

    code_json, out_json, err_json = _run_aggregate_status_main(bas, ["agg-cli", "--json", "--session", "test-session", "--from", "manager", "--allow-spoof"])
    full = json.loads(out_json)
    assert_true(code_json == 0 and err_json == "", f"{label}: --json should succeed cleanly: code={code_json} err={err_json!r}")
    assert_true(full.get("legs", {}).get("items", [{}])[0].get("response_chars") == 20, f"{label}: full JSON should preserve safe response metadata: {full}")
    assert_true((full.get("aggregate_response_watchdog") or {}).get("wake_id") == "wake-cli", f"{label}: full JSON should preserve watchdog correlation id: {full}")
    assert_true(calls == [
        ("test-session", {"op": "aggregate_status", "from": "manager", "aggregate_id": "agg-cli"}),
        ("test-session", {"op": "aggregate_status", "from": "manager", "aggregate_id": "agg-cli"}),
    ], f"{label}: daemon command calls mismatch: {calls}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_cli_unsupported_and_not_found_text(label: str, tmpdir: Path) -> None:
    bas = _import_aggregate_status_module()
    _patch_aggregate_status_for_unit(bas, error="unsupported command")
    code_old, out_old, err_old = _run_aggregate_status_main(bas, ["agg-cli", "--session", "test-session", "--from", "manager", "--allow-spoof"])
    assert_true(code_old == 1 and out_old == "", f"{label}: unsupported daemon should exit 1 with no stdout: code={code_old} out={out_old!r}")
    assert_true("does not support aggregate_status yet" in err_old and "Reload/restart" in err_old, f"{label}: unsupported daemon guidance missing: {err_old!r}")

    bas = _import_aggregate_status_module()
    _patch_aggregate_status_for_unit(bas, error="aggregate_not_found")
    code_nf, out_nf, err_nf = _run_aggregate_status_main(bas, ["agg-private", "--session", "test-session", "--from", "manager", "--allow-spoof"])
    assert_true(code_nf == 1 and out_nf == "", f"{label}: not-found should exit 1 with no stdout: code={code_nf} out={out_nf!r}")
    assert_true("not found for your sender alias" in err_nf and "not yours" in err_nf, f"{label}: not-found text should cover foreign/unknown without distinguishing: {err_nf!r}")
    print(f"  PASS  {label}")


def scenario_aggregate_status_cli_rejects_inactive_sender(label: str, tmpdir: Path) -> None:
    bas = _import_aggregate_status_module()
    calls = _patch_aggregate_status_for_unit(bas, state=_participants_state(["w1"]))

    code, out, err = _run_aggregate_status_main(bas, ["agg-cli", "--session", "test-session", "--from", "manager", "--allow-spoof"])

    assert_true(code == 2 and out == "", f"{label}: inactive sender should be rejected before socket call: code={code} out={out!r}")
    assert_true("sender 'manager' is not active" in err, f"{label}: inactive sender error should name alias: {err!r}")
    assert_true(calls == [], f"{label}: daemon should not be contacted after local identity rejection: {calls}")
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
    assert_true(code0 == 0 and out0 == "wake-0\n", f"{label}: zero alarm should print wake id only to stdout: code={code0} out={out0!r} err={err0!r}")
    assert_true(code1 == 0 and out1 == "wake-2.5\n", f"{label}: positive alarm should print wake id only to stdout: code={code1} out={out1!r} err={err1!r}")
    for wake_id, err in (("wake-0", err0), ("wake-2.5", err1)):
        for token in ("ALARM_SET", "[bridge:*]", "do not sleep/poll"):
            assert_true(token in err, f"{label}: alarm success hint missing token {token!r}: {err!r}")
        assert_true(wake_id not in err, f"{label}: wake id must remain stdout-only: {err!r}")
    assert_true(calls == [("test-session", "alice", 0.0, "now"), ("test-session", "alice", 2.5, None)], f"{label}: request_alarm calls mismatch: {calls}")
    print(f"  PASS  {label}")


def scenario_alarm_request_failure_prints_no_success_hint(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    ba.resolve_caller_from_pane = lambda **kwargs: bridge_identity.CallerResolution(True, "test-session", "alice")  # type: ignore[assignment]
    ba.ensure_daemon_running = lambda session: ""
    ba.room_status = lambda session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")
    ba.load_session = lambda session: _participants_state(["alice", "bob"])
    ba.request_alarm = lambda *args, **kwargs: (False, "", "daemon rejected alarm")
    code, out, err = _run_alarm_main(ba, ["3", "--session", "test-session", "--from", "alice", "--allow-spoof"])
    assert_true(code == 1, f"{label}: daemon rejection should return 1, got {code}")
    assert_true(out == "", f"{label}: failed alarm must not print stdout wake id: {out!r}")
    assert_true("daemon rejected alarm" in err, f"{label}: failure stderr should explain daemon error: {err!r}")
    assert_true("ALARM_SET" not in err and "do not sleep/poll" not in err, f"{label}: alarm success hint must not print on failure: {err!r}")
    print(f"  PASS  {label}")


def scenario_alarm_request_retries_timeout_with_stable_wake_id(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    run_dir = tmpdir / "run"
    run_dir.mkdir()
    socket_path = run_dir / "test-session.sock"
    received: list[dict] = []
    ready = threading.Event()

    def read_payload(conn: socket.socket) -> dict:
        raw = b""
        while b"\n" not in raw:
            chunk = conn.recv(65536)
            if not chunk:
                break
            raw += chunk
        payload = json.loads(raw.decode("utf-8"))
        received.append(payload)
        return payload

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(socket_path))
            srv.listen(4)
            ready.set()
            conn, _ = srv.accept()
            with conn:
                first = read_payload(conn)
                time.sleep(0.18)
                try:
                    conn.sendall((json.dumps({"ok": True, "wake_id": first["wake_id"]}) + "\n").encode("utf-8"))
                except OSError:
                    pass
            conn, _ = srv.accept()
            with conn:
                second = read_payload(conn)
                conn.sendall((json.dumps({"ok": True, "wake_id": second["wake_id"]}) + "\n").encode("utf-8"))

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert_true(ready.wait(1.0), f"{label}: socket server did not start")
    ba.run_root = lambda: run_dir  # type: ignore[assignment]
    ba.short_id = lambda prefix: "wake-111111111111"  # type: ignore[assignment]
    ba.ALARM_SOCKET_TIMEOUT_SECONDS = 0.15
    ba.ALARM_SOCKET_MAX_ATTEMPTS = 3
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        ok, wake_id, error = ba.request_alarm("test-session", "alice", 600.0, "follow up")
    thread.join(1.0)
    assert_true(ok and wake_id == "wake-111111111111" and error == "", f"{label}: retry should succeed with stable wake id: {(ok, wake_id, error)}")
    assert_true(not thread.is_alive(), f"{label}: socket server thread should finish")
    assert_true(len(received) == 2, f"{label}: expected first timeout plus one retry, got {received}")
    assert_true(received[0].get("wake_id") == received[1].get("wake_id") == "wake-111111111111", f"{label}: retry must reuse wake id: {received}")
    assert_true("retrying (2/3)" in stderr.getvalue() and "wake-111111111111" in stderr.getvalue(), f"{label}: retry breadcrumb should include wake id: {stderr.getvalue()!r}")
    print(f"  PASS  {label}")


def scenario_alarm_request_retry_exhaustion_is_bounded(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    run_dir = tmpdir / "run"
    run_dir.mkdir()
    socket_path = run_dir / "test-session.sock"
    received: list[dict] = []
    ready = threading.Event()

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(socket_path))
            srv.listen(4)
            ready.set()
            for _ in range(2):
                conn, _ = srv.accept()
                with conn:
                    raw = b""
                    while b"\n" not in raw:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                    received.append(json.loads(raw.decode("utf-8")))
                    time.sleep(0.12)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert_true(ready.wait(1.0), f"{label}: socket server did not start")
    ba.run_root = lambda: run_dir  # type: ignore[assignment]
    ba.short_id = lambda prefix: "wake-222222222222"  # type: ignore[assignment]
    ba.ALARM_SOCKET_TIMEOUT_SECONDS = 0.05
    ba.ALARM_SOCKET_MAX_ATTEMPTS = 2
    start = time.monotonic()
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        ok, wake_id, error = ba.request_alarm("test-session", "alice", 600.0, "never replies")
    elapsed = time.monotonic() - start
    thread.join(1.0)
    assert_true(not ok and wake_id == "", f"{label}: exhausted retry should fail without stdout wake id: {(ok, wake_id, error)}")
    assert_true(elapsed < 1.0, f"{label}: retry exhaustion should be bounded, elapsed={elapsed:.3f}s")
    assert_true("after 2 attempts" in error and "wake-222222222222" in error and "timed out" in error, f"{label}: error must include attempts, wake id, timeout: {error!r}")
    assert_true(len(received) == 2, f"{label}: server should receive bounded attempts only: {received}")
    assert_true({item.get("wake_id") for item in received} == {"wake-222222222222"}, f"{label}: all attempts should use stable wake id: {received}")
    print(f"  PASS  {label}")


def scenario_alarm_request_retries_lock_wait_with_stable_wake_id(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    run_dir = tmpdir / "run"
    run_dir.mkdir()
    socket_path = run_dir / "test-session.sock"
    received: list[dict] = []
    ready = threading.Event()

    def read_payload(conn: socket.socket) -> dict:
        raw = b""
        while b"\n" not in raw:
            chunk = conn.recv(65536)
            if not chunk:
                break
            raw += chunk
        payload = json.loads(raw.decode("utf-8"))
        received.append(payload)
        return payload

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(socket_path))
            srv.listen(4)
            ready.set()
            conn, _ = srv.accept()
            with conn:
                read_payload(conn)
                conn.sendall((json.dumps({"ok": False, "error": "lock_wait_exceeded", "error_kind": "lock_wait_exceeded"}) + "\n").encode("utf-8"))
            conn, _ = srv.accept()
            with conn:
                second = read_payload(conn)
                conn.sendall((json.dumps({"ok": True, "wake_id": second["wake_id"]}) + "\n").encode("utf-8"))

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert_true(ready.wait(1.0), f"{label}: socket server did not start")
    ba.run_root = lambda: run_dir  # type: ignore[assignment]
    ba.short_id = lambda prefix: "wake-555555555555"  # type: ignore[assignment]
    ba.ALARM_SOCKET_TIMEOUT_SECONDS = 0.15
    ba.ALARM_SOCKET_MAX_ATTEMPTS = 3
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        ok, wake_id, error = ba.request_alarm("test-session", "alice", 600.0, "lock retry")
    thread.join(1.0)
    assert_true(ok and wake_id == "wake-555555555555" and error == "", f"{label}: lock wait retry should succeed: {(ok, wake_id, error)}")
    assert_true(not thread.is_alive(), f"{label}: socket server thread should finish")
    assert_true(len(received) == 2, f"{label}: expected lock wait plus one retry, got {received}")
    assert_true(received[0].get("wake_id") == received[1].get("wake_id") == "wake-555555555555", f"{label}: retry must reuse wake id: {received}")
    assert_true("lock_wait_exceeded" in stderr.getvalue() and "retrying (2/3)" in stderr.getvalue(), f"{label}: retry breadcrumb should explain lock wait: {stderr.getvalue()!r}")
    print(f"  PASS  {label}")


def scenario_alarm_request_lock_wait_retry_exhaustion_is_bounded(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    run_dir = tmpdir / "run"
    run_dir.mkdir()
    socket_path = run_dir / "test-session.sock"
    received: list[dict] = []
    ready = threading.Event()

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(socket_path))
            srv.listen(4)
            ready.set()
            for _ in range(2):
                conn, _ = srv.accept()
                with conn:
                    raw = b""
                    while b"\n" not in raw:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                    received.append(json.loads(raw.decode("utf-8")))
                    conn.sendall((json.dumps({"ok": False, "error": "lock_wait_exceeded", "error_kind": "lock_wait_exceeded"}) + "\n").encode("utf-8"))

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert_true(ready.wait(1.0), f"{label}: socket server did not start")
    ba.run_root = lambda: run_dir  # type: ignore[assignment]
    ba.short_id = lambda prefix: "wake-666666666666"  # type: ignore[assignment]
    ba.ALARM_SOCKET_TIMEOUT_SECONDS = 0.15
    ba.ALARM_SOCKET_MAX_ATTEMPTS = 2
    start = time.monotonic()
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        ok, wake_id, error = ba.request_alarm("test-session", "alice", 600.0, "lock exhausted")
    elapsed = time.monotonic() - start
    thread.join(1.0)
    assert_true(not ok and wake_id == "", f"{label}: exhausted lock retry should fail without stdout wake id: {(ok, wake_id, error)}")
    assert_true(elapsed < 1.0, f"{label}: lock retry exhaustion should be bounded, elapsed={elapsed:.3f}s")
    assert_true("after 2 attempts" in error and "wake-666666666666" in error and "lock_wait_exceeded" in error, f"{label}: error must include attempts, wake id, lock wait: {error!r}")
    assert_true(len(received) == 2, f"{label}: server should receive bounded attempts only: {received}")
    assert_true({item.get("wake_id") for item in received} == {"wake-666666666666"}, f"{label}: all attempts should use stable wake id: {received}")
    assert_true("retrying (2/2)" in stderr.getvalue(), f"{label}: retry breadcrumb should include final attempt count: {stderr.getvalue()!r}")
    print(f"  PASS  {label}")


def scenario_alarm_request_non_retryable_daemon_error_is_immediate(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    run_dir = tmpdir / "run"
    run_dir.mkdir()
    socket_path = run_dir / "test-session.sock"
    received: list[dict] = []
    ready = threading.Event()

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(socket_path))
            srv.listen(1)
            ready.set()
            conn, _ = srv.accept()
            with conn:
                raw = b""
                while b"\n" not in raw:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                received.append(json.loads(raw.decode("utf-8")))
                conn.sendall((json.dumps({"ok": False, "error": "wake_id conflict"}) + "\n").encode("utf-8"))

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert_true(ready.wait(1.0), f"{label}: socket server did not start")
    ba.run_root = lambda: run_dir  # type: ignore[assignment]
    ba.short_id = lambda prefix: "wake-777777777777"  # type: ignore[assignment]
    ba.ALARM_SOCKET_TIMEOUT_SECONDS = 0.15
    ba.ALARM_SOCKET_MAX_ATTEMPTS = 3
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        ok, wake_id, error = ba.request_alarm("test-session", "alice", 600.0, "conflict")
    thread.join(1.0)
    assert_true(not ok and wake_id == "" and error == "wake_id conflict", f"{label}: non-retryable error should return as-is: {(ok, wake_id, error)}")
    assert_true(not thread.is_alive(), f"{label}: socket server thread should finish")
    assert_true(len(received) == 1, f"{label}: non-retryable error must not retry: {received}")
    assert_true("retrying" not in stderr.getvalue(), f"{label}: non-retryable error must not print retry breadcrumb: {stderr.getvalue()!r}")
    print(f"  PASS  {label}")


def scenario_alarm_request_rejects_mismatched_daemon_wake_id(label: str, tmpdir: Path) -> None:
    ba = _import_alarm_module()
    run_dir = tmpdir / "run"
    run_dir.mkdir()
    socket_path = run_dir / "test-session.sock"
    ready = threading.Event()

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(socket_path))
            srv.listen(1)
            ready.set()
            conn, _ = srv.accept()
            with conn:
                raw = b""
                while b"\n" not in raw:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                conn.sendall((json.dumps({"ok": True, "wake_id": "wake-333333333333"}) + "\n").encode("utf-8"))

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert_true(ready.wait(1.0), f"{label}: socket server did not start")
    ba.run_root = lambda: run_dir  # type: ignore[assignment]
    ba.short_id = lambda prefix: "wake-444444444444"  # type: ignore[assignment]
    ok, wake_id, error = ba.request_alarm("test-session", "alice", 600.0, "old daemon")
    thread.join(1.0)
    assert_true(not ok and wake_id == "", f"{label}: mismatched wake id should fail: {(ok, wake_id, error)}")
    assert_true("wake-333333333333" in error and "wake-444444444444" in error and "reload" in error, f"{label}: mismatch error should mention both ids and reload: {error!r}")
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


def scenario_daemon_socket_alarm_op_idempotent_wake_id(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    payload = {"op": "alarm", "from": "claude", "delay_seconds": 600.0, "body": "plan review", "wake_id": "wake-aaaaaaaaaaaa"}
    first = _daemon_command_result(d, dict(payload))
    second = _daemon_command_result(d, dict(payload))
    assert_true(first.get("ok") is True and first.get("wake_id") == "wake-aaaaaaaaaaaa", f"{label}: first alarm should register: {first}")
    assert_true(second.get("ok") is True and second.get("wake_id") == "wake-aaaaaaaaaaaa", f"{label}: duplicate alarm should be idempotent: {second}")
    assert_true(second.get("alarm_status") == "active" and second.get("duplicate") is True, f"{label}: duplicate should report active replay: {second}")
    assert_true(list(d.watchdogs.keys()) == ["wake-aaaaaaaaaaaa"], f"{label}: duplicate must not create another watchdog: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_daemon_socket_alarm_op_rejects_invalid_conflicting_wake_id(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    assert_true(
        bridge_daemon.ALARM_CLIENT_WAKE_ID_RE.fullmatch(bridge_daemon.short_id("wake")) is not None,
        f"{label}: daemon wake_id regex must match bridge_util.short_id('wake')",
    )
    invalid = _daemon_command_result(d, {"op": "alarm", "from": "claude", "delay_seconds": 600.0, "wake_id": "wake-nothex"})
    assert_true(invalid.get("ok") is False and "wake_id" in str(invalid.get("error") or ""), f"{label}: invalid wake_id must reject: {invalid}")
    assert_true(not d.watchdogs and not d.alarm_wake_tombstones, f"{label}: invalid wake_id must not mutate state")
    uppercase = _daemon_command_result(d, {"op": "alarm", "from": "claude", "delay_seconds": 600.0, "wake_id": "wake-ABCDEFABCDEF"})
    assert_true(uppercase.get("ok") is False and "wake_id" in str(uppercase.get("error") or ""), f"{label}: uppercase wake_id must reject: {uppercase}")
    assert_true(not d.watchdogs and not d.alarm_wake_tombstones, f"{label}: uppercase wake_id must not mutate state")

    d.watchdogs["wake-bbbbbbbbbbbb"] = {
        "sender": "claude",
        "deadline": time.time() + 600.0,
        "watchdog_phase": bridge_daemon.WATCHDOG_PHASE_RESPONSE,
        "ref_message_id": "msg-existing",
        "is_alarm": False,
    }
    before = dict(d.watchdogs)
    non_alarm_conflict = _daemon_command_result(d, {"op": "alarm", "from": "claude", "delay_seconds": 600.0, "wake_id": "wake-bbbbbbbbbbbb"})
    assert_true(non_alarm_conflict.get("ok") is False and "conflict" in str(non_alarm_conflict.get("error") or ""), f"{label}: non-alarm id conflict must reject: {non_alarm_conflict}")
    assert_true(d.watchdogs == before, f"{label}: non-alarm conflict must not mutate state")

    good = _daemon_command_result(d, {"op": "alarm", "from": "codex", "delay_seconds": 600.0, "body": "owned", "wake_id": "wake-cccccccccccc"})
    assert_true(good.get("ok") is True, f"{label}: owner alarm should register: {good}")
    before = dict(d.watchdogs)
    sender_conflict = _daemon_command_result(d, {"op": "alarm", "from": "claude", "delay_seconds": 600.0, "body": "owned", "wake_id": "wake-cccccccccccc"})
    assert_true(sender_conflict.get("ok") is False and "conflict" in str(sender_conflict.get("error") or ""), f"{label}: different sender id conflict must reject: {sender_conflict}")
    assert_true(d.watchdogs == before, f"{label}: sender conflict must not mutate state")

    metadata_conflict = _daemon_command_result(d, {"op": "alarm", "from": "codex", "delay_seconds": 601.0, "body": "owned", "wake_id": "wake-cccccccccccc"})
    assert_true(metadata_conflict.get("ok") is False and "conflict" in str(metadata_conflict.get("error") or ""), f"{label}: changed delay conflict must reject: {metadata_conflict}")
    assert_true(d.watchdogs == before, f"{label}: metadata conflict must not mutate state")
    conflict_events = [e for e in read_events(tmpdir / "events.raw.jsonl") if e.get("event") == "alarm_register_conflict"]
    reasons = {str(e.get("reason") or "") for e in conflict_events}
    assert_true({"existing_non_alarm", "sender_mismatch", "metadata_mismatch"}.issubset(reasons), f"{label}: conflicts should be logged with reasons: {conflict_events}")
    print(f"  PASS  {label}")


def scenario_daemon_socket_alarm_op_idempotent_after_cancelled_or_fired(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)

    cancel_payload = {"op": "alarm", "from": "claude", "delay_seconds": 600.0, "body": "cancel me", "wake_id": "wake-dddddddddddd"}
    first = _daemon_command_result(d, dict(cancel_payload))
    assert_true(first.get("ok") is True, f"{label}: alarm should register before cancel: {first}")
    d.enqueue_ipc_message(_qualifying_message("codex", "claude", kind="notice", body="done"))
    assert_true("wake-dddddddddddd" not in d.watchdogs, f"{label}: incoming notice should cancel alarm")
    cancel_replay = _daemon_command_result(d, dict(cancel_payload))
    assert_true(cancel_replay.get("ok") is True and cancel_replay.get("alarm_status") == "registered_then_cancelled", f"{label}: retry after cancel should not rearm: {cancel_replay}")
    assert_true("wake-dddddddddddd" not in d.watchdogs, f"{label}: cancelled replay must not rearm")

    fired_payload = {"op": "alarm", "from": "claude", "delay_seconds": 0.0, "body": "fire me", "wake_id": "wake-eeeeeeeeeeee"}
    fired_first = _daemon_command_result(d, dict(fired_payload))
    assert_true(fired_first.get("ok") is True, f"{label}: alarm should register before fire: {fired_first}")
    d.fire_watchdog("wake-eeeeeeeeeeee", dict(d.watchdogs["wake-eeeeeeeeeeee"]))
    assert_true("wake-eeeeeeeeeeee" not in d.watchdogs, f"{label}: fired alarm should be removed")
    fired_replay = _daemon_command_result(d, dict(fired_payload))
    assert_true(fired_replay.get("ok") is True and fired_replay.get("alarm_status") == "fired", f"{label}: retry after fire should not rearm: {fired_replay}")
    assert_true("wake-eeeeeeeeeeee" not in d.watchdogs, f"{label}: fired replay must not rearm")

    expired_payload = {"op": "alarm", "from": "claude", "delay_seconds": 600.0, "body": "expired tombstone", "wake_id": "wake-ffffffffffff"}
    first_expired = _daemon_command_result(d, dict(expired_payload))
    assert_true(first_expired.get("ok") is True, f"{label}: alarm should register before expired tombstone setup: {first_expired}")
    removed = d.watchdogs.pop("wake-ffffffffffff")
    d._record_alarm_wake_tombstone("wake-ffffffffffff", removed, "registered_then_cancelled")
    d.alarm_wake_tombstones["wake-ffffffffffff"]["expires_at"] = time.time() - 1.0
    expired_replay = _daemon_command_result(d, dict(expired_payload))
    assert_true(expired_replay.get("ok") is True and expired_replay.get("alarm_status") == "registered", f"{label}: expired tombstone should allow fresh registration: {expired_replay}")
    assert_true("wake-ffffffffffff" in d.watchdogs, f"{label}: expired tombstone replay should re-register")
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


def scenario_daemon_enqueue_rejects_no_auto_return_watchdog_atomically(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "agent_type": "claude", "pane": "%99"}, "codex": {"alias": "codex", "agent_type": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    good = test_message("msg-good-auto", frm="claude", to="codex", status="ingressing")
    good["watchdog_delay_sec"] = 30.0
    bad = test_message("msg-bad-noauto", frm="claude", to="codex", status="ingressing")
    bad["auto_return"] = False
    bad["watchdog_delay_sec"] = 30.0
    result = _daemon_command_result(d, {"op": "enqueue", "messages": [good, bad]})
    assert_true(result.get("ok") is False, f"{label}: malformed batch should reject: {result}")
    assert_true(result.get("error_kind") == "watchdog_requires_auto_return", f"{label}: stable error_kind expected: {result}")
    assert_true("watchdog requires auto_return" in str(result.get("error") or ""), f"{label}: error text should explain requirement: {result}")
    assert_true(d.queue.read() == [], f"{label}: batch rejection must be atomic: {d.queue.read()}")
    print(f"  PASS  {label}")


def scenario_daemon_enqueue_no_auto_return_watchdog_zero_normalized(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "agent_type": "claude", "pane": "%99"}, "codex": {"alias": "codex", "agent_type": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-noauto-zero", frm="claude", to="codex", status="ingressing")
    msg["auto_return"] = False
    msg["watchdog_delay_sec"] = 0.0
    result = _daemon_command_result(d, {"op": "enqueue", "messages": [msg]})
    assert_true(result.get("ok") is True, f"{label}: watchdog 0 should be accepted as disable: {result}")
    queue = d.queue.read()
    assert_true(len(queue) == 1 and queue[0].get("status") == "pending", f"{label}: row should be queued pending: {queue}")
    assert_true(queue[0].get("auto_return") is False, f"{label}: auto_return false should be preserved: {queue}")
    assert_true("watchdog_delay_sec" not in queue[0], f"{label}: watchdog 0 metadata should be normalized away: {queue}")
    assert_true(not d.watchdogs, f"{label}: no watchdog should be registered: {d.watchdogs}")
    print(f"  PASS  {label}")


def _prior_hint_daemon(tmpdir: Path) -> bridge_daemon.BridgeDaemon:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
        "carol": {"alias": "carol", "agent_type": "codex", "pane": "%93"},
        "dana": {"alias": "dana", "agent_type": "codex", "pane": "%94"},
    }
    return make_daemon(tmpdir, participants)


def _new_enqueue_message(message_id: str, *, sender: str = "alice", target: str = "bob", kind: str = "request") -> dict:
    msg = test_message(message_id, frm=sender, to=target, status="ingressing")
    msg["kind"] = kind
    msg["auto_return"] = kind == "request"
    return msg


def _hint_entries(result: dict) -> list[dict]:
    hints = result.get("hints") or []
    assert_true(isinstance(hints, list), f"expected hints list, got {hints!r}")
    return hints


def _single_prior_hint(label: str, result: dict) -> dict:
    hints = _hint_entries(result)
    assert_true(len(hints) == 1, f"{label}: expected one prior hint, got {hints!r} in {result}")
    return hints[0]


def scenario_daemon_prior_hint_pending_cancel(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.queue.update(lambda queue: queue.append(test_message("msg-prior-pending", frm="alice", to="bob", status="pending")) or None)

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-pending")])

    assert_true(result.get("ok") is True and result.get("ids") == ["msg-new-pending"], f"{label}: enqueue result mismatch: {result}")
    hint = _single_prior_hint(label, result)
    text = str(hint.get("text") or "")
    assert_true(hint.get("prior_kind") == "cancel" and hint.get("prior_status") == "pending", f"{label}: expected cancel pending hint: {hint}")
    assert_true("PRIOR_MESSAGE_HINT: bob still has your earlier message msg-prior-pending queued" in text, f"{label}: cancel text mismatch: {text!r}")
    assert_true("agent_cancel_message msg-prior-pending" in text and "otherwise this send simply queues behind it" in text, f"{label}: cancel action/tail missing: {text!r}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_delivered_interrupt(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.queue.update(lambda queue: queue.append(test_message("msg-prior-delivered", frm="alice", to="bob", status="delivered")) or None)

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-delivered")])

    hint = _single_prior_hint(label, result)
    text = str(hint.get("text") or "")
    assert_true(hint.get("prior_kind") == "interrupt" and hint.get("prior_status") == "delivered", f"{label}: expected interrupt delivered hint: {hint}")
    assert_true("agent_interrupt_peer bob --status" in text and "agent_interrupt_peer bob" in text, f"{label}: interrupt action missing: {text!r}")
    assert_true("so this new message can deliver" not in text, f"{label}: unsafe wording must stay absent: {text!r}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_inflight_pane_mode_cancel(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    prior = test_message("msg-prior-pane-mode", frm="alice", to="bob", status="inflight")
    prior["pane_mode_enter_deferred_since_ts"] = time.time()
    d.last_enter_ts["msg-prior-pane-mode"] = time.time()
    d.queue.update(lambda queue: queue.append(prior) or None)

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-pane-mode")])

    hint = _single_prior_hint(label, result)
    assert_true(hint.get("prior_kind") == "cancel" and hint.get("prior_status") == "inflight", f"{label}: pane-mode inflight should be cancellable: {hint}")
    assert_true("agent_cancel_message msg-prior-pane-mode" in str(hint.get("text") or ""), f"{label}: cancel command missing: {hint}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_inflight_after_enter_interrupt(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    prior = test_message("msg-prior-active-inflight", frm="alice", to="bob", status="inflight")
    d.last_enter_ts["msg-prior-active-inflight"] = time.time()
    d.queue.update(lambda queue: queue.append(prior) or None)

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-active-inflight")])

    hint = _single_prior_hint(label, result)
    assert_true(hint.get("prior_kind") == "interrupt" and hint.get("prior_status") == "inflight", f"{label}: post-enter inflight should use interrupt: {hint}")
    assert_true("agent_interrupt_peer bob --status" in str(hint.get("text") or ""), f"{label}: interrupt status check missing: {hint}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_terminal_or_absent_no_hint(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.queue.update(lambda queue: queue.append(test_message("msg-prior-cancelled", frm="alice", to="bob", status="cancelled")) or None)

    terminal_result = d.handle_enqueue_command([_new_enqueue_message("msg-new-terminal")])
    absent_result = d.handle_enqueue_command([_new_enqueue_message("msg-new-absent", target="dana")])

    assert_true(_hint_entries(terminal_result) == [], f"{label}: terminal row must not hint: {terminal_result}")
    assert_true(_hint_entries(absent_result) == [], f"{label}: absent prior must not hint: {absent_result}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_rejections_have_no_hints(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    _set_response_context(d, "bob", "alice")
    d.queue.update(lambda queue: queue.append(test_message("msg-prior-for-reject", frm="bob", to="alice", status="pending")) or None)
    guard_result = d.handle_enqueue_command([_new_enqueue_message("msg-new-guard", sender="bob", target="alice")])
    assert_true(guard_result.get("ok") is False and guard_result.get("error_kind") == "response_send_guard", f"{label}: guard should reject: {guard_result}")
    assert_true("hints" not in guard_result and _hint_entries(guard_result) == [], f"{label}: guard rejection must not carry hints: {guard_result}")

    d2 = _prior_hint_daemon(tmpdir / "watchdog")
    d2.queue.update(lambda queue: queue.append(test_message("msg-prior-watchdog", frm="alice", to="bob", status="pending")) or None)
    bad = _new_enqueue_message("msg-new-watchdog")
    bad["auto_return"] = False
    bad["watchdog_delay_sec"] = 30.0
    watchdog_result = d2.handle_enqueue_command([bad])
    assert_true(watchdog_result.get("ok") is False and watchdog_result.get("error_kind") == "watchdog_requires_auto_return", f"{label}: watchdog should reject: {watchdog_result}")
    assert_true("hints" not in watchdog_result and _hint_entries(watchdog_result) == [], f"{label}: watchdog rejection must not carry hints: {watchdog_result}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_aggregate_suffix(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    prior = test_message("msg-prior-agg", frm="alice", to="bob", status="delivered")
    prior["aggregate_id"] = "agg-prior"
    prior["aggregate_expected"] = ["bob", "carol"]
    prior["aggregate_message_ids"] = {"bob": "msg-prior-agg", "carol": "msg-prior-agg-carol"}
    d.queue.update(lambda queue: queue.append(prior) or None)

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-agg")])

    hint = _single_prior_hint(label, result)
    text = str(hint.get("text") or "")
    assert_true(hint.get("prior_aggregate_id") == "agg-prior", f"{label}: structured aggregate id missing: {hint}")
    assert_true("only this leg of aggregate agg-prior" in text and "fresh aggregate" in text, f"{label}: aggregate suffix missing: {text!r}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_notice_safe_wording(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.queue.update(lambda queue: queue.append(test_message("msg-prior-notice", frm="alice", to="bob", status="delivered")) or None)

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-notice", kind="notice")])

    text = str(_single_prior_hint(label, result).get("text") or "")
    assert_true("your queued follow-up will wait behind it" in text, f"{label}: notice-safe tail missing: {text!r}")
    assert_true("so this new message can deliver" not in text, f"{label}: unsafe delivery wording must stay absent: {text!r}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_same_pair_only(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.queue.update(
        lambda queue: (
            queue.append(test_message("msg-prior-other-target", frm="alice", to="carol", status="pending")),
            queue.append(test_message("msg-prior-other-sender", frm="carol", to="bob", status="pending")),
            None,
        )[-1]
    )

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-same-pair")])

    assert_true(_hint_entries(result) == [], f"{label}: different sender/target rows must not hint: {result}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_multi_target_partial(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.queue.update(
        lambda queue: (
            queue.append(test_message("msg-prior-bob", frm="alice", to="bob", status="pending")),
            queue.append(test_message("msg-prior-carol", frm="alice", to="carol", status="delivered")),
            None,
        )[-1]
    )
    messages = [
        _new_enqueue_message("msg-new-bob", target="bob"),
        _new_enqueue_message("msg-new-carol", target="carol"),
        _new_enqueue_message("msg-new-dana", target="dana"),
    ]

    result = d.handle_enqueue_command(messages)

    hints = _hint_entries(result)
    assert_true(len(hints) == 2, f"{label}: expected exactly two affected-target hints: {hints}")
    by_target = {hint.get("target"): hint for hint in hints}
    assert_true(set(by_target) == {"bob", "carol"}, f"{label}: hints should belong to bob/carol only: {hints}")
    assert_true(by_target["bob"].get("prior_kind") == "cancel" and by_target["carol"].get("prior_kind") == "interrupt", f"{label}: per-target classes mismatch: {hints}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_current_prompt_only(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.current_prompt_by_agent["bob"] = {
        "id": "msg-prior-active-context",
        "from": "alice",
        "kind": "request",
        "auto_return": True,
        "aggregate_id": "",
    }

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-context")])

    hint = _single_prior_hint(label, result)
    assert_true(hint.get("prior_kind") == "interrupt" and hint.get("prior_status") == "active", f"{label}: current prompt should produce active interrupt hint: {hint}")
    assert_true("msg-prior-active-context" in str(hint.get("text") or ""), f"{label}: active context id missing from hint: {hint}")
    print(f"  PASS  {label}")


def scenario_daemon_prior_hint_active_ctx_beats_pending_queue_row(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    d.queue.update(lambda queue: queue.append(test_message("msg-prior-pending-mask", frm="alice", to="bob", status="pending")) or None)
    d.current_prompt_by_agent["bob"] = {
        "id": "msg-prior-active-wins",
        "from": "alice",
        "kind": "request",
        "auto_return": True,
        "aggregate_id": "",
    }

    result = d.handle_enqueue_command([_new_enqueue_message("msg-new-active-wins")])

    hint = _single_prior_hint(label, result)
    text = str(hint.get("text") or "")
    assert_true(hint.get("prior_kind") == "interrupt", f"{label}: active context must win over pending row: {hint}")
    assert_true(hint.get("prior_message_id") == "msg-prior-active-wins", f"{label}: hint should point at active context id, not pending row: {hint}")
    assert_true("agent_interrupt_peer bob --status" in text, f"{label}: active context hint must guide status-confirmed interrupt: {text!r}")
    print(f"  PASS  {label}")


def scenario_prior_hint_classifier_drift_invariant(label: str, tmpdir: Path) -> None:
    d = _prior_hint_daemon(tmpdir)
    cases = [
        ("msg-inflight-pre", False, False, "cancel", False),
        ("msg-inflight-active", True, False, "interrupt", True),
        ("msg-inflight-deferred", True, True, "cancel", False),
        ("msg-inflight-deferred-no-enter", False, True, "cancel", False),
    ]
    for message_id, has_enter, deferred, expected_kind, expected_active in cases:
        item = test_message(message_id, frm="alice", to="bob", status="inflight")
        if has_enter:
            d.last_enter_ts[message_id] = time.time()
        else:
            d.last_enter_ts.pop(message_id, None)
        if deferred:
            item["pane_mode_enter_deferred_since_ts"] = time.time()
        prior_kind = d._classify_prior_for_hint(item)
        active = d._message_is_active_inflight_for_cancel(item)
        assert_true(prior_kind == expected_kind, f"{label}: classifier mismatch for {message_id}: {prior_kind!r}")
        assert_true(active is expected_active, f"{label}: active predicate mismatch for {message_id}: {active!r}")
        assert_true((prior_kind == "interrupt") == active, f"{label}: classifier drift for {message_id}: {prior_kind!r} vs active={active!r}")
    print(f"  PASS  {label}")


def scenario_daemon_upsert_no_auto_return_watchdog_rejected(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-noauto-extend", frm="claude", to="codex", status="inflight")
    msg["auto_return"] = False
    msg["watchdog_delay_sec"] = 60.0
    d.queue.update(lambda queue: queue.append(msg) or None)
    ok, err, deadline = d.upsert_message_watchdog("claude", "msg-noauto-extend", 60.0)
    assert_true(not ok and err == "watchdog_requires_auto_return" and deadline is None, f"{label}: no-auto row must not be extendable: {(ok, err, deadline)}")
    result = _daemon_command_result(d, {"op": "extend_watchdog", "from": "claude", "message_id": "msg-noauto-extend", "seconds": 60.0})
    assert_true(result.get("ok") is False and result.get("error") == "watchdog_requires_auto_return", f"{label}: socket extend should surface stable code: {result}")
    assert_true("auto" in str(result.get("hint") or "").lower(), f"{label}: hint should mention auto-return route: {result}")
    assert_true(not d.watchdogs, f"{label}: rejected extend must not arm watchdogs: {d.watchdogs}")
    print(f"  PASS  {label}")


def scenario_daemon_register_alarm_rejects_non_finite_and_negative(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    for value in ("not-a-number", float("nan"), float("inf"), float("-inf"), -1.0):
        wake_id = d.register_alarm("claude", value, None)  # type: ignore[arg-type]
        assert_true(wake_id is None, f"{label}: register_alarm must reject {value!r}, got {wake_id!r}")
    assert_true(not d.watchdogs, f"{label}: rejected alarms must not mutate watchdogs")
    generated = d.register_alarm("claude", 5.0, "legacy")
    supplied = d.register_alarm("claude", 5.0, "supplied", wake_id="wake-121212121212")
    conflict = d.register_alarm("codex", 5.0, "supplied", wake_id="wake-121212121212")
    assert_true(isinstance(generated, str) and generated.startswith("wake-"), f"{label}: legacy shim should return generated wake id string: {generated!r}")
    assert_true(supplied == "wake-121212121212", f"{label}: legacy shim should return supplied wake id string: {supplied!r}")
    assert_true(conflict is None, f"{label}: legacy shim should return None on supplied id conflict: {conflict!r}")
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


def scenario_daemon_no_auto_return_watchdog_arm_guard_strips(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    msg = test_message("msg-noauto-arm", frm="claude", to="codex", status="pending")
    msg["auto_return"] = False
    msg["watchdog_delay_sec"] = 60.0
    d.queue.update(lambda queue: queue.append(msg) or None)
    reserved = d.reserve_next("codex")
    assert_true(reserved is not None and reserved.get("status") == "inflight", f"{label}: row should still reserve: {reserved}")
    assert_true(not _watchdogs_for_message(d, "msg-noauto-arm"), f"{label}: no-auto row must not arm watchdog: {d.watchdogs}")
    queued = next((it for it in d.queue.read() if it.get("id") == "msg-noauto-arm"), None)
    assert_true(queued is not None and "watchdog_delay_sec" not in queued, f"{label}: guard should strip stale metadata: {queued}")
    events = read_events(tmpdir / "events.raw.jsonl")
    stripped = [e for e in events if e.get("event") == "watchdog_stripped_no_auto_return" and e.get("message_id") == "msg-noauto-arm"]
    assert_true(stripped and stripped[-1].get("watchdog_phase") == "delivery", f"{label}: strip event should be logged: {stripped}")
    print(f"  PASS  {label}")


def scenario_daemon_no_auto_return_watchdog_ingress_sanitizers(label: str, tmpdir: Path) -> None:
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}

    d_apply = make_daemon(tmpdir / "apply", participants)
    msg_apply = test_message("msg-noauto-apply", frm="claude", to="codex", status="ingressing")
    msg_apply["auto_return"] = False
    msg_apply["watchdog_delay_sec"] = 60.0
    d_apply.queue.update(lambda queue: queue.append(msg_apply) or None)
    with d_apply.state_lock:
        d_apply._apply_alarm_cancel_to_queued_message("msg-noauto-apply")
    queue_apply = d_apply.queue.read()
    assert_true(queue_apply[0].get("status") == "pending" and "watchdog_delay_sec" not in queue_apply[0], f"{label}: apply finalize should strip metadata: {queue_apply}")

    d_recover = make_daemon(tmpdir / "recover", participants)
    msg_recover = test_message("msg-noauto-recover", frm="claude", to="codex", status="ingressing")
    msg_recover["auto_return"] = False
    msg_recover["watchdog_delay_sec"] = 60.0
    d_recover.queue.update(lambda queue: queue.append(msg_recover) or None)
    d_recover._recover_ingressing_messages()
    queue_recover = d_recover.queue.read()
    assert_true(queue_recover[0].get("status") == "pending" and "watchdog_delay_sec" not in queue_recover[0], f"{label}: restart recovery should strip metadata: {queue_recover}")

    d_promote = make_daemon(tmpdir / "promote", participants)
    msg_promote = test_message("msg-noauto-promote", frm="claude", to="codex", status="ingressing")
    msg_promote["created_ts"] = "1970-01-01T00:00:00.000000Z"
    msg_promote["auto_return"] = False
    msg_promote["watchdog_delay_sec"] = 60.0
    d_promote.queue.update(lambda queue: queue.append(msg_promote) or None)
    d_promote.last_ingressing_check = 0.0
    d_promote._promote_aged_ingressing()
    queue_promote = d_promote.queue.read()
    assert_true(queue_promote[0].get("status") == "pending" and "watchdog_delay_sec" not in queue_promote[0], f"{label}: aged promotion should strip metadata: {queue_promote}")
    for subdir, msg_id in [("apply", "msg-noauto-apply"), ("recover", "msg-noauto-recover"), ("promote", "msg-noauto-promote")]:
        events = read_events(tmpdir / subdir / "events.raw.jsonl")
        assert_true(any(e.get("event") == "watchdog_stripped_no_auto_return" and e.get("message_id") == msg_id for e in events), f"{label}: {subdir} should log strip event: {events}")
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


def _result_items(d, *, source: str = "auto_return") -> list[dict]:
    return [item for item in d.queue.read() if item.get("kind") == "result" and item.get("source") == source]


def _redirect_file_from_body(body: str) -> Path:
    for line in str(body).splitlines():
        if line.startswith("File: "):
            return Path(line.removeprefix("File: ").strip())
    raise AssertionError(f"redirect body missing File line: {body!r}")


def _redirect_preview_from_body(body: str) -> str:
    for line in str(body).splitlines():
        if line.startswith("Preview: "):
            return json.loads(line.removeprefix("Preview: ").strip())
    raise AssertionError(f"redirect body missing Preview line: {body!r}")


def scenario_peer_result_redirect_single_over_limit(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    reply_text = "alpha\r\n" + ("x" * (MAX_PEER_BODY_CHARS + 50)) + "\x00tail"
    context = {
        "from": "alice",
        "id": "msg-original",
        "causal_id": "causal-single",
        "intent": "review",
        "hop_count": 1,
        "auto_return": True,
    }
    with patched_redirect_root(tmpdir / "share"):
        d.maybe_return_response("bob", reply_text, context)

    results = _result_items(d)
    assert_true(len(results) == 1, f"{label}: expected one auto-return result, got {results}")
    body = str(results[0].get("body") or "")
    assert_true("[bridge:body_redirected]" in body, f"{label}: stable redirect code missing: {body!r}")
    assert_true("Read this file; the preview is intentionally truncated." in body, f"{label}: read-file instruction missing: {body!r}")
    assert_true(len(body) <= bridge_daemon.PEER_RESULT_REDIRECT_WRAPPER_MAX_CHARS, f"{label}: wrapper exceeds budget: {len(body)}")
    prompt = bridge_daemon.build_peer_prompt(results[0], "nonce-redirect")
    assert_true("[bridge truncated peer body]" not in prompt, f"{label}: redirect wrapper must not be prompt-truncated")

    redirect_file = _redirect_file_from_body(body)
    expected = bridge_daemon.normalize_prompt_body_text(f"Result from bob:\n{reply_text}")
    assert_true(redirect_file.read_text(encoding="utf-8") == expected, f"{label}: redirected file content mismatch")
    assert_true(_redirect_preview_from_body(body) == expected[:bridge_daemon.PEER_RESULT_REDIRECT_PREVIEW_CHARS], f"{label}: preview must be first 100 normalized chars")
    assert_true(stat.S_IMODE(redirect_file.parent.stat().st_mode) == 0o700, f"{label}: redirect dir must be private")
    assert_true(stat.S_IMODE(redirect_file.stat().st_mode) == 0o600, f"{label}: redirect file must be private")
    events = read_events(tmpdir / "events.raw.jsonl")
    redirected = [e for e in events if e.get("event") == "body_redirected" and e.get("message_id") == results[0].get("id")]
    assert_true(redirected, f"{label}: redirect event missing")
    assert_true(redirected[-1].get("preview_chars") == len(_redirect_preview_from_body(body)), f"{label}: redirect event should log actual preview length")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_restart_queue_roundtrip(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    reply_text = "persisted " + ("x" * (MAX_PEER_BODY_CHARS + 20))
    expected = bridge_daemon.normalize_prompt_body_text(f"Result from bob:\n{reply_text}")
    with patched_redirect_root(tmpdir / "share"):
        d.maybe_return_response("bob", reply_text, {"from": "alice", "id": "msg-persist", "intent": "persist", "auto_return": True})

    reloaded_queue = read_json(tmpdir / "pending.json", [])
    reloaded = next((item for item in reloaded_queue if item.get("kind") == "result" and item.get("source") == "auto_return"), None)
    assert_true(reloaded is not None, f"{label}: persisted queue should contain redirected result")
    body = str(reloaded.get("body") or "")
    redirect_file = _redirect_file_from_body(body)
    assert_true(redirect_file.read_text(encoding="utf-8") == expected, f"{label}: persisted wrapper file path must remain readable")
    prompt = bridge_daemon.build_peer_prompt(reloaded, "nonce-restart")
    assert_true("[bridge:body_redirected]" in prompt and "[bridge truncated peer body]" not in prompt, f"{label}: reloaded redirected message should build an untruncated prompt")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_below_limit_inline(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    with patched_redirect_root(tmpdir / "share"):
        d.maybe_return_response("bob", "short reply", {"from": "alice", "id": "msg-short", "intent": "short", "auto_return": True})
        share_exists = (tmpdir / "share").exists()

    results = _result_items(d)
    assert_true(len(results) == 1, f"{label}: expected one result")
    body = str(results[0].get("body") or "")
    assert_true("[bridge:body_redirected]" not in body and body == "Result from bob:\nshort reply", f"{label}: below-limit body should stay inline: {body!r}")
    assert_true(not share_exists, f"{label}: below-limit result should not create redirect share root")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_write_failure_fallback(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    share_root = tmpdir / "share-file"
    share_root.write_text("not a directory", encoding="utf-8")
    with patched_redirect_root(share_root):
        d.maybe_return_response("bob", "x" * (MAX_PEER_BODY_CHARS + 10), {"from": "alice", "id": "msg-fail", "intent": "fail", "auto_return": True})

    results = _result_items(d)
    body = str(results[0].get("body") or "")
    assert_true("[bridge:body_redirect_failed]" in body and "reason=unsafe_path" in body, f"{label}: fallback marker/reason missing: {body[:300]!r}")
    prompt = bridge_daemon.build_peer_prompt(results[0], "nonce-fail")
    assert_true("[bridge truncated peer body]" in prompt, f"{label}: failed redirect should fall back to prompt truncation")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "body_redirect_failed" and e.get("reason") == "unsafe_path" for e in events), f"{label}: failure event missing")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_collision_and_symlink_safe(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    share_root = tmpdir / "share"
    reply_dir = share_root / bridge_daemon.PEER_RESULT_REDIRECT_DIRNAME
    reply_dir.mkdir(parents=True, mode=0o700)
    os.chmod(share_root, 0o1777)
    os.chmod(reply_dir, 0o700)

    collision = reply_dir / "reply-msg-111111111111.txt"
    collision.write_text("sentinel", encoding="utf-8")
    os.chmod(collision, 0o600)
    with patched_redirect_root(share_root):
        msg = bridge_daemon.make_message("bob", "alice", "collision", "x" * (MAX_PEER_BODY_CHARS + 1), kind="result", auto_return=False)
        msg["id"] = "msg-111111111111"
        d.redirect_oversized_result_body(msg)
    assert_true("[bridge:body_redirect_failed]" in msg.get("body", "") and "reason=collision" in msg.get("body", ""), f"{label}: collision should fail closed: {msg.get('body', '')[:200]!r}")
    assert_true(collision.read_text(encoding="utf-8") == "sentinel", f"{label}: collision must not overwrite existing file")

    symlink = reply_dir / "reply-msg-222222222222.txt"
    target = tmpdir / "symlink-target"
    os.symlink(target, symlink)
    with patched_redirect_root(share_root):
        msg2 = bridge_daemon.make_message("bob", "alice", "symlink", "y" * (MAX_PEER_BODY_CHARS + 1), kind="result", auto_return=False)
        msg2["id"] = "msg-222222222222"
        d.redirect_oversized_result_body(msg2)
    assert_true("[bridge:body_redirect_failed]" in msg2.get("body", "") and "reason=collision" in msg2.get("body", ""), f"{label}: symlink final path should fail closed as collision")
    assert_true(not target.exists(), f"{label}: symlink target must not be followed or created")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_share_root_owner_mismatch(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    share_root = tmpdir / "share"
    share_root.mkdir(mode=0o1777)
    real_lstat = os.lstat
    uid = os.getuid()

    class FakeStat:
        def __init__(self, original) -> None:
            self.st_mode = original.st_mode
            self.st_uid = uid + 1

    def fake_lstat(path):
        original = real_lstat(path)
        if Path(path) == share_root:
            return FakeStat(original)
        return original

    try:
        bridge_daemon.os.lstat = fake_lstat  # type: ignore[assignment]
        with patched_redirect_root(share_root):
            msg = bridge_daemon.make_message("bob", "alice", "owner", "x" * (MAX_PEER_BODY_CHARS + 1), kind="result", auto_return=False)
            d.redirect_oversized_result_body(msg)
    finally:
        bridge_daemon.os.lstat = real_lstat  # type: ignore[assignment]

    assert_true("[bridge:body_redirect_failed]" in msg.get("body", "") and "reason=permission_denied" in msg.get("body", ""), f"{label}: owner mismatch should fail permission_denied")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_replies_permission_drift_tightened(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    share_root = tmpdir / "share"
    reply_dir = share_root / bridge_daemon.PEER_RESULT_REDIRECT_DIRNAME
    reply_dir.mkdir(parents=True, mode=0o755)
    os.chmod(share_root, 0o1777)
    os.chmod(reply_dir, 0o755)

    with patched_redirect_root(share_root):
        msg = bridge_daemon.make_message("bob", "alice", "mode", "x" * (MAX_PEER_BODY_CHARS + 1), kind="result", auto_return=False)
        d.redirect_oversized_result_body(msg)

    assert_true("[bridge:body_redirected]" in msg.get("body", ""), f"{label}: private dir chmod recovery should allow redirect")
    assert_true(stat.S_IMODE(reply_dir.stat().st_mode) == 0o700, f"{label}: replies dir should be tightened to 0700")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_logs_reduced_preview_chars(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    parent = tmpdir / ("a" * 150)
    parent.mkdir()
    long_share_root = parent / ("b" * 150)
    with patched_redirect_root(long_share_root):
        msg = bridge_daemon.make_message("bob", "alice", "long-path", "x" * (MAX_PEER_BODY_CHARS + 1), kind="result", auto_return=False)
        d.redirect_oversized_result_body(msg)

    body = str(msg.get("body") or "")
    assert_true("[bridge:body_redirected]" in body, f"{label}: long path should still redirect: {body[:300]!r}")
    actual_preview_len = len(_redirect_preview_from_body(body))
    assert_true(actual_preview_len < bridge_daemon.PEER_RESULT_REDIRECT_PREVIEW_CHARS, f"{label}: test setup should force reduced preview length")
    events = read_events(tmpdir / "events.raw.jsonl")
    redirected = [e for e in events if e.get("event") == "body_redirected" and e.get("message_id") == msg.get("id")]
    assert_true(redirected and redirected[-1].get("preview_chars") == actual_preview_len, f"{label}: log must record actual reduced preview length: {redirected}")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_malformed_message_id_safe_filename(label: str, tmpdir: Path) -> None:
    participants = _participants_state(["alice", "bob"])["participants"]
    d = make_daemon(tmpdir, participants)
    with patched_redirect_root(tmpdir / "share"):
        msg = bridge_daemon.make_message("bob", "alice", "bad-id", "z" * (MAX_PEER_BODY_CHARS + 1), kind="result", auto_return=False)
        msg["id"] = "../bad"
        d.redirect_oversized_result_body(msg)

    body = str(msg.get("body") or "")
    redirect_file = _redirect_file_from_body(body)
    assert_true("[bridge:body_redirected]" in body, f"{label}: malformed id should still redirect with safe fallback")
    assert_true(redirect_file.name.startswith("reply-") and "bad" not in redirect_file.name and "/" not in redirect_file.name, f"{label}: unsafe id leaked into filename: {redirect_file}")
    assert_true(redirect_file.exists(), f"{label}: safe fallback file should exist")
    events = read_events(tmpdir / "events.raw.jsonl")
    assert_true(any(e.get("event") == "body_redirect_message_id_sanitized" and e.get("message_id") == "../bad" for e in events), f"{label}: sanitized-id event missing")
    print(f"  PASS  {label}")


def scenario_peer_result_redirect_aggregate_unique_once(label: str, tmpdir: Path) -> None:
    participants = {
        "manager": {"alias": "manager", "agent_type": "claude", "pane": "%97"},
        "w1": {"alias": "w1", "agent_type": "codex", "pane": "%96"},
        "w2": {"alias": "w2", "agent_type": "codex", "pane": "%95"},
    }
    d = make_daemon(tmpdir, participants)
    agg_id = "agg-redirect"
    common = {
        "aggregate_id": agg_id,
        "from": "manager",
        "aggregate_expected": ["w1", "w2"],
        "aggregate_message_ids": {"w1": "msg-agg-redir-w1", "w2": "msg-agg-redir-w2"},
        "causal_id": "causal-agg-redir",
        "intent": "review",
        "aggregate_mode": "all",
        "hop_count": 1,
    }
    with patched_redirect_root(tmpdir / "share"):
        d.collect_aggregate_response("w1", "w1:" + ("x" * MAX_PEER_BODY_CHARS), {**common, "id": "msg-agg-redir-w1"})
        d.collect_aggregate_response("w2", "w2:" + ("y" * MAX_PEER_BODY_CHARS), {**common, "id": "msg-agg-redir-w2"})
        d.collect_aggregate_response("w2", "duplicate", {**common, "id": "msg-agg-redir-w2"})

    results = _result_items(d, source="aggregate_return")
    assert_true(len(results) == 1, f"{label}: aggregate should queue exactly one result, got {results}")
    body = str(results[0].get("body") or "")
    assert_true("[bridge:body_redirected]" in body, f"{label}: aggregate result should redirect")
    redirect_file = _redirect_file_from_body(body)
    files = set((tmpdir / "share" / bridge_daemon.PEER_RESULT_REDIRECT_DIRNAME).glob("reply-*.txt"))
    assert_true(len(files) == 1 and files == {redirect_file}, f"{label}: aggregate should create one unique redirect file: files={files}, body={body!r}")
    content = redirect_file.read_text(encoding="utf-8")
    assert_true("Aggregated result for broadcast agg-redirect" in content and "--- w1 in_reply_to=msg-agg-redir-w1 ---" in content and "--- w2 in_reply_to=msg-agg-redir-w2 ---" in content, f"{label}: aggregate redirected content incomplete")
    print(f"  PASS  {label}")


def scenario_response_send_guard_socket_cli_error_kind(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob"])
    _patch_enqueue_for_unit(be, state)
    guard_error = bridge_response_guard.format_response_send_violation(
        bridge_response_guard.ResponseSendViolation(
            sender="bob",
            requester="alice",
            message_id="msg-guard-cli",
            outgoing_kind="request",
            blocked_targets=("alice",),
            source="test",
        )
    )
    be.enqueue_via_daemon_socket = lambda session, messages, **kwargs: (True, [], [], guard_error, "response_send_guard")

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
    for token in ("separate agent_send_peer", "current_prompt.from=alice", "third-party", "review/collaboration", "--force"):
        assert_true(token in err, f"{label}: canonical guard text missing {token!r}: {err!r}")
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
        attempted, ids, hints, error, error_kind = be.enqueue_via_daemon_socket(
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
    assert_true(hints == [], f"{label}: rejected socket response has no hints: {hints}")
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
    assert_true("separate agent_send_peer" in err and "current_prompt.from=alice" in err, f"{label}: blocked fallback explains requester-only guard: {err!r}")
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
    assert_true("target list includes that requester" in err and "current_prompt.from=alice" in err, f"{label}: --all error should mention requester inclusion: {err!r}")
    after = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    assert_true(before == after, f"{label}: blocked --all fallback must leave files unchanged")
    print(f"  PASS  {label}")


def scenario_response_send_guard_fallback_partial_blocks_unchanged(label: str, tmpdir: Path) -> None:
    be = _import_enqueue_module()
    state = _participants_state(["alice", "bob", "carol"])
    _patch_enqueue_for_unit(be, state)
    queue_file = tmpdir / "pending.json"
    state_file = tmpdir / "events.raw.jsonl"
    public_file = tmpdir / "events.jsonl"
    _write_json(queue_file, [_delivered_request("msg-delivered-partial", "alice", "bob")])
    state_file.write_text("raw-before\n", encoding="utf-8")
    public_file.write_text("public-before\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}

    code, out, err = _run_enqueue_main(
        be,
        [
            "--session", "test-session",
            "--from", "bob",
            "--to", "alice,carol",
            "--body", "partial while replying",
            "--queue-file", str(queue_file),
            "--state-file", str(state_file),
            "--public-state-file", str(public_file),
        ],
    )
    assert_true(code == 2, f"{label}: partial target list including requester must be blocked, got {code}, err={err!r}")
    assert_true(out == "", f"{label}: blocked partial has no stdout: {out!r}")
    for token in ("target list includes that requester", "current_prompt.from=alice", "third-party", "other validations still apply"):
        assert_true(token in err, f"{label}: partial error missing token {token!r}: {err!r}")
    after = {path: path.read_bytes() for path in (queue_file, state_file, public_file)}
    assert_true(before == after, f"{label}: blocked partial fallback must leave files unchanged")
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


def scenario_clear_reservation_blocks_delivery(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"}, "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"}}
    d = make_daemon(tmpdir, participants)
    d.clear_reservations["bob"] = {"clear_id": "clear-test", "target": "bob"}
    d.queue.update(lambda queue: queue.append(test_message("msg-clear-block", frm="alice", to="bob", status="pending")))

    assert_true(d.next_pending_candidate("bob") is None, f"{label}: next_pending_candidate must block clear target")
    assert_true(d.reserve_next("bob") is None, f"{label}: reserve_next must block clear target")
    item = _queue_item(d, "msg-clear-block")
    assert_true(item is not None and item.get("status") == "pending", f"{label}: row must remain pending behind clear: {item}")
    print(f"  PASS  {label}")


def scenario_clear_guard_formatter_force_guidance(label: str, tmpdir: Path) -> None:
    soft = bridge_clear_guard.ClearViolation(
        "target_originated_requests",
        "bob has outstanding request rows",
        hard=False,
        refs=["msg-request"],
    )
    hard = bridge_clear_guard.ClearViolation(
        "target_active_messages",
        "bob has active/post-pane-touch inbound work",
        refs=["msg-active"],
    )

    soft_only = bridge_clear_guard.format_clear_guard_result(
        bridge_clear_guard.ClearGuardResult(ok=False, soft_blockers=[soft], force_attempted=False)
    )
    assert_true(
        soft_only.startswith("soft:target_originated_requests:")
        and "Retry with --force to disable auto-return for target-originated requests." in soft_only,
        f"{label}: soft-only formatter must classify and guide: {soft_only}",
    )

    hard_only = bridge_clear_guard.format_clear_guard_result(
        bridge_clear_guard.ClearGuardResult(ok=False, hard_blockers=[hard], force_attempted=False)
    )
    assert_true(
        hard_only.startswith("hard:target_active_messages:")
        and "Retry with --force" not in hard_only,
        f"{label}: hard-only formatter must not recommend force: {hard_only}",
    )

    mixed = bridge_clear_guard.format_clear_guard_result(
        bridge_clear_guard.ClearGuardResult(ok=False, hard_blockers=[hard], soft_blockers=[soft], force_attempted=False)
    )
    assert_true(
        "hard:target_active_messages:" in mixed
        and "soft:target_originated_requests:" in mixed
        and "Retry with --force to disable auto-return for target-originated requests." in mixed,
        f"{label}: mixed non-force formatter must include one trailing force hint: {mixed}",
    )

    mixed_forced = bridge_clear_guard.format_clear_guard_result(
        bridge_clear_guard.ClearGuardResult(ok=False, hard_blockers=[hard], soft_blockers=[soft], force_attempted=True)
    )
    assert_true(
        "hard:target_active_messages:" in mixed_forced
        and "soft:target_originated_requests:" in mixed_forced
        and "Retry with --force" not in mixed_forced,
        f"{label}: mixed force-attempted formatter must not repeat force guidance: {mixed_forced}",
    )
    print(f"  PASS  {label}")


def scenario_clear_guard_force_truth_matches_policy(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"}, "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"}}

    request_daemon = make_daemon(tmpdir / "request", participants)
    request_daemon.queue.update(lambda queue: queue.append(test_message("msg-originated", frm="bob", to="alice", status="pending")))
    nonforce = request_daemon.clear_guard("bob", force=False)
    text = bridge_clear_guard.format_clear_guard_result(nonforce)
    assert_true(not nonforce.ok, f"{label}: non-force originated request should block")
    assert_true(not nonforce.hard_blockers, f"{label}: originated request must not be hard: {nonforce}")
    assert_true(
        [v.code for v in nonforce.soft_blockers] == ["target_originated_requests"],
        f"{label}: originated request should be a soft blocker: {nonforce}",
    )
    assert_true(
        "soft:target_originated_requests:" in text
        and "Retry with --force to disable auto-return for target-originated requests." in text,
        f"{label}: originated request text must advertise accurate force recovery: {text}",
    )
    forced = request_daemon.clear_guard("bob", force=True)
    assert_true(forced.ok and not forced.hard_blockers, f"{label}: force should allow originated request absent hard blockers: {forced}")

    aggregate_daemon = make_daemon(tmpdir / "aggregate", participants)
    with locked_json(Path(aggregate_daemon.aggregate_file), {"version": 1, "aggregates": {}}) as data:
        data.setdefault("aggregates", {})["agg-clear-guard"] = {
            "id": "agg-clear-guard",
            "requester": "bob",
            "expected": ["alice"],
            "message_ids": {"alice": "msg-agg-alice"},
            "status": "collecting",
            "delivered": False,
        }
    aggregate_guard = aggregate_daemon.clear_guard("bob", force=False)
    aggregate_text = bridge_clear_guard.format_clear_guard_result(aggregate_guard)
    assert_true(
        not aggregate_guard.ok
        and not aggregate_guard.hard_blockers
        and [v.code for v in aggregate_guard.soft_blockers] == ["target_aggregate_requester"],
        f"{label}: incomplete aggregate requester should be soft: {aggregate_guard}",
    )
    assert_true(
        "soft:target_aggregate_requester:" in aggregate_text
        and "Retry with --force to cancel incomplete aggregate waits." in aggregate_text,
        f"{label}: aggregate requester text must advertise accurate force recovery: {aggregate_text}",
    )

    active_daemon = make_daemon(tmpdir / "active", participants)
    active_daemon.queue.update(lambda queue: queue.append(test_message("msg-active", frm="alice", to="bob", status="submitted")))
    active_nonforce = active_daemon.clear_guard("bob", force=False)
    active_forced = active_daemon.clear_guard("bob", force=True)
    assert_true(
        not active_nonforce.ok
        and [v.code for v in active_nonforce.hard_blockers] == ["target_active_messages"]
        and not active_nonforce.soft_blockers,
        f"{label}: active inbound must be hard for non-force: {active_nonforce}",
    )
    assert_true(
        not active_forced.ok
        and [v.code for v in active_forced.hard_blockers] == ["target_active_messages"],
        f"{label}: active inbound must remain hard with force: {active_forced}",
    )

    mixed_daemon = make_daemon(tmpdir / "mixed", participants)
    mixed_daemon.queue.update(lambda queue: queue.extend([
        test_message("msg-mixed-active", frm="alice", to="bob", status="submitted"),
        test_message("msg-mixed-originated", frm="bob", to="alice", status="pending"),
    ]))
    mixed_forced_guard = mixed_daemon.clear_guard("bob", force=True)
    mixed_forced_text = bridge_clear_guard.format_clear_guard_result(mixed_forced_guard)
    assert_true(
        not mixed_forced_guard.ok
        and [v.code for v in mixed_forced_guard.hard_blockers] == ["target_active_messages"]
        and [v.code for v in mixed_forced_guard.soft_blockers] == ["target_originated_requests"],
        f"{label}: mixed force guard should expose hard plus soft context: {mixed_forced_guard}",
    )
    assert_true(
        "Retry with --force" not in mixed_forced_text,
        f"{label}: force-attempted hard failure must not recommend force again: {mixed_forced_text}",
    )
    print(f"  PASS  {label}")


def _clear_batch_participants() -> dict[str, dict]:
    return {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "status": "active"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "status": "active"},
        "carol": {"alias": "carol", "agent_type": "codex", "pane": "%93", "status": "active"},
        "dave": {"alias": "dave", "agent_type": "codex", "pane": "%94", "status": "active"},
    }


def _install_fake_clear_runner(
    d: bridge_daemon.BridgeDaemon,
    outcomes: dict[str, dict] | None = None,
    seen: list[str] | None = None,
    expected_targets: list[str] | None = None,
    on_first_touch=None,
) -> None:
    outcomes = outcomes or {}
    seen = seen if seen is not None else []

    def fake_run(
        caller: str,
        target: str,
        *,
        force: bool,
        existing_reservation: dict | None = None,
        hold_reservation_after_success: bool = False,
    ) -> dict:
        assert_true(existing_reservation is not None, f"fake clear for {target}: existing reservation required")
        if not seen:
            for expected in (expected_targets or ["bob", "carol", "dave"]):
                assert_true(expected in d.clear_reservations, f"batch should reserve {expected} before first pane touch")
            if on_first_touch is not None:
                on_first_touch()
        seen.append(target)
        outcome = dict(outcomes.get(target) or {"ok": True, "target": target, "cleared": True, "new_session_id": f"{target}-new"})
        if outcome.get("forced_leave"):
            # Keep this scenario local to batch behavior without exercising the
            # full identity-store cleanup path, which has dedicated coverage.
            d.clear_reservations.pop(target, None)
            participant = d.participants.get(target) or {}
            participant["status"] = "detached"
            d.participants.pop(target, None)
            notice_target = next((alias for alias in sorted(d.participants) if alias != target), "")
            if notice_target:
                d.queue_message(
                    bridge_daemon.make_message(
                        sender="bridge",
                        target=notice_target,
                        intent="clear_forced_leave_notice",
                        body=f"{target} forced leave",
                        causal_id=bridge_daemon.short_id("causal"),
                        hop_count=0,
                        auto_return=False,
                        kind="notice",
                        source="clear_forced_leave",
                    ),
                    deliver=False,
                )
        else:
            d.clear_reservations.pop(target, None)
        return outcome

    d.run_clear_peer = fake_run  # type: ignore[method-assign]


def scenario_clear_multi_guard_pass_reserves_all(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    guard_calls = 0
    original_guard_multi = d.clear_guard_multi

    def guard_once(*args, **kwargs):
        nonlocal guard_calls
        guard_calls += 1
        return original_guard_multi(*args, **kwargs)

    d.clear_guard_multi = guard_once  # type: ignore[method-assign]
    seen: list[str] = []

    def check_status_and_reentry() -> None:
        status = _daemon_command_result(d, {"op": "status"})
        try:
            active = {peer.get("alias"): peer.get("clear_active") for peer in status.get("peers") or []}
            assert_true(all(active.get(alias) is True for alias in ("bob", "carol", "dave")), f"{label}: status must show all batch targets clear_active: {status}")
        finally:
            d.command_context.info = {}
        reentry = d.handle_clear_peer("alice", "carol", force=False)
        assert_true(reentry.get("error_kind") == "clear_already_pending", f"{label}: re-entry should see clear_already_pending: {reentry}")

    _install_fake_clear_runner(d, seen=seen, on_first_touch=check_status_and_reentry)

    result = d.handle_clear_peers("alice", ["bob", "carol", "dave"], force=False)

    assert_true(result.get("ok") is True, f"{label}: batch should succeed: {result}")
    assert_true((result.get("summary") or {}).get("all_cleared") is True, f"{label}: all targets should clear: {result}")
    assert_true(seen == ["bob", "carol", "dave"], f"{label}: sequential order should be preserved: {seen}")
    assert_true(guard_calls == 1, f"{label}: multi guard should run once, got {guard_calls}")
    assert_true(not any(target in d.clear_reservations for target in ("bob", "carol", "dave")), f"{label}: reservations must be gone: {d.clear_reservations}")
    print(f"  PASS  {label}")


def scenario_clear_multi_guard_hard_blocker_rejects_all(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    d.queue.update(lambda queue: queue.append(test_message("msg-hard-clear", frm="alice", to="carol", status="submitted")))

    result = d.handle_clear_peers("alice", ["bob", "carol", "dave"], force=False)

    assert_true(result.get("ok") is False and result.get("error_kind") == "clear_blocked", f"{label}: hard blocker should reject: {result}")
    assert_true("carol hard:target_active_messages" in str(result.get("error") or ""), f"{label}: target-qualified hard blocker expected: {result}")
    assert_true("Retry with --force" not in str(result.get("error") or ""), f"{label}: multi guard must not suggest force: {result}")
    assert_true(not d.clear_reservations, f"{label}: rejected batch must not reserve: {d.clear_reservations}")
    print(f"  PASS  {label}")


def scenario_clear_multi_guard_soft_blocker_rejects_all(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    d.queue.update(lambda queue: queue.append(test_message("msg-soft-clear", frm="alice", to="carol", status="pending")))

    result = d.handle_clear_peers("alice", ["bob", "carol", "dave"], force=False)

    assert_true(result.get("ok") is False and result.get("error_kind") == "clear_blocked", f"{label}: soft blocker should reject: {result}")
    assert_true("carol soft:target_cancellable_messages" in str(result.get("error") or ""), f"{label}: target-qualified soft blocker expected: {result}")
    assert_true("Retry with --force" not in str(result.get("error") or ""), f"{label}: multi guard must not suggest force: {result}")
    assert_true(not d.clear_reservations, f"{label}: rejected batch must not reserve: {d.clear_reservations}")
    print(f"  PASS  {label}")


def scenario_clear_multi_partial_outcomes_and_cleanup(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    _install_fake_clear_runner(
        d,
        outcomes={
            "carol": {"ok": False, "target": "carol", "error": "probe_timeout", "error_kind": "probe_timeout", "forced_leave": True},
        },
    )

    result = d.handle_clear_peers("alice", ["bob", "carol", "dave"], force=False)

    statuses = {item.get("target"): item.get("status") for item in result.get("results") or []}
    assert_true(statuses == {"bob": "cleared", "carol": "forced_leave", "dave": "cleared"}, f"{label}: mixed status mapping wrong: {result}")
    assert_true("carol" not in d.participants, f"{label}: forced-leave target should be detached from active participants")
    assert_true(not any(target in d.clear_reservations for target in ("bob", "carol", "dave")), f"{label}: no orphan reservations: {d.clear_reservations}")
    notices = [item for item in d.queue.read() if item.get("source") == "clear_forced_leave" and item.get("to") == "carol"]
    assert_true(not notices, f"{label}: forced-leave notice must not target the removed peer")
    print(f"  PASS  {label}")


def scenario_clear_multi_pre_pane_failure_continues(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    _install_fake_clear_runner(
        d,
        outcomes={
            "carol": {"ok": False, "target": "carol", "error": "endpoint_lost", "error_kind": "endpoint_lost"},
        },
    )

    result = d.handle_clear_peers("alice", ["bob", "carol", "dave"], force=False)

    statuses = {item.get("target"): item.get("status") for item in result.get("results") or []}
    assert_true(statuses == {"bob": "cleared", "carol": "failed", "dave": "cleared"}, f"{label}: pre-pane failure status wrong: {result}")
    assert_true("carol" in d.participants, f"{label}: pre-pane failure must not detach target")
    assert_true(not any(target in d.clear_reservations for target in ("bob", "carol", "dave")), f"{label}: reservations cleaned: {d.clear_reservations}")
    print(f"  PASS  {label}")


def scenario_clear_multi_forced_leave_notice_waits_behind_reservation(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    checked = False

    def fake_run(
        caller: str,
        target: str,
        *,
        force: bool,
        existing_reservation: dict | None = None,
        hold_reservation_after_success: bool = False,
    ) -> dict:
        nonlocal checked
        if target == "bob":
            d.clear_reservations.pop(target, None)
            d.queue_message(
                bridge_daemon.make_message(
                    sender="bridge",
                    target="carol",
                    intent="clear_forced_leave_notice",
                    body="bob removed",
                    causal_id=bridge_daemon.short_id("causal"),
                    hop_count=0,
                    auto_return=False,
                    kind="notice",
                    source="clear_forced_leave",
                ),
                deliver=True,
            )
            row = next(item for item in d.queue.read() if item.get("source") == "clear_forced_leave")
            assert_true(row.get("status") == "pending", f"{label}: notice to reserved carol must stay pending: {row}")
            assert_true("carol" in d.clear_reservations, f"{label}: carol reservation must survive bob forced leave")
            checked = True
            return {"ok": False, "target": target, "error": "probe_timeout", "error_kind": "probe_timeout", "forced_leave": True}
        d.clear_reservations.pop(target, None)
        return {"ok": True, "target": target, "cleared": True}

    d.run_clear_peer = fake_run  # type: ignore[method-assign]
    result = d.handle_clear_peers("alice", ["bob", "carol", "dave"], force=False)
    assert_true(checked, f"{label}: forced-leave notice assertion did not run")
    assert_true(result.get("ok") is True, f"{label}: batch should return summary: {result}")
    assert_true(not any(target in d.clear_reservations for target in ("bob", "carol", "dave")), f"{label}: reservations cleaned")
    print(f"  PASS  {label}")


def scenario_clear_multi_lock_wait_failed_and_rerun(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    _install_fake_clear_runner(
        d,
        outcomes={
            "carol": {"ok": False, "target": "carol", "error": "lock_wait_exceeded", "error_kind": "lock_wait_exceeded"},
        },
        expected_targets=["bob", "carol"],
    )
    first = d.handle_clear_peers("alice", ["bob", "carol"], force=False)
    statuses = {item.get("target"): item.get("status") for item in first.get("results") or []}
    assert_true(statuses == {"bob": "cleared", "carol": "failed"}, f"{label}: lock wait should be failed and returned: {first}")
    assert_true(not d.clear_reservations, f"{label}: failed run must release reservations")

    _install_fake_clear_runner(d, expected_targets=["bob", "carol"])
    second = d.handle_clear_peers("alice", ["bob", "carol"], force=False)
    assert_true((second.get("summary") or {}).get("all_cleared") is True, f"{label}: immediate rerun should pass: {second}")
    print(f"  PASS  {label}")


def _patch_clear_identity_success(d: bridge_daemon.BridgeDaemon):
    original_replace = bridge_daemon.replace_attached_session_identity_for_clear
    d.clear_process_identity_for_replacement = lambda **_kwargs: {"status": "verified"}  # type: ignore[method-assign]

    def fake_replace(**kwargs):
        return {
            "ok": True,
            "live_record": {
                "agent": kwargs.get("agent_type"),
                "session_id": kwargs.get("new_session_id"),
                "process_identity": {"status": "verified"},
            },
        }

    bridge_daemon.replace_attached_session_identity_for_clear = fake_replace  # type: ignore[assignment]
    return original_replace


def _install_real_clear_fast_path(d: bridge_daemon.BridgeDaemon, *, fail_probe_for: str = "", block_clear_for: str = "", started: threading.Event | None = None, release: threading.Event | None = None) -> None:
    d.dry_run = False
    d.clear_post_clear_delay_seconds = 0.0
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": True, "pane": (d.participants.get(target) or {}).get("pane"), "reason": "ok"}  # type: ignore[method-assign]
    d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]

    def fake_send(_pane: str, text: str, *, target: str, message_id: str) -> dict:
        if text == "/clear":
            if target == block_clear_for and started is not None and release is not None:
                started.set()
                assert_true(release.wait(2.0), f"release event not set for {target}")
            return {"ok": True, "pane_touched": True, "error": ""}
        if target == fail_probe_for:
            raise RuntimeError("probe send exploded")
        reservation = d.clear_reservations.get(target) or {}
        reservation["probe_prompt_submitted"] = True
        reservation["probe_response_finished"] = True
        reservation["new_session_id"] = f"{target}-new"
        return {"ok": True, "pane_touched": True, "error": ""}

    d._clear_tmux_send = fake_send  # type: ignore[method-assign]


def scenario_clear_multi_real_post_touch_exception_forces_leave(label: str, tmpdir: Path) -> None:
    participants = _clear_batch_participants()
    with patched_environ(AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json")):
        d = make_daemon(tmpdir, participants)
        _install_real_clear_fast_path(d, fail_probe_for="bob")
        d.try_deliver_command_aware = lambda *args, **kwargs: None  # type: ignore[method-assign]
        original_replace = _patch_clear_identity_success(d)
        try:
            result = d.handle_clear_peers("alice", ["bob", "carol"], force=False)
        finally:
            bridge_daemon.replace_attached_session_identity_for_clear = original_replace  # type: ignore[assignment]

    statuses = {item.get("target"): item.get("status") for item in result.get("results") or []}
    assert_true(statuses == {"bob": "forced_leave", "carol": "cleared"}, f"{label}: post-touch exception should force-leave and continue: {result}")
    assert_true("bob" not in d.participants, f"{label}: forced-leave target should be removed from active participants")
    assert_true(not any(target in d.clear_reservations for target in ("bob", "carol")), f"{label}: reservations should clean up: {d.clear_reservations}")
    print(f"  PASS  {label}")


def scenario_clear_multi_real_success_holds_until_batch_end(label: str, tmpdir: Path) -> None:
    participants = _clear_batch_participants()
    started = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}
    with patched_environ(AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json")):
        d = make_daemon(tmpdir, participants)
        _install_real_clear_fast_path(d)
        delivery_calls: list[str] = []
        d.try_deliver_command_aware = lambda target, **_kwargs: delivery_calls.append(str(target or ""))  # type: ignore[method-assign]
        original_replace = _patch_clear_identity_success(d)

        def blocking_identity(**kwargs):
            if kwargs.get("session_id") == "carol-new":
                started.set()
                assert_true(release.wait(2.0), f"{label}: release event not set for carol")
            return {"status": "verified"}

        d.clear_process_identity_for_replacement = blocking_identity  # type: ignore[method-assign]

        def runner() -> None:
            try:
                result["value"] = d.handle_clear_peers("alice", ["bob", "carol"], force=False)
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        assert_true(started.wait(2.0), f"{label}: carol clear did not start")
        try:
            with d.state_lock:
                assert_true("bob" in d.clear_reservations and "carol" in d.clear_reservations, f"{label}: bob must remain held while carol clears: {d.clear_reservations}")
                assert_true((d.clear_reservations.get("bob") or {}).get("phase") == "batch_hold", f"{label}: bob should be in batch_hold phase: {d.clear_reservations.get('bob')}")
            status = _daemon_command_result(d, {"op": "status"})
            active = {peer.get("alias"): peer.get("clear_active") for peer in status.get("peers") or []}
            assert_true(active.get("bob") is True and active.get("carol") is True, f"{label}: status should show both reserved: {status}")
            assert_true(delivery_calls == [], f"{label}: delivery must not reopen before batch release: {delivery_calls}")
        finally:
            d.command_context.info = {}
            release.set()
            thread.join(2.0)
            bridge_daemon.replace_attached_session_identity_for_clear = original_replace  # type: ignore[assignment]

    assert_true(not thread.is_alive(), f"{label}: batch thread should finish")
    assert_true("error" not in result, f"{label}: batch thread error: {result.get('error')!r}")
    value = result.get("value")
    assert_true(isinstance(value, dict) and (value.get("summary") or {}).get("all_cleared") is True, f"{label}: batch should clear after release: {value}")
    assert_true(not any(target in d.clear_reservations for target in ("bob", "carol")), f"{label}: final cleanup should release reservations")
    assert_true(delivery_calls == ["bob", "carol"], f"{label}: delivery should reopen after final release: {delivery_calls}")
    print(f"  PASS  {label}")


def scenario_clear_multi_real_pre_pane_failure_holds_until_batch_end(label: str, tmpdir: Path) -> None:
    participants = _clear_batch_participants()
    participants["carol"]["hook_session_id"] = "carol-old"
    started = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}
    with patched_environ(AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json")):
        d = make_daemon(tmpdir, participants)
        d.dry_run = True
        d.clear_post_clear_delay_seconds = 0.0
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        bob_endpoint_failed = False

        def fake_resolve(target: str, purpose: str = "write") -> dict:
            nonlocal bob_endpoint_failed
            if target == "bob" and not bob_endpoint_failed:
                bob_endpoint_failed = True
                d.queue_message(test_message("msg-held-bob", frm="alice", to="bob", status="pending"), deliver=True)
                return {"ok": False, "pane": "", "reason": "endpoint_lost"}
            return {"ok": True, "pane": (d.participants.get(target) or {}).get("pane"), "reason": "ok"}

        def fake_send(_pane: str, text: str, *, target: str, message_id: str) -> dict:
            reservation = d.clear_reservations.get(target) or {}
            if text != "/clear":
                reservation["probe_prompt_submitted"] = True
                reservation["probe_response_finished"] = True
                reservation["new_session_id"] = f"{target}-new"
            return {"ok": True, "pane_touched": True, "error": ""}

        d.resolve_endpoint_detail = fake_resolve  # type: ignore[method-assign]
        d._clear_tmux_send = fake_send  # type: ignore[method-assign]
        original_replace = _patch_clear_identity_success(d)

        def blocking_identity(**kwargs):
            if kwargs.get("pane") == "%93" or kwargs.get("session_id") == "carol-old":
                started.set()
                assert_true(release.wait(10.0), f"{label}: release event not set for carol")
            return {"status": "verified"}

        d.clear_process_identity_for_replacement = blocking_identity  # type: ignore[method-assign]

        def runner() -> None:
            try:
                result["value"] = d.handle_clear_peers("alice", ["bob", "carol"], force=False)
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        try:
            assert_true(started.wait(10.0), f"{label}: carol clear did not start")
            with d.state_lock:
                assert_true("bob" in d.clear_reservations and "carol" in d.clear_reservations, f"{label}: failed bob must remain held while carol clears: {d.clear_reservations}")
                assert_true((d.clear_reservations.get("bob") or {}).get("phase") == "batch_hold_failed", f"{label}: bob should be held as failed: {d.clear_reservations.get('bob')}")
            held_row = next((item for item in d.queue.read() if item.get("id") == "msg-held-bob"), None)
            assert_true(held_row is not None and held_row.get("status") == "pending", f"{label}: inbound row should stay pending during batch hold: {held_row}")
        finally:
            d.command_context.info = {}
            release.set()
            thread.join(10.0)
            bridge_daemon.replace_attached_session_identity_for_clear = original_replace  # type: ignore[assignment]

    assert_true(not thread.is_alive(), f"{label}: batch thread should finish")
    assert_true("error" not in result, f"{label}: batch thread error: {result.get('error')!r}")
    value = result.get("value")
    statuses = {item.get("target"): item.get("status") for item in (value or {}).get("results") or []} if isinstance(value, dict) else {}
    assert_true(statuses == {"bob": "failed", "carol": "cleared"}, f"{label}: pre-pane failure should fail and continue: {value}")
    delivered_row = next((item for item in d.queue.read() if item.get("id") == "msg-held-bob"), None)
    assert_true(delivered_row is not None and delivered_row.get("status") == "inflight", f"{label}: inbound row should deliver after final release: {delivered_row}")
    assert_true(not any(target in d.clear_reservations for target in ("bob", "carol")), f"{label}: final cleanup should release reservations")
    print(f"  PASS  {label}")


def scenario_clear_guard_blocks_pending_self_clear(label: str, tmpdir: Path) -> None:
    d = make_daemon(tmpdir, _clear_batch_participants())
    d.pending_self_clears["bob"] = {"clear_id": "clear-self-bob", "caller": "bob", "target": "bob"}

    result = d.clear_guard("bob", force=False)

    assert_true(not result.ok, f"{label}: pending self-clear should block non-self clear")
    assert_true([violation.code for violation in result.hard_blockers] == ["clear_already_pending"], f"{label}: expected clear_already_pending hard blocker: {result}")
    print(f"  PASS  {label}")


def scenario_clear_multi_daemon_validation(label: str, tmpdir: Path) -> None:
    participants = _clear_batch_participants()
    participants["erin"] = {"alias": "erin", "agent_type": "codex", "pane": "%95", "status": "detached"}
    d = make_daemon(tmpdir, participants)
    cases = [
        ({"op": "clear_peer", "from": "alice", "targets": []}, "malformed_targets"),
        ({"op": "clear_peer", "from": "alice", "target": "bob", "targets": ["carol"]}, "malformed_targets"),
        ({"op": "clear_peer", "from": "alice", "targets": ["bob", "missing"]}, "unknown_target"),
        ({"op": "clear_peer", "from": "alice", "targets": ["bob", "erin"]}, "inactive_target"),
        ({"op": "clear_peer", "from": "alice", "targets": ["alice", "bob"]}, "multi_self_disallowed"),
        ({"op": "clear_peer", "from": "alice", "targets": ["bob", "carol"], "force": True}, "multi_force_disallowed"),
    ]
    for payload, expected in cases:
        result = _daemon_command_result(d, payload)
        assert_true(result.get("ok") is False and result.get("error_kind") == expected, f"{label}: {payload} expected {expected}, got {result}")

    seen: list[str] = []
    _install_fake_clear_runner(d, seen=seen, expected_targets=["bob", "carol"])
    deduped = _daemon_command_result(d, {"op": "clear_peer", "from": "alice", "targets": ["bob", "carol", "bob"]})
    assert_true(deduped.get("ok") is True and deduped.get("targets") == ["bob", "carol"], f"{label}: duplicates should dedupe: {deduped}")
    spoofed = _daemon_command_result(d, {"op": "clear_peer", "from": "bob", "targets": ["bob", "carol"]})
    assert_true(spoofed.get("error_kind") == "multi_self_disallowed", f"{label}: effective sender must feed self-rule: {spoofed}")
    print(f"  PASS  {label}")


def _run_clear_peer_cli(
    argv: list[str],
    response: dict,
    *,
    ok: bool = True,
    error: str = "",
    state: dict | None = None,
) -> tuple[int, str, str, list[dict]]:
    if state is None:
        state = _participants_state(["alice", "bob", "carol"])
    calls: list[dict] = []
    old_argv = sys.argv[:]
    old_resolve = bridge_clear_peer.resolve_caller_from_pane
    old_ensure = bridge_clear_peer.ensure_daemon_running
    old_room_status = bridge_clear_peer.room_status
    old_load_session = bridge_clear_peer.load_session
    old_send_command = bridge_clear_peer.send_command
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["agent_clear_peer", *argv]
        bridge_clear_peer.resolve_caller_from_pane = lambda **kwargs: argparse.Namespace(
            ok=True,
            session=kwargs.get("explicit_session") or "test-session",
            alias=kwargs.get("explicit_alias") or "alice",
            error="",
        )  # type: ignore[assignment]
        bridge_clear_peer.ensure_daemon_running = lambda _session: ""  # type: ignore[assignment]
        bridge_clear_peer.room_status = lambda _session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")  # type: ignore[assignment]
        bridge_clear_peer.load_session = lambda _session: state  # type: ignore[assignment]

        def fake_send_command(_session: str, payload: dict, *, timeout_seconds: float = bridge_clear_peer.CLEAR_SOCKET_TIMEOUT_SECONDS):
            calls.append({"payload": dict(payload), "timeout_seconds": timeout_seconds})
            return ok, dict(response), error

        bridge_clear_peer.send_command = fake_send_command  # type: ignore[assignment]
        with redirect_stdout(out), redirect_stderr(err):
            code = bridge_clear_peer.main()
    finally:
        sys.argv = old_argv
        bridge_clear_peer.resolve_caller_from_pane = old_resolve  # type: ignore[assignment]
        bridge_clear_peer.ensure_daemon_running = old_ensure  # type: ignore[assignment]
        bridge_clear_peer.room_status = old_room_status  # type: ignore[assignment]
        bridge_clear_peer.load_session = old_load_session  # type: ignore[assignment]
        bridge_clear_peer.send_command = old_send_command  # type: ignore[assignment]
    return code, out.getvalue(), err.getvalue(), calls


def scenario_clear_peer_cli_multi_and_compatibility(label: str, tmpdir: Path) -> None:
    state = _participants_state(["alice", "bob", "carol"])
    for argv in (["bob", "--to", "carol"], ["--all", "bob"], ["--all", "--to", "bob"]):
        code, _out, _err, calls = _run_clear_peer_cli(argv, {"ok": True}, state=state)
        assert_true(code == 2 and not calls, f"{label}: selector conflict should reject before daemon: {argv}, calls={calls}")

    code, _out, err, calls = _run_clear_peer_cli(["--to", "bob,carol", "--force"], {"ok": True}, state=state)
    assert_true(code == 2 and "multi_force_disallowed" in err and not calls, f"{label}: multi force reject: code={code}, err={err!r}, calls={calls}")
    code, _out, err, calls = _run_clear_peer_cli(["--to", "alice,bob"], {"ok": True}, state=state)
    assert_true(code == 2 and "multi_self_disallowed" in err and not calls, f"{label}: multi self reject: code={code}, err={err!r}, calls={calls}")

    code, out, err, _calls = _run_clear_peer_cli(["alice"], {"ok": True, "deferred": True, "target": "alice"}, state=state)
    assert_true(code == 0 and out == "agent_clear_peer: self-clear for alice is scheduled after the current turn ends (deferred).\n", f"{label}: self text changed: out={out!r}")
    assert_true(err == "Hint: further sends from this alias are blocked once the clear is reserved.\n", f"{label}: self hint changed: {err!r}")
    code, out, _err, _calls = _run_clear_peer_cli(["alice", "--json"], {"ok": True, "deferred": True, "target": "alice"}, state=state)
    assert_true(json.loads(out) == {"target": "alice", "cleared": False, "deferred": True, "force": False, "new_session_id": None}, f"{label}: self json changed: {out}")

    code, out, _err, calls = _run_clear_peer_cli(["bob"], {"ok": True, "target": "bob", "cleared": True, "new_session_id": "bob-new", "forced_leave": True}, state=state)
    assert_true(code == 0 and out == "agent_clear_peer: cleared bob.\n", f"{label}: single non-self text changed: {out!r}")
    code, out, _err, _calls = _run_clear_peer_cli(["bob", "--json"], {"ok": True, "target": "bob", "cleared": True, "new_session_id": "bob-new", "forced_leave": True}, state=state)
    assert_true("forced_leave" not in json.loads(out), f"{label}: single json must filter daemon additive fields: {out}")
    assert_true(calls and calls[0]["payload"].get("target") == "bob" and "targets" not in calls[0]["payload"], f"{label}: single payload must stay single-target: {calls}")

    code, _out, _err, calls = _run_clear_peer_cli(["--from", "bridge", "--allow-spoof", "--all"], {"ok": True, "results": [{"target": "alice", "status": "cleared", "ok": True, "cleared": True}, {"target": "bob", "status": "cleared", "ok": True, "cleared": True}, {"target": "carol", "status": "cleared", "ok": True, "cleared": True}]}, state=state)
    assert_true(code == 0 and calls[0]["payload"].get("targets") == ["alice", "bob", "carol"], f"{label}: bridge --all should include all active aliases: {calls}")
    two_peer = _participants_state(["alice", "bob"])
    code, out, _err, calls = _run_clear_peer_cli(["--all"], {"ok": True, "target": "bob", "cleared": True}, state=two_peer)
    assert_true(code == 0 and out == "agent_clear_peer: cleared bob.\n" and calls[0]["payload"].get("target") == "bob", f"{label}: attached --all two-peer should clear peer: out={out!r}, calls={calls}")
    print(f"  PASS  {label}")


def scenario_clear_multi_timeout_and_formatter(label: str, tmpdir: Path) -> None:
    soft = bridge_clear_guard.ClearViolation("target_cancellable_messages", "bob has cancellable pending/pre-active inbound work", hard=False, target="bob")
    text = bridge_clear_guard.format_clear_guard_result(
        bridge_clear_guard.ClearGuardResult(ok=False, soft_blockers=[soft], force_attempted=False),
        suppress_force_hint=True,
        include_targets=True,
    )
    assert_true("bob soft:target_cancellable_messages" in text and "Retry with --force" not in text, f"{label}: multi formatter wrong: {text}")

    participants = _clear_batch_participants()
    with patched_environ(AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC="3"):
        d = make_daemon(tmpdir, participants)
        d.begin_command_context("clear_peer", {"op": "clear_peer", "targets": ["bob", "carol"]})
        try:
            expected_client = (bridge_daemon.CLEAR_CLIENT_TIMEOUT_SECONDS + 2.0) * 2 + bridge_daemon.CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS
            assert_true(d.command_context.info.get("client_timeout") == expected_client, f"{label}: daemon client timeout formula: {d.command_context.info}")
            budget, margin = d.command_budget("clear_peer")
            expected_budget = d.clear_peer_post_lock_worst_case_seconds() * 2 + bridge_daemon.CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS
            assert_true(budget == expected_budget and margin == 20.0, f"{label}: daemon budget formula: budget={budget}, margin={margin}")
        finally:
            d.command_context.info = {}

        code, _out, _err, calls = _run_clear_peer_cli(
            ["--to", "bob,carol"],
            {
                "ok": True,
                "results": [
                    {"target": "bob", "status": "cleared", "ok": True, "cleared": True},
                    {"target": "carol", "status": "cleared", "ok": True, "cleared": True},
                ],
                "summary": {"all_cleared": True, "cleared": ["bob", "carol"], "forced_leave": [], "failed": [], "counts": {"cleared": 2, "forced_leave": 0, "failed": 0}},
            },
            state=_participants_state(["alice", "bob", "carol"]),
        )
        expected_cli = (bridge_clear_peer.CLEAR_SOCKET_TIMEOUT_SECONDS + 2.0) * 2 + bridge_clear_peer.CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS
        assert_true(code == 0 and calls[0]["timeout_seconds"] == expected_cli, f"{label}: cli timeout formula: calls={calls}")
    print(f"  PASS  {label}")


def scenario_self_clear_force_guidance_and_preserved_force(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "session-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "session-bob"},
    }

    d = make_daemon(tmpdir / "cancel", participants)
    d.pending_self_clears["alice"] = {"clear_id": "clear-self", "caller": "alice", "target": "alice", "force": False}
    d.queue.update(lambda queue: queue.append(test_message("msg-inbound", frm="bob", to="alice", status="pending")))
    with d.state_lock:
        d.promote_pending_self_clear_locked("alice")
    notice = d.queue.read()[0]
    body = str(notice.get("body") or "")
    assert_true(
        notice.get("source") == "self_clear_cancelled"
        and "soft:target_cancellable_messages:" in body
        and "Retry with --force to cancel cancellable inbound work." in body,
        f"{label}: self-clear cancellation should tell the target when force can recover: {notice}",
    )

    d2 = make_daemon(tmpdir / "force", participants)
    d2._self_clear_worker = lambda _target, _reservation: None  # type: ignore[method-assign]
    d2.pending_self_clears["alice"] = {"clear_id": "clear-self-force", "caller": "alice", "target": "alice", "force": True}
    d2.queue.update(lambda queue: queue.append(test_message("msg-inbound-force", frm="bob", to="alice", status="pending")))
    with d2.state_lock:
        d2.promote_pending_self_clear_locked("alice")
    reservation = d2.clear_reservations.get("alice") or {}
    assert_true(
        reservation.get("force") is True and "alice" not in d2.pending_self_clears,
        f"{label}: deferred self-clear must preserve original force value when promoted: {reservation}",
    )
    print(f"  PASS  {label}")


def scenario_requester_cleared_prompt_guard_and_notice(label: str, tmpdir: Path) -> None:
    participants = {
        "requester": {"alias": "requester", "agent_type": "codex", "pane": "%91"},
        "responder": {"alias": "responder", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    message = test_message("msg-requester-cleared", frm="requester", to="responder", status="delivered")
    message["auto_return"] = False
    message["requester_cleared"] = True
    message["requester_cleared_alias"] = "requester"
    d.queue.update(lambda queue: queue.append(message))

    prompt = bridge_daemon.build_peer_prompt(message, "nonce-test")
    assert_true("bridge will not deliver" in prompt and "auto-returns" not in prompt, f"{label}: requester-cleared prompt must not claim auto-return: {prompt}")

    violation = bridge_response_guard.response_send_violation(
        sender="responder",
        targets=["requester"],
        outgoing_kind="notice",
        force=False,
        contexts=bridge_response_guard.contexts_from_queue("responder", d.queue.read()),
        source="queue",
    )
    assert_true(violation is not None, f"{label}: queue-derived requester-cleared context must block separate send")

    d.current_prompt_by_agent["responder"] = {
        "id": "msg-requester-cleared",
        "from": "requester",
        "kind": "request",
        "auto_return": False,
        "requester_cleared": True,
        "requester_cleared_alias": "requester",
        "causal_id": "causal-test",
    }
    d.maybe_return_response("responder", "done", d.current_prompt_by_agent["responder"])
    queued_notice = [
        item for item in d.queue.read()
        if item.get("to") == "responder" and item.get("source") == "requester_cleared"
    ]
    assert_true(queued_notice, f"{label}: terminal requester-cleared response must queue notice to responder")
    print(f"  PASS  {label}")


def scenario_clear_marker_routes_fast_stop(label: str, tmpdir: Path) -> None:
    marker_file = tmpdir / "controlled-clears.json"
    events = tmpdir / "events.raw.jsonl"
    public_events = tmpdir / "events.jsonl"
    with patched_environ(
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(marker_file),
        AGENT_BRIDGE_SESSION=None,
        AGENT_BRIDGE_AGENT=None,
        TMUX_PANE="%92",
    ):
        marker = bridge_clear_marker.make_marker(
            bridge_session="test-session",
            alias="bob",
            agent="codex",
            old_session_id="old-session",
            probe_id="probe-fast",
            pane="%92",
            target="tmux:1.0",
            events_file=str(events),
            public_events_file=str(public_events),
            caller="alice",
            clear_id="clear-fast",
        )
        bridge_clear_marker.write_marker(marker)
        prompt_record = bridge_hook_logger.build_record(
            "codex",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "new-session",
                "turn_id": "turn-probe",
                "prompt": "[bridge-probe:probe-fast] hi",
            },
        )
        mapping = bridge_hook_logger.controlled_clear_mapping("codex", prompt_record)
        assert_true(mapping and mapping.get("bridge_session") == "test-session", f"{label}: prompt marker mapping failed: {mapping}")
        updated = bridge_clear_marker.read_markers()["markers"][marker["id"]]
        assert_true(updated.get("new_session_id") == "new-session" and updated.get("probe_turn_id") == "turn-probe", f"{label}: hook logger must write phase-2 keys: {updated}")
        stop_record = bridge_hook_logger.build_record(
            "codex",
            {
                "hook_event_name": "Stop",
                "session_id": "new-session",
                "turn_id": "turn-probe",
                "last_assistant_message": "bridge cleared probe-fast",
            },
        )
        stop_mapping = bridge_hook_logger.controlled_clear_mapping("codex", stop_record)
        assert_true(stop_mapping and stop_mapping.get("alias") == "bob", f"{label}: stop must route via phase-2 marker: {stop_mapping}")
    print(f"  PASS  {label}")


def scenario_clear_marker_probe_id_fallback_contracts(label: str, tmpdir: Path) -> None:
    marker_file = tmpdir / "controlled-clears.json"
    events = tmpdir / "events.raw.jsonl"
    public_events = tmpdir / "events.jsonl"
    fallback_events = tmpdir / "fallback.raw.jsonl"
    fallback_public = tmpdir / "fallback.jsonl"
    live_file = tmpdir / "live-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    with patched_environ(
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(marker_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_SESSION=None,
        AGENT_BRIDGE_AGENT=None,
        AGENT_BRIDGE_BUS=None,
        AGENT_BRIDGE_EVENTS=None,
        AGENT_BRIDGE_PUBLIC_EVENTS=None,
        TMUX_PANE=None,
    ):
        marker = bridge_clear_marker.make_marker(
            bridge_session="test-session",
            alias="bob",
            agent="claude",
            old_session_id="old-session",
            probe_id="probe-pane-missing",
            pane="%92",
            target="tmux:1.0",
            events_file=str(events),
            public_events_file=str(public_events),
            caller="alice",
            clear_id="clear-pane-missing",
        )
        bridge_clear_marker.write_marker(marker)
        code = _run_hook_logger_record(
            "claude",
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "new-session",
                "turn_id": "turn-probe",
                "prompt": "[bridge-probe:probe-pane-missing] hi",
            },
            state_file=fallback_events,
            public_state_file=fallback_public,
        )
        assert_true(code == 0, f"{label}: prompt hook logger failed")
        raw_events = read_events(events)
        prompt_record = next((event for event in raw_events if event.get("event") == "prompt_submitted" and event.get("attach_probe") == "probe-pane-missing"), None)
        assert_true(prompt_record and prompt_record.get("pane") == "%92", f"{label}: prompt must route via unique probe marker and normalize pane: {raw_events}")
        assert_true(not any(event.get("attach_probe") == "probe-pane-missing" for event in read_events(fallback_events)), f"{label}: prompt must not route to fallback bus")
        updated = bridge_clear_marker.read_markers()["markers"][marker["id"]]
        assert_true(updated.get("phase") == "prompt_seen" and updated.get("new_session_id") == "new-session", f"{label}: prompt must write phase-2 keys: {updated}")

        marker_mismatch = bridge_clear_marker.make_marker(
            bridge_session="test-session",
            alias="carol",
            agent="claude",
            old_session_id="old-mismatch",
            probe_id="probe-pane-mismatch",
            pane="%93",
            target="tmux:1.1",
            events_file=str(events),
            public_events_file=str(public_events),
        )
        bridge_clear_marker.write_marker(marker_mismatch)
        with patched_environ(TMUX_PANE="%not-the-marker-pane"):
            code = _run_hook_logger_record(
                "claude",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "new-mismatch",
                    "turn_id": "turn-mismatch",
                    "prompt": "[bridge-probe:probe-pane-mismatch] hi",
                },
                state_file=fallback_events,
                public_state_file=fallback_public,
            )
        assert_true(code == 0, f"{label}: mismatched-pane prompt hook logger failed")
        mismatch_record = next((event for event in read_events(events) if event.get("attach_probe") == "probe-pane-mismatch"), None)
        assert_true(mismatch_record and mismatch_record.get("pane") == "%93", f"{label}: mismatched pane prompt must route by unique probe marker: {read_events(events)}")
        live = read_json(live_file, {"panes": {}, "sessions": {}})
        locks = read_json(pane_locks_file, {"panes": {}})
        assert_true("%not-the-marker-pane" not in (live.get("panes") or {}), f"{label}: raw mismatched pane must not receive live record: {live}")
        assert_true("%not-the-marker-pane" not in (locks.get("panes") or {}), f"{label}: raw mismatched pane must not receive pane lock: {locks}")
        assert_true(((live.get("panes") or {}).get("%93") or {}).get("session_id") == "new-mismatch", f"{label}: marker pane should receive live record: {live}")

        bridge_clear_marker.write_marker({
            **bridge_clear_marker.make_marker(
                bridge_session="test-session",
                alias="bob",
                agent="claude",
                old_session_id="old-a",
                probe_id="probe-ambiguous",
                pane="%93",
                target="tmux:1.1",
                events_file=str(events),
            ),
            "id": "ambiguous-a",
        })
        bridge_clear_marker.write_marker({
            **bridge_clear_marker.make_marker(
                bridge_session="test-session",
                alias="carol",
                agent="claude",
                old_session_id="old-b",
                probe_id="probe-ambiguous",
                pane="%94",
                target="tmux:1.2",
                events_file=str(events),
            ),
            "id": "ambiguous-b",
        })
        ambiguous = bridge_clear_marker.find_for_prompt(pane="", agent="claude", attach_probe="probe-ambiguous")
        assert_true(ambiguous is None, f"{label}: ambiguous pane-less probe lookup must not choose first marker: {ambiguous}")

        marker_no_probe = bridge_clear_marker.make_marker(
            bridge_session="test-session",
            alias="dave",
            agent="claude",
            old_session_id="old-no-probe",
            probe_id="probe-no-auto",
            pane="%95",
            target="tmux:1.3",
            events_file=str(events),
        )
        bridge_clear_marker.write_marker(marker_no_probe)
        with patched_environ(TMUX_PANE="%95"):
            code = _run_hook_logger_record(
                "claude",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "new-no-probe",
                    "turn_id": "turn-no-probe",
                    "prompt": "manual prompt without bridge probe marker",
                },
                state_file=fallback_events,
                public_state_file=fallback_public,
            )
        assert_true(code == 0, f"{label}: no-probe prompt hook logger failed")
        no_probe = bridge_clear_marker.read_markers()["markers"][marker_no_probe["id"]]
        assert_true(no_probe.get("phase") == "pending_prompt" and not no_probe.get("probe_turn_id"), f"{label}: no-attach-probe prompt must not mark prompt_seen: {no_probe}")

        marker_stop = bridge_clear_marker.make_marker(
            bridge_session="test-session",
            alias="erin",
            agent="claude",
            old_session_id="old-stop",
            probe_id="probe-stop",
            pane="%96",
            target="tmux:1.4",
            events_file=str(events),
        )
        marker_stop["phase"] = "prompt_seen"
        marker_stop["new_session_id"] = "new-stop"
        marker_stop["probe_turn_id"] = "turn-stop"
        bridge_clear_marker.write_marker(marker_stop)
        turn_only = bridge_clear_marker.find_for_stop(pane="", agent="claude", session_id="", turn_id="turn-stop")
        by_session = bridge_clear_marker.find_for_stop(pane="", agent="claude", session_id="new-stop", turn_id="")
        assert_true(turn_only is None, f"{label}: pane-less Stop must not recover by turn_id alone: {turn_only}")
        assert_true(by_session and by_session.get("alias") == "erin", f"{label}: pane-less Stop may recover by new_session_id: {by_session}")
    print(f"  PASS  {label}")


def scenario_clear_post_clear_first_request_routes_reply(label: str, tmpdir: Path) -> None:
    state_root_dir = tmpdir / "state"
    session_dir = state_root_dir / "test-session"
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    fallback_events = tmpdir / "fallback.raw.jsonl"
    fallback_public = tmpdir / "fallback.jsonl"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "tmux:1.2"},
    }
    with patched_environ(
        AGENT_BRIDGE_STATE_DIR=str(state_root_dir),
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
        AGENT_BRIDGE_SESSION=None,
        AGENT_BRIDGE_AGENT=None,
        AGENT_BRIDGE_BUS=None,
        AGENT_BRIDGE_EVENTS=None,
        AGENT_BRIDGE_PUBLIC_EVENTS=None,
        TMUX_PANE="%92",
    ):
        d = make_daemon(session_dir, participants)
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        original_tmux_target = bridge_identity.tmux_target_for_pane
        original_probe = bridge_identity.probe_agent_process
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.2"  # type: ignore[assignment]
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "verified", "pane": pane, "agent": agent, "processes": []}  # type: ignore[assignment]
        try:
            replaced = bridge_identity.replace_attached_session_identity_for_clear(
                bridge_session="test-session",
                alias="bob",
                agent_type="codex",
                old_session_id="old-session",
                new_session_id="new-session",
                pane="%92",
                target="tmux:1.2",
            )
            assert_true(replaced.get("ok") is True, f"{label}: identity replacement should succeed: {replaced}")
            registry = json.loads(registry_file.read_text(encoding="utf-8"))
            mapping = (registry.get("sessions") or {}).get("codex:new-session") or {}
            assert_true(mapping.get("events_file") == str(d.state_file), f"{label}: post-clear mapping must preserve raw bus path: {mapping}")
            assert_true(mapping.get("public_events_file") == str(d.public_state_file), f"{label}: post-clear mapping must preserve public bus path: {mapping}")
            assert_true(mapping.get("queue_file") == str(d.queue.path), f"{label}: post-clear mapping must preserve queue path: {mapping}")

            d.queue_message(test_message("msg-post-clear", frm="alice", to="bob", status="pending"))
            row = _queue_item(d, "msg-post-clear") or {}
            nonce = str(row.get("nonce") or "")
            assert_true(row.get("status") == "inflight" and d.reserved.get("bob") == "msg-post-clear" and nonce, f"{label}: first post-clear request should reserve/inflight with nonce: row={row}, reserved={d.reserved}")

            old_argv = sys.argv[:]
            old_stdin = sys.stdin
            try:
                sys.argv = [
                    "bridge_hook_logger.py",
                    "--agent",
                    "codex",
                    "--state-file",
                    str(fallback_events),
                    "--public-state-file",
                    str(fallback_public),
                ]
                sys.stdin = io.StringIO(json.dumps({
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "new-session",
                    "turn_id": "turn-post-clear",
                    "prompt": f"[bridge:{nonce}] from=alice kind=request",
                    "cwd": "/tmp",
                    "model": "codex-test",
                }))
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = bridge_hook_logger.main()
            finally:
                sys.argv = old_argv
                sys.stdin = old_stdin
            assert_true(code == 0, f"{label}: hook logger should succeed")

            raw_events = read_events(Path(d.state_file))
            prompt_record = next((event for event in reversed(raw_events) if event.get("event") == "prompt_submitted" and event.get("nonce") == nonce), None)
            assert_true(prompt_record is not None, f"{label}: unscoped post-clear hook must route to room raw bus, not fallback")
            fallback_records = read_events(fallback_events)
            assert_true(not any(event.get("nonce") == nonce for event in fallback_records), f"{label}: post-clear hook must not fall back to default state file")

            d.handle_prompt_submitted(prompt_record)
            ctx = d.current_prompt_by_agent.get("bob") or {}
            delivered = _queue_item(d, "msg-post-clear") or {}
            assert_true(ctx.get("id") == "msg-post-clear" and ctx.get("turn_id") == "turn-post-clear", f"{label}: prompt_submitted should bind current context: {ctx}")
            assert_true(delivered.get("status") == "delivered", f"{label}: prompt_submitted should mark row delivered: {delivered}")
            assert_true(d.reserved.get("bob") is None and "msg-post-clear" not in d.last_enter_ts, f"{label}: prompt_submitted should clear reserved and retry-enter state")

            d.handle_response_finished({
                "agent": "codex",
                "bridge_agent": "bob",
                "event": "response_finished",
                "hook_event_name": "Stop",
                "session_id": "new-session",
                "turn_id": "turn-post-clear",
                "last_assistant_message": "살아있습니다.",
            })
            auto_returns = [
                item for item in d.queue.read()
                if item.get("source") == "auto_return" and item.get("reply_to") == "msg-post-clear" and item.get("to") == "alice"
            ]
            assert_true(auto_returns and "살아있습니다." in str(auto_returns[0].get("body") or ""), f"{label}: response should auto-return to requester: {auto_returns}")
            assert_true(_queue_item(d, "msg-post-clear") is None, f"{label}: original request should be removed after terminal response")
            assert_true(not (d.current_prompt_by_agent.get("bob") or {}).get("id") and d.busy.get("bob") is False, f"{label}: terminal response should clear target context")
        finally:
            bridge_identity.tmux_target_for_pane = original_tmux_target  # type: ignore[assignment]
            bridge_identity.probe_agent_process = original_probe  # type: ignore[assignment]
    print(f"  PASS  {label}")


def scenario_clear_with_existing_inbound_queue_routes_replies(label: str, tmpdir: Path) -> None:
    state_root_dir = tmpdir / "state"
    session_dir = state_root_dir / "test-session"
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-old", "target": "tmux:1.1"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "bob-old", "target": "tmux:1.2"},
    }
    with patched_environ(
        AGENT_BRIDGE_STATE_DIR=str(state_root_dir),
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
    ):
        d = make_daemon(session_dir, participants)
        d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": True, "pane": "%92" if target == "bob" else "%91", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        original_tmux_target = bridge_identity.tmux_target_for_pane
        original_probe = bridge_identity.probe_agent_process
        targets = {"%91": "tmux:1.1", "%92": "tmux:1.2"}
        bridge_identity.tmux_target_for_pane = lambda pane: targets.get(str(pane), str(pane))  # type: ignore[assignment]
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "verified", "pane": pane, "agent": agent, "processes": []}  # type: ignore[assignment]
        try:
            bob_replaced = bridge_identity.replace_attached_session_identity_for_clear(
                bridge_session="test-session",
                alias="bob",
                agent_type="codex",
                old_session_id="bob-old",
                new_session_id="bob-new",
                pane="%92",
                target="tmux:1.2",
            )
            assert_true(bob_replaced.get("ok") is True, f"{label}: bob post-clear identity replacement should succeed: {bob_replaced}")
            d.session_mtime_ns = None
            d.reload_participants()

            message_ids = ["msg-clear-queue-1", "msg-clear-queue-2", "msg-clear-queue-3"]
            for idx, message_id in enumerate(message_ids, start=1):
                msg = test_message(message_id, frm="alice", to="bob", status="pending")
                msg["body"] = f"queued request {idx}"
                d.queue_message(msg)
            first = _queue_item(d, message_ids[0]) or {}
            assert_true(first.get("status") == "inflight" and first.get("nonce"), f"{label}: first queued request should reserve: {first}")
            for message_id in message_ids[1:]:
                row = _queue_item(d, message_id) or {}
                assert_true(row.get("status") == "pending", f"{label}: later requests should wait behind first: {row}")

            alice_replaced = bridge_identity.replace_attached_session_identity_for_clear(
                bridge_session="test-session",
                alias="alice",
                agent_type="codex",
                old_session_id="alice-old",
                new_session_id="alice-new",
                pane="%91",
                target="tmux:1.1",
            )
            assert_true(alice_replaced.get("ok") is True, f"{label}: source post-clear identity replacement should not corrupt queued rows: {alice_replaced}")
            d.session_mtime_ns = None
            d.reload_participants()

            for idx, message_id in enumerate(message_ids, start=1):
                row = _queue_item(d, message_id) or {}
                nonce = str(row.get("nonce") or "")
                assert_true(row.get("status") == "inflight" and nonce, f"{label}: request {idx} should be active one-at-a-time before prompt: {row}")
                d.handle_prompt_submitted({
                    "agent": "codex",
                    "bridge_agent": "bob",
                    "session_id": "bob-new",
                    "nonce": nonce,
                    "turn_id": f"turn-clear-queue-{idx}",
                    "prompt": f"[bridge:{nonce}] from=alice kind=request",
                })
                delivered = _queue_item(d, message_id) or {}
                assert_true(delivered.get("status") == "delivered", f"{label}: request {idx} prompt should bind: {delivered}")
                assert_true(d.reserved.get("bob") is None, f"{label}: request {idx} prompt should clear bob reservation: {d.reserved}")
                if idx < len(message_ids):
                    next_row = _queue_item(d, message_ids[idx]) or {}
                    assert_true(next_row.get("status") == "pending", f"{label}: next request must not advance before terminal response: {next_row}")

                d.handle_response_finished({
                    "agent": "codex",
                    "bridge_agent": "bob",
                    "session_id": "bob-new",
                    "turn_id": f"turn-clear-queue-{idx}",
                    "last_assistant_message": f"reply {idx}",
                })
                assert_true(_queue_item(d, message_id) is None, f"{label}: request {idx} should be removed after terminal response")
                auto_return = [
                    item for item in d.queue.read()
                    if item.get("source") == "auto_return"
                    and item.get("reply_to") == message_id
                    and item.get("to") == "alice"
                    and f"reply {idx}" in str(item.get("body") or "")
                ]
                assert_true(auto_return, f"{label}: request {idx} response should auto-return to alice")
                if idx < len(message_ids):
                    next_row = _queue_item(d, message_ids[idx]) or {}
                    assert_true(next_row.get("status") == "inflight" and next_row.get("nonce"), f"{label}: next request should advance after terminal response: {next_row}")
            assert_true(not any((_queue_item(d, message_id) or {}).get("to") == "bob" for message_id in message_ids), f"{label}: all original bob-bound requests should be consumed")
        finally:
            bridge_identity.tmux_target_for_pane = original_tmux_target  # type: ignore[assignment]
            bridge_identity.probe_agent_process = original_probe  # type: ignore[assignment]
    print(f"  PASS  {label}")


def scenario_clear_file_fallback_blocked_sender_aged_ingress(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"}, "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"}}
    d = make_daemon(tmpdir, participants)
    d.pending_self_clears["alice"] = {"clear_id": "clear-self", "target": "alice"}
    old = test_message("msg-aged-blocked", frm="alice", to="bob", status="ingressing")
    old["created_ts"] = "2000-01-01T00:00:00Z"
    d.queue.update(lambda queue: queue.append(old))

    d._promote_aged_ingressing()
    assert_true(_queue_item(d, "msg-aged-blocked") is None, f"{label}: aged ingress from blocked sender must be dropped")
    events = read_events(Path(d.state_file))
    assert_true(any(e.get("event") == "ingressing_promotion_dropped_sender_clear_blocked" for e in events), f"{label}: drop must be logged")
    print(f"  PASS  {label}")


def _set_command_context_near_lock_deadline(d: bridge_daemon.BridgeDaemon, op: str, remaining: float) -> None:
    d.begin_command_context(op)
    timeout = float(d.command_context.info.get("client_timeout") or 0.0)
    d.command_context.info["started_ts"] = time.monotonic() - max(0.0, timeout - remaining)


def _run_with_held_state_lock(
    d: bridge_daemon.BridgeDaemon,
    op: str,
    call,
    *,
    remaining: float,
    label: str,
):
    result: dict[str, object] = {}

    def worker() -> None:
        try:
            _set_command_context_near_lock_deadline(d, op, remaining)
            result["value"] = call()
        except Exception as exc:
            result["error"] = exc
        finally:
            d.command_context.info = {}

    with d.state_lock:
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(1.0)
        assert_true(not thread.is_alive(), f"{label}: {op} must stop waiting before the held lock is released")
    thread.join(1.0)
    assert_true("error" not in result, f"{label}: {op} raised {result.get('error')!r}")
    return result.get("value")


def _run_hook_logger_record(agent: str, payload: dict, *, state_file: Path, public_state_file: Path) -> int:
    old_argv = sys.argv[:]
    old_stdin = sys.stdin
    try:
        sys.argv = [
            "bridge_hook_logger.py",
            "--agent",
            agent,
            "--state-file",
            str(state_file),
            "--public-state-file",
            str(public_state_file),
        ]
        sys.stdin = io.StringIO(json.dumps(payload))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return int(bridge_hook_logger.main())
    finally:
        sys.argv = old_argv
        sys.stdin = old_stdin


def scenario_clear_post_clear_delay_config_and_settle(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
    }
    with patched_environ(AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC=None):
        d_default = make_daemon(tmpdir / "default", participants)
        assert_true(d_default.clear_post_clear_delay_seconds == bridge_daemon.CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS, f"{label}: default clear settle delay expected")
        budget, _margin = d_default.command_budget("clear_peer")
        assert_true(budget == bridge_daemon.CLEAR_POST_LOCK_WORST_CASE_SECONDS + bridge_daemon.CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS, f"{label}: clear budget must include default delay")
    for raw, expected in [
        ("0", 0.0),
        ("not-a-number", bridge_daemon.CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS),
        ("inf", bridge_daemon.CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS),
        ("nan", bridge_daemon.CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS),
        ("-2", 0.0),
    ]:
        with patched_environ(AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC=raw):
            d = make_daemon(tmpdir / f"delay-{raw.replace('-', 'neg')}", participants)
            assert_true(d.clear_post_clear_delay_seconds == expected, f"{label}: delay {raw!r} parsed as {expected}, got {d.clear_post_clear_delay_seconds}")

    with patched_environ(
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json"),
        AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC="0.05",
    ):
        d = make_daemon(tmpdir / "settle", participants)
        d.dry_run = False
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        sends: list[str] = []

        def fake_send(_pane: str, text: str, *, target: str, message_id: str) -> dict:
            sends.append(text)
            return {"ok": True, "pane_touched": True, "error": ""}

        d._clear_tmux_send = fake_send  # type: ignore[method-assign]
        result: dict[str, object] = {}

        def runner() -> None:
            result["value"] = d.run_clear_peer("alice", "bob", force=False)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        saw_settle = False
        saw_probe = False
        for _ in range(200):
            with d.state_lock:
                reservation = d.clear_reservations.get("bob") or {}
                if reservation.get("phase") == "post_clear_settle":
                    saw_settle = True
                    assert_true(sends == ["/clear"], f"{label}: probe must not paste during settle: {sends}")
                if reservation.get("phase") == "waiting_probe":
                    saw_probe = True
                    assert_true(len(sends) == 2 and sends[0] == "/clear" and "[bridge-probe:" in sends[1], f"{label}: probe should paste after settle: {sends}")
                    reservation["failure_reason"] = "test_stop"
                    condition = reservation.get("condition")
                    if isinstance(condition, threading.Condition):
                        condition.notify_all()
                    break
            time.sleep(0.005)
        thread.join(1.0)
        assert_true(saw_settle and saw_probe, f"{label}: expected post_clear_settle then waiting_probe phases, result={result}, sends={sends}")
        assert_true(not thread.is_alive(), f"{label}: runner should stop after injected failure")

    long_delay = 240.0
    with patched_environ(
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears-long.json"),
        AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC=str(long_delay),
    ):
        d = make_daemon(tmpdir / "long-settle", participants)
        d.dry_run = False
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        d._clear_tmux_send = lambda _pane, _text, *, target, message_id: {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]
        result = {}

        def runner_long() -> None:
            result["value"] = d.run_clear_peer("alice", "bob", force=False)

        thread = threading.Thread(target=runner_long, daemon=True)
        thread.start()
        saw_long_settle = False
        for _ in range(200):
            with d.state_lock:
                reservation = d.clear_reservations.get("bob") or {}
                if reservation.get("phase") == "post_clear_settle":
                    saw_long_settle = True
                    marker = (bridge_clear_marker.read_markers().get("markers") or {}).get(str(reservation.get("identity_marker_id") or "")) or {}
                    ttl = float(marker.get("expires_ts") or 0.0) - float(marker.get("created_ts") or 0.0)
                    required_ttl = bridge_clear_marker.ttl_for_clear_lifetime(
                        settle_delay_seconds=long_delay,
                        probe_timeout_seconds=bridge_daemon.CLEAR_PROBE_TIMEOUT_SECONDS,
                    )
                    assert_true(ttl >= required_ttl - 0.001, f"{label}: marker TTL must cover configured clear lifetime: ttl={ttl}, required={required_ttl}, marker={marker}")
                    assert_true(ttl > bridge_clear_marker.CONTROLLED_CLEAR_MARKER_TTL_SECONDS, f"{label}: long settle must extend default marker TTL: ttl={ttl}")
                    reservation["failure_reason"] = "test_stop"
                    condition = reservation.get("condition")
                    if isinstance(condition, threading.Condition):
                        condition.notify_all()
                    break
            time.sleep(0.005)
        thread.join(1.0)
        assert_true(saw_long_settle, f"{label}: long settle marker should be inspectable, result={result}")
        assert_true(not thread.is_alive(), f"{label}: long-settle runner should stop after injected failure")
    print(f"  PASS  {label}")


def scenario_clear_settle_pane_not_ready_forces_leave(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
    }
    with patched_environ(
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json"),
        AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC="0",
    ):
        d = make_daemon(tmpdir, participants)
        d.dry_run = False
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        mode_calls: list[str] = []

        def pane_mode(_pane: str) -> dict:
            mode_calls.append("mode")
            if len(mode_calls) == 1:
                return {"in_mode": False, "mode": "", "error": ""}
            return {"in_mode": True, "mode": "copy-mode", "error": ""}

        d.pane_mode_status = pane_mode  # type: ignore[method-assign]
        sends: list[str] = []
        d._clear_tmux_send = lambda _pane, text, *, target, message_id: sends.append(text) or {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]
        forced: list[str] = []

        def fake_force_leave(target: str, *, caller: str, reason: str, reservation: dict) -> None:
            forced.append(reason)
            d.clear_reservations.pop(target, None)

        d.force_leave_after_clear_failure = fake_force_leave  # type: ignore[method-assign]
        result = d.run_clear_peer("alice", "bob", force=False)
        assert_true(result.get("error_kind") == "clear_settle_pane_not_ready", f"{label}: settle pane failure expected: {result}")
        assert_true(forced and forced[0].startswith("clear_settle_pane_not_ready:"), f"{label}: forced leave reason should be stable: {forced}")
        assert_true(sends == ["/clear"], f"{label}: probe must not paste after settle pane failure: {sends}")
        assert_true(len(mode_calls) == 2, f"{label}: pane mode should be checked before and after settle: {mode_calls}")
    print(f"  PASS  {label}")


def scenario_clear_identity_helper_preserves_verified_live_identity(label: str, tmpdir: Path) -> None:
    state_dir = tmpdir / "state"
    session_dir = state_dir / "test-session"
    session_dir.mkdir(parents=True)
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    (session_dir / "session.json").write_text(json.dumps({
        "session": "test-session",
        "participants": {
            "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "status": "active"}
        },
    }), encoding="utf-8")
    identity = verified_identity("codex", "%92", pid=9200, start_time="920")
    live_new = identity_live_record(agent="codex", alias="bob", session_id="new-session", pane="%92", process_identity=identity)
    live_file.write_text(json.dumps({"version": 1, "panes": {"%92": live_new}, "sessions": {"codex:new-session": live_new}}), encoding="utf-8")
    with patched_environ(
        AGENT_BRIDGE_STATE_DIR=str(state_dir),
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
    ):
        result = bridge_identity.replace_attached_session_identity_for_clear(
            bridge_session="test-session",
            alias="bob",
            agent_type="codex",
            old_session_id="old-session",
            new_session_id="new-session",
            pane="%92",
            target="test:1.2",
            process_identity={},
        )
        assert_true(result.get("ok") is True, f"{label}: helper should preserve existing verified identity: {result}")
        assert_true(((result.get("live_record") or {}).get("process_identity") or {}).get("status") == "verified", f"{label}: result live_record lost identity: {result}")
        live_after = json.loads(live_file.read_text(encoding="utf-8"))
        assert_true((((live_after.get("panes") or {}).get("%92") or {}).get("process_identity") or {}).get("status") == "verified", f"{label}: pane live identity lost: {live_after}")
        assert_true((((live_after.get("sessions") or {}).get("codex:new-session") or {}).get("process_identity") or {}).get("status") == "verified", f"{label}: session live identity lost: {live_after}")
    print(f"  PASS  {label}")


def scenario_clear_requires_verified_process_identity(label: str, tmpdir: Path) -> None:
    state_root_dir = tmpdir / "state"
    session_dir = state_root_dir / "test-session"
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
    }
    with patched_environ(
        AGENT_BRIDGE_STATE_DIR=str(state_root_dir),
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json"),
        AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC="0",
    ):
        d = make_daemon(session_dir, participants)
        d.dry_run = False
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        d._clear_tmux_send = lambda _pane, _text, *, target, message_id: {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]
        old_probe = bridge_daemon.probe_agent_process
        bridge_daemon.probe_agent_process = lambda pane, agent: {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}  # type: ignore[assignment]
        result: dict[str, object] = {}
        try:
            def runner() -> None:
                result["value"] = d.run_clear_peer("alice", "bob", force=False)

            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            for _ in range(200):
                with d.state_lock:
                    reservation = d.clear_reservations.get("bob")
                    if reservation and reservation.get("phase") == "waiting_probe":
                        marker_id = str(reservation.get("identity_marker_id") or "")
                        probe_id = str(reservation.get("probe_id") or "")
                        bridge_clear_marker.update_marker(
                            marker_id,
                            lambda current: {
                                **current,
                                "phase": "prompt_seen",
                                "new_session_id": "new-session",
                                "probe_turn_id": "turn-probe",
                            },
                        )
                        d.handle_clear_prompt_submitted_locked(
                            "bob",
                            {
                                "event": "prompt_submitted",
                                "attach_probe": probe_id,
                                "session_id": "new-session",
                                "turn_id": "turn-probe",
                            },
                        )
                        break
                time.sleep(0.01)
            d.handle_response_finished({
                "agent": "codex",
                "bridge_agent": "bob",
                "event": "response_finished",
                "session_id": "new-session",
                "turn_id": "turn-probe",
                "last_assistant_message": "probe complete",
            })
            thread.join(2.0)
        finally:
            bridge_daemon.probe_agent_process = old_probe  # type: ignore[assignment]
        value = result.get("value")
        assert_true(isinstance(value, dict) and value.get("error_kind") == "clear_process_identity_unverified", f"{label}: clear must fail without verified process identity: {value}")
        state = read_json(session_dir / "session.json", {})
        bob = (state.get("participants") or {}).get("bob") or {}
        assert_true(bob.get("status") == "detached" and bob.get("endpoint_status") == "cleared", f"{label}: failed post-touch clear must not leave active alias: {bob}")
    print(f"  PASS  {label}")


def scenario_clear_codex_post_clear_endpoint_verify_immediately(label: str, tmpdir: Path) -> None:
    state_root_dir = tmpdir / "state"
    session_dir = state_root_dir / "test-session"
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session", "target": "test:1.1"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
    }
    with patched_environ(
        AGENT_BRIDGE_STATE_DIR=str(state_root_dir),
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json"),
        AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC="0",
        AGENT_BRIDGE_SESSION=None,
        AGENT_BRIDGE_AGENT=None,
        AGENT_BRIDGE_BUS=None,
        AGENT_BRIDGE_EVENTS=None,
        AGENT_BRIDGE_PUBLIC_EVENTS=None,
        TMUX_PANE="%92",
    ):
        d = make_daemon(session_dir, participants)
        d.dry_run = False
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        sends: list[str] = []
        d._clear_tmux_send = lambda _pane, text, *, target, message_id: sends.append(text) or {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]
        identity = verified_identity("codex", "%92", pid=9300, start_time="930")
        old_target = bridge_identity.tmux_target_for_pane
        old_identity_probe = bridge_identity.probe_agent_process
        old_daemon_probe = bridge_daemon.probe_agent_process
        bridge_identity.tmux_target_for_pane = lambda pane: "test:1.2" if pane == "%92" else "test:1.1"  # type: ignore[assignment]
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        bridge_daemon.probe_agent_process = lambda pane, agent: dict(identity)  # type: ignore[assignment]
        result: dict[str, object] = {}
        try:
            def runner() -> None:
                result["value"] = d.run_clear_peer("alice", "bob", force=False)

            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            reservation: dict | None = None
            for _ in range(200):
                with d.state_lock:
                    reservation = d.clear_reservations.get("bob")
                    if reservation and reservation.get("phase") == "waiting_probe":
                        break
                time.sleep(0.01)
            assert_true(reservation and reservation.get("phase") == "waiting_probe", f"{label}: clear probe should be waiting: {reservation}")
            probe_id = str(reservation.get("probe_id") or "")
            code = _run_hook_logger_record(
                "codex",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "new-session",
                    "turn_id": "turn-probe",
                    "prompt": f"[bridge-probe:{probe_id}] bridge clear probe",
                    "cwd": "/tmp",
                    "model": "codex-test",
                },
                state_file=Path(d.state_file),
                public_state_file=Path(d.public_state_file),
            )
            assert_true(code == 0, f"{label}: prompt hook logger failed")
            prompt_record = next(event for event in reversed(read_events(Path(d.state_file))) if event.get("event") == "prompt_submitted" and event.get("attach_probe") == probe_id)
            d.handle_prompt_submitted(prompt_record)

            code = _run_hook_logger_record(
                "codex",
                {
                    "hook_event_name": "Stop",
                    "session_id": "new-session",
                    "turn_id": "turn-probe",
                    "last_assistant_message": "bridge clear probe complete",
                    "cwd": "/tmp",
                    "model": "codex-test",
                },
                state_file=Path(d.state_file),
                public_state_file=Path(d.public_state_file),
            )
            assert_true(code == 0, f"{label}: stop hook logger failed")
            stop_record = next(event for event in reversed(read_events(Path(d.state_file))) if event.get("event") == "response_finished" and event.get("turn_id") == "turn-probe")
            d.handle_response_finished(stop_record)
            thread.join(2.0)
        finally:
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
            bridge_identity.probe_agent_process = old_identity_probe  # type: ignore[assignment]
            bridge_daemon.probe_agent_process = old_daemon_probe  # type: ignore[assignment]
        value = result.get("value")
        assert_true(isinstance(value, dict) and value.get("ok") is True, f"{label}: clear should complete: {value}")
        assert_true(len(sends) == 2 and sends[0] == "/clear" and "[bridge-probe:" in sends[1], f"{label}: send order should be clear then probe: {sends}")
        state = read_json(session_dir / "session.json", {})
        bob = (state.get("participants") or {}).get("bob") or {}
        old_identity_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "bob", bob)
        finally:
            bridge_identity.probe_agent_process = old_identity_probe  # type: ignore[assignment]
        assert_true(detail.get("ok") is True and detail.get("reason") == "ok", f"{label}: endpoint should verify immediately without user prompt: {detail}")
        live = read_json(live_file, {"panes": {}, "sessions": {}})
        live_pane = (live.get("panes") or {}).get("%92") or {}
        live_session = (live.get("sessions") or {}).get("codex:new-session") or {}
        assert_true(((live_pane.get("process_identity") or {}).get("status") == "verified"), f"{label}: pane live identity missing: {live}")
        assert_true(((live_session.get("process_identity") or {}).get("status") == "verified"), f"{label}: session live identity missing: {live}")
    print(f"  PASS  {label}")


def scenario_clear_claude_post_clear_probe_pasted_and_completes(label: str, tmpdir: Path) -> None:
    state_root_dir = tmpdir / "state"
    session_dir = state_root_dir / "test-session"
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    fallback_events = tmpdir / "fallback.raw.jsonl"
    fallback_public = tmpdir / "fallback.jsonl"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session", "target": "test:1.1"},
        "bob": {"alias": "bob", "agent_type": "claude", "pane": "%93", "hook_session_id": "old-claude-session", "target": "test:1.3"},
    }
    with patched_environ(
        AGENT_BRIDGE_STATE_DIR=str(state_root_dir),
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json"),
        AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC="0.05",
        AGENT_BRIDGE_SESSION=None,
        AGENT_BRIDGE_AGENT=None,
        AGENT_BRIDGE_BUS=None,
        AGENT_BRIDGE_EVENTS=None,
        AGENT_BRIDGE_PUBLIC_EVENTS=None,
        TMUX_PANE="%93",
    ):
        d = make_daemon(session_dir, participants)
        d.dry_run = False
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%93", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        sends: list[str] = []
        d._clear_tmux_send = lambda _pane, text, *, target, message_id: sends.append(text) or {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]
        forced: list[str] = []
        original_force_leave = d.force_leave_after_clear_failure

        def record_force_leave(target: str, *, caller: str, reason: str, reservation: dict) -> None:
            forced.append(reason)
            original_force_leave(target, caller=caller, reason=reason, reservation=reservation)

        d.force_leave_after_clear_failure = record_force_leave  # type: ignore[method-assign]
        identity = verified_identity("claude", "%93", pid=9400, start_time="940")
        old_live = identity_live_record(
            agent="claude",
            alias="bob",
            session_id="old-claude-session",
            pane="%93",
            process_identity=identity,
        )
        old_mapping = {
            "agent": "claude",
            "alias": "bob",
            "session_id": "old-claude-session",
            "bridge_session": "test-session",
            "pane": "%93",
            "target": "test:1.3",
            "events_file": str(d.state_file),
            "public_events_file": str(d.public_state_file),
            "queue_file": str(d.queue.path),
            "attached_at": utc_now(),
            "last_seen_at": utc_now(),
        }
        registry_file.write_text(json.dumps({"version": 1, "sessions": {"claude:old-claude-session": old_mapping}}), encoding="utf-8")
        pane_locks_file.write_text(json.dumps({
            "version": 1,
            "panes": {
                "%93": {
                    "bridge_session": "test-session",
                    "agent": "claude",
                    "alias": "bob",
                    "target": "test:1.3",
                    "hook_session_id": "old-claude-session",
                },
            },
        }), encoding="utf-8")
        live_file.write_text(json.dumps({
            "version": 1,
            "panes": {"%93": old_live},
            "sessions": {"claude:old-claude-session": old_live},
        }), encoding="utf-8")
        old_target = bridge_identity.tmux_target_for_pane
        old_identity_probe = bridge_identity.probe_agent_process
        old_daemon_probe = bridge_daemon.probe_agent_process
        bridge_identity.tmux_target_for_pane = lambda pane: "test:1.3" if pane == "%93" else "test:1.1"  # type: ignore[assignment]
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        bridge_daemon.probe_agent_process = lambda pane, agent: dict(identity)  # type: ignore[assignment]
        result: dict[str, object] = {}
        try:
            def runner() -> None:
                result["value"] = d.run_clear_peer("alice", "bob", force=False)

            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            reservation: dict | None = None
            for _ in range(200):
                with d.state_lock:
                    reservation = d.clear_reservations.get("bob")
                    if reservation and reservation.get("phase") == "post_clear_settle":
                        break
                time.sleep(0.01)
            assert_true(reservation and reservation.get("phase") == "post_clear_settle", f"{label}: claude clear must enter settle before probe: {reservation}")
            assert_true(sends == ["/clear"], f"{label}: SessionStart should occur before probe paste in this regression: {sends}")
            code = _run_hook_logger_record(
                "claude",
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": "old-claude-session",
                    "cwd": "/tmp",
                    "model": "claude-test",
                },
                state_file=fallback_events,
                public_state_file=fallback_public,
            )
            assert_true(code == 0, f"{label}: claude old SessionEnd hook logger failed")
            state_after_end = read_json(session_dir / "session.json", {})
            bob_after_end = (state_after_end.get("participants") or {}).get("bob") or {}
            assert_true(bob_after_end.get("status", "active") == "active", f"{label}: old-session SessionEnd during controlled clear must not detach participant: {bob_after_end}")
            locks_after_end = read_json(pane_locks_file, {"panes": {}})
            lock_after_end = (locks_after_end.get("panes") or {}).get("%93") or {}
            assert_true(lock_after_end.get("hook_session_id") == "old-claude-session", f"{label}: old-session SessionEnd must not remove pane lock: {locks_after_end}")
            registry_after_end = read_json(registry_file, {"sessions": {}})
            assert_true("claude:old-claude-session" in (registry_after_end.get("sessions") or {}), f"{label}: old-session SessionEnd must not remove attached registry: {registry_after_end}")
            live_after_end = read_json(live_file, {"panes": {}, "sessions": {}})
            assert_true("%93" not in (live_after_end.get("panes") or {}), f"{label}: old live pane may be cleaned after SessionEnd: {live_after_end}")
            assert_true("claude:old-claude-session" not in (live_after_end.get("sessions") or {}), f"{label}: old live session may be cleaned after SessionEnd: {live_after_end}")
            assert_true(
                any(event.get("event") == "controlled_clear_session_end_suppressed" for event in read_events(Path(d.state_file))),
                f"{label}: suppression diagnostic should be logged: {read_events(Path(d.state_file))}",
            )
            code = _run_hook_logger_record(
                "claude",
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "new-claude-session",
                    "source": "clear",
                    "cwd": "/tmp",
                    "model": "claude-test",
                },
                state_file=fallback_events,
                public_state_file=fallback_public,
            )
            assert_true(code == 0, f"{label}: claude SessionStart hook logger failed")
            session_record = next((event for event in reversed(read_events(Path(d.state_file))) if event.get("event") == "session_start" and event.get("session_id") == "new-claude-session"), None)
            assert_true(session_record and session_record.get("pane") == "%93", f"{label}: SessionStart must route to room bus via clear marker: {read_events(Path(d.state_file))}")
            assert_true(not any(event.get("session_id") == "new-claude-session" for event in read_events(fallback_events)), f"{label}: SessionStart must not route to fallback bus")
            marker_id = str(reservation.get("identity_marker_id") or "")
            marker_after_session = (bridge_clear_marker.read_markers().get("markers") or {}).get(marker_id) or {}
            assert_true(marker_after_session.get("new_session_id") == "new-claude-session" and marker_after_session.get("phase") == "pending_prompt", f"{label}: SessionStart may record session but not mark prompt_seen: {marker_after_session}")

            for _ in range(200):
                with d.state_lock:
                    reservation = d.clear_reservations.get("bob")
                    if reservation and reservation.get("phase") == "waiting_probe":
                        break
                time.sleep(0.01)
            assert_true(reservation and reservation.get("phase") == "waiting_probe", f"{label}: claude clear must reach probe wait after settle: {reservation}")
            assert_true(len(sends) == 2 and sends[0] == "/clear" and "[bridge-probe:" in sends[1], f"{label}: probe should paste after settle: {sends}")
            probe_id = str(reservation.get("probe_id") or "")
            with patched_environ(TMUX_PANE=None):
                code = _run_hook_logger_record(
                    "claude",
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "new-claude-session",
                        "prompt": f"[bridge-probe:{probe_id}] bridge clear probe",
                        "cwd": "/tmp",
                        "model": "claude-test",
                    },
                    state_file=fallback_events,
                    public_state_file=fallback_public,
                )
            assert_true(code == 0, f"{label}: claude prompt hook logger failed")
            prompt_record = next(event for event in reversed(read_events(Path(d.state_file))) if event.get("event") == "prompt_submitted" and event.get("attach_probe") == probe_id)
            assert_true(prompt_record.get("pane") == "%93", f"{label}: prompt_submitted must normalize marker pane: {prompt_record}")
            assert_true(prompt_record.get("session_id") == "new-claude-session" and "turn_id" not in prompt_record, f"{label}: production Claude prompt hook shape should have no turn_id: {prompt_record}")
            assert_true(not any(event.get("attach_probe") == probe_id for event in read_events(fallback_events)), f"{label}: prompt_submitted must not route to fallback bus")
            d.handle_prompt_submitted(prompt_record)
            assert_true(
                any(event.get("event") == "clear_probe_prompt_seen" and event.get("target") == "bob" for event in read_events(Path(d.state_file))),
                f"{label}: daemon should accept no-turn prompt phase2: {read_events(Path(d.state_file))}",
            )
            with patched_environ(TMUX_PANE=None):
                code = _run_hook_logger_record(
                    "claude",
                    {
                        "hook_event_name": "Stop",
                        "session_id": "new-claude-session",
                        "last_assistant_message": "bridge clear probe complete",
                        "cwd": "/tmp",
                        "model": "claude-test",
                    },
                    state_file=fallback_events,
                    public_state_file=fallback_public,
                )
            assert_true(code == 0, f"{label}: claude stop hook logger failed")
            stop_record = next(event for event in reversed(read_events(Path(d.state_file))) if event.get("event") == "response_finished" and event.get("session_id") == "new-claude-session")
            assert_true(stop_record.get("pane") == "%93", f"{label}: Stop must normalize marker pane: {stop_record}")
            assert_true("turn_id" not in stop_record, f"{label}: production Claude Stop hook shape should have no turn_id: {stop_record}")
            d.handle_response_finished(stop_record)
            assert_true(
                any(event.get("event") == "clear_probe_response_finished" and event.get("target") == "bob" for event in read_events(Path(d.state_file))),
                f"{label}: daemon should accept no-turn response by new session id: {read_events(Path(d.state_file))}",
            )
            thread.join(2.0)
        finally:
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
            bridge_identity.probe_agent_process = old_identity_probe  # type: ignore[assignment]
            bridge_daemon.probe_agent_process = old_daemon_probe  # type: ignore[assignment]
        value = result.get("value")
        assert_true(isinstance(value, dict) and value.get("ok") is True, f"{label}: claude clear should complete: {value}")
        assert_true(not forced, f"{label}: forced leave must not run: {forced}")
        state = read_json(session_dir / "session.json", {})
        bob = (state.get("participants") or {}).get("bob") or {}
        assert_true(bob.get("status") == "active" and bob.get("hook_session_id") == "new-claude-session", f"{label}: alias should remain active after clear: {bob}")
    print(f"  PASS  {label}")


def scenario_clear_session_end_suppression_is_exact(label: str, tmpdir: Path) -> None:
    def run_session_end(*, pane: str, session_id: str, events: Path, public_events: Path) -> None:
        with patched_environ(
            AGENT_BRIDGE_SESSION=None,
            AGENT_BRIDGE_AGENT=None,
            AGENT_BRIDGE_BUS=None,
            AGENT_BRIDGE_EVENTS=None,
            AGENT_BRIDGE_PUBLIC_EVENTS=None,
            TMUX_PANE=pane,
        ):
            code = _run_hook_logger_record(
                "claude",
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": session_id,
                    "cwd": "/tmp",
                    "model": "claude-test",
                },
                state_file=events,
                public_state_file=public_events,
            )
        assert_true(code == 0, f"{label}: SessionEnd hook logger failed")

    normal_dir = tmpdir / "normal"
    with isolated_identity_env(normal_dir) as state_root_path:
        write_identity_fixture(state_root_path, alias="bob", agent="claude", session_id="old-session", pane="%98")
        events = state_root_path / "test-session" / "events.raw.jsonl"
        public_events = state_root_path / "test-session" / "events.jsonl"
        run_session_end(pane="%98", session_id="old-session", events=events, public_events=public_events)
        state = read_json(state_root_path / "test-session" / "session.json", {})
        bob = (state.get("participants") or {}).get("bob") or {}
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true(bob.get("status") == "detached", f"{label}: normal SessionEnd must detach participant: {bob}")
        assert_true("%98" not in (locks.get("panes") or {}), f"{label}: normal SessionEnd must remove matching pane lock: {locks}")

    near_dir = tmpdir / "near-miss"
    with isolated_identity_env(near_dir) as state_root_path:
        marker_file = near_dir / "controlled-clears.json"
        with patched_environ(AGENT_BRIDGE_CONTROLLED_CLEARS=str(marker_file)):
            write_identity_fixture(state_root_path, alias="bob", agent="claude", session_id="old-session", pane="%98")
            marker = bridge_clear_marker.make_marker(
                bridge_session="test-session",
                alias="bob",
                agent="claude",
                old_session_id="different-old-session",
                probe_id="probe-near-miss",
                pane="%98",
                target="tmux:1.0",
                events_file=str(state_root_path / "test-session" / "events.raw.jsonl"),
            )
            bridge_clear_marker.write_marker(marker)
            events = state_root_path / "test-session" / "events.raw.jsonl"
            public_events = state_root_path / "test-session" / "events.jsonl"
            run_session_end(pane="%98", session_id="old-session", events=events, public_events=public_events)
            state = read_json(state_root_path / "test-session" / "session.json", {})
            bob = (state.get("participants") or {}).get("bob") or {}
            locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
            assert_true(bob.get("status") == "detached", f"{label}: near-miss marker must not suppress SessionEnd: {bob}")
            assert_true("%98" not in (locks.get("panes") or {}), f"{label}: near-miss marker must not keep pane lock: {locks}")
    print(f"  PASS  {label}")


def scenario_command_state_lock_timeout_before_mutation(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "session-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "session-bob"},
    }

    d = make_daemon(tmpdir / "enqueue", participants)
    required = sum(d.command_budget("enqueue"))
    value = _run_with_held_state_lock(
        d,
        "enqueue",
        lambda: d.handle_enqueue_command([test_message("msg-lock-enqueue", frm="alice", to="bob")]),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: enqueue timeout response expected: {value}")
    assert_true(_queue_item(d, "msg-lock-enqueue") is None, f"{label}: enqueue must not mutate queue after lock timeout")

    d = make_daemon(tmpdir / "alarm", participants)
    required = sum(d.command_budget("alarm"))
    value = _run_with_held_state_lock(
        d,
        "alarm",
        lambda: d.register_alarm_result("alice", 5.0, "wake", wake_id="wake-111111111111"),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: alarm timeout response expected: {value}")
    assert_true("wake-111111111111" not in d.watchdogs, f"{label}: alarm must not register after lock timeout")

    d = make_daemon(tmpdir / "cancel", participants)
    d.queue.update(lambda queue: queue.append(test_message("msg-lock-cancel", frm="alice", to="bob", status="pending")))
    required = sum(d.command_budget("cancel_message"))
    value = _run_with_held_state_lock(
        d,
        "cancel_message",
        lambda: d.cancel_message("alice", "msg-lock-cancel"),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: cancel timeout response expected: {value}")
    assert_true(_queue_item(d, "msg-lock-cancel") is not None, f"{label}: cancel must not remove queue row after lock timeout")

    d = make_daemon(tmpdir / "extend", participants)
    delivered = test_message("msg-lock-extend", frm="alice", to="bob", status="delivered")
    delivered["auto_return"] = True
    d.queue.update(lambda queue: queue.append(delivered))
    required = sum(d.command_budget("extend_watchdog"))
    value = _run_with_held_state_lock(
        d,
        "extend_watchdog",
        lambda: d.upsert_message_watchdog("alice", "msg-lock-extend", 10.0),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(value == (False, "lock_wait_exceeded", None), f"{label}: extend timeout tuple expected: {value}")
    assert_true(not d.watchdogs, f"{label}: extend must not register watchdog after lock timeout")

    d = make_daemon(tmpdir / "interrupt", participants)
    required = sum(d.command_budget("interrupt"))
    value = _run_with_held_state_lock(
        d,
        "interrupt",
        lambda: d.handle_interrupt("alice", "bob"),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: interrupt timeout response expected: {value}")
    assert_true(d.reserved.get("bob") is None and d.busy.get("bob") is False, f"{label}: interrupt must not mutate target routing state")

    d = make_daemon(tmpdir / "clear", participants)
    required = sum(d.command_budget("clear_peer"))
    value = _run_with_held_state_lock(
        d,
        "clear_peer",
        lambda: d.handle_clear_peer("alice", "bob", force=False),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: clear timeout response expected: {value}")
    assert_true("bob" not in d.clear_reservations, f"{label}: clear must not install reservation after lock timeout")

    d = make_daemon(tmpdir / "wait-status", participants)
    required = sum(d.command_budget("wait_status"))
    value = _run_with_held_state_lock(
        d,
        "wait_status",
        lambda: d.build_wait_status("alice"),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: wait_status timeout response expected: {value}")

    d = make_daemon(tmpdir / "aggregate-status", participants)
    required = sum(d.command_budget("aggregate_status"))
    value = _run_with_held_state_lock(
        d,
        "aggregate_status",
        lambda: d.build_aggregate_status("alice", "agg-lock"),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: aggregate_status timeout response expected: {value}")

    d = make_daemon(tmpdir / "clear-hold", participants)
    d.held_interrupt["bob"] = {"target": "bob", "prior_message_id": "msg-held", "since_ts": time.time()}
    required = sum(d.command_budget("clear_hold"))
    value = _run_with_held_state_lock(
        d,
        "clear_hold",
        lambda: d.release_hold("bob", reason="manual_clear_by_alice", by_sender="alice"),
        remaining=required + 0.02,
        label=label,
    )
    assert_true(isinstance(value, dict) and value.get("error_kind") == "lock_wait_exceeded", f"{label}: clear_hold timeout response expected: {value}")
    assert_true("bob" in d.held_interrupt, f"{label}: clear_hold must not pop hold after lock timeout")
    print(f"  PASS  {label}")


def scenario_clear_success_delivery_defers_near_deadline(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"}, "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"}}
    d = make_daemon(tmpdir, participants)
    calls: list[str | None] = []
    d.try_deliver = lambda target=None: calls.append(target)
    remaining = bridge_daemon.TMUX_DELIVERY_WORST_CASE_SECONDS + bridge_daemon.COMMAND_SAFETY_MARGIN_SECONDS - 0.1
    _set_command_context_near_lock_deadline(d, "clear_peer", remaining)
    try:
        d.try_deliver_command_aware("bob")
    finally:
        d.command_context.info = {}
    assert_true(not calls, f"{label}: clear success delivery must defer when deadline cannot cover tmux delivery")
    events = read_events(Path(d.state_file))
    assert_true(any(e.get("event") == "command_delivery_deferred_deadline" and e.get("target") == "bob" for e in events), f"{label}: deferral must be logged")
    source = inspect.getsource(bridge_daemon.BridgeDaemon.run_clear_peer)
    assert_true("try_deliver_command_aware(target)" in source, f"{label}: clear success path must use command-aware delivery")
    print(f"  PASS  {label}")


def scenario_clear_success_delivery_lock_wait_does_not_fail_after_commit(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"},
    }
    with patched_environ(AGENT_BRIDGE_CONTROLLED_CLEARS=str(tmpdir / "controlled-clears.json")):
        d = make_daemon(tmpdir, participants)
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        d._clear_tmux_send = lambda _pane, _text, *, target, message_id: {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]

        reload_unlocked_calls = 0
        original_reload_unlocked = d._reload_participants_unlocked

        def reload_unlocked_with_budget_drop() -> None:
            nonlocal reload_unlocked_calls
            reload_unlocked_calls += 1
            original_reload_unlocked()
            if reload_unlocked_calls == 2:
                _set_command_context_near_lock_deadline(d, "clear_peer", 50.0)

        d._reload_participants_unlocked = reload_unlocked_with_budget_drop  # type: ignore[method-assign]
        reload_calls: list[str] = []
        original_reload = d.reload_participants

        def observed_reload() -> None:
            reload_calls.append("reload_participants")
            original_reload()

        d.reload_participants = observed_reload  # type: ignore[method-assign]

        _set_command_context_near_lock_deadline(d, "clear_peer", 120.0)
        try:
            value = d.run_clear_peer("alice", "bob", force=False)
        finally:
            d.command_context.info = {}

        assert_true(isinstance(value, dict) and value.get("ok") is True and value.get("cleared") is True, f"{label}: committed clear must return success, not lock_wait_exceeded: {value}")
        assert_true(reload_calls == ["reload_participants"], f"{label}: post-commit delivery path should reach reload once in this regression: {reload_calls}")
        assert_true("bob" not in d.clear_reservations, f"{label}: reservation must be cleared after successful clear")
        events = read_events(Path(d.state_file))
        assert_true(any(e.get("event") == "clear_peer_completed" and e.get("target") == "bob" for e in events), f"{label}: clear completion should be logged")
        assert_true(any(e.get("event") == "command_delivery_deferred_lock_wait" and e.get("target") == "bob" for e in events), f"{label}: post-commit delivery lock wait must be deferred, not propagated")
    print(f"  PASS  {label}")


def scenario_clear_probe_subprocess_timeouts(label: str, tmpdir: Path) -> None:
    calls: list[dict] = []
    original_probe_run = bridge_pane_probe.subprocess.run

    def timeout_run(cmd, **kwargs):
        calls.append(dict(kwargs))
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    bridge_pane_probe.subprocess.run = timeout_run
    try:
        result = bridge_pane_probe.probe_agent_process("%92", "codex")
    finally:
        bridge_pane_probe.subprocess.run = original_probe_run
    assert_true(result.get("status") == "unknown" and result.get("reason") == "tmux_timeout", f"{label}: tmux timeout should be bounded unknown: {result}")
    assert_true(calls and all(call.get("timeout") == bridge_pane_probe.PROCESS_PROBE_TIMEOUT_SECONDS for call in calls), f"{label}: pane probe subprocesses need timeout: {calls}")

    identity_calls: list[dict] = []
    original_identity_run = bridge_identity.subprocess.run

    def identity_timeout_run(cmd, **kwargs):
        identity_calls.append(dict(kwargs))
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    bridge_identity.subprocess.run = identity_timeout_run
    try:
        target = bridge_identity.tmux_target_for_pane("%92")
        pane = bridge_identity.tmux_pane_for_target("session:1.1")
    finally:
        bridge_identity.subprocess.run = original_identity_run
    assert_true(target == "%92" and pane == "", f"{label}: identity tmux helpers should return bounded fallbacks: target={target!r} pane={pane!r}")
    assert_true(identity_calls and all(call.get("timeout") == bridge_identity.IDENTITY_TMUX_TIMEOUT_SECONDS for call in identity_calls), f"{label}: identity tmux subprocesses need timeout: {identity_calls}")
    print(f"  PASS  {label}")


def scenario_clear_marker_phase2_missing_forces_leave(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "agent_type": "codex", "pane": "%91"}, "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92"}}
    d = make_daemon(tmpdir, participants)
    called: list[dict] = []

    def fake_force_leave(target: str, *, caller: str, reason: str, reservation: dict) -> None:
        called.append({"target": target, "caller": caller, "reason": reason})

    d.force_leave_after_clear_failure = fake_force_leave  # type: ignore[method-assign]
    d.clear_reservations["bob"] = {
        "target": "bob",
        "caller": "alice",
        "probe_id": "probe-missing-phase2",
        "identity_marker_id": "missing-marker",
        "pane_touched": True,
        "condition": threading.Condition(d.state_lock),
    }
    with d.state_lock:
        handled = d.handle_clear_prompt_submitted_locked(
            "bob",
            {
                "event": "prompt_submitted",
                "attach_probe": "probe-missing-phase2",
                "session_id": "new-session-from-bus",
                "turn_id": "turn-from-bus",
            },
        )
    assert_true(handled is True, f"{label}: clear prompt should be handled")
    reservation = d.clear_reservations["bob"]
    assert_true(reservation.get("failure_reason") == "clear_marker_phase2_missing", f"{label}: missing marker keys must fail the reservation: {reservation}")
    assert_true(not reservation.get("new_session_id"), f"{label}: daemon must not reconstruct new_session_id from the bus record")
    assert_true(called and called[0].get("reason") == "clear_marker_phase2_missing", f"{label}: missing marker phase2 must force leave: {called}")
    events = read_events(Path(d.state_file))
    assert_true(any(e.get("event") == "clear_marker_phase2_missing" for e in events), f"{label}: explicit marker diagnostic required")
    print(f"  PASS  {label}")


def scenario_clear_marker_phase2_rejects_record_turn_without_marker_turn(label: str, tmpdir: Path) -> None:
    marker_file = tmpdir / "controlled-clears.json"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "session-alice"},
        "bob": {"alias": "bob", "agent_type": "claude", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
    }
    with patched_environ(AGENT_BRIDGE_CONTROLLED_CLEARS=str(marker_file)):
        d = make_daemon(tmpdir, participants)
        marker = bridge_clear_marker.make_marker(
            bridge_session="test-session",
            alias="bob",
            agent="claude",
            old_session_id="old-session",
            probe_id="probe-no-marker-turn",
            pane="%92",
            target="test:1.2",
            events_file=str(d.state_file),
            public_events_file=str(d.public_state_file),
        )
        marker["phase"] = "prompt_seen"
        marker["new_session_id"] = "new-session"
        bridge_clear_marker.write_marker(marker)
        d.clear_reservations["bob"] = {
            "target": "bob",
            "caller": "alice",
            "probe_id": "probe-no-marker-turn",
            "identity_marker_id": marker["id"],
            "pane_touched": False,
            "condition": threading.Condition(d.state_lock),
        }
        with d.state_lock:
            handled = d.handle_clear_prompt_submitted_locked(
                "bob",
                {
                    "event": "prompt_submitted",
                    "attach_probe": "probe-no-marker-turn",
                    "session_id": "new-session",
                    "turn_id": "turn-from-record-only",
                },
            )
        assert_true(handled is True, f"{label}: clear prompt should be handled")
        reservation = d.clear_reservations["bob"]
        assert_true(reservation.get("failure_reason") == "clear_marker_phase2_missing", f"{label}: record-only turn id must fail closed: {reservation}")
        assert_true(not reservation.get("new_session_id") and not reservation.get("probe_turn_id"), f"{label}: daemon must not reconstruct phase2 keys from bus record: {reservation}")
        events = read_events(Path(d.state_file))
        diagnostic = next((event for event in reversed(events) if event.get("event") == "clear_marker_phase2_missing"), None)
        assert_true(
            diagnostic
            and diagnostic.get("marker_phase") == "prompt_seen"
            and diagnostic.get("marker_new_session_present") is True
            and diagnostic.get("marker_probe_turn_id") == ""
            and diagnostic.get("record_turn_id") == "turn-from-record-only",
            f"{label}: diagnostic should expose record-only turn mismatch: {events}",
        )
    print(f"  PASS  {label}")


def scenario_run_clear_peer_phase2_failure_returns_immediately(label: str, tmpdir: Path) -> None:
    marker_file = tmpdir / "controlled-clears.json"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "session-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
    }
    with patched_environ(
        AGENT_BRIDGE_CONTROLLED_CLEARS=str(marker_file),
        AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC="0",
    ):
        d = make_daemon(tmpdir, participants)
        d.dry_run = False
        d.resolve_endpoint_detail = lambda _target, purpose="write": {"ok": True, "pane": "%92", "reason": "ok"}  # type: ignore[method-assign]
        d.pane_mode_status = lambda _pane: {"in_mode": False, "mode": "", "error": ""}  # type: ignore[method-assign]
        d._clear_tmux_send = lambda _pane, _text, *, target, message_id: {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]
        forced: list[str] = []

        def fake_force_leave(target: str, *, caller: str, reason: str, reservation: dict) -> None:
            forced.append(reason)

        d.force_leave_after_clear_failure = fake_force_leave  # type: ignore[method-assign]
        result: dict[str, object] = {}

        def runner() -> None:
            result["value"] = d.run_clear_peer("alice", "bob", force=False)

        thread = threading.Thread(target=runner, daemon=True)
        start = time.monotonic()
        thread.start()
        reservation = None
        for _ in range(100):
            with d.state_lock:
                reservation = d.clear_reservations.get("bob")
                if reservation and reservation.get("phase") == "waiting_probe":
                    probe_id = str(reservation.get("probe_id") or "")
                    d.handle_clear_prompt_submitted_locked(
                        "bob",
                        {
                            "event": "prompt_submitted",
                            "attach_probe": probe_id,
                            "session_id": "new-session-from-bus",
                            "turn_id": "turn-phase2",
                        },
                    )
                    break
            time.sleep(0.01)
        thread.join(1.0)
        elapsed = time.monotonic() - start
        assert_true(not thread.is_alive(), f"{label}: run_clear_peer waiter should wake promptly on phase2 failure")
        value = result.get("value")
        assert_true(isinstance(value, dict) and value.get("error_kind") == "clear_marker_phase2_missing", f"{label}: concrete phase2 error expected: {value}")
        assert_true(elapsed < 1.0, f"{label}: phase2 failure should not wait for probe timeout, elapsed={elapsed:.3f}s")
        assert_true(forced == ["clear_marker_phase2_missing"], f"{label}: forced-leave should be invoked exactly once with concrete reason: {forced}")
    print(f"  PASS  {label}")


def scenario_clear_force_leave_cleans_identity_stores(label: str, tmpdir: Path) -> None:
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
    }
    with patched_environ(
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
    ):
        d = make_daemon(tmpdir / "daemon", participants)
        registry_file.write_text(json.dumps({
            "version": 1,
            "sessions": {
                "codex:old-session": {"alias": "bob", "session_id": "old-session", "pane": "%92"},
                "codex:new-session": {"alias": "bob", "session_id": "new-session", "pane": "%92"},
                "codex:alice-session": {"alias": "alice", "session_id": "alice-session", "pane": "%91"},
            },
        }), encoding="utf-8")
        pane_locks_file.write_text(json.dumps({
            "version": 1,
            "panes": {
                "%92": {"alias": "bob", "hook_session_id": "old-session"},
                "%93": {"alias": "bob", "hook_session_id": "new-session"},
                "%91": {"alias": "alice", "hook_session_id": "alice-session"},
            },
        }), encoding="utf-8")
        live_file.write_text(json.dumps({
            "version": 1,
            "panes": {
                "%92": {"alias": "bob", "agent": "codex", "session_id": "old-session"},
                "%93": {"alias": "bob", "agent": "codex", "session_id": "new-session"},
                "%91": {"alias": "alice", "agent": "codex", "session_id": "alice-session"},
            },
            "sessions": {
                "codex:old-session": {"alias": "bob", "session_id": "old-session", "pane": "%92"},
                "codex:new-session": {"alias": "bob", "session_id": "new-session", "pane": "%93"},
                "codex:alice-session": {"alias": "alice", "session_id": "alice-session", "pane": "%91"},
            },
        }), encoding="utf-8")

        d.force_leave_after_clear_failure(
            "bob",
            caller="alice",
            reason="test_failure",
            reservation={"old_session_id": "old-session", "new_session_id": "new-session", "agent": "codex"},
        )

        session = json.loads(Path(d.session_file).read_text(encoding="utf-8"))
        bob = (session.get("participants") or {}).get("bob") or {}
        assert_true(bob.get("status") == "detached" and bob.get("endpoint_status") == "cleared", f"{label}: participant must be detached: {bob}")
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
        assert_true("codex:old-session" not in registry["sessions"] and "codex:new-session" not in registry["sessions"], f"{label}: old/new registry entries must be removed: {registry}")
        locks = json.loads(pane_locks_file.read_text(encoding="utf-8"))
        assert_true(not any((rec or {}).get("alias") == "bob" for rec in locks["panes"].values()), f"{label}: bob pane locks must be removed: {locks}")
        live = json.loads(live_file.read_text(encoding="utf-8"))
        assert_true("codex:old-session" not in live["sessions"] and "codex:new-session" not in live["sessions"], f"{label}: old/new live sessions must be removed: {live}")
        assert_true(not any((rec or {}).get("alias") == "bob" for rec in live["panes"].values()), f"{label}: bob live panes must be removed: {live}")
    print(f"  PASS  {label}")


def scenario_clear_force_leave_unowned_lock_serializes_cleanup(label: str, tmpdir: Path) -> None:
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    participants = {
        "alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "alice-session"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "target": "test:1.2"},
        "carol": {"alias": "carol", "agent_type": "codex", "pane": "%93", "hook_session_id": "carol-session"},
    }
    with patched_environ(
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
    ):
        d = make_daemon(tmpdir / "daemon", participants)
        registry_file.write_text(json.dumps({
            "version": 1,
            "sessions": {
                "codex:old-session": {"alias": "bob", "session_id": "old-session", "pane": "%92"},
                "codex:new-session": {"alias": "bob", "session_id": "new-session", "pane": "%92"},
            },
        }), encoding="utf-8")
        pane_locks_file.write_text(json.dumps({
            "version": 1,
            "panes": {"%92": {"alias": "bob", "hook_session_id": "old-session"}},
        }), encoding="utf-8")
        live_file.write_text(json.dumps({
            "version": 1,
            "panes": {"%92": {"alias": "bob", "agent": "codex", "session_id": "old-session"}},
            "sessions": {
                "codex:old-session": {"alias": "bob", "session_id": "old-session", "pane": "%92"},
                "codex:new-session": {"alias": "bob", "session_id": "new-session", "pane": "%92"},
            },
        }), encoding="utf-8")
        d.clear_reservations["bob"] = {"target": "bob", "old_session_id": "old-session", "new_session_id": "new-session", "agent": "codex"}
        aggregate_leg = test_message("msg-forceleave-agg", frm="carol", to="bob", status="pending")
        aggregate_leg.update({
            "aggregate_id": "agg-forceleave-lock",
            "aggregate_expected": ["bob"],
            "aggregate_message_ids": {"bob": "msg-forceleave-agg"},
            "aggregate_mode": "all",
            "aggregate_started_ts": utc_now(),
        })
        d.queue.update(lambda queue: queue.extend([
            test_message("msg-forceleave-lock", frm="alice", to="bob", status="pending"),
            aggregate_leg,
        ]))
        reload_calls: list[str] = []
        original_reload = d.reload_participants

        def forbidden_reload() -> None:
            reload_calls.append("reload_participants")
            original_reload()

        d.reload_participants = forbidden_reload  # type: ignore[method-assign]
        result: dict[str, object] = {}

        def worker() -> None:
            try:
                _set_command_context_near_lock_deadline(d, "clear_peer", 50.0)
                d.force_leave_after_clear_failure(
                    "bob",
                    caller="alice",
                    reason="lock_wait_exceeded_after_identity",
                    reservation={"old_session_id": "old-session", "new_session_id": "new-session", "agent": "codex"},
                )
                result["done"] = True
            except Exception as exc:
                result["error"] = exc
            finally:
                d.command_context.info = {}

        with d.state_lock:
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            time.sleep(0.05)
            assert_true(thread.is_alive(), f"{label}: unowned forced-leave cleanup must wait for state_lock")
            assert_true(_queue_item(d, "msg-forceleave-lock") is not None, f"{label}: queue must not mutate before lock is released")
            assert_true("bob" in d.clear_reservations, f"{label}: reservation must not clear before lock is released")
        thread.join(1.0)
        assert_true(not thread.is_alive(), f"{label}: forced-leave cleanup should finish after lock release")
        assert_true("error" not in result and result.get("done") is True, f"{label}: forced-leave worker failed: {result}")
        assert_true(not reload_calls, f"{label}: forced-leave must not re-enter reload_participants from exhausted command context")
        assert_true(_queue_item(d, "msg-forceleave-lock") is None, f"{label}: forced-leave should remove inbound rows after lock release")
        assert_true(_queue_item(d, "msg-forceleave-agg") is None, f"{label}: forced-leave should remove inbound aggregate rows after lock release")
        assert_true("bob" not in d.clear_reservations, f"{label}: forced-leave should clear reservation after lock release")
        aggregate_results = [
            item for item in d.queue.read()
            if item.get("source") == "aggregate_return" and item.get("aggregate_id") == "agg-forceleave-lock"
        ]
        assert_true(len(aggregate_results) == 1 and aggregate_results[0].get("to") == "carol", f"{label}: aggregate result must be queued for requester without delivery: {aggregate_results}")
        notices = [
            item for item in d.queue.read()
            if item.get("source") == "clear_forced_leave"
        ]
        notice_targets = sorted(str(item.get("to") or "") for item in notices)
        assert_true(notice_targets == ["alice", "carol"], f"{label}: forced-leave should queue notices for all remaining peers without delivery: {notice_targets}")
        events = read_events(Path(d.state_file))
        assert_true(not any(event.get("event") == "aggregate_interrupt_inject_failed" for event in events), f"{label}: aggregate synthetic reply should not fail under exhausted command context")
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
        live = json.loads(live_file.read_text(encoding="utf-8"))
        assert_true("codex:old-session" not in registry["sessions"] and "codex:new-session" not in registry["sessions"], f"{label}: registry old/new entries must be cleaned: {registry}")
        assert_true("codex:old-session" not in live["sessions"] and "codex:new-session" not in live["sessions"], f"{label}: live old/new entries must be cleaned: {live}")
    print(f"  PASS  {label}")


def scenario_clear_identity_helper_success_and_failure(label: str, tmpdir: Path) -> None:
    state_dir = tmpdir / "state"
    session_dir = state_dir / "test-session"
    session_dir.mkdir(parents=True)
    registry_file = tmpdir / "attached-sessions.json"
    pane_locks_file = tmpdir / "pane-locks.json"
    live_file = tmpdir / "live-sessions.json"
    (session_dir / "session.json").write_text(json.dumps({
        "session": "test-session",
        "participants": {
            "bob": {"alias": "bob", "agent_type": "codex", "pane": "%92", "hook_session_id": "old-session", "status": "active"}
        },
        "panes": {"bob": "%92"},
        "targets": {"bob": "old-target"},
        "hook_session_ids": {"bob": "old-session"},
    }), encoding="utf-8")
    registry_file.write_text(json.dumps({"version": 1, "sessions": {"codex:old-session": {"alias": "bob", "session_id": "old-session", "pane": "%92"}}}), encoding="utf-8")
    pane_locks_file.write_text(json.dumps({"version": 1, "panes": {"%91": {"bridge_session": "test-session", "alias": "bob", "hook_session_id": "old-session"}}}), encoding="utf-8")
    live_file.write_text(json.dumps({"version": 1, "panes": {"%92": {"agent": "codex", "alias": "bob", "session_id": "old-session", "pane": "%92"}}, "sessions": {"codex:old-session": {"session_id": "old-session", "pane": "%92"}}}), encoding="utf-8")
    with patched_environ(
        AGENT_BRIDGE_STATE_DIR=str(state_dir),
        AGENT_BRIDGE_ATTACH_REGISTRY=str(registry_file),
        AGENT_BRIDGE_PANE_LOCKS=str(pane_locks_file),
        AGENT_BRIDGE_LIVE_SESSIONS=str(live_file),
    ):
        result = bridge_identity.replace_attached_session_identity_for_clear(
            bridge_session="test-session",
            alias="bob",
            agent_type="codex",
            old_session_id="old-session",
            new_session_id="new-session",
            pane="%92",
            target="test:1.2",
            cwd="/tmp",
            model="gpt-test",
            process_identity={"status": "verified"},
        )
        assert_true(result.get("ok") is True, f"{label}: identity helper success expected: {result}")
        session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        assert_true(session["participants"]["bob"]["hook_session_id"] == "new-session", f"{label}: session participant must update: {session}")
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
        assert_true("codex:old-session" not in registry["sessions"] and "codex:new-session" in registry["sessions"], f"{label}: registry replacement expected: {registry}")
        locks = json.loads(pane_locks_file.read_text(encoding="utf-8"))
        assert_true(locks["panes"]["%92"]["hook_session_id"] == "new-session" and "%91" not in locks["panes"], f"{label}: pane lock replacement expected: {locks}")
        live = json.loads(live_file.read_text(encoding="utf-8"))
        assert_true(live["panes"]["%92"]["session_id"] == "new-session" and "codex:new-session" in live["sessions"], f"{label}: live replacement expected: {live}")

        failure = bridge_identity.replace_attached_session_identity_for_clear(
            bridge_session="test-session",
            alias="missing",
            agent_type="codex",
            old_session_id="old2",
            new_session_id="new2",
            pane="%99",
        )
        assert_true(failure.get("ok") is False and failure.get("error") == "alias_missing", f"{label}: missing alias should fail cleanly: {failure}")
        after = json.loads(registry_file.read_text(encoding="utf-8"))
        assert_true("codex:new2" not in after["sessions"], f"{label}: failed helper must not mint new mapping: {after}")
    print(f"  PASS  {label}")


def scenario_force_clear_requester_dual_write_queue_and_active(label: str, tmpdir: Path) -> None:
    participants = {
        "requester": {"alias": "requester", "agent_type": "codex", "pane": "%91"},
        "responder": {"alias": "responder", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    pending = test_message("msg-force-queue", frm="requester", to="responder", status="pending")
    pending["kind"] = "request"
    pending["auto_return"] = True
    d.queue.update(lambda queue: queue.append(pending))
    d.current_prompt_by_agent["responder"] = {
        "id": "msg-force-active",
        "from": "requester",
        "to": "responder",
        "kind": "request",
        "auto_return": True,
    }

    result = d.apply_force_clear_invalidation("requester", "operator")
    assert_true("msg-force-queue" in result.get("requester_cleared_message_ids", []), f"{label}: queue requester-cleared id expected: {result}")
    row = _queue_item(d, "msg-force-queue") or {}
    assert_true(row.get("auto_return") is False and row.get("requester_cleared") is True and row.get("requester_cleared_alias") == "requester", f"{label}: queue row must carry requester_cleared metadata: {row}")
    ctx = d.current_prompt_by_agent["responder"]
    assert_true(ctx.get("auto_return") is False and ctx.get("requester_cleared") is True and ctx.get("requester_cleared_alias") == "requester", f"{label}: active context must carry requester_cleared metadata: {ctx}")
    print(f"  PASS  {label}")


def scenario_clear_aggregate_requester_late_reply_suppressed(label: str, tmpdir: Path) -> None:
    participants = {
        "requester": {"alias": "requester", "agent_type": "codex", "pane": "%91"},
        "worker": {"alias": "worker", "agent_type": "codex", "pane": "%92"},
    }
    d = make_daemon(tmpdir, participants)
    with locked_json(Path(d.aggregate_file), {"version": 1, "aggregates": {}}) as data:
        data.setdefault("aggregates", {})["agg-clear"] = {
            "id": "agg-clear",
            "requester": "requester",
            "expected": ["worker"],
            "message_ids": {"worker": "msg-agg-worker"},
            "replies": {},
            "status": "collecting",
            "delivered": False,
        }
    d.apply_force_clear_invalidation("requester", "operator")
    aggregate = (read_json(Path(d.aggregate_file), {"aggregates": {}}).get("aggregates") or {}).get("agg-clear") or {}
    assert_true(aggregate.get("status") == "cancelled_requester_cleared", f"{label}: aggregate requester must be cancelled: {aggregate}")
    context = {
        "id": "msg-agg-worker",
        "from": "requester",
        "aggregate_id": "agg-clear",
        "aggregate_expected": ["worker"],
        "aggregate_message_ids": {"worker": "msg-agg-worker"},
        "aggregate_mode": "all",
        "causal_id": "causal-agg-clear",
    }
    d.collect_aggregate_response("worker", "late reply", context)
    aggregate_after = (read_json(Path(d.aggregate_file), {"aggregates": {}}).get("aggregates") or {}).get("agg-clear") or {}
    assert_true(aggregate_after.get("status") == "cancelled_requester_cleared" and not aggregate_after.get("replies"), f"{label}: late reply must not recreate aggregate result: {aggregate_after}")
    assert_true(not any(item.get("intent") == "message_aggregate_result" for item in d.queue.read()), f"{label}: no aggregate result should be queued")
    events = read_events(Path(d.state_file))
    assert_true(any(e.get("event") == "aggregate_reply_ignored_requester_cleared" for e in events), f"{label}: late reply suppression should be logged")
    print(f"  PASS  {label}")


def scenario_self_clear_promotion_and_cancel_notice_ordering(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "agent_type": "codex", "pane": "%91", "hook_session_id": "session-alice"}}
    d = make_daemon(tmpdir / "promote", participants)
    d._self_clear_worker = lambda _target, _reservation: None  # type: ignore[method-assign]
    d.pending_self_clears["alice"] = {"clear_id": "clear-self", "caller": "alice", "target": "alice", "force": False}
    with d.state_lock:
        d.promote_pending_self_clear_locked("alice")
    assert_true("alice" in d.clear_reservations and "alice" not in d.pending_self_clears, f"{label}: pending self-clear must promote under state lock")

    d2 = make_daemon(tmpdir / "cancel", participants)
    older = test_message("msg-older-inbound", frm="bridge", to="alice", status="pending")
    d2.queue.update(lambda queue: queue.append(older))
    d2.pending_self_clears["alice"] = {"clear_id": "clear-self", "caller": "alice", "target": "alice", "force": False}
    d2.busy["alice"] = True
    with d2.state_lock:
        d2.promote_pending_self_clear_locked("alice")
    queue = d2.queue.read()
    assert_true(queue and queue[0].get("source") == "self_clear_cancelled", f"{label}: cancellation notice must be prepended before older inbound: {queue}")
    assert_true("alice" not in d2.clear_reservations and "alice" not in d2.pending_self_clears, f"{label}: cancelled self-clear must not leave pending state")
    print(f"  PASS  {label}")


def scenario_clear_marker_ttl_expiry_removes_marker(label: str, tmpdir: Path) -> None:
    marker_file = tmpdir / "controlled-clears.json"
    with patched_environ(AGENT_BRIDGE_CONTROLLED_CLEARS=str(marker_file)):
        marker = bridge_clear_marker.make_marker(
            bridge_session="test-session",
            alias="bob",
            agent="codex",
            old_session_id="old-session",
            probe_id="probe-expire",
            pane="%92",
            target="test:1.2",
            events_file=str(tmpdir / "events.raw.jsonl"),
        )
        marker["expires_ts"] = time.time() - 1
        bridge_clear_marker.write_marker(marker)
        removed = bridge_clear_marker.cleanup_expired_or_orphaned()
        assert_true(removed and removed[0].get("id") == marker["id"], f"{label}: expired marker should be removed: {removed}")
        assert_true(bridge_clear_marker.find_for_clear_window(pane="%92", agent="codex", session_id="old-session") is None, f"{label}: marker lookup should stop after TTL cleanup")
    print(f"  PASS  {label}")






def scenarios() -> list[tuple[str, object]]:
    from regression import docs_contracts, hook_config, install_uninstall, send_enqueue, view_peer
    from regression.registry import scenario_index

    phase3 = scenario_index(
        install_uninstall.SCENARIOS,
        hook_config.SCENARIOS,
        view_peer.SCENARIOS,
        docs_contracts.SCENARIOS,
        send_enqueue.SCENARIOS,
    )
    return [
        ('lifecycle_delivered_terminal', scenario_lifecycle),
        ('held_interrupt_does_not_block_delivery', scenario_held_interrupt_does_not_block_delivery),
        ('reserve_next_ignores_held_marker', scenario_reserve_next_ignores_held_marker),
        ('held_marker_persists_through_delivery', scenario_held_marker_persists_through_delivery),
        ('esc_fail_no_state_change', scenario_esc_fail_no_state_change),
        ('clear_hold_logs_event', scenario_clear_hold),
        ('clear_hold_socket_still_pops_held', scenario_clear_hold_socket_still_pops_held),
        ('aggregate_interrupt_synthetic_reply', scenario_aggregate_interrupt_synthetic),
        ('interrupt_pending_replacement_delivers', scenario_interrupt_pending_replacement_delivers),
        ('interrupt_new_replacement_after_interrupt_delivers', scenario_interrupt_new_replacement_after_interrupt_delivers),
        ('interrupt_claude_with_active_sends_esc_then_cc', scenario_interrupt_claude_with_active_sends_esc_then_cc),
        ('interrupt_codex_sends_only_esc', scenario_interrupt_codex_sends_only_esc),
        ('interrupt_unknown_type_falls_back_to_esc', scenario_interrupt_unknown_type_falls_back_to_esc),
        ('interrupt_claude_idle_skips_cc', scenario_interrupt_claude_idle_skips_cc),
        ('interrupt_claude_cc_failure_does_not_revert_state', scenario_interrupt_claude_cc_failure_does_not_revert_state),
        ('interrupt_holds_state_lock_through_sequence', scenario_interrupt_holds_state_lock_through_sequence),
        ('interrupt_no_try_deliver_between_keys', scenario_interrupt_no_try_deliver_between_keys),
        ('interrupt_env_override_disables_cc', scenario_interrupt_env_override_disables_cc),
        ('interrupt_key_delay_env_nonfinite_uses_default', scenario_interrupt_key_delay_env_nonfinite_uses_default),
        ('interrupt_key_delay_env_clamps_out_of_range', scenario_interrupt_key_delay_env_clamps_out_of_range),
        ('claude_interrupt_keys_invalid_uses_default', scenario_claude_interrupt_keys_invalid_uses_default),
        ('claude_interrupt_keys_empty_uses_default', scenario_claude_interrupt_keys_empty_uses_default),
        ('send_interrupt_key_timeout_returns_false', scenario_send_interrupt_key_timeout_returns_false),
        ('clear_hold_clears_interrupt_partial_failure_gate', scenario_clear_hold_clears_interrupt_partial_failure_gate),
        ('interrupt_double_within_no_active_skips_cc', scenario_interrupt_double_within_no_active_skips_cc),
        ('interrupt_double_with_fresh_delivery_sends_cc_again', scenario_interrupt_double_with_fresh_delivery_sends_cc_again),
        ('interrupt_peer_cli_exit_nonzero_on_partial_key_failure', scenario_interrupt_peer_cli_exit_nonzero_on_partial_key_failure),
        ('interrupt_peer_action_text_and_json_modes', scenario_interrupt_peer_action_text_and_json_modes),
        ('interrupt_peer_clear_hold_text_and_json_modes', scenario_interrupt_peer_clear_hold_text_and_json_modes),
        ('interrupt_peer_status_json_unchanged_and_json_rejected', scenario_interrupt_peer_status_json_unchanged_and_json_rejected),
        ('interrupted_late_prompt_submitted_before_replacement', scenario_interrupted_late_prompt_submitted_before_replacement),
        ('interrupted_late_prompt_submitted_after_replacement', scenario_interrupted_late_prompt_submitted_after_replacement),
        ('interrupted_late_turn_stop_preserves_replacement', scenario_interrupted_late_turn_stop_preserves_replacement),
        ('interrupted_no_turn_stop_no_context_suppressed', scenario_interrupted_no_turn_stop_no_context_suppressed),
        ('interrupted_no_turn_race_routes_replacement_then_suppresses_old', scenario_interrupted_no_turn_race_routes_replacement_then_suppresses_old),
        ('interrupted_inflight_tombstone_retains_on_unrelated_stop', scenario_interrupted_inflight_tombstone_retains_on_unrelated_stop),
        ('interrupted_empty_values_do_not_match_tombstone', scenario_interrupted_empty_values_do_not_match_tombstone),
        ('interrupted_tombstone_current_ctx_id_match_cleans', scenario_interrupted_tombstone_current_ctx_id_match_cleans),
        ('interrupted_tombstone_current_ctx_turn_match_cleans', scenario_interrupted_tombstone_current_ctx_turn_match_cleans),
        ('interrupted_tombstone_stale_stop_preserves_replacement_watchdog', scenario_interrupted_tombstone_stale_stop_preserves_replacement_watchdog),
        ('interrupted_tombstone_aggregate_ctx_does_not_cancel_aggregate_watchdog', scenario_interrupted_tombstone_aggregate_ctx_does_not_cancel_aggregate_watchdog),
        ('aggregate_late_real_stop_after_interrupt_does_not_overwrite', scenario_aggregate_late_real_stop_after_interrupt_does_not_overwrite),
        ('watchdog_cancel_on_empty_response', scenario_watchdog_cancel_on_empty_response),
        ('alarm_cancelled_by_qualifying_request', scenario_alarm_cancelled_by_qualifying_request),
        ('socket_path_alarm_cancel', scenario_socket_path_alarm_cancel),
        ('response_send_guard_socket_request_notice', scenario_response_send_guard_socket_request_notice),
        ('response_send_guard_socket_force_and_other_peer', scenario_response_send_guard_socket_force_and_other_peer),
        ('response_send_guard_socket_no_auto_return_allowed', scenario_response_send_guard_socket_no_auto_return_allowed),
        ('response_send_guard_socket_atomic_multi', scenario_response_send_guard_socket_atomic_multi),
        ('response_send_guard_socket_aggregate_and_held', scenario_response_send_guard_socket_aggregate_and_held),
        ('response_send_guard_after_response_finished_allowed', scenario_response_send_guard_after_response_finished_allowed),
        ('fallback_path_alarm_cancel', scenario_fallback_path_alarm_cancel),
        ('ingressing_not_delivered_before_finalize', scenario_ingressing_not_delivered_before_finalize),
        ('replay_does_not_cancel_later_alarm', scenario_replay_does_not_cancel_later_alarm),
        ('bridge_origin_fallback_ingressing_promoted', scenario_bridge_origin_fallback_ingressing_promoted),
        ('socket_normalizes_non_ingressing_status', scenario_socket_normalizes_non_ingressing_status),
        ('aggregate_fallback_finalize', scenario_aggregate_fallback_finalize),
        ('aged_ingressing_promoted_by_maintenance', scenario_aged_ingressing_promoted_by_maintenance),
        ('aged_ingressing_does_not_cancel_alarms', scenario_aged_ingressing_does_not_cancel_alarms),
        ('aged_ingressing_malformed_timestamp_promoted', scenario_aged_ingressing_malformed_timestamp_promoted),
        ('fresh_ingressing_not_promoted_by_maintenance', scenario_fresh_ingressing_not_promoted_by_maintenance),
        ('alarm_not_cancelled_by_result', scenario_alarm_not_cancelled_by_result),
        ('alarm_not_cancelled_by_bridge', scenario_alarm_not_cancelled_by_bridge),
        ('user_prompt_does_not_cancel_alarm', scenario_user_prompt_does_not_cancel_alarm),
        ('extend_wait_upserts_watchdog', scenario_extend_wait_upserts_watchdog),
        ('extend_wait_aggregate_rejected', scenario_extend_wait_aggregate_rejected),
        ('extend_wait_unknown_message', scenario_extend_wait_unknown_message),
        ('extend_wait_terminal_tombstone_classification', scenario_extend_wait_terminal_tombstone_classification),
        ('extend_wait_pending_rejected', scenario_extend_wait_pending_rejected),
        ('extend_wait_not_owner', scenario_extend_wait_not_owner),
        ('cancel_message_pending_removes_row', scenario_cancel_message_pending_removes_row),
        ('cancel_message_inflight_pre_paste', scenario_cancel_message_inflight_pre_paste),
        ('cancel_message_pane_mode_deferred_inflight_cancellable', scenario_cancel_message_pane_mode_deferred_inflight_cancellable),
        ('cancel_message_inflight_after_enter_rejects_with_interrupt_guidance', scenario_cancel_message_inflight_after_enter_rejects_with_interrupt_guidance),
        ('cancel_message_submitted_rejects_with_interrupt_guidance', scenario_cancel_message_submitted_rejects_with_interrupt_guidance),
        ('cancel_message_delivered_rejects_with_interrupt_guidance', scenario_cancel_message_delivered_rejects_with_interrupt_guidance),
        ('cancel_message_ownership_violation', scenario_cancel_message_ownership_violation),
        ('cancel_message_idempotent_after_terminal', scenario_cancel_message_idempotent_after_terminal),
        ('cancel_message_aggregate_per_leg_keeps_other_legs', scenario_cancel_message_aggregate_per_leg_keeps_other_legs),
        ('cancel_message_action_text_and_json_modes', scenario_cancel_message_action_text_and_json_modes),
        ('cancel_message_action_error_text_modes', scenario_cancel_message_action_error_text_modes),
        ('aggregate_status_open_from_queue_and_reply', scenario_aggregate_status_open_from_queue_and_reply),
        ('aggregate_status_zero_reply_from_queue', scenario_aggregate_status_zero_reply_from_queue),
        ('aggregate_status_cancelled_and_timeout_synthetic_reasons', scenario_aggregate_status_cancelled_and_timeout_synthetic_reasons),
        ('aggregate_status_legacy_synthetic_reply_uses_tombstone', scenario_aggregate_status_legacy_synthetic_reply_uses_tombstone),
        ('aggregate_status_foreign_and_unknown_indistinguishable', scenario_aggregate_status_foreign_and_unknown_indistinguishable),
        ('aggregate_status_source_conflict_fails_closed', scenario_aggregate_status_source_conflict_fails_closed),
        ('aggregate_status_legacy_fallback_and_cap', scenario_aggregate_status_legacy_fallback_and_cap),
        ('aggregate_status_watchdog_only_anchor', scenario_aggregate_status_watchdog_only_anchor),
        ('wait_status_empty_self_view', scenario_wait_status_empty_self_view),
        ('wait_status_outstanding_watchdogs_alarms_pending_inbound', scenario_wait_status_outstanding_watchdogs_alarms_pending_inbound),
        ('wait_status_aggregate_waits_privacy_and_completed_result', scenario_wait_status_aggregate_waits_privacy_and_completed_result),
        ('wait_status_caps_and_summary_counts', scenario_wait_status_caps_and_summary_counts),
        ('delivery_watchdog_arms_on_reserve', scenario_delivery_watchdog_arms_on_reserve),
        ('delivery_watchdog_fire_inflight_notice', scenario_delivery_watchdog_fire_inflight_notice),
        ('watchdog_fire_after_terminal_suppresses_notice', scenario_watchdog_fire_after_terminal_suppresses_notice),
        ('pending_watchdog_wake_removed_on_terminal', scenario_pending_watchdog_wake_removed_on_terminal),
        ('delivery_watchdog_extend_replaces_wake', scenario_delivery_watchdog_extend_replaces_wake),
        ('delivery_watchdog_submitted_extend_and_text', scenario_delivery_watchdog_submitted_extend_and_text),
        ('delivery_watchdog_aggregate_pending_extend_rejected', scenario_delivery_watchdog_aggregate_pending_extend_rejected),
        ('delivery_watchdog_replaced_by_response_on_delivered', scenario_delivery_watchdog_replaced_by_response_on_delivered),
        ('delivery_watchdog_requeue_cancels', scenario_delivery_watchdog_requeue_cancels),
        ('delivery_watchdog_mark_pending_cancels', scenario_delivery_watchdog_mark_pending_cancels),
        ('delivery_watchdog_pane_mode_reverts_cancel', scenario_delivery_watchdog_pane_mode_reverts_cancel),
        ('delivery_watchdog_phase_mismatch_skipped', scenario_delivery_watchdog_phase_mismatch_skipped),
        ('watchdog_phase_legacy_default_response', scenario_watchdog_phase_legacy_default_response),
        ('delivery_watchdog_aggregate_leg_coexists_with_response', scenario_delivery_watchdog_aggregate_leg_coexists_with_response),
        ('delivery_watchdog_aggregate_interrupt_cancels_leg_only', scenario_delivery_watchdog_aggregate_interrupt_cancels_leg_only),
        ('aggregate_response_watchdog_text_uses_progress', scenario_aggregate_response_watchdog_text_uses_progress),
        ('aggregate_completion_suppresses_pending_watchdog_wake', scenario_aggregate_completion_suppresses_pending_watchdog_wake),
        ('duplicate_enqueue_does_not_cancel_alarm', scenario_duplicate_enqueue_does_not_cancel_alarm),
        ('alarm_op_invalid_delay_is_rejected', scenario_alarm_op_invalid_delay_is_rejected_not_crashed),
        ('send_peer_watchdog_negative_one_rejected', phase3['send_peer_watchdog_negative_one_rejected']),
        ('send_peer_watchdog_nan_rejected', phase3['send_peer_watchdog_nan_rejected']),
        ('send_peer_watchdog_inf_rejected', phase3['send_peer_watchdog_inf_rejected']),
        ('send_peer_watchdog_minus_inf_rejected', phase3['send_peer_watchdog_minus_inf_rejected']),
        ('send_peer_watchdog_zero_disables_for_request', phase3['send_peer_watchdog_zero_disables_for_request']),
        ('send_peer_watchdog_finite_positive_succeeds', phase3['send_peer_watchdog_finite_positive_succeeds']),
        ('send_peer_no_auto_return_explicit_watchdog_rejected', phase3['send_peer_no_auto_return_explicit_watchdog_rejected']),
        ('send_peer_no_auto_return_watchdog_zero_succeeds', phase3['send_peer_no_auto_return_watchdog_zero_succeeds']),
        ('send_peer_no_auto_return_invalid_watchdog_value_precedence', phase3['send_peer_no_auto_return_invalid_watchdog_value_precedence']),
        ('send_peer_no_auto_return_skips_default_watchdog', phase3['send_peer_no_auto_return_skips_default_watchdog']),
        ('send_peer_auto_return_default_watchdog_still_attaches', phase3['send_peer_auto_return_default_watchdog_still_attaches']),
        ('send_peer_no_auto_return_broadcast_watchdog_rejected', phase3['send_peer_no_auto_return_broadcast_watchdog_rejected']),
        ('send_peer_watchdog_inf_with_notice_reports_finite_error_first', phase3['send_peer_watchdog_inf_with_notice_reports_finite_error_first']),
        ('send_peer_watchdog_zero_with_notice_still_rejects_request_only', phase3['send_peer_watchdog_zero_with_notice_still_rejects_request_only']),
        ('send_peer_watchdog_abc_argparse_error', phase3['send_peer_watchdog_abc_argparse_error']),
        ('extend_wait_zero_negative_nan_inf_rejected', scenario_extend_wait_zero_negative_nan_inf_rejected),
        ('extend_wait_finite_positive_calls_request_extend', scenario_extend_wait_finite_positive_calls_request_extend),
        ('extend_wait_terminal_error_texts', scenario_extend_wait_terminal_error_texts),
        ('wait_status_cli_summary_and_json', scenario_wait_status_cli_summary_and_json),
        ('wait_status_cli_unsupported_old_daemon', scenario_wait_status_cli_unsupported_old_daemon),
        ('wait_status_cli_rejects_inactive_sender', scenario_wait_status_cli_rejects_inactive_sender),
        ('aggregate_status_cli_summary_and_json', scenario_aggregate_status_cli_summary_and_json),
        ('aggregate_status_cli_unsupported_and_not_found_text', scenario_aggregate_status_cli_unsupported_and_not_found_text),
        ('aggregate_status_cli_rejects_inactive_sender', scenario_aggregate_status_cli_rejects_inactive_sender),
        ('alarm_negative_nan_inf_minus_inf_rejected', scenario_alarm_negative_nan_inf_minus_inf_rejected),
        ('alarm_zero_and_finite_positive_call_request_alarm', scenario_alarm_zero_and_finite_positive_call_request_alarm),
        ('alarm_request_failure_prints_no_success_hint', scenario_alarm_request_failure_prints_no_success_hint),
        ('alarm_request_retries_timeout_with_stable_wake_id', scenario_alarm_request_retries_timeout_with_stable_wake_id),
        ('alarm_request_retry_exhaustion_is_bounded', scenario_alarm_request_retry_exhaustion_is_bounded),
        ('alarm_request_retries_lock_wait_with_stable_wake_id', scenario_alarm_request_retries_lock_wait_with_stable_wake_id),
        ('alarm_request_lock_wait_retry_exhaustion_is_bounded', scenario_alarm_request_lock_wait_retry_exhaustion_is_bounded),
        ('alarm_request_non_retryable_daemon_error_is_immediate', scenario_alarm_request_non_retryable_daemon_error_is_immediate),
        ('alarm_request_rejects_mismatched_daemon_wake_id', scenario_alarm_request_rejects_mismatched_daemon_wake_id),
        ('resolve_default_watchdog_seconds_env_table', scenario_resolve_default_watchdog_seconds_env_table),
        ('daemon_socket_alarm_op_rejects_non_finite', scenario_daemon_socket_alarm_op_rejects_non_finite),
        ('daemon_socket_alarm_op_idempotent_wake_id', scenario_daemon_socket_alarm_op_idempotent_wake_id),
        ('daemon_socket_alarm_op_rejects_invalid_conflicting_wake_id', scenario_daemon_socket_alarm_op_rejects_invalid_conflicting_wake_id),
        ('daemon_socket_alarm_op_idempotent_after_cancelled_or_fired', scenario_daemon_socket_alarm_op_idempotent_after_cancelled_or_fired),
        ('daemon_socket_extend_watchdog_op_rejects_non_finite', scenario_daemon_socket_extend_watchdog_op_rejects_non_finite),
        ('daemon_upsert_message_watchdog_rejects_non_finite', scenario_daemon_upsert_message_watchdog_rejects_non_finite),
        ('daemon_enqueue_rejects_no_auto_return_watchdog_atomically', scenario_daemon_enqueue_rejects_no_auto_return_watchdog_atomically),
        ('daemon_enqueue_no_auto_return_watchdog_zero_normalized', scenario_daemon_enqueue_no_auto_return_watchdog_zero_normalized),
        ('daemon_prior_hint_pending_cancel', scenario_daemon_prior_hint_pending_cancel),
        ('daemon_prior_hint_delivered_interrupt', scenario_daemon_prior_hint_delivered_interrupt),
        ('daemon_prior_hint_inflight_pane_mode_cancel', scenario_daemon_prior_hint_inflight_pane_mode_cancel),
        ('daemon_prior_hint_inflight_after_enter_interrupt', scenario_daemon_prior_hint_inflight_after_enter_interrupt),
        ('daemon_prior_hint_terminal_or_absent_no_hint', scenario_daemon_prior_hint_terminal_or_absent_no_hint),
        ('daemon_prior_hint_rejections_have_no_hints', scenario_daemon_prior_hint_rejections_have_no_hints),
        ('daemon_prior_hint_aggregate_suffix', scenario_daemon_prior_hint_aggregate_suffix),
        ('daemon_prior_hint_notice_safe_wording', scenario_daemon_prior_hint_notice_safe_wording),
        ('daemon_prior_hint_same_pair_only', scenario_daemon_prior_hint_same_pair_only),
        ('daemon_prior_hint_multi_target_partial', scenario_daemon_prior_hint_multi_target_partial),
        ('daemon_prior_hint_current_prompt_only', scenario_daemon_prior_hint_current_prompt_only),
        ('daemon_prior_hint_active_ctx_beats_pending_queue_row', scenario_daemon_prior_hint_active_ctx_beats_pending_queue_row),
        ('prior_hint_classifier_drift_invariant', scenario_prior_hint_classifier_drift_invariant),
        ('daemon_upsert_no_auto_return_watchdog_rejected', scenario_daemon_upsert_no_auto_return_watchdog_rejected),
        ('daemon_register_alarm_rejects_non_finite_and_negative', scenario_daemon_register_alarm_rejects_non_finite_and_negative),
        ('daemon_mark_message_delivered_ignores_non_finite_watchdog', scenario_daemon_mark_message_delivered_ignores_non_finite_watchdog),
        ('daemon_no_auto_return_watchdog_arm_guard_strips', scenario_daemon_no_auto_return_watchdog_arm_guard_strips),
        ('daemon_no_auto_return_watchdog_ingress_sanitizers', scenario_daemon_no_auto_return_watchdog_ingress_sanitizers),
        ('daemon_socket_ack_message_unsupported_does_not_delete_queue', scenario_daemon_socket_ack_message_unsupported_does_not_delete_queue),
        ('daemon_ack_message_helper_removed', scenario_daemon_ack_message_helper_removed),
        ('stale_watchdog_skipped', scenario_stale_watchdog_skipped),
        ('alarm_fire_text_includes_rearm_hint', scenario_alarm_fire_text_includes_rearm_hint),
        ('watchdog_pending_text_omits_held_interrupt', scenario_watchdog_pending_text_omits_held_interrupt),
        ('pane_mode_pending_defers_without_attempt', scenario_pane_mode_pending_defers_without_attempt),
        ('pane_mode_clears_then_delivers', scenario_pane_mode_clears_then_delivers),
        ('pane_mode_force_cancel_after_grace', scenario_pane_mode_force_cancel_after_grace),
        ('pane_mode_nonforce_mode_stays_pending', scenario_pane_mode_nonforce_mode_stays_pending),
        ('pane_mode_busy_target_does_not_start_timer', scenario_pane_mode_busy_target_does_not_start_timer),
        ('retry_enter_skips_pane_mode', scenario_retry_enter_skips_pane_mode),
        ('pane_mode_probe_failure_defers_pending', scenario_pane_mode_probe_failure_defers_pending),
        ('pane_mode_force_cancel_failure_stays_pending', scenario_pane_mode_force_cancel_failure_stays_pending),
        ('enter_deferred_survives_stale_requeue_and_restart', scenario_enter_deferred_survives_stale_requeue_and_restart),
        ('pre_enter_probe_failure_defers_enter', scenario_pre_enter_probe_failure_defers_enter),
        ('pane_mode_grace_zero_disables_cancel', scenario_pane_mode_grace_zero_disables_cancel),
        ('wait_for_probe_retries_enter_with_pane_id', scenario_wait_for_probe_retries_enter_with_pane_id),
        ('wait_for_probe_no_retry_without_pane_id', scenario_wait_for_probe_no_retry_without_pane_id),
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
        ('orphan_nonce_in_user_prompt', scenario_orphan_nonce_in_user_prompt),
        ('prompt_intercept_request_notice_body', scenario_prompt_intercept_request_notice_body),
        ('prompt_intercept_bridge_notice_no_source_notice', scenario_prompt_intercept_bridge_notice_no_source_notice),
        ('prompt_intercept_response_guard_queue_allows', scenario_prompt_intercept_response_guard_queue_allows),
        ('prompt_intercept_mixed_inflight_requeues', scenario_prompt_intercept_mixed_inflight_requeues),
        ('prompt_submitted_duplicate_noop', scenario_prompt_submitted_duplicate_noop),
        ('prompt_submitted_duplicate_without_nonce_noop', scenario_prompt_submitted_duplicate_without_nonce_noop),
        ('prompt_intercept_aggregate_completes', scenario_prompt_intercept_aggregate_completes),
        ('prompt_intercept_held_drain_noop', scenario_prompt_intercept_held_drain_noop),
        ('held_drain_stale_stop_preserves_new_ctx', scenario_held_drain_stale_stop_preserves_new_ctx),
        ('held_drain_missing_identity_does_not_clear_active_ctx', scenario_held_drain_missing_identity_does_not_clear_active_ctx),
        ('held_drain_matching_prior_id_without_turn_still_clears_ctx', scenario_held_drain_matching_prior_id_without_turn_still_clears_ctx),
        ('stale_hold_ignored_active_context_routes_normally', scenario_stale_hold_ignored_active_context_routes_normally),
        ('stale_hold_old_stop_with_active_ctx_applies_a4_mismatch', scenario_stale_hold_old_stop_with_active_ctx_applies_a4_mismatch),
        ('held_no_prior_id_with_active_ctx_classified_stale', scenario_held_no_prior_id_with_active_ctx_classified_stale),
        ('held_matching_prior_id_turn_id_mismatch_falls_through_to_a4', scenario_held_matching_prior_id_turn_id_mismatch_falls_through_to_a4),
        ('held_with_no_context_drain_still_works', scenario_held_with_no_context_drain_still_works),
        ('held_matching_prior_id_with_matching_turn_drain_still_works', scenario_held_matching_prior_id_with_matching_turn_drain_still_works),
        ('held_with_interrupted_tombstone_match_classify_first', scenario_held_with_interrupted_tombstone_match_classify_first),
        ('aggregate_leg_unaffected_by_held_marker', scenario_aggregate_leg_unaffected_by_held_marker),
        ('prompt_intercept_inflight_only_requeues', scenario_prompt_intercept_inflight_only_requeues),
        ('consume_once_basic', scenario_consume_once_basic),
        ('consume_once_empty_response', scenario_consume_once_empty_response),
        ('empty_response_whitespace_only_routes_sentinel', scenario_empty_response_whitespace_only_routes_sentinel),
        ('empty_response_preserves_nonempty_with_surrounding_whitespace', scenario_empty_response_preserves_nonempty_with_surrounding_whitespace),
        ('empty_response_self_return_still_skipped', scenario_empty_response_self_return_still_skipped),
        ('empty_response_auto_return_false_still_skipped', scenario_empty_response_auto_return_false_still_skipped),
        ('empty_response_inactive_requester_still_skipped', scenario_empty_response_inactive_requester_still_skipped),
        ('empty_response_unicode_whitespace_routes_sentinel', scenario_empty_response_unicode_whitespace_routes_sentinel),
        ('nonce_mismatch_fail_closed', scenario_nonce_mismatch_fail_closed),
        ('no_observed_nonce_with_candidate_fail_closed', scenario_no_observed_nonce_with_candidate_fail_closed),
        ('daemon_restart_queue_scan', scenario_daemon_restart_queue_scan),
        ('daemon_reload_inflight_idempotent', scenario_daemon_reload_inflight_idempotent),
        ('ambiguous_inflight_fail_closed', scenario_ambiguous_inflight_fail_closed),
        ('stale_reserved_orphan_swept', scenario_stale_reserved_orphan_swept),
        ('held_drain_skips_consume_once', scenario_held_drain_skips_consume_once),
        ('matching_nonce_contaminated_body_residual', scenario_matching_nonce_contaminated_body_documents_residual),
        ('aggregate_consume_once_no_overwrite', scenario_aggregate_consume_once_no_overwrite),
        ('empty_response_in_aggregate_path_unchanged', scenario_empty_response_in_aggregate_path_unchanged),
        ('nonce_mismatch_stops_enter_retry', scenario_nonce_mismatch_stops_enter_retry),
        ('nonce_missing_stops_enter_retry', scenario_nonce_missing_stops_enter_retry),
        ('hook_logger_anchored_regex', scenario_hook_logger_anchored_regex),
        ('turn_id_mismatch_preserves_ctx', scenario_turn_id_mismatch_preserves_ctx),
        ('turn_id_mismatch_annotation_is_idempotent', scenario_turn_id_mismatch_annotation_is_idempotent),
        ('turn_id_mismatch_matching_stop_before_expiry_cleans_normally', scenario_turn_id_mismatch_matching_stop_before_expiry_cleans_normally),
        ('turn_id_mismatch_expiry_unblocks_target', scenario_turn_id_mismatch_expiry_unblocks_target),
        ('turn_id_mismatch_expiry_waits_for_watchdog_deadline', scenario_turn_id_mismatch_expiry_waits_for_watchdog_deadline),
        ('turn_id_mismatch_watchdog_fire_preserves_post_grace', scenario_turn_id_mismatch_watchdog_fire_preserves_post_grace),
        ('turn_id_mismatch_aggregate_watchdog_fire_preserves_post_grace', scenario_turn_id_mismatch_aggregate_watchdog_fire_preserves_post_grace),
        ('turn_id_mismatch_post_watchdog_grace_zero_allows_same_tick_expiry', scenario_turn_id_mismatch_post_watchdog_grace_zero_allows_same_tick_expiry),
        ('turn_id_mismatch_extend_wait_before_fire_defers_without_stamp', scenario_turn_id_mismatch_extend_wait_before_fire_defers_without_stamp),
        ('turn_id_mismatch_expiry_does_not_remove_non_delivered_queue_rows', scenario_turn_id_mismatch_expiry_does_not_remove_non_delivered_queue_rows),
        ('turn_id_mismatch_aggregate_leg_expiry_no_synthetic_reply', scenario_turn_id_mismatch_aggregate_leg_expiry_no_synthetic_reply),
        ('turn_id_mismatch_sweep_no_op_without_annotation', scenario_turn_id_mismatch_sweep_no_op_without_annotation),
        ('turn_id_mismatch_sweep_no_op_after_normal_pop', scenario_turn_id_mismatch_sweep_no_op_after_normal_pop),
        ('turn_id_mismatch_notice_ctx_follows_same_expiry', scenario_turn_id_mismatch_notice_ctx_follows_same_expiry),
        ('short_id_format', scenario_short_id_format),
        ('resolve_targets_single', scenario_resolve_targets_single),
        ('resolve_targets_multi_basic', scenario_resolve_targets_multi_basic),
        ('resolve_targets_order_preserved', scenario_resolve_targets_order_preserved),
        ('resolve_targets_dedup', scenario_resolve_targets_dedup),
        ('resolve_targets_strip_empties', scenario_resolve_targets_strip_empties),
        ('resolve_targets_reserved_alone', scenario_resolve_targets_reserved_alone),
        ('resolve_targets_reserved_mix_rejected', scenario_resolve_targets_reserved_mix_rejected),
        ('resolve_targets_unknown_rejected', scenario_resolve_targets_unknown_rejected),
        ('resolve_targets_sender_in_list_rejected', scenario_resolve_targets_sender_in_list_rejected),
        ('resolve_targets_empty_after_strip_rejected', scenario_resolve_targets_empty_after_strip_rejected),
        ('aggregate_trigger_request_multi', scenario_aggregate_trigger_request_multi),
        ('aggregate_trigger_single_no', scenario_aggregate_trigger_single_no),
        ('aggregate_trigger_notice_no', scenario_aggregate_trigger_notice_no),
        ('aggregate_trigger_bridge_sender_no', scenario_aggregate_trigger_bridge_sender_no),
        ('aggregate_trigger_no_auto_return_no', scenario_aggregate_trigger_no_auto_return_no),
        ('enqueue_aggregate_metadata_modes', phase3['enqueue_aggregate_metadata_modes']),
        ('prune_keeps_recent_n', scenario_prune_keeps_recent_n),
        ('prune_disabled_retention_zero', scenario_prune_disabled_retention_zero),
        ('prune_below_retention', scenario_prune_below_retention),
        ('prune_missing_forgotten_dir_safe', scenario_prune_missing_forgotten_dir_safe),
        ('resolve_forgotten_retention_invalid_env', scenario_resolve_forgotten_retention_invalid_env),
        ('queue_status_counts', scenario_queue_status_counts),
        ('queue_status_counts_missing_file', scenario_queue_status_counts_missing_file),
        ('uninstall_helper_print_paths', phase3['uninstall_helper_print_paths']),
        ('uninstall_helper_refuses_dangerous_path', phase3['uninstall_helper_refuses_dangerous_path']),
        ('uninstall_sh_dry_run_invokes_hook_helper_with_dry_run', phase3['uninstall_sh_dry_run_invokes_hook_helper_with_dry_run']),
        ('uninstall_sh_non_dry_run_removes_hook_entries', phase3['uninstall_sh_non_dry_run_removes_hook_entries']),
        ('uninstall_sh_keep_hooks_skips_helper_under_dry_run', phase3['uninstall_sh_keep_hooks_skips_helper_under_dry_run']),
        ('uninstall_sh_hook_helper_failure_aborts', phase3['uninstall_sh_hook_helper_failure_aborts']),
        ('direct_exec_targets_executable', phase3['direct_exec_targets_executable']),
        ('healthcheck_executable_helper_distinguishes_states', phase3['healthcheck_executable_helper_distinguishes_states']),
        ('python_env_override_removed_from_tracked_files', phase3['python_env_override_removed_from_tracked_files']),
        ('install_sh_python_version_gate', phase3['install_sh_python_version_gate']),
        ('bridge_healthcheck_sh_python_version_gate', phase3['bridge_healthcheck_sh_python_version_gate']),
        ('install_sh_chmods_target_or_fails', phase3['install_sh_chmods_target_or_fails']),
        ('install_sh_default_updates_shell_rc', phase3['install_sh_default_updates_shell_rc']),
        ('install_sh_shell_rc_dry_run_and_opt_out', phase3['install_sh_shell_rc_dry_run_and_opt_out']),
        ('install_sh_shell_rc_target_selection', phase3['install_sh_shell_rc_target_selection']),
        ('install_sh_shell_rc_backup_and_path_short_circuit', phase3['install_sh_shell_rc_backup_and_path_short_circuit']),
        ('install_sh_shell_quotes_bin_dir_metacharacters', phase3['install_sh_shell_quotes_bin_dir_metacharacters']),
        ('install_sh_shell_rc_replaces_existing_marker_block', phase3['install_sh_shell_rc_replaces_existing_marker_block']),
        ('install_sh_claude_editor_mode_missing_skips', phase3['install_sh_claude_editor_mode_missing_skips']),
        ('install_sh_claude_editor_mode_updates_with_backup', phase3['install_sh_claude_editor_mode_updates_with_backup']),
        ('install_sh_claude_editor_mode_updates_global_and_settings', phase3['install_sh_claude_editor_mode_updates_global_and_settings']),
        ('install_sh_claude_editor_mode_missing_global_updates_settings', phase3['install_sh_claude_editor_mode_missing_global_updates_settings']),
        ('install_sh_claude_editor_mode_invalid_global_still_updates_settings', phase3['install_sh_claude_editor_mode_invalid_global_still_updates_settings']),
        ('install_sh_claude_editor_mode_normal_noop', phase3['install_sh_claude_editor_mode_normal_noop']),
        ('install_sh_claude_editor_mode_absent_adds_root', phase3['install_sh_claude_editor_mode_absent_adds_root']),
        ('install_sh_claude_editor_mode_invalid_json_skips', phase3['install_sh_claude_editor_mode_invalid_json_skips']),
        ('install_sh_claude_editor_mode_invalid_utf8_skips', phase3['install_sh_claude_editor_mode_invalid_utf8_skips']),
        ('install_sh_claude_editor_mode_bom_skips', phase3['install_sh_claude_editor_mode_bom_skips']),
        ('install_sh_claude_editor_mode_non_object_skips', phase3['install_sh_claude_editor_mode_non_object_skips']),
        ('install_sh_claude_editor_mode_dry_run_no_write', phase3['install_sh_claude_editor_mode_dry_run_no_write']),
        ('install_sh_claude_editor_mode_preserves_mode', phase3['install_sh_claude_editor_mode_preserves_mode']),
        ('install_sh_claude_editor_mode_symlink_preserved', phase3['install_sh_claude_editor_mode_symlink_preserved']),
        ('install_sh_claude_editor_mode_nested_root_absent_warns_skips', phase3['install_sh_claude_editor_mode_nested_root_absent_warns_skips']),
        ('install_sh_claude_editor_mode_nested_with_root_warns_updates_root_only', phase3['install_sh_claude_editor_mode_nested_with_root_warns_updates_root_only']),
        ('install_sh_claude_editor_mode_skip_flag_no_write', phase3['install_sh_claude_editor_mode_skip_flag_no_write']),
        ('install_sh_claude_editor_mode_broken_symlink_warns', phase3['install_sh_claude_editor_mode_broken_symlink_warns']),
        ('install_sh_claude_editor_mode_concurrent_change_aborts', phase3['install_sh_claude_editor_mode_concurrent_change_aborts']),
        ('install_sh_hook_failure_hard_fails', phase3['install_sh_hook_failure_hard_fails']),
        ('install_sh_hook_failure_ignore_flag_allows_success', phase3['install_sh_hook_failure_ignore_flag_allows_success']),
        ('install_sh_hook_dry_run_failure_hard_fails', phase3['install_sh_hook_dry_run_failure_hard_fails']),
        ('install_sh_hook_success_succeeds', phase3['install_sh_hook_success_succeeds']),
        ('install_sh_shim_failure_still_hard_fails_under_override', phase3['install_sh_shim_failure_still_hard_fails_under_override']),
        ('install_sh_skip_hooks_with_ignore_flag_is_noop', phase3['install_sh_skip_hooks_with_ignore_flag_is_noop']),
        ('bridge_install_hooks_rejects_malformed_json_without_overwrite', phase3['bridge_install_hooks_rejects_malformed_json_without_overwrite']),
        ('bridge_install_hooks_rejects_non_object_json_without_overwrite', phase3['bridge_install_hooks_rejects_non_object_json_without_overwrite']),
        ('bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite', phase3['bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite']),
        ('bridge_install_hooks_missing_json_still_creates', phase3['bridge_install_hooks_missing_json_still_creates']),
        ('bridge_install_hooks_invalid_utf8_fails_without_overwrite', phase3['bridge_install_hooks_invalid_utf8_fails_without_overwrite']),
        ('bridge_install_hooks_existing_valid_json_object_merges_correctly', phase3['bridge_install_hooks_existing_valid_json_object_merges_correctly']),
        ('bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite', phase3['bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite']),
        ('bridge_install_hooks_codex_config_ignores_nested_codex_hooks', phase3['bridge_install_hooks_codex_config_ignores_nested_codex_hooks']),
        ('bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst', phase3['bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst']),
        ('bridge_install_hooks_codex_config_updates_only_scoped_keys', phase3['bridge_install_hooks_codex_config_updates_only_scoped_keys']),
        ('bridge_install_hooks_codex_config_inserts_features_key_before_next_table', phase3['bridge_install_hooks_codex_config_inserts_features_key_before_next_table']),
        ('bridge_install_hooks_codex_config_dry_run_no_write', phase3['bridge_install_hooks_codex_config_dry_run_no_write']),
        ('bridge_install_hooks_codex_config_empty_features_section_inserts', phase3['bridge_install_hooks_codex_config_empty_features_section_inserts']),
        ('bridge_install_hooks_codex_config_first_table_is_array_table', phase3['bridge_install_hooks_codex_config_first_table_is_array_table']),
        ('bridge_install_hooks_codex_config_table_header_with_trailing_comment', phase3['bridge_install_hooks_codex_config_table_header_with_trailing_comment']),
        ('bridge_install_hooks_codex_config_commented_out_assignments_ignored', phase3['bridge_install_hooks_codex_config_commented_out_assignments_ignored']),
        ('bridge_install_hooks_codex_config_no_trailing_newline_handled', phase3['bridge_install_hooks_codex_config_no_trailing_newline_handled']),
        ('bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged', phase3['bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged']),
        ('codex_config_marker_records_and_uninstall_restores_updated_values', phase3['codex_config_marker_records_and_uninstall_restores_updated_values']),
        ('codex_config_marker_records_inserted_keys_and_uninstall_removes_them', phase3['codex_config_marker_records_inserted_keys_and_uninstall_removes_them']),
        ('codex_config_missing_created_by_install_removed_on_uninstall_when_bridge_only', phase3['codex_config_missing_created_by_install_removed_on_uninstall_when_bridge_only']),
        ('codex_config_already_enabled_keys_create_no_marker', phase3['codex_config_already_enabled_keys_create_no_marker']),
        ('codex_config_reinstall_preserves_original_marker', phase3['codex_config_reinstall_preserves_original_marker']),
        ('codex_config_user_changed_after_install_is_not_clobbered', phase3['codex_config_user_changed_after_install_is_not_clobbered']),
        ('codex_config_dry_run_install_writes_no_marker_or_config', phase3['codex_config_dry_run_install_writes_no_marker_or_config']),
        ('codex_config_dry_run_uninstall_with_marker_writes_no_config_or_marker', phase3['codex_config_dry_run_uninstall_with_marker_writes_no_config_or_marker']),
        ('codex_config_skip_codex_does_not_touch_config_or_marker', phase3['codex_config_skip_codex_does_not_touch_config_or_marker']),
        ('codex_config_uninstall_with_malformed_marker_aborts_clean', phase3['codex_config_uninstall_with_malformed_marker_aborts_clean']),
        ('codex_config_uninstall_with_unknown_marker_version_aborts', phase3['codex_config_uninstall_with_unknown_marker_version_aborts']),
        ('codex_config_uninstall_with_marker_path_mismatch_aborts', phase3['codex_config_uninstall_with_marker_path_mismatch_aborts']),
        ('codex_config_uninstall_with_unknown_flag_key_aborts', phase3['codex_config_uninstall_with_unknown_flag_key_aborts']),
        ('codex_config_uninstall_with_invalid_operation_aborts', phase3['codex_config_uninstall_with_invalid_operation_aborts']),
        ('codex_config_uninstall_with_updated_missing_original_line_aborts', phase3['codex_config_uninstall_with_updated_missing_original_line_aborts']),
        ('codex_config_marker_write_failure_aborts_before_config_write', phase3['codex_config_marker_write_failure_aborts_before_config_write']),
        ('codex_config_inserted_features_section_with_user_added_keys_keeps_section', phase3['codex_config_inserted_features_section_with_user_added_keys_keeps_section']),
        ('codex_config_existing_empty_config_records_existed_true_and_uninstall_keeps_it', phase3['codex_config_existing_empty_config_records_existed_true_and_uninstall_keeps_it']),
        ('codex_config_marker_normalized_path_accepts_equivalent_path', phase3['codex_config_marker_normalized_path_accepts_equivalent_path']),
        ('restart_dry_run_no_side_effect', scenario_restart_dry_run_no_side_effect),
        ('recover_orphan_delivered', scenario_recover_orphan_delivered),
        ('recover_orphan_delivered_aggregate_member', scenario_recover_orphan_delivered_aggregate_member),
        ('prune_concurrent_stat_safe', scenario_prune_concurrent_stat_safe),
        ('format_peer_list_model_safe_default', phase3['format_peer_list_model_safe_default']),
        ('format_peer_list_full_includes_operator_fields', phase3['format_peer_list_full_includes_operator_fields']),
        ('bridge_manage_summary_concise', phase3['bridge_manage_summary_concise']),
        ('bridge_manage_summary_defaults', phase3['bridge_manage_summary_defaults']),
        ('bridge_manage_summary_legacy_state_fallback', phase3['bridge_manage_summary_legacy_state_fallback']),
        ('bridge_manage_summary_missing_session_exits', phase3['bridge_manage_summary_missing_session_exits']),
        ('model_safe_participants_strips_endpoints', phase3['model_safe_participants_strips_endpoints']),
        ('model_safe_participants_uses_active_only', phase3['model_safe_participants_uses_active_only']),
        ('list_peers_json_daemon_status_strips_pid', phase3['list_peers_json_daemon_status_strips_pid']),
        ('view_peer_rejects_snapshot_without_snapshot_mode', phase3['view_peer_rejects_snapshot_without_snapshot_mode']),
        ('view_peer_rejects_page_with_since_last_or_search', phase3['view_peer_rejects_page_with_since_last_or_search']),
        ('view_peer_allows_page_in_live_onboard_older_and_snapshot_in_older_search', phase3['view_peer_allows_page_in_live_onboard_older_and_snapshot_in_older_search']),
        ('view_peer_rejects_empty_search_before_session_lookup', phase3['view_peer_rejects_empty_search_before_session_lookup']),
        ('view_peer_empty_search_counts_as_search_for_mode_errors', phase3['view_peer_empty_search_counts_as_search_for_mode_errors']),
        ('view_peer_nonempty_search_still_reaches_validation', phase3['view_peer_nonempty_search_still_reaches_validation']),
        ('view_peer_search_with_page_after_a19_reports_page_error', phase3['view_peer_search_with_page_after_a19_reports_page_error']),
        ('view_peer_doc_surfaces_disclose_search_semantics', phase3['view_peer_doc_surfaces_disclose_search_semantics']),
        ('interrupt_peer_doc_surfaces_disclose_no_op_race', phase3['interrupt_peer_doc_surfaces_disclose_no_op_race']),
        ('interrupt_peer_doc_surfaces_no_queued_active_directive', phase3['interrupt_peer_doc_surfaces_no_queued_active_directive']),
        ('prompt_intercepted_doc_surfaces_disclose_user_typing_collision', phase3['prompt_intercepted_doc_surfaces_disclose_user_typing_collision']),
        ('response_send_guard_doc_surfaces_are_precise', phase3['response_send_guard_doc_surfaces_are_precise']),
        ('probe_prompt_is_compact_quickstart', phase3['probe_prompt_is_compact_quickstart']),
        ('send_peer_wait_doc_surfaces_name_blocking_consequence', phase3['send_peer_wait_doc_surfaces_name_blocking_consequence']),
        ('watchdog_phase_doc_surfaces_are_consistent', phase3['watchdog_phase_doc_surfaces_are_consistent']),
        ('wait_status_doc_surfaces_anti_polling', phase3['wait_status_doc_surfaces_anti_polling']),
        ('aggregate_status_doc_surfaces_leg_level_and_anti_polling', phase3['aggregate_status_doc_surfaces_leg_level_and_anti_polling']),
        ('view_peer_render_output_model_safe', phase3['view_peer_render_output_model_safe']),
        ('view_peer_search_explicit_snapshot_uses_safe_ref', phase3['view_peer_search_explicit_snapshot_uses_safe_ref']),
        ('view_peer_snapshot_ref_collision_unique', phase3['view_peer_snapshot_ref_collision_unique']),
        ('view_peer_capture_errors_sanitized', phase3['view_peer_capture_errors_sanitized']),
        ('view_peer_snapshot_not_found_hides_full_id', phase3['view_peer_snapshot_not_found_hides_full_id']),
        ('view_peer_since_last_matches_changed_volatile_chrome', phase3['view_peer_since_last_matches_changed_volatile_chrome']),
        ('view_peer_since_last_legacy_tail_derives_stable_anchor', phase3['view_peer_since_last_legacy_tail_derives_stable_anchor']),
        ('view_peer_since_last_ambiguous_current_anchor_skips_to_unique', phase3['view_peer_since_last_ambiguous_current_anchor_skips_to_unique']),
        ('view_peer_since_last_matches_anchor_before_long_delta', phase3['view_peer_since_last_matches_anchor_before_long_delta']),
        ('view_peer_since_last_build_rejects_duplicate_previous_window', phase3['view_peer_since_last_build_rejects_duplicate_previous_window']),
        ('view_peer_since_last_uncertain_does_not_advance_cursor', phase3['view_peer_since_last_uncertain_does_not_advance_cursor']),
        ('view_peer_since_last_upgrade_reset_when_no_legacy_anchor', phase3['view_peer_since_last_upgrade_reset_when_no_legacy_anchor']),
        ('view_peer_since_last_low_info_lines_do_not_anchor', phase3['view_peer_since_last_low_info_lines_do_not_anchor']),
        ('view_peer_since_last_claude_status_variants_are_volatile', phase3['view_peer_since_last_claude_status_variants_are_volatile']),
        ('view_peer_since_last_status_classifier_preserves_prose', phase3['view_peer_since_last_status_classifier_preserves_prose']),
        ('view_peer_since_last_claude_status_lines_do_not_anchor', phase3['view_peer_since_last_claude_status_lines_do_not_anchor']),
        ('view_peer_since_last_filters_stored_volatile_anchor_lines', phase3['view_peer_since_last_filters_stored_volatile_anchor_lines']),
        ('view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality', phase3['view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality']),
        ('view_peer_since_last_volatile_only_claude_status_delta', phase3['view_peer_since_last_volatile_only_claude_status_delta']),
        ('view_peer_since_last_codex_status_variants_preserved', phase3['view_peer_since_last_codex_status_variants_preserved']),
        ('view_peer_since_last_volatile_only_delta', phase3['view_peer_since_last_volatile_only_delta']),
        ('view_peer_since_last_short_delta_consumed_once', phase3['view_peer_since_last_short_delta_consumed_once']),
        ('view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update', phase3['view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update']),
        ('view_peer_since_last_consumed_tail_does_not_hide_new_duplicate', phase3['view_peer_since_last_consumed_tail_does_not_hide_new_duplicate']),
        ('view_peer_since_last_consumed_tail_anchor_change_resets', phase3['view_peer_since_last_consumed_tail_anchor_change_resets']),
        ('view_peer_since_last_consumed_tail_mismatch_clears', phase3['view_peer_since_last_consumed_tail_mismatch_clears']),
        ('view_peer_since_last_consumed_tail_cap', phase3['view_peer_since_last_consumed_tail_cap']),
        ('view_peer_since_last_consumed_tail_ignores_volatile_churn', phase3['view_peer_since_last_consumed_tail_ignores_volatile_churn']),
        ('view_peer_since_last_codex_prompt_placeholder_not_anchor', phase3['view_peer_since_last_codex_prompt_placeholder_not_anchor']),
        ('view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor', phase3['view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor']),
        ('view_peer_since_last_preserves_codex_bridge_prompt_lines', phase3['view_peer_since_last_preserves_codex_bridge_prompt_lines']),
        ('view_peer_since_last_preserves_trailing_semantic_codex_arrow', phase3['view_peer_since_last_preserves_trailing_semantic_codex_arrow']),
        ('view_peer_since_last_preserves_semantic_codex_arrow_before_footer', phase3['view_peer_since_last_preserves_semantic_codex_arrow_before_footer']),
        ('view_peer_since_last_claude_partial_status_fragments_are_volatile', phase3['view_peer_since_last_claude_partial_status_fragments_are_volatile']),
        ('view_peer_since_last_partial_status_preserves_prose', phase3['view_peer_since_last_partial_status_preserves_prose']),
        ('endpoint_rejects_stale_pane_lock_without_live', scenario_endpoint_rejects_stale_pane_lock_without_live),
        ('endpoint_rejects_same_pane_new_live_identity', scenario_endpoint_rejects_same_pane_new_live_identity),
        ('endpoint_probe_unknown_does_not_mutate', scenario_endpoint_probe_unknown_does_not_mutate),
        ('endpoint_accepts_matching_process_fingerprint', scenario_endpoint_accepts_matching_process_fingerprint),
        ('backfill_refuses_to_mint_without_live_record', scenario_backfill_refuses_to_mint_without_live_record),
        ('backfill_refuses_other_live_identity', scenario_backfill_refuses_other_live_identity),
        ('backfill_rejects_changed_process_fingerprint', scenario_backfill_rejects_changed_process_fingerprint),
        ('backfill_allows_fresh_hook_proof_create', scenario_backfill_allows_fresh_hook_proof_create),
        ('backfill_fresh_probe_repairs_unscoped_live_mismatch', scenario_backfill_fresh_probe_repairs_unscoped_live_mismatch),
        ('unscoped_hook_canonicalizes_via_pane_lock', scenario_unscoped_hook_canonicalizes_via_pane_lock),
        ('unscoped_hook_canonicalizes_during_attach_gap', scenario_unscoped_hook_canonicalizes_during_attach_gap),
        ('unscoped_hook_canonicalizes_via_attached_registry', scenario_unscoped_hook_canonicalizes_via_attached_registry),
        ('unscoped_hook_new_process_fails_closed', scenario_unscoped_hook_new_process_fails_closed),
        ('unscoped_hook_cross_reboot_fails_closed', scenario_unscoped_hook_cross_reboot_fails_closed),
        ('unscoped_hook_missing_prior_fingerprint_fails_closed', scenario_unscoped_hook_missing_prior_fingerprint_fails_closed),
        ('unscoped_hook_different_agent_fails_closed', scenario_unscoped_hook_different_agent_fails_closed),
        ('unscoped_hook_same_session_no_canonicalization_event', scenario_unscoped_hook_same_session_no_canonicalization_event),
        ('scoped_different_session_no_canonicalization', scenario_scoped_different_session_no_canonicalization),
        ('session_ended_payload_mismatch_no_canonicalization', scenario_session_ended_payload_mismatch_no_canonicalization),
        ('resolver_reconnects_exact_mismatch_shape', scenario_resolver_reconnects_exact_mismatch_shape),
        ('hook_reconnects_exact_mismatch_shape', scenario_hook_reconnects_exact_mismatch_shape),
        ('cross_pane_candidate_mismatch_blocks_reconnect', scenario_cross_pane_candidate_mismatch_blocks_reconnect),
        ('codex1_incident_replay_canonicalizes_repeated_unscoped_hooks', scenario_codex1_incident_replay_canonicalizes_repeated_unscoped_hooks),
        ('target_recovery_reconnects_stale_pane_by_codex_transcript', scenario_target_recovery_reconnects_stale_pane_by_codex_transcript),
        ('target_recovery_blocks_transcript_session_mismatch', scenario_target_recovery_blocks_transcript_session_mismatch),
        ('target_recovery_blocks_missing_transcript', scenario_target_recovery_blocks_missing_transcript),
        ('target_recovery_blocks_wrong_pid_transcript', scenario_target_recovery_blocks_wrong_pid_transcript),
        ('target_recovery_blocks_different_agent_at_target', scenario_target_recovery_blocks_different_agent_at_target),
        ('target_recovery_blocks_same_pane_target', scenario_target_recovery_blocks_same_pane_target),
        ('target_recovery_blocks_unresolvable_target', scenario_target_recovery_blocks_unresolvable_target),
        ('target_recovery_skips_without_participant_target', scenario_target_recovery_skips_without_participant_target),
        ('target_recovery_read_purpose_does_not_mutate', scenario_target_recovery_read_purpose_does_not_mutate),
        ('target_recovery_env_disable_blocks', scenario_target_recovery_env_disable_blocks),
        ('tmux_display_pane_empty_metadata_is_unavailable', scenario_tmux_display_pane_empty_metadata_is_unavailable),
        ('codex_rollout_path_regex_is_strict', scenario_codex_rollout_path_regex_is_strict),
        ('target_recovery_only_matching_alias_recovers', scenario_target_recovery_only_matching_alias_recovers),
        ('hook_unknown_preserves_verified_process_identity', scenario_hook_unknown_preserves_verified_process_identity),
        ('probe_tmux_access_failure_unknown', scenario_probe_tmux_access_failure_unknown),
        ('endpoint_read_mismatch_does_not_mutate', scenario_endpoint_read_mismatch_does_not_mutate),
        ('verified_candidate_ordering_prefers_pane_then_newest', scenario_verified_candidate_ordering_prefers_pane_then_newest),
        ('resume_new_pane_reconnects_unknown_old_and_logs', scenario_resume_new_pane_reconnects_unknown_old_and_logs),
        ('resume_unknown_old_opt_out_blocks_switch', scenario_resume_unknown_old_opt_out_blocks_switch),
        ('hook_cached_prior_unknown_does_not_reconnect', scenario_hook_cached_prior_unknown_does_not_reconnect),
        ('resolver_reconnects_to_alternate_verified_live_record', scenario_resolver_reconnects_to_alternate_verified_live_record),
        ('resolver_candidate_unknown_on_final_probe_does_not_reconnect', scenario_resolver_candidate_unknown_on_final_probe_does_not_reconnect),
        ('resolver_read_reconnect_logs_distinct_reason', scenario_resolver_read_reconnect_logs_distinct_reason),
        ('session_end_replacement_uses_verified_candidate', scenario_session_end_replacement_uses_verified_candidate),
        ('reconnect_rereads_mapping_before_write', scenario_reconnect_rereads_mapping_before_write),
        ('caller_reconnects_from_resumed_pane', scenario_caller_reconnects_from_resumed_pane),
        ('no_probe_requires_verified_live_identity', scenario_no_probe_requires_verified_live_identity),
        ('daemon_undeliverable_request_returns_result', scenario_daemon_undeliverable_request_returns_result),
        ('interrupt_endpoint_lost_finalizes_delivered_non_aggregate', scenario_interrupt_endpoint_lost_finalizes_delivered_non_aggregate),
        ('interrupt_endpoint_lost_finalizes_delivered_aggregate', scenario_interrupt_endpoint_lost_finalizes_delivered_aggregate),
        ('retry_enter_endpoint_lost_does_not_press_enter', scenario_retry_enter_endpoint_lost_does_not_press_enter),
        ('direct_notices_suppress_unverified_endpoint', scenario_direct_notices_suppress_unverified_endpoint),
        ('view_peer_unverified_endpoint_uses_daemon_not_local_capture', phase3['view_peer_unverified_endpoint_uses_daemon_not_local_capture']),
        ('daemon_startup_backfill_summary_logs_repair_hint', scenario_daemon_startup_backfill_summary_logs_repair_hint),
        ('peer_body_size_helper_boundaries', scenario_peer_body_size_helper_boundaries),
        ('send_peer_rejects_oversized_body_before_subprocess', phase3['send_peer_rejects_oversized_body_before_subprocess']),
        ('send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim', phase3['send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim']),
        ('send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim', phase3['send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim']),
        ('send_peer_watchdog_bare_minus_inf_argparse_error_at_shim', phase3['send_peer_watchdog_bare_minus_inf_argparse_error_at_shim']),
        ('send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim', phase3['send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim']),
        ('send_peer_watchdog_finite_value_forwarded_with_equals', phase3['send_peer_watchdog_finite_value_forwarded_with_equals']),
        ('send_peer_body_input_diagnostics_are_specific', phase3['send_peer_body_input_diagnostics_are_specific']),
        ('send_peer_rejects_split_inline_body', phase3['send_peer_rejects_split_inline_body']),
        ('send_peer_rejects_implicit_split_inline_body', phase3['send_peer_rejects_implicit_split_inline_body']),
        ('send_peer_accepts_option_after_destination_with_stdin', phase3['send_peer_accepts_option_after_destination_with_stdin']),
        ('send_peer_accepts_option_after_implicit_target_with_stdin', phase3['send_peer_accepts_option_after_implicit_target_with_stdin']),
        ('send_peer_accepts_options_after_destination_before_inline_body', phase3['send_peer_accepts_options_after_destination_before_inline_body']),
        ('send_peer_rejects_option_after_inline_body', phase3['send_peer_rejects_option_after_inline_body']),
        ('send_peer_rejects_duplicate_destination_after_destination', phase3['send_peer_rejects_duplicate_destination_after_destination']),
        ('send_peer_implicit_target_option_without_body_reports_body_required', phase3['send_peer_implicit_target_option_without_body_reports_body_required']),
        ('send_peer_rejects_allow_spoof_after_implicit_target', phase3['send_peer_rejects_allow_spoof_after_implicit_target']),
        ('send_peer_single_inline_body_uses_stdin_handoff', phase3['send_peer_single_inline_body_uses_stdin_handoff']),
        ('send_peer_request_success_prints_anti_wait_hint', phase3['send_peer_request_success_prints_anti_wait_hint']),
        ('send_peer_prior_hint_precedes_success_hint', phase3['send_peer_prior_hint_precedes_success_hint']),
        ('send_peer_notice_success_prints_alarm_and_anti_wait_hints', phase3['send_peer_notice_success_prints_alarm_and_anti_wait_hints']),
        ('send_peer_aggregate_request_success_prints_result_hint', phase3['send_peer_aggregate_request_success_prints_result_hint']),
        ('send_peer_subprocess_failure_prints_no_success_hint', phase3['send_peer_subprocess_failure_prints_no_success_hint']),
        ('send_peer_ambient_socket_stdin_does_not_block', phase3['send_peer_ambient_socket_stdin_does_not_block']),
        ('send_peer_inline_body_accepts_empty_non_tty_stdin', phase3['send_peer_inline_body_accepts_empty_non_tty_stdin']),
        ('send_peer_explicit_stdin_multibyte_body', phase3['send_peer_explicit_stdin_multibyte_body']),
        ('send_peer_implicit_target_allows_stdin', phase3['send_peer_implicit_target_allows_stdin']),
        ('send_peer_implicit_target_resolves_session_from_pane', phase3['send_peer_implicit_target_resolves_session_from_pane']),
        ('send_peer_implicit_target_stdin_resolves_session_from_pane', phase3['send_peer_implicit_target_stdin_resolves_session_from_pane']),
        ('send_peer_precheck_fails_open_identity_errors_owned_by_validator', phase3['send_peer_precheck_fails_open_identity_errors_owned_by_validator']),
        ('send_peer_allow_spoof_requires_explicit_destination_without_session', phase3['send_peer_allow_spoof_requires_explicit_destination_without_session']),
        ('send_peer_rejects_allow_spoof_attached_value', phase3['send_peer_rejects_allow_spoof_attached_value']),
        ('send_peer_all_rejects_leading_alias_body', phase3['send_peer_all_rejects_leading_alias_body']),
        ('send_peer_rejects_stdin_with_positional_body', phase3['send_peer_rejects_stdin_with_positional_body']),
        ('send_peer_rejects_pipe_with_positional_body', phase3['send_peer_rejects_pipe_with_positional_body']),
        ('send_peer_pipe_only_body_still_supported', phase3['send_peer_pipe_only_body_still_supported']),
        ('send_peer_precheck_option_table_matches_parser', phase3['send_peer_precheck_option_table_matches_parser']),
        ('enqueue_rejects_body_and_stdin_before_session_lookup', phase3['enqueue_rejects_body_and_stdin_before_session_lookup']),
        ('enqueue_rejects_empty_body_and_stdin', phase3['enqueue_rejects_empty_body_and_stdin']),
        ('enqueue_rejects_body_and_stdin_argv_order_independent', phase3['enqueue_rejects_body_and_stdin_argv_order_independent']),
        ('enqueue_body_only_still_works', phase3['enqueue_body_only_still_works']),
        ('enqueue_stdin_only_still_works', phase3['enqueue_stdin_only_still_works']),
        ('enqueue_aggregate_stdout_socket_and_fallback', phase3['enqueue_aggregate_stdout_socket_and_fallback']),
        ('enqueue_aggregate_stdout_all_and_negative_cases', phase3['enqueue_aggregate_stdout_all_and_negative_cases']),
        ('enqueue_bare_body_without_value_remains_argparse_owned', phase3['enqueue_bare_body_without_value_remains_argparse_owned']),
        ('enqueue_rejects_oversized_body_unchanged', phase3['enqueue_rejects_oversized_body_unchanged']),
        ('enqueue_stdin_rejects_oversized_body_unchanged', phase3['enqueue_stdin_rejects_oversized_body_unchanged']),
        ('enqueue_socket_success_hints_stderr_stdout_unchanged', phase3['enqueue_socket_success_hints_stderr_stdout_unchanged']),
        ('enqueue_fallback_prior_hint_pending_cancel', phase3['enqueue_fallback_prior_hint_pending_cancel']),
        ('enqueue_fallback_prior_hint_submitted_interrupt', phase3['enqueue_fallback_prior_hint_submitted_interrupt']),
        ('enqueue_fallback_prior_hint_plain_inflight_omitted', phase3['enqueue_fallback_prior_hint_plain_inflight_omitted']),
        ('enqueue_fallback_prior_hint_pane_mode_inflight_cancel', phase3['enqueue_fallback_prior_hint_pane_mode_inflight_cancel']),
        ('enqueue_fallback_prior_hint_write_failure_suppresses_hint', phase3['enqueue_fallback_prior_hint_write_failure_suppresses_hint']),
        ('enqueue_fallback_response_send_guard_no_hint', phase3['enqueue_fallback_response_send_guard_no_hint']),
        ('alarm_cancel_preserves_at_limit_body', scenario_alarm_cancel_preserves_at_limit_body),
        ('prompt_body_preserves_multiline_and_sanitizes', scenario_prompt_body_preserves_multiline_and_sanitizes),
        ('build_peer_prompt_signature_drops_max_hops', scenario_build_peer_prompt_signature_drops_max_hops),
        ('tmux_paste_buffer_delivery_sequence', scenario_tmux_paste_buffer_delivery_sequence),
        ('daemon_logs_body_truncated_for_legacy_long_body', scenario_daemon_logs_body_truncated_for_legacy_long_body),
        ('peer_result_redirect_single_over_limit', scenario_peer_result_redirect_single_over_limit),
        ('peer_result_redirect_restart_queue_roundtrip', scenario_peer_result_redirect_restart_queue_roundtrip),
        ('peer_result_redirect_below_limit_inline', scenario_peer_result_redirect_below_limit_inline),
        ('peer_result_redirect_write_failure_fallback', scenario_peer_result_redirect_write_failure_fallback),
        ('peer_result_redirect_collision_and_symlink_safe', scenario_peer_result_redirect_collision_and_symlink_safe),
        ('peer_result_redirect_share_root_owner_mismatch', scenario_peer_result_redirect_share_root_owner_mismatch),
        ('peer_result_redirect_replies_permission_drift_tightened', scenario_peer_result_redirect_replies_permission_drift_tightened),
        ('peer_result_redirect_logs_reduced_preview_chars', scenario_peer_result_redirect_logs_reduced_preview_chars),
        ('peer_result_redirect_malformed_message_id_safe_filename', scenario_peer_result_redirect_malformed_message_id_safe_filename),
        ('peer_result_redirect_aggregate_unique_once', scenario_peer_result_redirect_aggregate_unique_once),
        ('response_send_guard_socket_cli_error_kind', scenario_response_send_guard_socket_cli_error_kind),
        ('response_send_guard_socket_error_kind_parse', scenario_response_send_guard_socket_error_kind_parse),
        ('enqueue_fallback_success_silent_with_raw_diagnostic', phase3['enqueue_fallback_success_silent_with_raw_diagnostic']),
        ('enqueue_fallback_write_failure_preserves_stderr', phase3['enqueue_fallback_write_failure_preserves_stderr']),
        ('response_send_guard_fallback_blocks_unchanged', scenario_response_send_guard_fallback_blocks_unchanged),
        ('response_send_guard_fallback_all_blocks_unchanged', scenario_response_send_guard_fallback_all_blocks_unchanged),
        ('response_send_guard_fallback_partial_blocks_unchanged', scenario_response_send_guard_fallback_partial_blocks_unchanged),
        ('response_send_guard_fallback_force_allows', scenario_response_send_guard_fallback_force_allows),
        ('response_send_guard_fallback_no_auto_return_allowed', scenario_response_send_guard_fallback_no_auto_return_allowed),
        ('response_send_guard_fallback_false_positive_resistance', scenario_response_send_guard_fallback_false_positive_resistance),
        ('clear_reservation_blocks_delivery', scenario_clear_reservation_blocks_delivery),
        ('clear_guard_formatter_force_guidance', scenario_clear_guard_formatter_force_guidance),
        ('clear_guard_force_truth_matches_policy', scenario_clear_guard_force_truth_matches_policy),
        ('clear_multi_guard_pass_reserves_all', scenario_clear_multi_guard_pass_reserves_all),
        ('clear_multi_guard_hard_blocker_rejects_all', scenario_clear_multi_guard_hard_blocker_rejects_all),
        ('clear_multi_guard_soft_blocker_rejects_all', scenario_clear_multi_guard_soft_blocker_rejects_all),
        ('clear_multi_partial_outcomes_and_cleanup', scenario_clear_multi_partial_outcomes_and_cleanup),
        ('clear_multi_pre_pane_failure_continues', scenario_clear_multi_pre_pane_failure_continues),
        ('clear_multi_forced_leave_notice_waits_behind_reservation', scenario_clear_multi_forced_leave_notice_waits_behind_reservation),
        ('clear_multi_lock_wait_failed_and_rerun', scenario_clear_multi_lock_wait_failed_and_rerun),
        ('clear_multi_real_post_touch_exception_forces_leave', scenario_clear_multi_real_post_touch_exception_forces_leave),
        ('clear_multi_real_success_holds_until_batch_end', scenario_clear_multi_real_success_holds_until_batch_end),
        ('clear_multi_real_pre_pane_failure_holds_until_batch_end', scenario_clear_multi_real_pre_pane_failure_holds_until_batch_end),
        ('clear_guard_blocks_pending_self_clear', scenario_clear_guard_blocks_pending_self_clear),
        ('clear_multi_daemon_validation', scenario_clear_multi_daemon_validation),
        ('clear_peer_cli_multi_and_compatibility', scenario_clear_peer_cli_multi_and_compatibility),
        ('clear_multi_timeout_and_formatter', scenario_clear_multi_timeout_and_formatter),
        ('self_clear_force_guidance_and_preserved_force', scenario_self_clear_force_guidance_and_preserved_force),
        ('requester_cleared_prompt_guard_and_notice', scenario_requester_cleared_prompt_guard_and_notice),
        ('clear_marker_routes_fast_stop', scenario_clear_marker_routes_fast_stop),
        ('clear_marker_probe_id_fallback_contracts', scenario_clear_marker_probe_id_fallback_contracts),
        ('clear_post_clear_first_request_routes_reply', scenario_clear_post_clear_first_request_routes_reply),
        ('clear_with_existing_inbound_queue_routes_replies', scenario_clear_with_existing_inbound_queue_routes_replies),
        ('clear_file_fallback_blocked_sender_aged_ingress', scenario_clear_file_fallback_blocked_sender_aged_ingress),
        ('clear_post_clear_delay_config_and_settle', scenario_clear_post_clear_delay_config_and_settle),
        ('clear_settle_pane_not_ready_forces_leave', scenario_clear_settle_pane_not_ready_forces_leave),
        ('clear_identity_helper_preserves_verified_live_identity', scenario_clear_identity_helper_preserves_verified_live_identity),
        ('clear_requires_verified_process_identity', scenario_clear_requires_verified_process_identity),
        ('clear_codex_post_clear_endpoint_verify_immediately', scenario_clear_codex_post_clear_endpoint_verify_immediately),
        ('clear_claude_post_clear_probe_pasted_and_completes', scenario_clear_claude_post_clear_probe_pasted_and_completes),
        ('clear_session_end_suppression_is_exact', scenario_clear_session_end_suppression_is_exact),
        ('command_state_lock_timeout_before_mutation', scenario_command_state_lock_timeout_before_mutation),
        ('clear_success_delivery_defers_near_deadline', scenario_clear_success_delivery_defers_near_deadline),
        ('clear_success_delivery_lock_wait_does_not_fail_after_commit', scenario_clear_success_delivery_lock_wait_does_not_fail_after_commit),
        ('clear_probe_subprocess_timeouts', scenario_clear_probe_subprocess_timeouts),
        ('clear_marker_phase2_missing_forces_leave', scenario_clear_marker_phase2_missing_forces_leave),
        ('clear_marker_phase2_rejects_record_turn_without_marker_turn', scenario_clear_marker_phase2_rejects_record_turn_without_marker_turn),
        ('run_clear_peer_phase2_failure_returns_immediately', scenario_run_clear_peer_phase2_failure_returns_immediately),
        ('clear_force_leave_cleans_identity_stores', scenario_clear_force_leave_cleans_identity_stores),
        ('clear_force_leave_unowned_lock_serializes_cleanup', scenario_clear_force_leave_unowned_lock_serializes_cleanup),
        ('clear_identity_helper_success_and_failure', scenario_clear_identity_helper_success_and_failure),
        ('force_clear_requester_dual_write_queue_and_active', scenario_force_clear_requester_dual_write_queue_and_active),
        ('clear_aggregate_requester_late_reply_suppressed', scenario_clear_aggregate_requester_late_reply_suppressed),
        ('self_clear_promotion_and_cancel_notice_ordering', scenario_self_clear_promotion_and_cancel_notice_ordering),
        ('clear_marker_ttl_expiry_removes_marker', scenario_clear_marker_ttl_expiry_removes_marker),
    ]

def main(argv: list[str] | None = None) -> int:
    from regression.runner import main as runner_main

    return runner_main(argv, scenarios)


if __name__ == "__main__":
    raise SystemExit(main())
