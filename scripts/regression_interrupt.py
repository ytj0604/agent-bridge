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
from contextlib import redirect_stderr, redirect_stdout
import inspect
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from regression.harness import (
    _active_turn,
    _auto_return_results,
    _daemon_command_result,
    _make_delivered_context,
    _make_inflight,
    _participants_state,
    _plant_watchdog,
    _queue_item,
    assert_true,
    identity_live_record,
    isolated_identity_env,
    make_daemon,
    patched_environ,
    read_events,
    read_raw_events,
    set_identity_target,
    test_message,
    verified_identity,
    write_identity_fixture,
    write_live_identity_records,
)

import bridge_daemon  # noqa: E402
import bridge_hook_logger  # noqa: E402
import bridge_identity  # noqa: E402
import bridge_interrupt_peer  # noqa: E402
import bridge_clear_peer  # noqa: E402
import bridge_clear_guard  # noqa: E402
import bridge_clear_marker  # noqa: E402
import bridge_leave  # noqa: E402
import bridge_pane_probe  # noqa: E402
import bridge_response_guard  # noqa: E402
from bridge_util import locked_json, read_json, utc_now, write_json_atomic  # noqa: E402


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


# ---------- v1.5.2 scenarios: state-based delivery matching + consume-once ----------
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


# ---------- v1.5.x scenarios: multi-target send_peer ----------


# ---------- v1.5.x scenarios: aggregate trigger guards (unit-style) ----------


# ---------- v1.5.x scenarios: forgotten retention + restart guards ----------


