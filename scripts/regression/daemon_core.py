from __future__ import annotations

import argparse
import inspect
import io
import json
import os
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

from .harness import (
    LIBEXEC,
    _active_turn,
    _assert_auto_return_result_shape,
    _auto_return_results,
    _daemon_command_result,
    _delivered_request,
    _enqueue_alarm,
    _import_daemon_ctl,
    _import_enqueue_module,
    _make_delivered_context,
    _make_inflight,
    _participants_state,
    _patch_enqueue_for_unit,
    _plant_watchdog,
    _qualifying_message,
    _queue_item,
    _run_enqueue_main,
    _set_response_context,
    _write_json,
    assert_true,
    make_daemon,
    patched_redirect_root,
    read_events,
    test_message,
)

import bridge_daemon  # noqa: E402
import bridge_hook_logger  # noqa: E402
import bridge_response_guard  # noqa: E402

from bridge_util import (
    MAX_INLINE_SEND_BODY_CHARS,
    MAX_PEER_BODY_CHARS,
    RESTART_PRESERVED_INFLIGHT_KEY,
    read_json,
    read_limited_text,
    utc_now,
    validate_peer_body_size,
)  # noqa: E402


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


SCENARIOS = [
    ('lifecycle_delivered_terminal', scenario_lifecycle),
    ('response_send_guard_socket_request_notice', scenario_response_send_guard_socket_request_notice),
    ('response_send_guard_socket_force_and_other_peer', scenario_response_send_guard_socket_force_and_other_peer),
    ('response_send_guard_socket_no_auto_return_allowed', scenario_response_send_guard_socket_no_auto_return_allowed),
    ('response_send_guard_socket_atomic_multi', scenario_response_send_guard_socket_atomic_multi),
    ('response_send_guard_socket_aggregate_and_held', scenario_response_send_guard_socket_aggregate_and_held),
    ('response_send_guard_after_response_finished_allowed', scenario_response_send_guard_after_response_finished_allowed),
    ('ingressing_not_delivered_before_finalize', scenario_ingressing_not_delivered_before_finalize),
    ('bridge_origin_fallback_ingressing_promoted', scenario_bridge_origin_fallback_ingressing_promoted),
    ('socket_normalizes_non_ingressing_status', scenario_socket_normalizes_non_ingressing_status),
    ('aggregate_fallback_finalize', scenario_aggregate_fallback_finalize),
    ('aged_ingressing_promoted_by_maintenance', scenario_aged_ingressing_promoted_by_maintenance),
    ('aged_ingressing_malformed_timestamp_promoted', scenario_aged_ingressing_malformed_timestamp_promoted),
    ('fresh_ingressing_not_promoted_by_maintenance', scenario_fresh_ingressing_not_promoted_by_maintenance),
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
    ('daemon_socket_ack_message_unsupported_does_not_delete_queue', scenario_daemon_socket_ack_message_unsupported_does_not_delete_queue),
    ('daemon_ack_message_helper_removed', scenario_daemon_ack_message_helper_removed),
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
    ('prune_keeps_recent_n', scenario_prune_keeps_recent_n),
    ('prune_disabled_retention_zero', scenario_prune_disabled_retention_zero),
    ('prune_below_retention', scenario_prune_below_retention),
    ('prune_missing_forgotten_dir_safe', scenario_prune_missing_forgotten_dir_safe),
    ('resolve_forgotten_retention_invalid_env', scenario_resolve_forgotten_retention_invalid_env),
    ('queue_status_counts', scenario_queue_status_counts),
    ('queue_status_counts_missing_file', scenario_queue_status_counts_missing_file),
    ('restart_dry_run_no_side_effect', scenario_restart_dry_run_no_side_effect),
    ('recover_orphan_delivered', scenario_recover_orphan_delivered),
    ('recover_orphan_delivered_aggregate_member', scenario_recover_orphan_delivered_aggregate_member),
    ('prune_concurrent_stat_safe', scenario_prune_concurrent_stat_safe),
    ('peer_body_size_helper_boundaries', scenario_peer_body_size_helper_boundaries),
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
    ('response_send_guard_fallback_blocks_unchanged', scenario_response_send_guard_fallback_blocks_unchanged),
    ('response_send_guard_fallback_all_blocks_unchanged', scenario_response_send_guard_fallback_all_blocks_unchanged),
    ('response_send_guard_fallback_partial_blocks_unchanged', scenario_response_send_guard_fallback_partial_blocks_unchanged),
    ('response_send_guard_fallback_force_allows', scenario_response_send_guard_fallback_force_allows),
    ('response_send_guard_fallback_no_auto_return_allowed', scenario_response_send_guard_fallback_no_auto_return_allowed),
    ('response_send_guard_fallback_false_positive_resistance', scenario_response_send_guard_fallback_false_positive_resistance),
]
