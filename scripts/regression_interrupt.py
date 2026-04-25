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
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBEXEC = ROOT / "libexec" / "agent-bridge"
sys.path.insert(0, str(LIBEXEC))

import bridge_daemon  # noqa: E402
from bridge_util import utc_now  # noqa: E402


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
        max_hops=4,
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


def scenario_held_blocks_delivery(label: str, tmpdir: Path) -> None:
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
    d.enqueue_ipc_message(new_msg)
    # try_deliver should NOT pick this up because bob is held
    d.try_deliver("codex")
    item = next((it for it in d.queue.read() if it.get("id") == "msg-test-2"), None)
    assert_true(item is not None and item.get("status") == "pending", f"{label}: held target must not consume new pending message")
    # Release hold via response_finished and verify delivery proceeds
    finish_record = {"agent": "codex", "bridge_agent": "codex", "last_assistant_message": "drained"}
    d.handle_response_finished(finish_record)
    assert_true("codex" not in d.held_interrupt, f"{label}: held_interrupt must clear on response_finished")
    d.try_deliver("codex")
    item = next((it for it in d.queue.read() if it.get("id") == "msg-test-2"), None)
    assert_true(item is not None and item.get("status") == "inflight", f"{label}: after hold release, pending should reserve to inflight (got {item.get('status') if item else 'missing'})")
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
    assert_true("w1" in d.held_interrupt, f"{label}: w1 should be held")
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


def scenario_watchdog_cancel_on_empty_response(label: str, tmpdir: Path) -> None:
    # v1.5: watchdog arms at delivery, not enqueue. The cancel-on-empty
    # invariant from v1 is preserved: even an empty/no-text response must
    # still cancel the watchdog at terminal handle_response_finished.
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
    d.handle_response_finished({"agent": "codex", "bridge_agent": "codex", "turn_id": "t-wd", "last_assistant_message": ""})
    assert_true(not any(wd.get("ref_message_id") == "msg-wd-1" for wd in d.watchdogs.values()), f"{label}: watchdog must be cancelled even when response text is empty")
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


# ---------- v1.5.2 scenarios: state-based delivery matching + consume-once ----------

def _make_inflight(d, message_id: str, frm: str, to: str, nonce: str, *, auto_return: bool = True, kind: str = "request") -> None:
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
    def add(queue):
        queue.append(msg)
        return None
    d.queue.update(add)
    d.reserved[to] = message_id


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
    """Empty terminal response also consumes ctx — subsequent Stop must not route."""
    participants = {"claude": {"alias": "claude", "pane": "%99"}, "codex": {"alias": "codex", "pane": "%98"}}
    d = make_daemon(tmpdir, participants)
    _make_inflight(d, "msg-empty-1", frm="codex", to="claude", nonce="n-empty-1")
    d.handle_prompt_submitted({"agent": "claude", "bridge_agent": "claude", "nonce": "n-empty-1", "turn_id": "t-e-1", "prompt": "[bridge:n-empty-1] x"})
    # Empty Stop: maybe_return_response will skip routing on empty, but ctx must still be consumed
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-e-1", "last_assistant_message": ""})
    assert_true(d.current_prompt_by_agent.get("claude") is None, f"{label}: empty Stop must still consume ctx")
    # Next Stop has no ctx to route through
    d.handle_response_finished({"agent": "claude", "bridge_agent": "claude", "turn_id": "t-e-2", "last_assistant_message": "later unrelated reply"})
    routed = [it for it in d.queue.read() if it.get("from") == "claude" and it.get("to") == "codex" and it.get("kind") == "result"]
    assert_true(len(routed) == 0, f"{label}: no auto-route should occur after empty Stop consumed ctx")
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


def scenario_stale_reserved_rejected(label: str, tmpdir: Path) -> None:
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
    assert_true(item is not None and item.get("status") == "delivered", f"{label}: stale reserved must not flip an already-delivered item, status={item.get('status') if item else None}")
    ctx = d.current_prompt_by_agent.get("claude") or {}
    assert_true(ctx.get("id") is None, f"{label}: ctx must not bind to stale reserved item")
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
        "prompt": "[bridge:live-nonce] from=codex kind=request causal_id=c. Reply normally; bridge returns it. Request: hello\nUSER PASTED EXTRA CONTENT HERE",
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    is for normal terminal path only — stale Stops are skipped earlier)."""
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix="bridge-regression-"))
    try:
        scenarios = [
            ("lifecycle_delivered_terminal", scenario_lifecycle),
            ("held_blocks_delivery", scenario_held_blocks_delivery),
            ("esc_fail_no_state_change", scenario_esc_fail_no_state_change),
            ("clear_hold_logs_event", scenario_clear_hold),
            ("aggregate_interrupt_synthetic_reply", scenario_aggregate_interrupt_synthetic),
            ("watchdog_cancel_on_empty_response", scenario_watchdog_cancel_on_empty_response),
            ("alarm_cancelled_by_qualifying_request", scenario_alarm_cancelled_by_qualifying_request),
            ("socket_path_alarm_cancel", scenario_socket_path_alarm_cancel),
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
            ("stale_watchdog_skipped", scenario_stale_watchdog_skipped),
            ("orphan_nonce_in_user_prompt", scenario_orphan_nonce_in_user_prompt),
            ("consume_once_basic", scenario_consume_once_basic),
            ("consume_once_empty_response", scenario_consume_once_empty_response),
            ("nonce_mismatch_fail_closed", scenario_nonce_mismatch_fail_closed),
            ("no_observed_nonce_with_candidate_fail_closed", scenario_no_observed_nonce_with_candidate_fail_closed),
            ("daemon_restart_queue_scan", scenario_daemon_restart_queue_scan),
            ("ambiguous_inflight_fail_closed", scenario_ambiguous_inflight_fail_closed),
            ("stale_reserved_rejected", scenario_stale_reserved_rejected),
            ("held_drain_skips_consume_once", scenario_held_drain_skips_consume_once),
            ("matching_nonce_contaminated_body_residual", scenario_matching_nonce_contaminated_body_documents_residual),
            ("aggregate_consume_once_no_overwrite", scenario_aggregate_consume_once_no_overwrite),
            ("nonce_mismatch_stops_enter_retry", scenario_nonce_mismatch_stops_enter_retry),
            ("nonce_missing_stops_enter_retry", scenario_nonce_missing_stops_enter_retry),
            ("hook_logger_anchored_regex", scenario_hook_logger_anchored_regex),
            ("turn_id_mismatch_preserves_ctx", scenario_turn_id_mismatch_preserves_ctx),
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
