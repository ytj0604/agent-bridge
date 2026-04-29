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
from pathlib import Path

from .harness import (
    _daemon_command_result,
    _participants_state,
    _queue_item,
    assert_true,
    identity_live_record,
    isolated_identity_env,
    make_daemon,
    patched_environ,
    read_events,
    test_message,
    verified_identity,
    write_identity_fixture,
)

import bridge_daemon  # noqa: E402
import bridge_hook_logger  # noqa: E402
import bridge_identity  # noqa: E402
import bridge_clear_peer  # noqa: E402
import bridge_clear_guard  # noqa: E402
import bridge_clear_marker  # noqa: E402
import bridge_pane_probe  # noqa: E402
import bridge_response_guard  # noqa: E402
from bridge_util import locked_json, read_json, utc_now  # noqa: E402


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


SCENARIOS = [
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
