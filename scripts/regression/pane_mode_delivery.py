from __future__ import annotations

import time
from pathlib import Path

from .harness import (
    assert_true,
    make_daemon,
    read_events,
    test_message,
)

import bridge_attach  # noqa: E402

from bridge_util import (
    RESTART_PRESERVED_INFLIGHT_KEY,
    utc_now,
)  # noqa: E402


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
    d.run_tmux_paste_literal_touch_result = lambda pane, prompt, **kwargs: literal_calls.append(pane) or {"ok": True, "pane_touched": True, "error": ""}  # type: ignore[method-assign]
    d.run_tmux_enter = lambda pane: enter_calls.append(pane)  # type: ignore[method-assign]
    msg = test_message("msg-mode-pre-enter")
    def add(queue): queue.append(msg); return None
    d.queue.update(add)
    d.try_deliver("codex")
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


SCENARIOS = [
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
]