# ---------- v1.5.x P1 follow-up: dry-run safety + orphan delivered + concurrent prune ----------


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
    from regression import (
        install_uninstall,
        hook_config,
        view_peer,
        docs_contracts,
        send_enqueue,
        join_leave_attach,
        aggregate_wait_status,
        watchdog_alarm,
        cancel_message,
        pane_mode_delivery,
        daemon_core,
    )
    from regression.registry import scenario_index

    moved = scenario_index(
        install_uninstall.SCENARIOS,
        hook_config.SCENARIOS,
        view_peer.SCENARIOS,
        docs_contracts.SCENARIOS,
        send_enqueue.SCENARIOS,
        join_leave_attach.SCENARIOS,
        aggregate_wait_status.SCENARIOS,
        watchdog_alarm.SCENARIOS,
        cancel_message.SCENARIOS,
        pane_mode_delivery.SCENARIOS,
        daemon_core.SCENARIOS,
    )
    return [
        ('lifecycle_delivered_terminal', moved['lifecycle_delivered_terminal']),
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
        ('watchdog_cancel_on_empty_response', moved['watchdog_cancel_on_empty_response']),
        ('alarm_cancelled_by_qualifying_request', moved['alarm_cancelled_by_qualifying_request']),
        ('socket_path_alarm_cancel', moved['socket_path_alarm_cancel']),
        ('response_send_guard_socket_request_notice', moved['response_send_guard_socket_request_notice']),
        ('response_send_guard_socket_force_and_other_peer', moved['response_send_guard_socket_force_and_other_peer']),
        ('response_send_guard_socket_no_auto_return_allowed', moved['response_send_guard_socket_no_auto_return_allowed']),
        ('response_send_guard_socket_atomic_multi', moved['response_send_guard_socket_atomic_multi']),
        ('response_send_guard_socket_aggregate_and_held', moved['response_send_guard_socket_aggregate_and_held']),
        ('response_send_guard_after_response_finished_allowed', moved['response_send_guard_after_response_finished_allowed']),
        ('fallback_path_alarm_cancel', moved['fallback_path_alarm_cancel']),
        ('ingressing_not_delivered_before_finalize', moved['ingressing_not_delivered_before_finalize']),
        ('replay_does_not_cancel_later_alarm', moved['replay_does_not_cancel_later_alarm']),
        ('bridge_origin_fallback_ingressing_promoted', moved['bridge_origin_fallback_ingressing_promoted']),
        ('socket_normalizes_non_ingressing_status', moved['socket_normalizes_non_ingressing_status']),
        ('aggregate_fallback_finalize', moved['aggregate_fallback_finalize']),
        ('aged_ingressing_promoted_by_maintenance', moved['aged_ingressing_promoted_by_maintenance']),
        ('aged_ingressing_does_not_cancel_alarms', moved['aged_ingressing_does_not_cancel_alarms']),
        ('aged_ingressing_malformed_timestamp_promoted', moved['aged_ingressing_malformed_timestamp_promoted']),
        ('fresh_ingressing_not_promoted_by_maintenance', moved['fresh_ingressing_not_promoted_by_maintenance']),
        ('alarm_not_cancelled_by_result', moved['alarm_not_cancelled_by_result']),
        ('alarm_not_cancelled_by_bridge', moved['alarm_not_cancelled_by_bridge']),
        ('user_prompt_does_not_cancel_alarm', moved['user_prompt_does_not_cancel_alarm']),
        ('extend_wait_upserts_watchdog', moved['extend_wait_upserts_watchdog']),
        ('extend_wait_aggregate_rejected', moved['extend_wait_aggregate_rejected']),
        ('extend_wait_unknown_message', moved['extend_wait_unknown_message']),
        ('extend_wait_terminal_tombstone_classification', moved['extend_wait_terminal_tombstone_classification']),
        ('extend_wait_pending_rejected', moved['extend_wait_pending_rejected']),
        ('extend_wait_not_owner', moved['extend_wait_not_owner']),
        ('cancel_message_pending_removes_row', moved['cancel_message_pending_removes_row']),
        ('cancel_message_inflight_pre_paste', moved['cancel_message_inflight_pre_paste']),
        ('cancel_message_pane_mode_deferred_inflight_cancellable', moved['cancel_message_pane_mode_deferred_inflight_cancellable']),
        ('cancel_message_inflight_after_enter_rejects_with_interrupt_guidance', moved['cancel_message_inflight_after_enter_rejects_with_interrupt_guidance']),
        ('cancel_message_submitted_rejects_with_interrupt_guidance', moved['cancel_message_submitted_rejects_with_interrupt_guidance']),
        ('cancel_message_delivered_rejects_with_interrupt_guidance', moved['cancel_message_delivered_rejects_with_interrupt_guidance']),
        ('cancel_message_ownership_violation', moved['cancel_message_ownership_violation']),
        ('cancel_message_idempotent_after_terminal', moved['cancel_message_idempotent_after_terminal']),
        ('cancel_message_aggregate_per_leg_keeps_other_legs', moved['cancel_message_aggregate_per_leg_keeps_other_legs']),
        ('cancel_message_action_text_and_json_modes', moved['cancel_message_action_text_and_json_modes']),
        ('cancel_message_action_error_text_modes', moved['cancel_message_action_error_text_modes']),
        ('aggregate_status_open_from_queue_and_reply', moved['aggregate_status_open_from_queue_and_reply']),
        ('aggregate_status_zero_reply_from_queue', moved['aggregate_status_zero_reply_from_queue']),
        ('aggregate_status_cancelled_and_timeout_synthetic_reasons', moved['aggregate_status_cancelled_and_timeout_synthetic_reasons']),
        ('aggregate_status_legacy_synthetic_reply_uses_tombstone', moved['aggregate_status_legacy_synthetic_reply_uses_tombstone']),
        ('aggregate_status_foreign_and_unknown_indistinguishable', moved['aggregate_status_foreign_and_unknown_indistinguishable']),
        ('aggregate_status_source_conflict_fails_closed', moved['aggregate_status_source_conflict_fails_closed']),
        ('aggregate_status_legacy_fallback_and_cap', moved['aggregate_status_legacy_fallback_and_cap']),
        ('aggregate_status_watchdog_only_anchor', moved['aggregate_status_watchdog_only_anchor']),
        ('wait_status_empty_self_view', moved['wait_status_empty_self_view']),
        ('wait_status_outstanding_watchdogs_alarms_pending_inbound', moved['wait_status_outstanding_watchdogs_alarms_pending_inbound']),
        ('wait_status_aggregate_waits_privacy_and_completed_result', moved['wait_status_aggregate_waits_privacy_and_completed_result']),
        ('wait_status_caps_and_summary_counts', moved['wait_status_caps_and_summary_counts']),
        ('delivery_watchdog_arms_on_reserve', moved['delivery_watchdog_arms_on_reserve']),
        ('delivery_watchdog_fire_inflight_notice', moved['delivery_watchdog_fire_inflight_notice']),
        ('watchdog_fire_after_terminal_suppresses_notice', moved['watchdog_fire_after_terminal_suppresses_notice']),
        ('pending_watchdog_wake_removed_on_terminal', moved['pending_watchdog_wake_removed_on_terminal']),
        ('delivery_watchdog_extend_replaces_wake', moved['delivery_watchdog_extend_replaces_wake']),
        ('delivery_watchdog_submitted_extend_and_text', moved['delivery_watchdog_submitted_extend_and_text']),
        ('delivery_watchdog_aggregate_pending_extend_rejected', moved['delivery_watchdog_aggregate_pending_extend_rejected']),
        ('delivery_watchdog_replaced_by_response_on_delivered', moved['delivery_watchdog_replaced_by_response_on_delivered']),
        ('delivery_watchdog_requeue_cancels', moved['delivery_watchdog_requeue_cancels']),
        ('delivery_watchdog_mark_pending_cancels', moved['delivery_watchdog_mark_pending_cancels']),
        ('delivery_watchdog_pane_mode_reverts_cancel', moved['delivery_watchdog_pane_mode_reverts_cancel']),
        ('delivery_watchdog_phase_mismatch_skipped', moved['delivery_watchdog_phase_mismatch_skipped']),
        ('watchdog_phase_legacy_default_response', moved['watchdog_phase_legacy_default_response']),
        ('delivery_watchdog_aggregate_leg_coexists_with_response', moved['delivery_watchdog_aggregate_leg_coexists_with_response']),
        ('delivery_watchdog_aggregate_interrupt_cancels_leg_only', moved['delivery_watchdog_aggregate_interrupt_cancels_leg_only']),
        ('aggregate_response_watchdog_text_uses_progress', moved['aggregate_response_watchdog_text_uses_progress']),
        ('aggregate_completion_suppresses_pending_watchdog_wake', moved['aggregate_completion_suppresses_pending_watchdog_wake']),
        ('duplicate_enqueue_does_not_cancel_alarm', moved['duplicate_enqueue_does_not_cancel_alarm']),
        ('alarm_op_invalid_delay_is_rejected', moved['alarm_op_invalid_delay_is_rejected']),
        ('send_peer_watchdog_negative_one_rejected', moved['send_peer_watchdog_negative_one_rejected']),
        ('send_peer_watchdog_nan_rejected', moved['send_peer_watchdog_nan_rejected']),
        ('send_peer_watchdog_inf_rejected', moved['send_peer_watchdog_inf_rejected']),
        ('send_peer_watchdog_minus_inf_rejected', moved['send_peer_watchdog_minus_inf_rejected']),
        ('send_peer_watchdog_zero_disables_for_request', moved['send_peer_watchdog_zero_disables_for_request']),
        ('send_peer_watchdog_finite_positive_succeeds', moved['send_peer_watchdog_finite_positive_succeeds']),
        ('send_peer_no_auto_return_explicit_watchdog_rejected', moved['send_peer_no_auto_return_explicit_watchdog_rejected']),
        ('send_peer_no_auto_return_watchdog_zero_succeeds', moved['send_peer_no_auto_return_watchdog_zero_succeeds']),
        ('send_peer_no_auto_return_invalid_watchdog_value_precedence', moved['send_peer_no_auto_return_invalid_watchdog_value_precedence']),
        ('send_peer_no_auto_return_skips_default_watchdog', moved['send_peer_no_auto_return_skips_default_watchdog']),
        ('send_peer_auto_return_default_watchdog_still_attaches', moved['send_peer_auto_return_default_watchdog_still_attaches']),
        ('send_peer_no_auto_return_broadcast_watchdog_rejected', moved['send_peer_no_auto_return_broadcast_watchdog_rejected']),
        ('send_peer_watchdog_inf_with_notice_reports_finite_error_first', moved['send_peer_watchdog_inf_with_notice_reports_finite_error_first']),
        ('send_peer_watchdog_zero_with_notice_still_rejects_request_only', moved['send_peer_watchdog_zero_with_notice_still_rejects_request_only']),
        ('send_peer_watchdog_abc_argparse_error', moved['send_peer_watchdog_abc_argparse_error']),
        ('extend_wait_zero_negative_nan_inf_rejected', moved['extend_wait_zero_negative_nan_inf_rejected']),
        ('extend_wait_finite_positive_calls_request_extend', moved['extend_wait_finite_positive_calls_request_extend']),
        ('extend_wait_terminal_error_texts', moved['extend_wait_terminal_error_texts']),
        ('wait_status_cli_summary_and_json', moved['wait_status_cli_summary_and_json']),
        ('wait_status_cli_unsupported_old_daemon', moved['wait_status_cli_unsupported_old_daemon']),
        ('wait_status_cli_rejects_inactive_sender', moved['wait_status_cli_rejects_inactive_sender']),
        ('aggregate_status_cli_summary_and_json', moved['aggregate_status_cli_summary_and_json']),
        ('aggregate_status_cli_unsupported_and_not_found_text', moved['aggregate_status_cli_unsupported_and_not_found_text']),
        ('aggregate_status_cli_rejects_inactive_sender', moved['aggregate_status_cli_rejects_inactive_sender']),
        ('alarm_negative_nan_inf_minus_inf_rejected', moved['alarm_negative_nan_inf_minus_inf_rejected']),
        ('alarm_zero_and_finite_positive_call_request_alarm', moved['alarm_zero_and_finite_positive_call_request_alarm']),
        ('alarm_request_failure_prints_no_success_hint', moved['alarm_request_failure_prints_no_success_hint']),
        ('alarm_request_retries_timeout_with_stable_wake_id', moved['alarm_request_retries_timeout_with_stable_wake_id']),
        ('alarm_request_retry_exhaustion_is_bounded', moved['alarm_request_retry_exhaustion_is_bounded']),
        ('alarm_request_retries_lock_wait_with_stable_wake_id', moved['alarm_request_retries_lock_wait_with_stable_wake_id']),
        ('alarm_request_lock_wait_retry_exhaustion_is_bounded', moved['alarm_request_lock_wait_retry_exhaustion_is_bounded']),
        ('alarm_request_non_retryable_daemon_error_is_immediate', moved['alarm_request_non_retryable_daemon_error_is_immediate']),
        ('alarm_request_rejects_mismatched_daemon_wake_id', moved['alarm_request_rejects_mismatched_daemon_wake_id']),
        ('resolve_default_watchdog_seconds_env_table', moved['resolve_default_watchdog_seconds_env_table']),
        ('daemon_socket_alarm_op_rejects_non_finite', moved['daemon_socket_alarm_op_rejects_non_finite']),
        ('daemon_socket_alarm_op_idempotent_wake_id', moved['daemon_socket_alarm_op_idempotent_wake_id']),
        ('daemon_socket_alarm_op_rejects_invalid_conflicting_wake_id', moved['daemon_socket_alarm_op_rejects_invalid_conflicting_wake_id']),
        ('daemon_socket_alarm_op_idempotent_after_cancelled_or_fired', moved['daemon_socket_alarm_op_idempotent_after_cancelled_or_fired']),
        ('daemon_socket_extend_watchdog_op_rejects_non_finite', moved['daemon_socket_extend_watchdog_op_rejects_non_finite']),
        ('daemon_upsert_message_watchdog_rejects_non_finite', moved['daemon_upsert_message_watchdog_rejects_non_finite']),
        ('daemon_enqueue_rejects_no_auto_return_watchdog_atomically', moved['daemon_enqueue_rejects_no_auto_return_watchdog_atomically']),
        ('daemon_enqueue_no_auto_return_watchdog_zero_normalized', moved['daemon_enqueue_no_auto_return_watchdog_zero_normalized']),
        ('daemon_prior_hint_pending_cancel', moved['daemon_prior_hint_pending_cancel']),
        ('daemon_prior_hint_delivered_interrupt', moved['daemon_prior_hint_delivered_interrupt']),
        ('daemon_prior_hint_inflight_pane_mode_cancel', moved['daemon_prior_hint_inflight_pane_mode_cancel']),
        ('daemon_prior_hint_inflight_after_enter_interrupt', moved['daemon_prior_hint_inflight_after_enter_interrupt']),
        ('daemon_prior_hint_terminal_or_absent_no_hint', moved['daemon_prior_hint_terminal_or_absent_no_hint']),
        ('daemon_prior_hint_rejections_have_no_hints', moved['daemon_prior_hint_rejections_have_no_hints']),
        ('daemon_prior_hint_aggregate_suffix', moved['daemon_prior_hint_aggregate_suffix']),
        ('daemon_prior_hint_notice_safe_wording', moved['daemon_prior_hint_notice_safe_wording']),
        ('daemon_prior_hint_same_pair_only', moved['daemon_prior_hint_same_pair_only']),
        ('daemon_prior_hint_multi_target_partial', moved['daemon_prior_hint_multi_target_partial']),
        ('daemon_prior_hint_current_prompt_only', moved['daemon_prior_hint_current_prompt_only']),
        ('daemon_prior_hint_active_ctx_beats_pending_queue_row', moved['daemon_prior_hint_active_ctx_beats_pending_queue_row']),
        ('prior_hint_classifier_drift_invariant', moved['prior_hint_classifier_drift_invariant']),
        ('daemon_upsert_no_auto_return_watchdog_rejected', moved['daemon_upsert_no_auto_return_watchdog_rejected']),
        ('daemon_register_alarm_rejects_non_finite_and_negative', moved['daemon_register_alarm_rejects_non_finite_and_negative']),
        ('daemon_mark_message_delivered_ignores_non_finite_watchdog', moved['daemon_mark_message_delivered_ignores_non_finite_watchdog']),
        ('daemon_no_auto_return_watchdog_arm_guard_strips', moved['daemon_no_auto_return_watchdog_arm_guard_strips']),
        ('daemon_no_auto_return_watchdog_ingress_sanitizers', moved['daemon_no_auto_return_watchdog_ingress_sanitizers']),
        ('daemon_socket_ack_message_unsupported_does_not_delete_queue', moved['daemon_socket_ack_message_unsupported_does_not_delete_queue']),
        ('daemon_ack_message_helper_removed', moved['daemon_ack_message_helper_removed']),
        ('stale_watchdog_skipped', moved['stale_watchdog_skipped']),
        ('alarm_fire_text_includes_rearm_hint', moved['alarm_fire_text_includes_rearm_hint']),
        ('watchdog_pending_text_omits_held_interrupt', moved['watchdog_pending_text_omits_held_interrupt']),
        ('pane_mode_pending_defers_without_attempt', moved['pane_mode_pending_defers_without_attempt']),
        ('pane_mode_clears_then_delivers', moved['pane_mode_clears_then_delivers']),
        ('pane_mode_force_cancel_after_grace', moved['pane_mode_force_cancel_after_grace']),
        ('pane_mode_nonforce_mode_stays_pending', moved['pane_mode_nonforce_mode_stays_pending']),
        ('pane_mode_busy_target_does_not_start_timer', moved['pane_mode_busy_target_does_not_start_timer']),
        ('retry_enter_skips_pane_mode', moved['retry_enter_skips_pane_mode']),
        ('pane_mode_probe_failure_defers_pending', moved['pane_mode_probe_failure_defers_pending']),
        ('pane_mode_force_cancel_failure_stays_pending', moved['pane_mode_force_cancel_failure_stays_pending']),
        ('enter_deferred_survives_stale_requeue_and_restart', moved['enter_deferred_survives_stale_requeue_and_restart']),
        ('pre_enter_probe_failure_defers_enter', moved['pre_enter_probe_failure_defers_enter']),
        ('pane_mode_grace_zero_disables_cancel', moved['pane_mode_grace_zero_disables_cancel']),
        ('wait_for_probe_retries_enter_with_pane_id', moved['wait_for_probe_retries_enter_with_pane_id']),
        ('wait_for_probe_no_retry_without_pane_id', moved['wait_for_probe_no_retry_without_pane_id']),
        ('detached_leave_explicit_cleanup', moved['detached_leave_explicit_cleanup']),
        ('detached_leave_no_notice_when_only_inactive_remain', moved['detached_leave_no_notice_when_only_inactive_remain']),
        ('detached_join_same_alias_same_pane', moved['detached_join_same_alias_same_pane']),
        ('detached_join_same_alias_different_pane', moved['detached_join_same_alias_different_pane']),
        ('detached_join_different_alias_same_pane_allowed', moved['detached_join_different_alias_same_pane_allowed']),
        ('active_join_same_alias_still_rejected', moved['active_join_same_alias_still_rejected']),
        ('join_blank_status_same_pane_still_rejected', moved['join_blank_status_same_pane_still_rejected']),
        ('join_other_session_pane_lock_still_rejected', moved['join_other_session_pane_lock_still_rejected']),
        ('join_probe_passes_pane_id_to_wait', moved['join_probe_passes_pane_id_to_wait']),
        ('bridge_attach_start_daemon_argv_includes_from_start', moved['bridge_attach_start_daemon_argv_includes_from_start']),
        ('bridge_daemon_ctl_start_subparser_accepts_from_start', moved['bridge_daemon_ctl_start_subparser_accepts_from_start']),
        ('daemon_command_forwards_from_start', moved['daemon_command_forwards_from_start']),
        ('daemon_command_ignores_legacy_max_hops', moved['daemon_command_ignores_legacy_max_hops']),
        ('bridge_daemon_ctl_subcommands_reject_max_hops', moved['bridge_daemon_ctl_subcommands_reject_max_hops']),
        ('bridge_daemon_rejects_max_hops_cli', moved['bridge_daemon_rejects_max_hops_cli']),
        ('bridge_daemon_ctl_start_argv_includes_from_start_end_to_end', moved['bridge_daemon_ctl_start_argv_includes_from_start_end_to_end']),
        ('daemon_follow_from_start_replays_prompt_submitted', moved['daemon_follow_from_start_replays_prompt_submitted']),
        ('daemon_follow_from_start_false_skips_pre_existing_record', moved['daemon_follow_from_start_false_skips_pre_existing_record']),
        ('daemon_follow_from_start_handles_self_daemon_started_safely', moved['daemon_follow_from_start_handles_self_daemon_started_safely']),
        ('orphan_nonce_in_user_prompt', moved['orphan_nonce_in_user_prompt']),
        ('prompt_intercept_request_notice_body', moved['prompt_intercept_request_notice_body']),
        ('prompt_intercept_bridge_notice_no_source_notice', moved['prompt_intercept_bridge_notice_no_source_notice']),
        ('prompt_intercept_response_guard_queue_allows', moved['prompt_intercept_response_guard_queue_allows']),
        ('prompt_intercept_mixed_inflight_requeues', moved['prompt_intercept_mixed_inflight_requeues']),
        ('prompt_submitted_duplicate_noop', moved['prompt_submitted_duplicate_noop']),
        ('prompt_submitted_duplicate_without_nonce_noop', moved['prompt_submitted_duplicate_without_nonce_noop']),
        ('prompt_intercept_aggregate_completes', moved['prompt_intercept_aggregate_completes']),
        ('prompt_intercept_held_drain_noop', moved['prompt_intercept_held_drain_noop']),
        ('held_drain_stale_stop_preserves_new_ctx', moved['held_drain_stale_stop_preserves_new_ctx']),
        ('held_drain_missing_identity_does_not_clear_active_ctx', moved['held_drain_missing_identity_does_not_clear_active_ctx']),
        ('held_drain_matching_prior_id_without_turn_still_clears_ctx', moved['held_drain_matching_prior_id_without_turn_still_clears_ctx']),
        ('stale_hold_ignored_active_context_routes_normally', moved['stale_hold_ignored_active_context_routes_normally']),
        ('stale_hold_old_stop_with_active_ctx_applies_a4_mismatch', moved['stale_hold_old_stop_with_active_ctx_applies_a4_mismatch']),
        ('held_no_prior_id_with_active_ctx_classified_stale', moved['held_no_prior_id_with_active_ctx_classified_stale']),
        ('held_matching_prior_id_turn_id_mismatch_falls_through_to_a4', moved['held_matching_prior_id_turn_id_mismatch_falls_through_to_a4']),
        ('held_with_no_context_drain_still_works', moved['held_with_no_context_drain_still_works']),
        ('held_matching_prior_id_with_matching_turn_drain_still_works', moved['held_matching_prior_id_with_matching_turn_drain_still_works']),
        ('held_with_interrupted_tombstone_match_classify_first', moved['held_with_interrupted_tombstone_match_classify_first']),
        ('aggregate_leg_unaffected_by_held_marker', moved['aggregate_leg_unaffected_by_held_marker']),
        ('prompt_intercept_inflight_only_requeues', moved['prompt_intercept_inflight_only_requeues']),
        ('consume_once_basic', moved['consume_once_basic']),
        ('consume_once_empty_response', moved['consume_once_empty_response']),
        ('empty_response_whitespace_only_routes_sentinel', moved['empty_response_whitespace_only_routes_sentinel']),
        ('empty_response_preserves_nonempty_with_surrounding_whitespace', moved['empty_response_preserves_nonempty_with_surrounding_whitespace']),
        ('empty_response_self_return_still_skipped', moved['empty_response_self_return_still_skipped']),
        ('empty_response_auto_return_false_still_skipped', moved['empty_response_auto_return_false_still_skipped']),
        ('empty_response_inactive_requester_still_skipped', moved['empty_response_inactive_requester_still_skipped']),
        ('empty_response_unicode_whitespace_routes_sentinel', moved['empty_response_unicode_whitespace_routes_sentinel']),
        ('nonce_mismatch_fail_closed', moved['nonce_mismatch_fail_closed']),
        ('no_observed_nonce_with_candidate_fail_closed', moved['no_observed_nonce_with_candidate_fail_closed']),
        ('daemon_restart_queue_scan', moved['daemon_restart_queue_scan']),
        ('daemon_reload_inflight_idempotent', moved['daemon_reload_inflight_idempotent']),
        ('ambiguous_inflight_fail_closed', moved['ambiguous_inflight_fail_closed']),
        ('stale_reserved_orphan_swept', moved['stale_reserved_orphan_swept']),
        ('held_drain_skips_consume_once', moved['held_drain_skips_consume_once']),
        ('matching_nonce_contaminated_body_residual', moved['matching_nonce_contaminated_body_residual']),
        ('aggregate_consume_once_no_overwrite', moved['aggregate_consume_once_no_overwrite']),
        ('empty_response_in_aggregate_path_unchanged', moved['empty_response_in_aggregate_path_unchanged']),
        ('nonce_mismatch_stops_enter_retry', moved['nonce_mismatch_stops_enter_retry']),
        ('nonce_missing_stops_enter_retry', moved['nonce_missing_stops_enter_retry']),
        ('hook_logger_anchored_regex', moved['hook_logger_anchored_regex']),
        ('turn_id_mismatch_preserves_ctx', moved['turn_id_mismatch_preserves_ctx']),
        ('turn_id_mismatch_annotation_is_idempotent', moved['turn_id_mismatch_annotation_is_idempotent']),
        ('turn_id_mismatch_matching_stop_before_expiry_cleans_normally', moved['turn_id_mismatch_matching_stop_before_expiry_cleans_normally']),
        ('turn_id_mismatch_expiry_unblocks_target', moved['turn_id_mismatch_expiry_unblocks_target']),
        ('turn_id_mismatch_expiry_waits_for_watchdog_deadline', moved['turn_id_mismatch_expiry_waits_for_watchdog_deadline']),
        ('turn_id_mismatch_watchdog_fire_preserves_post_grace', moved['turn_id_mismatch_watchdog_fire_preserves_post_grace']),
        ('turn_id_mismatch_aggregate_watchdog_fire_preserves_post_grace', moved['turn_id_mismatch_aggregate_watchdog_fire_preserves_post_grace']),
        ('turn_id_mismatch_post_watchdog_grace_zero_allows_same_tick_expiry', moved['turn_id_mismatch_post_watchdog_grace_zero_allows_same_tick_expiry']),
        ('turn_id_mismatch_extend_wait_before_fire_defers_without_stamp', moved['turn_id_mismatch_extend_wait_before_fire_defers_without_stamp']),
        ('turn_id_mismatch_expiry_does_not_remove_non_delivered_queue_rows', moved['turn_id_mismatch_expiry_does_not_remove_non_delivered_queue_rows']),
        ('turn_id_mismatch_aggregate_leg_expiry_no_synthetic_reply', moved['turn_id_mismatch_aggregate_leg_expiry_no_synthetic_reply']),
        ('turn_id_mismatch_sweep_no_op_without_annotation', moved['turn_id_mismatch_sweep_no_op_without_annotation']),
        ('turn_id_mismatch_sweep_no_op_after_normal_pop', moved['turn_id_mismatch_sweep_no_op_after_normal_pop']),
        ('turn_id_mismatch_notice_ctx_follows_same_expiry', moved['turn_id_mismatch_notice_ctx_follows_same_expiry']),
        ('short_id_format', moved['short_id_format']),
        ('resolve_targets_single', moved['resolve_targets_single']),
        ('resolve_targets_multi_basic', moved['resolve_targets_multi_basic']),
        ('resolve_targets_order_preserved', moved['resolve_targets_order_preserved']),
        ('resolve_targets_dedup', moved['resolve_targets_dedup']),
        ('resolve_targets_strip_empties', moved['resolve_targets_strip_empties']),
        ('resolve_targets_reserved_alone', moved['resolve_targets_reserved_alone']),
        ('resolve_targets_reserved_mix_rejected', moved['resolve_targets_reserved_mix_rejected']),
        ('resolve_targets_unknown_rejected', moved['resolve_targets_unknown_rejected']),
        ('resolve_targets_sender_in_list_rejected', moved['resolve_targets_sender_in_list_rejected']),
        ('resolve_targets_empty_after_strip_rejected', moved['resolve_targets_empty_after_strip_rejected']),
        ('aggregate_trigger_request_multi', moved['aggregate_trigger_request_multi']),
        ('aggregate_trigger_single_no', moved['aggregate_trigger_single_no']),
        ('aggregate_trigger_notice_no', moved['aggregate_trigger_notice_no']),
        ('aggregate_trigger_bridge_sender_no', moved['aggregate_trigger_bridge_sender_no']),
        ('aggregate_trigger_no_auto_return_no', moved['aggregate_trigger_no_auto_return_no']),
        ('enqueue_aggregate_metadata_modes', moved['enqueue_aggregate_metadata_modes']),
        ('prune_keeps_recent_n', moved['prune_keeps_recent_n']),
        ('prune_disabled_retention_zero', moved['prune_disabled_retention_zero']),
        ('prune_below_retention', moved['prune_below_retention']),
        ('prune_missing_forgotten_dir_safe', moved['prune_missing_forgotten_dir_safe']),
        ('resolve_forgotten_retention_invalid_env', moved['resolve_forgotten_retention_invalid_env']),
        ('queue_status_counts', moved['queue_status_counts']),
        ('queue_status_counts_missing_file', moved['queue_status_counts_missing_file']),
        ('uninstall_helper_print_paths', moved['uninstall_helper_print_paths']),
        ('uninstall_helper_refuses_dangerous_path', moved['uninstall_helper_refuses_dangerous_path']),
        ('uninstall_sh_dry_run_invokes_hook_helper_with_dry_run', moved['uninstall_sh_dry_run_invokes_hook_helper_with_dry_run']),
        ('uninstall_sh_non_dry_run_removes_hook_entries', moved['uninstall_sh_non_dry_run_removes_hook_entries']),
        ('uninstall_sh_keep_hooks_skips_helper_under_dry_run', moved['uninstall_sh_keep_hooks_skips_helper_under_dry_run']),
        ('uninstall_sh_hook_helper_failure_aborts', moved['uninstall_sh_hook_helper_failure_aborts']),
        ('direct_exec_targets_executable', moved['direct_exec_targets_executable']),
        ('healthcheck_executable_helper_distinguishes_states', moved['healthcheck_executable_helper_distinguishes_states']),
        ('python_env_override_removed_from_tracked_files', moved['python_env_override_removed_from_tracked_files']),
        ('install_sh_python_version_gate', moved['install_sh_python_version_gate']),
        ('bridge_healthcheck_sh_python_version_gate', moved['bridge_healthcheck_sh_python_version_gate']),
        ('install_sh_chmods_target_or_fails', moved['install_sh_chmods_target_or_fails']),
        ('install_sh_default_updates_shell_rc', moved['install_sh_default_updates_shell_rc']),
        ('install_sh_shell_rc_dry_run_and_opt_out', moved['install_sh_shell_rc_dry_run_and_opt_out']),
        ('install_sh_shell_rc_target_selection', moved['install_sh_shell_rc_target_selection']),
        ('install_sh_shell_rc_backup_and_path_short_circuit', moved['install_sh_shell_rc_backup_and_path_short_circuit']),
        ('install_sh_shell_quotes_bin_dir_metacharacters', moved['install_sh_shell_quotes_bin_dir_metacharacters']),
        ('install_sh_shell_rc_replaces_existing_marker_block', moved['install_sh_shell_rc_replaces_existing_marker_block']),
        ('install_sh_claude_editor_mode_missing_skips', moved['install_sh_claude_editor_mode_missing_skips']),
        ('install_sh_claude_editor_mode_updates_with_backup', moved['install_sh_claude_editor_mode_updates_with_backup']),
        ('install_sh_claude_editor_mode_updates_global_and_settings', moved['install_sh_claude_editor_mode_updates_global_and_settings']),
        ('install_sh_claude_editor_mode_missing_global_updates_settings', moved['install_sh_claude_editor_mode_missing_global_updates_settings']),
        ('install_sh_claude_editor_mode_invalid_global_still_updates_settings', moved['install_sh_claude_editor_mode_invalid_global_still_updates_settings']),
        ('install_sh_claude_editor_mode_normal_noop', moved['install_sh_claude_editor_mode_normal_noop']),
        ('install_sh_claude_editor_mode_absent_adds_root', moved['install_sh_claude_editor_mode_absent_adds_root']),
        ('install_sh_claude_editor_mode_invalid_json_skips', moved['install_sh_claude_editor_mode_invalid_json_skips']),
        ('install_sh_claude_editor_mode_invalid_utf8_skips', moved['install_sh_claude_editor_mode_invalid_utf8_skips']),
        ('install_sh_claude_editor_mode_bom_skips', moved['install_sh_claude_editor_mode_bom_skips']),
        ('install_sh_claude_editor_mode_non_object_skips', moved['install_sh_claude_editor_mode_non_object_skips']),
        ('install_sh_claude_editor_mode_dry_run_no_write', moved['install_sh_claude_editor_mode_dry_run_no_write']),
        ('install_sh_claude_editor_mode_preserves_mode', moved['install_sh_claude_editor_mode_preserves_mode']),
        ('install_sh_claude_editor_mode_symlink_preserved', moved['install_sh_claude_editor_mode_symlink_preserved']),
        ('install_sh_claude_editor_mode_nested_root_absent_warns_skips', moved['install_sh_claude_editor_mode_nested_root_absent_warns_skips']),
        ('install_sh_claude_editor_mode_nested_with_root_warns_updates_root_only', moved['install_sh_claude_editor_mode_nested_with_root_warns_updates_root_only']),
        ('install_sh_claude_editor_mode_skip_flag_no_write', moved['install_sh_claude_editor_mode_skip_flag_no_write']),
        ('install_sh_claude_editor_mode_broken_symlink_warns', moved['install_sh_claude_editor_mode_broken_symlink_warns']),
        ('install_sh_claude_editor_mode_concurrent_change_aborts', moved['install_sh_claude_editor_mode_concurrent_change_aborts']),
        ('install_sh_hook_failure_hard_fails', moved['install_sh_hook_failure_hard_fails']),
        ('install_sh_hook_failure_ignore_flag_allows_success', moved['install_sh_hook_failure_ignore_flag_allows_success']),
        ('install_sh_hook_dry_run_failure_hard_fails', moved['install_sh_hook_dry_run_failure_hard_fails']),
        ('install_sh_hook_success_succeeds', moved['install_sh_hook_success_succeeds']),
        ('install_sh_shim_failure_still_hard_fails_under_override', moved['install_sh_shim_failure_still_hard_fails_under_override']),
        ('install_sh_skip_hooks_with_ignore_flag_is_noop', moved['install_sh_skip_hooks_with_ignore_flag_is_noop']),
        ('bridge_install_hooks_rejects_malformed_json_without_overwrite', moved['bridge_install_hooks_rejects_malformed_json_without_overwrite']),
        ('bridge_install_hooks_rejects_non_object_json_without_overwrite', moved['bridge_install_hooks_rejects_non_object_json_without_overwrite']),
        ('bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite', moved['bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite']),
        ('bridge_install_hooks_missing_json_still_creates', moved['bridge_install_hooks_missing_json_still_creates']),
        ('bridge_install_hooks_invalid_utf8_fails_without_overwrite', moved['bridge_install_hooks_invalid_utf8_fails_without_overwrite']),
        ('bridge_install_hooks_existing_valid_json_object_merges_correctly', moved['bridge_install_hooks_existing_valid_json_object_merges_correctly']),
        ('bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite', moved['bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite']),
        ('bridge_install_hooks_codex_config_ignores_nested_codex_hooks', moved['bridge_install_hooks_codex_config_ignores_nested_codex_hooks']),
        ('bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst', moved['bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst']),
        ('bridge_install_hooks_codex_config_updates_only_scoped_keys', moved['bridge_install_hooks_codex_config_updates_only_scoped_keys']),
        ('bridge_install_hooks_codex_config_inserts_features_key_before_next_table', moved['bridge_install_hooks_codex_config_inserts_features_key_before_next_table']),
        ('bridge_install_hooks_codex_config_dry_run_no_write', moved['bridge_install_hooks_codex_config_dry_run_no_write']),
        ('bridge_install_hooks_codex_config_empty_features_section_inserts', moved['bridge_install_hooks_codex_config_empty_features_section_inserts']),
        ('bridge_install_hooks_codex_config_first_table_is_array_table', moved['bridge_install_hooks_codex_config_first_table_is_array_table']),
        ('bridge_install_hooks_codex_config_table_header_with_trailing_comment', moved['bridge_install_hooks_codex_config_table_header_with_trailing_comment']),
        ('bridge_install_hooks_codex_config_commented_out_assignments_ignored', moved['bridge_install_hooks_codex_config_commented_out_assignments_ignored']),
        ('bridge_install_hooks_codex_config_no_trailing_newline_handled', moved['bridge_install_hooks_codex_config_no_trailing_newline_handled']),
        ('bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged', moved['bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged']),
        ('codex_config_marker_records_and_uninstall_restores_updated_values', moved['codex_config_marker_records_and_uninstall_restores_updated_values']),
        ('codex_config_marker_records_inserted_keys_and_uninstall_removes_them', moved['codex_config_marker_records_inserted_keys_and_uninstall_removes_them']),
        ('codex_config_missing_created_by_install_removed_on_uninstall_when_bridge_only', moved['codex_config_missing_created_by_install_removed_on_uninstall_when_bridge_only']),
        ('codex_config_already_enabled_keys_create_no_marker', moved['codex_config_already_enabled_keys_create_no_marker']),
        ('codex_config_reinstall_preserves_original_marker', moved['codex_config_reinstall_preserves_original_marker']),
        ('codex_config_user_changed_after_install_is_not_clobbered', moved['codex_config_user_changed_after_install_is_not_clobbered']),
        ('codex_config_dry_run_install_writes_no_marker_or_config', moved['codex_config_dry_run_install_writes_no_marker_or_config']),
        ('codex_config_dry_run_uninstall_with_marker_writes_no_config_or_marker', moved['codex_config_dry_run_uninstall_with_marker_writes_no_config_or_marker']),
        ('codex_config_skip_codex_does_not_touch_config_or_marker', moved['codex_config_skip_codex_does_not_touch_config_or_marker']),
        ('codex_config_uninstall_with_malformed_marker_aborts_clean', moved['codex_config_uninstall_with_malformed_marker_aborts_clean']),
        ('codex_config_uninstall_with_unknown_marker_version_aborts', moved['codex_config_uninstall_with_unknown_marker_version_aborts']),
        ('codex_config_uninstall_with_marker_path_mismatch_aborts', moved['codex_config_uninstall_with_marker_path_mismatch_aborts']),
        ('codex_config_uninstall_with_unknown_flag_key_aborts', moved['codex_config_uninstall_with_unknown_flag_key_aborts']),
        ('codex_config_uninstall_with_invalid_operation_aborts', moved['codex_config_uninstall_with_invalid_operation_aborts']),
        ('codex_config_uninstall_with_updated_missing_original_line_aborts', moved['codex_config_uninstall_with_updated_missing_original_line_aborts']),
        ('codex_config_marker_write_failure_aborts_before_config_write', moved['codex_config_marker_write_failure_aborts_before_config_write']),
        ('codex_config_inserted_features_section_with_user_added_keys_keeps_section', moved['codex_config_inserted_features_section_with_user_added_keys_keeps_section']),
        ('codex_config_existing_empty_config_records_existed_true_and_uninstall_keeps_it', moved['codex_config_existing_empty_config_records_existed_true_and_uninstall_keeps_it']),
        ('codex_config_marker_normalized_path_accepts_equivalent_path', moved['codex_config_marker_normalized_path_accepts_equivalent_path']),
        ('restart_dry_run_no_side_effect', moved['restart_dry_run_no_side_effect']),
        ('recover_orphan_delivered', moved['recover_orphan_delivered']),
        ('recover_orphan_delivered_aggregate_member', moved['recover_orphan_delivered_aggregate_member']),
        ('prune_concurrent_stat_safe', moved['prune_concurrent_stat_safe']),
        ('format_peer_list_model_safe_default', moved['format_peer_list_model_safe_default']),
        ('format_peer_list_full_includes_operator_fields', moved['format_peer_list_full_includes_operator_fields']),
        ('bridge_manage_summary_concise', moved['bridge_manage_summary_concise']),
        ('bridge_manage_summary_defaults', moved['bridge_manage_summary_defaults']),
        ('bridge_manage_summary_legacy_state_fallback', moved['bridge_manage_summary_legacy_state_fallback']),
        ('bridge_manage_summary_missing_session_exits', moved['bridge_manage_summary_missing_session_exits']),
        ('model_safe_participants_strips_endpoints', moved['model_safe_participants_strips_endpoints']),
        ('model_safe_participants_uses_active_only', moved['model_safe_participants_uses_active_only']),
        ('list_peers_json_daemon_status_strips_pid', moved['list_peers_json_daemon_status_strips_pid']),
        ('view_peer_rejects_snapshot_without_snapshot_mode', moved['view_peer_rejects_snapshot_without_snapshot_mode']),
        ('view_peer_rejects_page_with_since_last_or_search', moved['view_peer_rejects_page_with_since_last_or_search']),
        ('view_peer_allows_page_in_live_onboard_older_and_snapshot_in_older_search', moved['view_peer_allows_page_in_live_onboard_older_and_snapshot_in_older_search']),
        ('view_peer_rejects_empty_search_before_session_lookup', moved['view_peer_rejects_empty_search_before_session_lookup']),
        ('view_peer_empty_search_counts_as_search_for_mode_errors', moved['view_peer_empty_search_counts_as_search_for_mode_errors']),
        ('view_peer_nonempty_search_still_reaches_validation', moved['view_peer_nonempty_search_still_reaches_validation']),
        ('view_peer_search_with_page_after_a19_reports_page_error', moved['view_peer_search_with_page_after_a19_reports_page_error']),
        ('view_peer_doc_surfaces_disclose_search_semantics', moved['view_peer_doc_surfaces_disclose_search_semantics']),
        ('interrupt_peer_doc_surfaces_disclose_no_op_race', moved['interrupt_peer_doc_surfaces_disclose_no_op_race']),
        ('interrupt_peer_doc_surfaces_no_queued_active_directive', moved['interrupt_peer_doc_surfaces_no_queued_active_directive']),
        ('prompt_intercepted_doc_surfaces_disclose_user_typing_collision', moved['prompt_intercepted_doc_surfaces_disclose_user_typing_collision']),
        ('response_send_guard_doc_surfaces_are_precise', moved['response_send_guard_doc_surfaces_are_precise']),
        ('probe_prompt_is_compact_quickstart', moved['probe_prompt_is_compact_quickstart']),
        ('send_peer_wait_doc_surfaces_name_blocking_consequence', moved['send_peer_wait_doc_surfaces_name_blocking_consequence']),
        ('watchdog_phase_doc_surfaces_are_consistent', moved['watchdog_phase_doc_surfaces_are_consistent']),
        ('wait_status_doc_surfaces_anti_polling', moved['wait_status_doc_surfaces_anti_polling']),
        ('aggregate_status_doc_surfaces_leg_level_and_anti_polling', moved['aggregate_status_doc_surfaces_leg_level_and_anti_polling']),
        ('view_peer_render_output_model_safe', moved['view_peer_render_output_model_safe']),
        ('view_peer_search_explicit_snapshot_uses_safe_ref', moved['view_peer_search_explicit_snapshot_uses_safe_ref']),
        ('view_peer_snapshot_ref_collision_unique', moved['view_peer_snapshot_ref_collision_unique']),
        ('view_peer_capture_errors_sanitized', moved['view_peer_capture_errors_sanitized']),
        ('view_peer_snapshot_not_found_hides_full_id', moved['view_peer_snapshot_not_found_hides_full_id']),
        ('view_peer_since_last_matches_changed_volatile_chrome', moved['view_peer_since_last_matches_changed_volatile_chrome']),
        ('view_peer_since_last_legacy_tail_derives_stable_anchor', moved['view_peer_since_last_legacy_tail_derives_stable_anchor']),
        ('view_peer_since_last_ambiguous_current_anchor_skips_to_unique', moved['view_peer_since_last_ambiguous_current_anchor_skips_to_unique']),
        ('view_peer_since_last_matches_anchor_before_long_delta', moved['view_peer_since_last_matches_anchor_before_long_delta']),
        ('view_peer_since_last_build_rejects_duplicate_previous_window', moved['view_peer_since_last_build_rejects_duplicate_previous_window']),
        ('view_peer_since_last_uncertain_does_not_advance_cursor', moved['view_peer_since_last_uncertain_does_not_advance_cursor']),
        ('view_peer_since_last_upgrade_reset_when_no_legacy_anchor', moved['view_peer_since_last_upgrade_reset_when_no_legacy_anchor']),
        ('view_peer_since_last_low_info_lines_do_not_anchor', moved['view_peer_since_last_low_info_lines_do_not_anchor']),
        ('view_peer_since_last_claude_status_variants_are_volatile', moved['view_peer_since_last_claude_status_variants_are_volatile']),
        ('view_peer_since_last_status_classifier_preserves_prose', moved['view_peer_since_last_status_classifier_preserves_prose']),
        ('view_peer_since_last_claude_status_lines_do_not_anchor', moved['view_peer_since_last_claude_status_lines_do_not_anchor']),
        ('view_peer_since_last_filters_stored_volatile_anchor_lines', moved['view_peer_since_last_filters_stored_volatile_anchor_lines']),
        ('view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality', moved['view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality']),
        ('view_peer_since_last_volatile_only_claude_status_delta', moved['view_peer_since_last_volatile_only_claude_status_delta']),
        ('view_peer_since_last_codex_status_variants_preserved', moved['view_peer_since_last_codex_status_variants_preserved']),
        ('view_peer_since_last_volatile_only_delta', moved['view_peer_since_last_volatile_only_delta']),
        ('view_peer_since_last_short_delta_consumed_once', moved['view_peer_since_last_short_delta_consumed_once']),
        ('view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update', moved['view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update']),
        ('view_peer_since_last_consumed_tail_does_not_hide_new_duplicate', moved['view_peer_since_last_consumed_tail_does_not_hide_new_duplicate']),
        ('view_peer_since_last_consumed_tail_anchor_change_resets', moved['view_peer_since_last_consumed_tail_anchor_change_resets']),
        ('view_peer_since_last_consumed_tail_mismatch_clears', moved['view_peer_since_last_consumed_tail_mismatch_clears']),
        ('view_peer_since_last_consumed_tail_cap', moved['view_peer_since_last_consumed_tail_cap']),
        ('view_peer_since_last_consumed_tail_ignores_volatile_churn', moved['view_peer_since_last_consumed_tail_ignores_volatile_churn']),
        ('view_peer_since_last_codex_prompt_placeholder_not_anchor', moved['view_peer_since_last_codex_prompt_placeholder_not_anchor']),
        ('view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor', moved['view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor']),
        ('view_peer_since_last_preserves_codex_bridge_prompt_lines', moved['view_peer_since_last_preserves_codex_bridge_prompt_lines']),
        ('view_peer_since_last_preserves_trailing_semantic_codex_arrow', moved['view_peer_since_last_preserves_trailing_semantic_codex_arrow']),
        ('view_peer_since_last_preserves_semantic_codex_arrow_before_footer', moved['view_peer_since_last_preserves_semantic_codex_arrow_before_footer']),
        ('view_peer_since_last_claude_partial_status_fragments_are_volatile', moved['view_peer_since_last_claude_partial_status_fragments_are_volatile']),
        ('view_peer_since_last_partial_status_preserves_prose', moved['view_peer_since_last_partial_status_preserves_prose']),
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
        ('view_peer_unverified_endpoint_uses_daemon_not_local_capture', moved['view_peer_unverified_endpoint_uses_daemon_not_local_capture']),
        ('daemon_startup_backfill_summary_logs_repair_hint', scenario_daemon_startup_backfill_summary_logs_repair_hint),
        ('peer_body_size_helper_boundaries', moved['peer_body_size_helper_boundaries']),
        ('send_peer_rejects_oversized_body_before_subprocess', moved['send_peer_rejects_oversized_body_before_subprocess']),
        ('send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim', moved['send_peer_watchdog_notice_inf_reports_finite_error_first_at_shim']),
        ('send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim', moved['send_peer_watchdog_notice_zero_still_rejects_request_only_at_shim']),
        ('send_peer_watchdog_bare_minus_inf_argparse_error_at_shim', moved['send_peer_watchdog_bare_minus_inf_argparse_error_at_shim']),
        ('send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim', moved['send_peer_watchdog_equals_minus_inf_reports_finite_error_at_shim']),
        ('send_peer_watchdog_finite_value_forwarded_with_equals', moved['send_peer_watchdog_finite_value_forwarded_with_equals']),
        ('send_peer_body_input_diagnostics_are_specific', moved['send_peer_body_input_diagnostics_are_specific']),
        ('send_peer_rejects_split_inline_body', moved['send_peer_rejects_split_inline_body']),
        ('send_peer_rejects_implicit_split_inline_body', moved['send_peer_rejects_implicit_split_inline_body']),
        ('send_peer_accepts_option_after_destination_with_stdin', moved['send_peer_accepts_option_after_destination_with_stdin']),
        ('send_peer_accepts_option_after_implicit_target_with_stdin', moved['send_peer_accepts_option_after_implicit_target_with_stdin']),
        ('send_peer_accepts_options_after_destination_before_inline_body', moved['send_peer_accepts_options_after_destination_before_inline_body']),
        ('send_peer_rejects_option_after_inline_body', moved['send_peer_rejects_option_after_inline_body']),
        ('send_peer_rejects_duplicate_destination_after_destination', moved['send_peer_rejects_duplicate_destination_after_destination']),
        ('send_peer_implicit_target_option_without_body_reports_body_required', moved['send_peer_implicit_target_option_without_body_reports_body_required']),
        ('send_peer_rejects_allow_spoof_after_implicit_target', moved['send_peer_rejects_allow_spoof_after_implicit_target']),
        ('send_peer_single_inline_body_uses_stdin_handoff', moved['send_peer_single_inline_body_uses_stdin_handoff']),
        ('send_peer_request_success_prints_anti_wait_hint', moved['send_peer_request_success_prints_anti_wait_hint']),
        ('send_peer_prior_hint_precedes_success_hint', moved['send_peer_prior_hint_precedes_success_hint']),
        ('send_peer_notice_success_prints_alarm_and_anti_wait_hints', moved['send_peer_notice_success_prints_alarm_and_anti_wait_hints']),
        ('send_peer_aggregate_request_success_prints_result_hint', moved['send_peer_aggregate_request_success_prints_result_hint']),
        ('send_peer_subprocess_failure_prints_no_success_hint', moved['send_peer_subprocess_failure_prints_no_success_hint']),
        ('send_peer_ambient_socket_stdin_does_not_block', moved['send_peer_ambient_socket_stdin_does_not_block']),
        ('send_peer_inline_body_accepts_empty_non_tty_stdin', moved['send_peer_inline_body_accepts_empty_non_tty_stdin']),
        ('send_peer_explicit_stdin_multibyte_body', moved['send_peer_explicit_stdin_multibyte_body']),
        ('send_peer_implicit_target_allows_stdin', moved['send_peer_implicit_target_allows_stdin']),
        ('send_peer_implicit_target_resolves_session_from_pane', moved['send_peer_implicit_target_resolves_session_from_pane']),
        ('send_peer_implicit_target_stdin_resolves_session_from_pane', moved['send_peer_implicit_target_stdin_resolves_session_from_pane']),
        ('send_peer_precheck_fails_open_identity_errors_owned_by_validator', moved['send_peer_precheck_fails_open_identity_errors_owned_by_validator']),
        ('send_peer_allow_spoof_requires_explicit_destination_without_session', moved['send_peer_allow_spoof_requires_explicit_destination_without_session']),
        ('send_peer_rejects_allow_spoof_attached_value', moved['send_peer_rejects_allow_spoof_attached_value']),
        ('send_peer_all_rejects_leading_alias_body', moved['send_peer_all_rejects_leading_alias_body']),
        ('send_peer_rejects_stdin_with_positional_body', moved['send_peer_rejects_stdin_with_positional_body']),
        ('send_peer_rejects_pipe_with_positional_body', moved['send_peer_rejects_pipe_with_positional_body']),
        ('send_peer_pipe_only_body_still_supported', moved['send_peer_pipe_only_body_still_supported']),
        ('send_peer_precheck_option_table_matches_parser', moved['send_peer_precheck_option_table_matches_parser']),
        ('enqueue_rejects_body_and_stdin_before_session_lookup', moved['enqueue_rejects_body_and_stdin_before_session_lookup']),
        ('enqueue_rejects_empty_body_and_stdin', moved['enqueue_rejects_empty_body_and_stdin']),
        ('enqueue_rejects_body_and_stdin_argv_order_independent', moved['enqueue_rejects_body_and_stdin_argv_order_independent']),
        ('enqueue_body_only_still_works', moved['enqueue_body_only_still_works']),
        ('enqueue_stdin_only_still_works', moved['enqueue_stdin_only_still_works']),
        ('enqueue_aggregate_stdout_socket_and_fallback', moved['enqueue_aggregate_stdout_socket_and_fallback']),
        ('enqueue_aggregate_stdout_all_and_negative_cases', moved['enqueue_aggregate_stdout_all_and_negative_cases']),
        ('enqueue_bare_body_without_value_remains_argparse_owned', moved['enqueue_bare_body_without_value_remains_argparse_owned']),
        ('enqueue_rejects_oversized_body_unchanged', moved['enqueue_rejects_oversized_body_unchanged']),
        ('enqueue_stdin_rejects_oversized_body_unchanged', moved['enqueue_stdin_rejects_oversized_body_unchanged']),
        ('enqueue_socket_success_hints_stderr_stdout_unchanged', moved['enqueue_socket_success_hints_stderr_stdout_unchanged']),
        ('enqueue_fallback_prior_hint_pending_cancel', moved['enqueue_fallback_prior_hint_pending_cancel']),
        ('enqueue_fallback_prior_hint_submitted_interrupt', moved['enqueue_fallback_prior_hint_submitted_interrupt']),
        ('enqueue_fallback_prior_hint_plain_inflight_omitted', moved['enqueue_fallback_prior_hint_plain_inflight_omitted']),
        ('enqueue_fallback_prior_hint_pane_mode_inflight_cancel', moved['enqueue_fallback_prior_hint_pane_mode_inflight_cancel']),
        ('enqueue_fallback_prior_hint_write_failure_suppresses_hint', moved['enqueue_fallback_prior_hint_write_failure_suppresses_hint']),
        ('enqueue_fallback_response_send_guard_no_hint', moved['enqueue_fallback_response_send_guard_no_hint']),
        ('alarm_cancel_preserves_at_limit_body', moved['alarm_cancel_preserves_at_limit_body']),
        ('prompt_body_preserves_multiline_and_sanitizes', moved['prompt_body_preserves_multiline_and_sanitizes']),
        ('build_peer_prompt_signature_drops_max_hops', moved['build_peer_prompt_signature_drops_max_hops']),
        ('tmux_paste_buffer_delivery_sequence', moved['tmux_paste_buffer_delivery_sequence']),
        ('daemon_logs_body_truncated_for_legacy_long_body', moved['daemon_logs_body_truncated_for_legacy_long_body']),
        ('peer_result_redirect_single_over_limit', moved['peer_result_redirect_single_over_limit']),
        ('peer_result_redirect_restart_queue_roundtrip', moved['peer_result_redirect_restart_queue_roundtrip']),
        ('peer_result_redirect_below_limit_inline', moved['peer_result_redirect_below_limit_inline']),
        ('peer_result_redirect_write_failure_fallback', moved['peer_result_redirect_write_failure_fallback']),
        ('peer_result_redirect_collision_and_symlink_safe', moved['peer_result_redirect_collision_and_symlink_safe']),
        ('peer_result_redirect_share_root_owner_mismatch', moved['peer_result_redirect_share_root_owner_mismatch']),
        ('peer_result_redirect_replies_permission_drift_tightened', moved['peer_result_redirect_replies_permission_drift_tightened']),
        ('peer_result_redirect_logs_reduced_preview_chars', moved['peer_result_redirect_logs_reduced_preview_chars']),
        ('peer_result_redirect_malformed_message_id_safe_filename', moved['peer_result_redirect_malformed_message_id_safe_filename']),
        ('peer_result_redirect_aggregate_unique_once', moved['peer_result_redirect_aggregate_unique_once']),
        ('response_send_guard_socket_cli_error_kind', moved['response_send_guard_socket_cli_error_kind']),
        ('response_send_guard_socket_error_kind_parse', moved['response_send_guard_socket_error_kind_parse']),
        ('enqueue_fallback_success_silent_with_raw_diagnostic', moved['enqueue_fallback_success_silent_with_raw_diagnostic']),
        ('enqueue_fallback_write_failure_preserves_stderr', moved['enqueue_fallback_write_failure_preserves_stderr']),
        ('response_send_guard_fallback_blocks_unchanged', moved['response_send_guard_fallback_blocks_unchanged']),
        ('response_send_guard_fallback_all_blocks_unchanged', moved['response_send_guard_fallback_all_blocks_unchanged']),
        ('response_send_guard_fallback_partial_blocks_unchanged', moved['response_send_guard_fallback_partial_blocks_unchanged']),
        ('response_send_guard_fallback_force_allows', moved['response_send_guard_fallback_force_allows']),
        ('response_send_guard_fallback_no_auto_return_allowed', moved['response_send_guard_fallback_no_auto_return_allowed']),
        ('response_send_guard_fallback_false_positive_resistance', moved['response_send_guard_fallback_false_positive_resistance']),
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
