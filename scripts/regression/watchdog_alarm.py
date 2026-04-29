from __future__ import annotations

from contextlib import redirect_stderr
import argparse
import io
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

from .harness import (
    _assert_auto_return_result_shape,
    _auto_return_results,
    _daemon_command_result,
    _delivered_request,
    _enqueue_alarm,
    _import_enqueue_module,
    _participants_state,
    _plant_watchdog,
    _qualifying_message,
    _queue_item,
    _watchdogs_for_message,
    assert_true,
    make_daemon,
    patched_environ,
    read_events,
    test_message,
)

import bridge_daemon  # noqa: E402
import bridge_identity  # noqa: E402

from bridge_util import (
    MAX_INLINE_SEND_BODY_CHARS,
    MAX_PEER_BODY_CHARS,
    locked_json,
    utc_now,
)  # noqa: E402


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

def _add_watchdog_request(d, message_id: str, *, frm: str = "claude", to: str = "codex", aggregate_id: str = "") -> None:
    msg = test_message(message_id, frm=frm, to=to, status="pending")
    msg["watchdog_delay_sec"] = 60.0
    if aggregate_id:
        msg["aggregate_id"] = aggregate_id
        msg["aggregate_expected"] = ["codex", "bob"]
        msg["aggregate_message_ids"] = {"codex": message_id, "bob": "msg-agg-bob"}
    d.enqueue_ipc_message(msg)

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

def _import_extend_wait_module():
    import importlib
    bew = importlib.import_module("bridge_extend_wait")
    return importlib.reload(bew)

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


SCENARIOS = [
    ('watchdog_cancel_on_empty_response', scenario_watchdog_cancel_on_empty_response),
    ('alarm_cancelled_by_qualifying_request', scenario_alarm_cancelled_by_qualifying_request),
    ('socket_path_alarm_cancel', scenario_socket_path_alarm_cancel),
    ('fallback_path_alarm_cancel', scenario_fallback_path_alarm_cancel),
    ('replay_does_not_cancel_later_alarm', scenario_replay_does_not_cancel_later_alarm),
    ('aged_ingressing_does_not_cancel_alarms', scenario_aged_ingressing_does_not_cancel_alarms),
    ('alarm_not_cancelled_by_result', scenario_alarm_not_cancelled_by_result),
    ('alarm_not_cancelled_by_bridge', scenario_alarm_not_cancelled_by_bridge),
    ('user_prompt_does_not_cancel_alarm', scenario_user_prompt_does_not_cancel_alarm),
    ('extend_wait_upserts_watchdog', scenario_extend_wait_upserts_watchdog),
    ('extend_wait_aggregate_rejected', scenario_extend_wait_aggregate_rejected),
    ('extend_wait_unknown_message', scenario_extend_wait_unknown_message),
    ('extend_wait_terminal_tombstone_classification', scenario_extend_wait_terminal_tombstone_classification),
    ('extend_wait_pending_rejected', scenario_extend_wait_pending_rejected),
    ('extend_wait_not_owner', scenario_extend_wait_not_owner),
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
    ('extend_wait_zero_negative_nan_inf_rejected', scenario_extend_wait_zero_negative_nan_inf_rejected),
    ('extend_wait_finite_positive_calls_request_extend', scenario_extend_wait_finite_positive_calls_request_extend),
    ('extend_wait_terminal_error_texts', scenario_extend_wait_terminal_error_texts),
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
    ('daemon_upsert_no_auto_return_watchdog_rejected', scenario_daemon_upsert_no_auto_return_watchdog_rejected),
    ('daemon_register_alarm_rejects_non_finite_and_negative', scenario_daemon_register_alarm_rejects_non_finite_and_negative),
    ('daemon_mark_message_delivered_ignores_non_finite_watchdog', scenario_daemon_mark_message_delivered_ignores_non_finite_watchdog),
    ('daemon_no_auto_return_watchdog_arm_guard_strips', scenario_daemon_no_auto_return_watchdog_arm_guard_strips),
    ('daemon_no_auto_return_watchdog_ingress_sanitizers', scenario_daemon_no_auto_return_watchdog_ingress_sanitizers),
    ('stale_watchdog_skipped', scenario_stale_watchdog_skipped),
    ('alarm_fire_text_includes_rearm_hint', scenario_alarm_fire_text_includes_rearm_hint),
    ('watchdog_pending_text_omits_held_interrupt', scenario_watchdog_pending_text_omits_held_interrupt),
    ('alarm_cancel_preserves_at_limit_body', scenario_alarm_cancel_preserves_at_limit_body),
]
