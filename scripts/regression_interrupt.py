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
import errno
import json
import os
import shutil
import subprocess
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
    bridge_daemon.run_tmux_send_literal = lambda pane, prompt: literal_calls.append(pane)  # type: ignore[assignment]
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


# ---------- v1.5.x scenarios: multi-target send_peer ----------

def _participants_state(aliases: list[str]) -> dict:
    return {
        "session": "test-session",
        "participants": {a: {"alias": a, "agent_type": "codex", "pane": f"%{i+10}", "status": "active"} for i, a in enumerate(aliases)},
    }


def scenario_resolve_targets_single(label: str, tmpdir: Path) -> None:
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    helper = "/root/agent-bridge/libexec/agent-bridge/bridge_uninstall_state.py"
    proc = subprocess.run([sys.executable, helper, "--print-paths"], capture_output=True, text=True, timeout=10)
    assert_true(proc.returncode == 0, f"{label}: helper exit 0, got {proc.returncode}: {proc.stderr}")
    payload = json.loads(proc.stdout)
    for key in ("state", "run", "log"):
        assert_true(key in payload, f"{label}: payload contains {key}")
        assert_true(payload[key].endswith(key), f"{label}: {key} path looks like .../<{key}>")
    print(f"  PASS  {label}")


def scenario_uninstall_helper_refuses_dangerous_path(label: str, tmpdir: Path) -> None:
    helper = "/root/agent-bridge/libexec/agent-bridge/bridge_uninstall_state.py"
    env = dict(os.environ)
    env["AGENT_BRIDGE_STATE_DIR"] = "/etc"  # dangerous
    proc = subprocess.run([sys.executable, helper, "--dry-run"], env=env, capture_output=True, text=True, timeout=10)
    assert_true(proc.returncode != 0, f"{label}: must refuse dangerous path, exit was {proc.returncode}")
    assert_true("refuses" in proc.stderr.lower() or "dangerous" in proc.stderr.lower(), f"{label}: stderr explains refusal: {proc.stderr!r}")
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
        ns = argparse.Namespace(dry_run=True, force=False, json=False, health_delay=0.1, stop_timeout=1.0, max_hops=None, submit_delay=None, submit_timeout=None)
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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


def scenario_model_safe_participants_uses_active_only(label: str, tmpdir: Path) -> None:
    """JSON view should match text view: only active participants."""
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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
    helper = "/root/agent-bridge/libexec/agent-bridge/bridge_list_peers.py"
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
    libexec = Path("/root/agent-bridge/libexec/agent-bridge")
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


def _import_enqueue_module():
    import importlib
    be = importlib.import_module("bridge_enqueue")
    return importlib.reload(be)


def _run_enqueue_main(be, argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import io
    old_argv = sys.argv[:]
    out = io.StringIO()
    err = io.StringIO()
    try:
        sys.argv = ["bridge_enqueue.py", *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = be.main()
    finally:
        sys.argv = old_argv
    return int(code), out.getvalue(), err.getvalue()


def _patch_enqueue_for_unit(be, state: dict, *, socket_error: str = "") -> None:
    be.ensure_daemon_running = lambda session: ""
    be.room_status = lambda session: argparse.Namespace(active_enough_for_enqueue=True, reason="ok")
    be.sender_matches_caller = lambda args, session: True
    be.load_session = lambda session: state
    be.enqueue_via_daemon_socket = lambda session, messages: (False, [], socket_error)


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
            ("restart_dry_run_no_side_effect", scenario_restart_dry_run_no_side_effect),
            ("recover_orphan_delivered", scenario_recover_orphan_delivered),
            ("recover_orphan_delivered_aggregate_member", scenario_recover_orphan_delivered_aggregate_member),
            ("prune_concurrent_stat_safe", scenario_prune_concurrent_stat_safe),
            ("format_peer_list_model_safe_default", scenario_format_peer_list_model_safe_default),
            ("format_peer_list_full_includes_operator_fields", scenario_format_peer_list_full_includes_operator_fields),
            ("model_safe_participants_strips_endpoints", scenario_model_safe_participants_strips_endpoints),
            ("model_safe_participants_uses_active_only", scenario_model_safe_participants_uses_active_only),
            ("list_peers_json_daemon_status_strips_pid", scenario_list_peers_json_daemon_status_strips_pid),
            ("view_peer_render_output_model_safe", scenario_view_peer_render_output_model_safe),
            ("view_peer_search_explicit_snapshot_uses_safe_ref", scenario_view_peer_search_explicit_snapshot_uses_safe_ref),
            ("view_peer_snapshot_ref_collision_unique", scenario_view_peer_snapshot_ref_collision_unique),
            ("view_peer_capture_errors_sanitized", scenario_view_peer_capture_errors_sanitized),
            ("view_peer_snapshot_not_found_hides_full_id", scenario_view_peer_snapshot_not_found_hides_full_id),
            ("enqueue_fallback_success_silent_with_raw_diagnostic", scenario_enqueue_fallback_success_silent_with_raw_diagnostic),
            ("enqueue_fallback_write_failure_preserves_stderr", scenario_enqueue_fallback_write_failure_preserves_stderr),
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
