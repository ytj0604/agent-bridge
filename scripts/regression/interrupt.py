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

from .harness import (
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


SCENARIOS = [
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
]
