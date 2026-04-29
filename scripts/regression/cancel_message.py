from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

from .harness import (
    _plant_watchdog,
    _queue_item,
    _watchdogs_for_message,
    assert_true,
    make_daemon,
    test_message,
)

import bridge_cancel_message  # noqa: E402

from bridge_util import read_json  # noqa: E402


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


SCENARIOS = [
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
]
