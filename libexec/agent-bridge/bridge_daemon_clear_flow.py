#!/usr/bin/env python3
import os
import threading
import time
import uuid
from pathlib import Path

from bridge_clear_guard import (
    ClearGuardResult,
    ClearViolation,
    active_queue_rows,
    cancellable_queue_rows,
    format_clear_guard_result,
    target_originated_requests,
)
from bridge_clear_marker import make_marker, read_markers, remove_marker, ttl_for_clear_lifetime, update_marker, write_marker
from bridge_daemon_messages import make_message
from bridge_identity import verified_process_identity
from bridge_instructions import probe_prompt
from bridge_participants import format_peer_summary
from bridge_paths import state_root
from bridge_util import classify_prior_for_hint, locked_json, normalize_kind, short_id, utc_now


CLEAR_PROBE_TIMEOUT_SECONDS = 180.0
CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS = 1.0
CLEAR_POST_LOCK_WORST_CASE_SECONDS = 75.0
# Keep in sync with bridge_clear_peer.CLEAR_SOCKET_TIMEOUT_SECONDS.
CLEAR_CLIENT_TIMEOUT_SECONDS = 180.0
CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS = 10.0
CLEAR_LOCK_WAIT_BUDGET_SECONDS = 85.0
COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS = 0.5
COMMAND_SAFETY_MARGIN_SECONDS = 0.5


def _is_command_lock_wait_exceeded(exc: Exception) -> bool:
    return exc.__class__.__name__ == "CommandLockWaitExceeded"


def clear_peer_post_lock_worst_case_seconds(d) -> float:
    return CLEAR_POST_LOCK_WORST_CASE_SECONDS + max(0.0, float(d.clear_post_clear_delay_seconds or 0.0))


def clear_peer_batch_post_lock_worst_case_seconds(d, target_count: int) -> float:
    count = max(1, int(target_count or 1))
    base = d.clear_peer_post_lock_worst_case_seconds() * count
    if count > 1:
        base += CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS
    return base


def clear_peer_client_timeout_seconds(d, target_count: int) -> float:
    count = max(1, int(target_count or 1))
    if count <= 1:
        return CLEAR_CLIENT_TIMEOUT_SECONDS
    settle_extra = max(0.0, float(d.clear_post_clear_delay_seconds or 0.0) - CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS)
    return (CLEAR_CLIENT_TIMEOUT_SECONDS + settle_extra) * count + CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS


def clear_target_count_from_request(d, request: dict) -> int:
    targets = request.get("targets")
    if isinstance(targets, list) and targets:
        return len(targets)
    return 1


def sender_blocked_by_clear(d, sender: str) -> bool:
    if not sender or sender == "bridge":
        return False
    return sender in d.clear_reservations or sender in d.pending_self_clears


def _clear_guard_from_snapshots(
    d,
    target: str,
    *,
    force: bool,
    queue_snapshot: list[dict],
    aggregates: dict,
) -> ClearGuardResult:
    hard: list[ClearViolation] = []
    soft: list[ClearViolation] = []

    def add_violation(violation: ClearViolation) -> None:
        violation.target = violation.target or target
        if violation.hard:
            hard.append(violation)
        else:
            soft.append(violation)

    if target in d.clear_reservations:
        add_violation(ClearViolation("clear_already_pending", f"clear already active for {target}"))
    if target in d.pending_self_clears:
        add_violation(ClearViolation("clear_already_pending", f"self-clear already pending for {target}"))
    if d.busy.get(target) or (d.current_prompt_by_agent.get(target) or {}).get("id"):
        add_violation(ClearViolation("target_busy", f"{target} is currently processing a prompt"))
    inbound_active = active_queue_rows(queue_snapshot, target=target, last_enter_ts=d.last_enter_ts)
    if inbound_active:
        add_violation(
            ClearViolation(
                "target_active_messages",
                f"{target} has active/post-pane-touch inbound work",
                refs=[str(item.get("id") or "") for item in inbound_active if item.get("id")],
            )
        )
    inbound_cancellable = cancellable_queue_rows(queue_snapshot, target=target, last_enter_ts=d.last_enter_ts)
    if inbound_cancellable:
        add_violation(
            ClearViolation(
                "target_cancellable_messages",
                f"{target} has cancellable pending/pre-active inbound work",
                hard=False,
                refs=[str(item.get("id") or "") for item in inbound_cancellable if item.get("id")],
            )
        )
    originated_requests = target_originated_requests(queue_snapshot, target=target)
    if originated_requests:
        refs = [str(item.get("id") or "") for item in originated_requests if item.get("id")]
        add_violation(
            ClearViolation(
                "target_originated_requests",
                f"{target} has outstanding request rows",
                hard=False,
                refs=refs,
            )
        )
    target_alarms = [
        wake_id
        for wake_id, wd in d.watchdogs.items()
        if wd and wd.get("is_alarm") and str(wd.get("sender") or "") == target
    ]
    if target_alarms:
        add_violation(
            ClearViolation(
                "target_owned_alarms",
                f"{target} owns active alarms",
                hard=False,
                refs=target_alarms,
            )
        )
    requester_aggs = [
        str(agg_id)
        for agg_id, agg in aggregates.items()
        if isinstance(agg, dict)
        and str(agg.get("requester") or "") == target
        and str(agg.get("status") or "collecting") not in {"complete", "cancelled_requester_cleared"}
    ]
    if requester_aggs:
        add_violation(
            ClearViolation(
                "target_aggregate_requester",
                f"{target} owns incomplete aggregate waits",
                hard=False,
                refs=requester_aggs,
            )
        )
    if hard or (soft and not force):
        return ClearGuardResult(ok=False, hard_blockers=hard, soft_blockers=soft, force_attempted=force)
    return ClearGuardResult(ok=True, hard_blockers=hard, soft_blockers=soft, force_attempted=force)


def clear_guard(d, target: str, *, force: bool) -> ClearGuardResult:
    queue_snapshot = list(d.queue.read())
    aggregates = d.aggregates.read_aggregates()
    return _clear_guard_from_snapshots(
        d,
        target,
        force=force,
        queue_snapshot=queue_snapshot,
        aggregates=aggregates,
    )


def clear_guard_multi(
    d,
    targets: list[str],
    *,
    force: bool,
    queue_snapshot: list[dict],
    aggregates: dict,
) -> ClearGuardResult:
    hard: list[ClearViolation] = []
    soft: list[ClearViolation] = []
    for target in targets:
        result = _clear_guard_from_snapshots(
            d,
            target,
            force=force,
            queue_snapshot=queue_snapshot,
            aggregates=aggregates,
        )
        hard.extend(result.hard_blockers)
        soft.extend(result.soft_blockers)
    return ClearGuardResult(
        ok=not hard and not (soft and not force),
        hard_blockers=hard,
        soft_blockers=soft,
        force_attempted=force,
    )


def apply_force_clear_invalidation(d, target: str, caller: str) -> dict:
    now_iso = utc_now()
    cancelled_alarms: list[str] = []
    removed_rows: list[dict] = []
    requester_cleared_ids: list[str] = []
    cancelled_aggregates: list[str] = []
    for wake_id, wd in list(d.watchdogs.items()):
        if wd and wd.get("is_alarm") and str(wd.get("sender") or "") == target:
            removed = d.watchdogs.pop(wake_id, None)
            if removed:
                d._record_alarm_wake_tombstone(wake_id, removed, "cancelled_by_clear")
                cancelled_alarms.append(wake_id)

    def queue_mutator(queue: list[dict]) -> None:
        kept: list[dict] = []
        for item in queue:
            if str(item.get("to") or "") == target and classify_prior_for_hint(item, d.last_enter_ts) == "cancel":
                removed_rows.append(dict(item))
                continue
            if str(item.get("from") or "") == target and normalize_kind(item.get("kind"), "request") == "request":
                item["auto_return"] = False
                item["requester_cleared"] = True
                item["requester_cleared_alias"] = target
                item["requester_cleared_at"] = now_iso
                item["requester_cleared_by"] = caller
                item["updated_ts"] = now_iso
                requester_cleared_ids.append(str(item.get("id") or ""))
            kept.append(item)
        queue[:] = kept

    d.queue.update(queue_mutator)
    for row in removed_rows:
        msg_id = str(row.get("id") or "")
        nonce = str(row.get("nonce") or "")
        if nonce:
            d.discard_nonce(nonce)
        if msg_id:
            d.last_enter_ts.pop(msg_id, None)
            d.cancel_watchdogs_for_message(msg_id, reason="cancelled_by_clear")
            d.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="cancelled_by_clear")
        d._record_message_tombstone(
            target,
            row,
            by_sender=caller,
            reason="cancelled_by_clear",
            suppress_late_hooks=bool(nonce),
            prompt_submitted_seen=False,
        )
        if row.get("aggregate_id"):
            d._record_aggregate_interrupted_reply(row, by_sender=caller, reason="cancelled_by_clear")
    for msg_id in requester_cleared_ids:
        d.cancel_watchdogs_for_message(msg_id, reason="requester_cleared")
        d.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="requester_cleared")
    for responder, context in list(d.current_prompt_by_agent.items()):
        if str(context.get("from") or "") != target:
            continue
        context["auto_return"] = False
        context["requester_cleared"] = True
        context["requester_cleared_alias"] = target
        context["requester_cleared_at"] = now_iso
        context["requester_cleared_by"] = caller
        msg_id = str(context.get("id") or "")
        if msg_id:
            d.cancel_watchdogs_for_message(msg_id, reason="requester_cleared")
            d.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="requester_cleared")
        d.log(
            "active_requester_cleared",
            responder=responder,
            requester=target,
            message_id=msg_id,
            by_sender=caller,
        )

    def aggregate_mutator(data: dict) -> None:
        aggregates = data.setdefault("aggregates", {})
        for agg_id, aggregate in list(aggregates.items()):
            if not isinstance(aggregate, dict):
                continue
            if str(aggregate.get("requester") or "") != target:
                continue
            if str(aggregate.get("status") or "") == "complete":
                continue
            aggregate["status"] = "cancelled_requester_cleared"
            aggregate["cancelled_at"] = now_iso
            aggregate["cancelled_by"] = caller
            aggregate["delivered"] = False
            aggregate["updated_ts"] = now_iso
            cancelled_aggregates.append(str(agg_id))

    d.aggregates.update(aggregate_mutator)
    for agg_id in cancelled_aggregates:
        d.cancel_watchdogs_for_aggregate(agg_id, reason="requester_cleared")
        d.suppress_pending_watchdog_wakes(ref_aggregate_id=agg_id, reason="requester_cleared")
    d.log(
        "clear_force_invalidated",
        target=target,
        by_sender=caller,
        cancelled_alarm_ids=cancelled_alarms,
        removed_message_ids=[row.get("id") for row in removed_rows],
        requester_cleared_message_ids=requester_cleared_ids,
        cancelled_aggregate_ids=cancelled_aggregates,
    )
    return {
        "cancelled_alarms": cancelled_alarms,
        "removed_message_ids": [row.get("id") for row in removed_rows],
        "requester_cleared_message_ids": requester_cleared_ids,
        "cancelled_aggregate_ids": cancelled_aggregates,
    }


def force_leave_after_clear_failure(d, target: str, *, caller: str, reason: str, reservation: dict | None = None) -> None:
    # Forced-leave cleanup is a daemon-internal recovery path, not a
    # client-visible command mutation. It must run serialized even if the
    # originating command budget is exhausted, so take the raw state lock
    # here rather than re-entering command_state_lock.
    if not d._state_lock_owned_by_current_thread():
        with d.state_lock:
            d.force_leave_after_clear_failure(target, caller=caller, reason=reason, reservation=reservation)
        return
    reservation = reservation or d.clear_reservations.get(target) or {}
    reservation["forced_leave"] = True
    reservation["forced_leave_reason"] = reason
    old_session_id = str(reservation.get("old_session_id") or "")
    new_session_id = str(reservation.get("new_session_id") or "")
    participant = d.participants.get(target) or {}
    agent_type = str(participant.get("agent_type") or reservation.get("agent") or "")
    now_iso = utc_now()
    removed_rows: list[dict] = []

    def queue_mutator(queue: list[dict]) -> None:
        kept: list[dict] = []
        for item in queue:
            if str(item.get("to") or "") == target:
                removed_rows.append(dict(item))
                continue
            if str(item.get("from") or "") == target and normalize_kind(item.get("kind"), "request") == "request":
                item["auto_return"] = False
                item["requester_cleared"] = True
                item["requester_cleared_alias"] = target
                item["requester_cleared_at"] = now_iso
                item["requester_cleared_by"] = caller
                item["updated_ts"] = now_iso
            kept.append(item)
        queue[:] = kept

    d.queue.update(queue_mutator)
    for row in removed_rows:
        msg_id = str(row.get("id") or "")
        if msg_id:
            d.cancel_watchdogs_for_message(msg_id, reason="clear_forced_leave")
            d.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="clear_forced_leave")
        if row.get("aggregate_id"):
            d._record_aggregate_interrupted_reply(row, by_sender=caller, reason="endpoint_lost", deliver=False)
    for wake_id, wd in list(d.watchdogs.items()):
        if wd and str(wd.get("sender") or "") == target:
            removed = d.watchdogs.pop(wake_id, None)
            if removed and removed.get("is_alarm"):
                d._record_alarm_wake_tombstone(wake_id, removed, "clear_forced_leave")
            d.log("watchdog_cancelled", wake_id=wake_id, reason="clear_forced_leave")
    d.busy[target] = False
    d.reserved[target] = None
    d.current_prompt_by_agent.pop(target, None)
    d.interrupt_partial_failure_blocks.pop(target, None)
    d.pending_self_clears.pop(target, None)
    marker_id_value = str(reservation.get("identity_marker_id") or "")
    if marker_id_value:
        remove_marker(marker_id_value)
    d.clear_reservations.pop(target, None)
    try:
        d._mark_participant_detached_for_clear(
            target,
            agent_type=agent_type,
            old_session_id=old_session_id,
            new_session_id=new_session_id,
            reason=reason,
        )
    except Exception as exc:
        d.safe_log("clear_forced_leave_cleanup_failed", target=target, error=str(exc), reason=reason)
    d.session_mtime_ns = None
    d._reload_participants_unlocked()
    for alias in sorted(d.participants):
        if alias == target:
            continue
        notice = make_message(
            sender="bridge",
            target=alias,
            intent="clear_forced_leave_notice",
            body=f"[bridge:peer_cleared] {target} was removed from the bridge after clear failed ({reason}).",
            causal_id=short_id("causal"),
            hop_count=0,
            auto_return=False,
            kind="notice",
            source="clear_forced_leave",
        )
        d.queue_message(notice, deliver=False)
    d.log(
        "clear_forced_leave",
        target=target,
        by_sender=caller,
        reason=reason,
        removed_message_ids=[row.get("id") for row in removed_rows],
    )


def _mark_participant_detached_for_clear(
    d,
    target: str,
    *,
    agent_type: str,
    old_session_id: str,
    new_session_id: str,
    reason: str,
) -> None:
    session_path = d.session_file
    attached_path = state_root() / "attached-sessions.json"
    pane_locks_path = state_root() / "pane-locks.json"
    live_path = state_root() / "live-sessions.json"
    # Honor test/installation overrides used by bridge_identity.
    attached_path = Path(os.environ.get("AGENT_BRIDGE_ATTACH_REGISTRY", str(attached_path)))
    pane_locks_path = Path(os.environ.get("AGENT_BRIDGE_PANE_LOCKS", str(pane_locks_path)))
    live_path = Path(os.environ.get("AGENT_BRIDGE_LIVE_SESSIONS", str(live_path)))
    with locked_json(session_path, {"session": d.bridge_session, "participants": {}}) as state:
        with locked_json(attached_path, {"version": 1, "sessions": {}}) as registry:
            with locked_json(pane_locks_path, {"version": 1, "panes": {}}) as locks:
                with locked_json(live_path, {"version": 1, "panes": {}, "sessions": {}}) as live:
                    participant = (state.setdefault("participants", {}) or {}).get(target)
                    pane = ""
                    if isinstance(participant, dict):
                        pane = str(participant.get("pane") or "")
                        participant["status"] = "detached"
                        participant["detached_at"] = utc_now()
                        participant["detach_reason"] = reason
                        participant["endpoint_status"] = "cleared"
                    for key in (old_session_id, new_session_id):
                        if key and agent_type:
                            registry.setdefault("sessions", {}).pop(f"{agent_type}:{key}", None)
                            live.setdefault("sessions", {}).pop(f"{agent_type}:{key}", None)
                    panes = locks.setdefault("panes", {})
                    for lock_pane, record in list(panes.items()):
                        if not isinstance(record, dict):
                            continue
                        if record.get("alias") == target or record.get("hook_session_id") in {old_session_id, new_session_id}:
                            del panes[lock_pane]
                    live_panes = live.setdefault("panes", {})
                    for live_pane, record in list(live_panes.items()):
                        if not isinstance(record, dict):
                            continue
                        if live_pane == pane or record.get("alias") == target or record.get("session_id") in {old_session_id, new_session_id}:
                            del live_panes[live_pane]


def clear_target_lookup_error(d, target: str) -> dict:
    all_participants = d.session_state.get("participants") or {}
    if isinstance(all_participants, dict) and target in all_participants:
        return {
            "ok": False,
            "error": f"target {target!r} is not an active participant",
            "error_kind": "inactive_target",
            "target": target,
        }
    return {
        "ok": False,
        "error": f"unknown target alias {target!r}; active aliases: {', '.join(sorted(d.participants))}",
        "error_kind": "unknown_target",
        "target": target,
    }


def validate_clear_targets_payload(d, sender: str, targets_payload: object, *, force: bool) -> dict:
    if not isinstance(targets_payload, list):
        return {"ok": False, "error": "targets must be a non-empty list of aliases", "error_kind": "malformed_targets"}
    if not targets_payload:
        return {"ok": False, "error": "targets must be a non-empty list of aliases", "error_kind": "malformed_targets"}
    seen: set[str] = set()
    targets: list[str] = []
    for raw in targets_payload:
        if not isinstance(raw, str):
            return {"ok": False, "error": "targets entries must be aliases", "error_kind": "malformed_targets"}
        target = raw.strip()
        if not target:
            return {"ok": False, "error": "targets entries must be non-empty aliases", "error_kind": "malformed_targets"}
        if target in seen:
            continue
        if target not in d.participants:
            return d.clear_target_lookup_error(target)
        seen.add(target)
        targets.append(target)
    if not targets:
        return {"ok": False, "error": "targets must be a non-empty list of aliases", "error_kind": "malformed_targets"}
    if len(targets) > 1 and force:
        return {
            "ok": False,
            "error": "--force is only supported for single-target clear; specify exactly one alias when using --force",
            "error_kind": "multi_force_disallowed",
        }
    if len(targets) > 1 and sender and sender != "bridge" and sender in targets:
        return {
            "ok": False,
            "error": "self-clear must be the only target; specify only your own alias or omit yourself",
            "error_kind": "multi_self_disallowed",
        }
    return {"ok": True, "targets": targets}


def clear_batch_summary(d, results: list[dict]) -> dict:
    by_status: dict[str, list[str]] = {"cleared": [], "forced_leave": [], "failed": []}
    for result in results:
        status = str(result.get("status") or "failed")
        target = str(result.get("target") or "")
        by_status.setdefault(status, []).append(target)
    return {
        "counts": {status: len(targets) for status, targets in by_status.items()},
        "cleared": by_status.get("cleared", []),
        "forced_leave": by_status.get("forced_leave", []),
        "failed": by_status.get("failed", []),
        "all_cleared": len(by_status.get("cleared", [])) == len(results),
    }


def normalize_clear_batch_result(d, target: str, result: dict) -> dict:
    if result.get("ok") and result.get("cleared"):
        status = "cleared"
    elif result.get("forced_leave"):
        status = "forced_leave"
    else:
        status = "failed"
    normalized = {
        "target": target,
        "status": status,
        "ok": bool(result.get("ok")),
        "cleared": bool(result.get("cleared")),
        "forced_leave": bool(result.get("forced_leave")),
    }
    if result.get("error"):
        normalized["error"] = str(result.get("error") or "")
    if result.get("error_kind"):
        normalized["error_kind"] = str(result.get("error_kind") or "")
    if result.get("new_session_id"):
        normalized["new_session_id"] = result.get("new_session_id")
    return normalized


def clear_batch_exception_requires_forced_leave(d, reservation: dict) -> bool:
    if reservation.get("pane_touched"):
        return True
    phase = str(reservation.get("phase") or "")
    return phase not in {"", "reserved", "pending_prompt"}


def hold_clear_reservation_for_batch_failure(d, reservation: dict | None, reason: str) -> None:
    if reservation is None:
        return
    reservation["phase"] = "batch_hold_failed"
    reservation["batch_hold_complete"] = True
    reservation["batch_hold_failure_reason"] = reason


def handle_clear_peer(d, sender: str, target: str, *, force: bool) -> dict:
    if not target:
        return {"ok": False, "error": "target required"}
    if target in d.clear_reservations:
        return {"ok": False, "error": "clear_already_pending", "error_kind": "clear_already_pending"}
    if sender == target and sender != "bridge":
        try:
            lock_ctx = d.command_state_lock(command_class="clear_peer")
            lock_ctx.__enter__()
        except Exception as exc:
            if not _is_command_lock_wait_exceeded(exc):
                raise
            return d.lock_wait_exceeded_response("clear_peer")
        try:
            if not d.command_deadline_ok(
                post_lock_worst_case=COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS,
                margin=COMMAND_SAFETY_MARGIN_SECONDS,
                command_class="clear_peer",
            ):
                return d.lock_wait_exceeded_response("clear_peer")
            if target in d.clear_reservations or target in d.pending_self_clears:
                return {"ok": False, "error": "clear_already_pending", "error_kind": "clear_already_pending"}
            d.pending_self_clears[target] = {
                "clear_id": short_id("clear"),
                "caller": sender,
                "target": target,
                "force": bool(force),
                "created_at": utc_now(),
                "created_ts": time.time(),
            }
        finally:
            lock_ctx.__exit__(None, None, None)
        d.log("self_clear_deferred", target=target, by_sender=sender, force=bool(force))
        return {"ok": True, "deferred": True, "target": target}
    return d.run_clear_peer(sender, target, force=force)


def handle_clear_peers(d, sender: str, targets: list[str], *, force: bool) -> dict:
    validation = d.validate_clear_targets_payload(sender, targets, force=force)
    if not validation.get("ok"):
        return validation
    targets = list(validation.get("targets") or [])
    if len(targets) == 1:
        return d.handle_clear_peer(sender, targets[0], force=force)

    reservations: dict[str, dict] = {}
    try:
        lock_ctx = d.command_state_lock(
            post_lock_worst_case=d.clear_peer_batch_post_lock_worst_case_seconds(len(targets)),
            margin=20.0,
            command_class="clear_peer",
        )
        lock_ctx.__enter__()
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        return d.lock_wait_exceeded_response("clear_peer")
    try:
        if not d.command_deadline_ok(
            post_lock_worst_case=d.clear_peer_batch_post_lock_worst_case_seconds(len(targets)),
            margin=20.0,
            command_class="clear_peer",
        ):
            return d.lock_wait_exceeded_response("clear_peer")
        d._reload_participants_unlocked()
        validation = d.validate_clear_targets_payload(sender, targets, force=force)
        if not validation.get("ok"):
            return validation
        targets = list(validation.get("targets") or [])
        queue_snapshot = list(d.queue.read())
        aggregates = d.aggregates.read_aggregates()
        guard = d.clear_guard_multi(
            targets,
            force=False,
            queue_snapshot=queue_snapshot,
            aggregates=aggregates,
        )
        if not guard.ok:
            return {
                "ok": False,
                "error": format_clear_guard_result(
                    guard,
                    suppress_force_hint=True,
                    include_targets=True,
                ),
                "error_kind": "clear_blocked",
                "hard_blockers": [v.__dict__ for v in guard.hard_blockers],
                "soft_blockers": [v.__dict__ for v in guard.soft_blockers],
            }
        for target in targets:
            participant = dict(d.participants.get(target) or {})
            reservation = d._new_clear_reservation(sender, target, force=False, participant=participant)
            reservations[target] = reservation
            d.clear_reservations[target] = reservation
        d.log("clear_peer_batch_reserved", by_sender=sender, targets=targets)
    finally:
        lock_ctx.__exit__(None, None, None)

    results: list[dict] = []
    try:
        for target in targets:
            reservation = reservations.get(target) or {}
            try:
                result = d.run_clear_peer(
                    sender,
                    target,
                    force=False,
                    existing_reservation=reservation,
                    hold_reservation_after_success=True,
                )
            except Exception as exc:
                d.safe_log("clear_peer_batch_target_exception", target=target, by_sender=sender, error=str(exc))
                if d.clear_batch_exception_requires_forced_leave(reservation):
                    d.force_leave_after_clear_failure(
                        target,
                        caller=sender,
                        reason=f"batch_exception:{exc}",
                        reservation=reservation,
                    )
                    result = {
                        "ok": False,
                        "error": str(exc),
                        "error_kind": "exception",
                        "forced_leave": True,
                        "target": target,
                    }
                else:
                    result = {"ok": False, "error": str(exc), "error_kind": "exception", "target": target}
                    with d.state_lock:
                        if d.clear_reservations.get(target) is reservation:
                            marker_id_value = str(reservation.get("identity_marker_id") or "")
                            if marker_id_value:
                                remove_marker(marker_id_value)
                            d.clear_reservations.pop(target, None)
            results.append(d.normalize_clear_batch_result(target, result if isinstance(result, dict) else {}))
    finally:
        released_targets: list[str] = []
        with d.state_lock:
            for target, reservation in reservations.items():
                if d.clear_reservations.get(target) is reservation:
                    marker_id_value = str(reservation.get("identity_marker_id") or "")
                    if marker_id_value:
                        remove_marker(marker_id_value)
                    d.clear_reservations.pop(target, None)
                    released_targets.append(target)
                    d.log("clear_peer_batch_reservation_released", target=target, by_sender=sender)
        for target in released_targets:
            d.request_and_drain_delivery(target, command_aware=True, reason="clear_batch_release")

    summary = d.clear_batch_summary(results)
    d.log("clear_peer_batch_completed", by_sender=sender, targets=targets, summary=summary)
    return {"ok": True, "targets": targets, "results": results, "summary": summary}


def _new_clear_reservation(d, caller: str, target: str, *, force: bool, participant: dict, pane: str = "") -> dict:
    clear_id = short_id("clear")
    probe_id = f"{d.bridge_session}:{target}:{uuid.uuid4().hex[:10]}"
    return {
        "clear_id": clear_id,
        "caller": caller,
        "target": target,
        "force": bool(force),
        "probe_id": probe_id,
        "old_session_id": str(participant.get("hook_session_id") or ""),
        "old_turn_id_hint": "",
        "deadline_ts": time.time() + CLEAR_PROBE_TIMEOUT_SECONDS,
        "phase": "reserved",
        "pane_touched": False,
        "probe_prompt_submitted": False,
        "probe_turn_id": "",
        "new_session_id": "",
        "identity_marker_id": "",
        "pane": pane,
        "agent": str(participant.get("agent_type") or ""),
        "condition": threading.Condition(d.state_lock),
    }


def _write_clear_marker_locked(d, reservation: dict, participant: dict, pane: str) -> str:
    marker_ttl_seconds = ttl_for_clear_lifetime(
        settle_delay_seconds=d.clear_post_clear_delay_seconds,
        probe_timeout_seconds=CLEAR_PROBE_TIMEOUT_SECONDS,
    )
    marker = make_marker(
        bridge_session=d.bridge_session,
        alias=str(reservation.get("target") or ""),
        agent=str(participant.get("agent_type") or ""),
        old_session_id=str(reservation.get("old_session_id") or ""),
        probe_id=str(reservation.get("probe_id") or ""),
        pane=pane,
        target=str(participant.get("target") or pane),
        events_file=str(d.state_file),
        public_events_file=str(d.public_state_file or ""),
        caller=str(reservation.get("caller") or ""),
        clear_id=str(reservation.get("clear_id") or ""),
        ttl_seconds=marker_ttl_seconds,
    )
    marker_id_value = write_marker(marker)
    reservation["identity_marker_id"] = marker_id_value
    reservation["phase"] = "pending_prompt"
    return marker_id_value


def _clear_probe_text(d, target: str, probe_id: str) -> str:
    return probe_prompt("clear", probe_id, target, format_peer_summary(d.session_state))


def _wait_for_clear_settle_locked(d, reservation: dict) -> None:
    delay = max(0.0, float(d.clear_post_clear_delay_seconds or 0.0))
    reservation["phase"] = "post_clear_settle"
    reservation["post_clear_settle_delay_sec"] = delay
    d.log(
        "clear_post_clear_settle",
        target=reservation.get("target"),
        by_sender=reservation.get("caller"),
        delay_sec=delay,
    )
    if delay <= 0:
        return
    condition = reservation.get("condition")
    if not isinstance(condition, threading.Condition):
        return
    deadline = time.time() + delay
    while not reservation.get("failure_reason"):
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        condition.wait(min(remaining, 0.25))


def run_clear_peer(
    d,
    caller: str,
    target: str,
    *,
    force: bool,
    existing_reservation: dict | None = None,
    hold_reservation_after_success: bool = False,
) -> dict:
    reservation: dict | None = existing_reservation
    participant: dict = {}
    endpoint_detail: dict = {}
    try:
        state_lock_ctx = d.command_state_lock(
            post_lock_worst_case=d.clear_peer_post_lock_worst_case_seconds(),
            margin=20.0,
            command_class="clear_peer",
        )
        state_lock_ctx.__enter__()
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        if hold_reservation_after_success:
            d.hold_clear_reservation_for_batch_failure(reservation, "lock_wait_exceeded")
        return d.lock_wait_exceeded_response("clear_peer")
    try:
        if not d.command_deadline_ok(
            post_lock_worst_case=d.clear_peer_post_lock_worst_case_seconds(),
            margin=20.0,
            command_class="clear_peer",
        ):
            if hold_reservation_after_success:
                d.hold_clear_reservation_for_batch_failure(reservation, "lock_wait_exceeded")
            return d.lock_wait_exceeded_response("clear_peer")
        d._reload_participants_unlocked()
        if target not in d.participants:
            if hold_reservation_after_success:
                d.hold_clear_reservation_for_batch_failure(reservation, "target_inactive")
            return {"ok": False, "error": f"target {target!r} is not an active participant"}
        participant = dict(d.participants.get(target) or {})
        if reservation is None:
            guard = d.clear_guard(target, force=force)
            if not guard.ok:
                return {
                    "ok": False,
                    "error": format_clear_guard_result(guard),
                    "error_kind": "clear_blocked",
                    "hard_blockers": [v.__dict__ for v in guard.hard_blockers],
                    "soft_blockers": [v.__dict__ for v in guard.soft_blockers],
                }
            reservation = d._new_clear_reservation(caller, target, force=force, participant=participant)
            d.clear_reservations[target] = reservation
        else:
            d.clear_reservations[target] = reservation
            reservation.setdefault("condition", threading.Condition(d.state_lock))
            reservation.setdefault("deadline_ts", time.time() + CLEAR_PROBE_TIMEOUT_SECONDS)
            reservation.setdefault("phase", "reserved")
            reservation.setdefault("force", bool(force))

        endpoint_detail = d.resolve_endpoint_detail(target, purpose="write")
        pane = str(endpoint_detail.get("pane") or "") if endpoint_detail.get("ok") else ""
        if not pane:
            if hold_reservation_after_success:
                d.hold_clear_reservation_for_batch_failure(reservation, "endpoint_lost")
            else:
                d.clear_reservations.pop(target, None)
                marker_id_existing = str(reservation.get("identity_marker_id") or "")
                if marker_id_existing:
                    remove_marker(marker_id_existing)
            return {"ok": False, "error": str(endpoint_detail.get("reason") or "endpoint_lost"), "error_kind": "endpoint_lost", "target": target}
        reservation["pane"] = pane
        mode_status = d.pane_mode_status(pane)
        if mode_status.get("error") or mode_status.get("in_mode"):
            if hold_reservation_after_success:
                d.hold_clear_reservation_for_batch_failure(reservation, "pane_not_ready")
            else:
                d.clear_reservations.pop(target, None)
                marker_id_existing = str(reservation.get("identity_marker_id") or "")
                if marker_id_existing:
                    remove_marker(marker_id_existing)
            return {"ok": False, "error": str(mode_status.get("error") or "pane_in_mode"), "error_kind": "pane_not_ready", "target": target}

        marker_id_value = d._write_clear_marker_locked(reservation, participant, pane)
        reservation["phase"] = "writing_clear"
        clear_status = d._clear_tmux_send(pane, "/clear", target=target, message_id=str(reservation.get("clear_id") or "clear"))
        reservation["pane_touched"] = bool(clear_status.get("pane_touched"))
        if not clear_status.get("ok"):
            if reservation.get("pane_touched"):
                d.force_leave_after_clear_failure(target, caller=caller, reason=f"clear_write_failed:{clear_status.get('error')}", reservation=reservation)
            else:
                if hold_reservation_after_success:
                    d.hold_clear_reservation_for_batch_failure(reservation, "clear_write_failed")
                else:
                    d.clear_reservations.pop(target, None)
                    remove_marker(marker_id_value)
            return {
                "ok": False,
                "error": str(clear_status.get("error") or "clear_write_failed"),
                "error_kind": "clear_write_failed",
                "pane_touched": bool(reservation.get("pane_touched")),
                "forced_leave": bool(reservation.get("forced_leave")),
                "target": target,
            }

        d._wait_for_clear_settle_locked(reservation)
        if reservation.get("failure_reason"):
            return {
                "ok": False,
                "error": str(reservation.get("failure_reason") or "clear_failed"),
                "error_kind": str(reservation.get("failure_reason") or "clear_failed"),
                "forced_leave": bool(reservation.get("forced_leave")),
                "target": target,
            }
        settle_mode_status = d.pane_mode_status(pane)
        if settle_mode_status.get("error") or settle_mode_status.get("in_mode"):
            reason_detail = str(settle_mode_status.get("error") or settle_mode_status.get("mode") or "pane_in_mode")
            reason = f"clear_settle_pane_not_ready:{reason_detail}"
            d.force_leave_after_clear_failure(target, caller=caller, reason=reason, reservation=reservation)
            return {"ok": False, "error": reason, "error_kind": "clear_settle_pane_not_ready", "forced_leave": True, "target": target}

        if force:
            try:
                reservation["force_invalidation"] = d.apply_force_clear_invalidation(target, caller)
            except Exception as exc:
                d.safe_log("clear_force_invalidation_failed", target=target, by_sender=caller, error=str(exc))
                d.force_leave_after_clear_failure(target, caller=caller, reason=f"force_invalidation_failed:{exc}", reservation=reservation)
                return {"ok": False, "error": str(exc), "error_kind": "force_invalidation_failed", "forced_leave": True, "target": target}

        probe_text = d._clear_probe_text(target, str(reservation.get("probe_id") or ""))
        reservation["phase"] = "writing_probe"
        probe_status = d._clear_tmux_send(pane, probe_text, target=target, message_id=str(reservation.get("probe_id") or "probe"))
        if not probe_status.get("ok"):
            d.force_leave_after_clear_failure(target, caller=caller, reason=f"probe_write_failed:{probe_status.get('error')}", reservation=reservation)
            return {"ok": False, "error": str(probe_status.get("error") or "probe_write_failed"), "error_kind": "probe_write_failed", "forced_leave": True, "target": target}
        reservation["phase"] = "waiting_probe"
        reservation["deadline_ts"] = time.time() + CLEAR_PROBE_TIMEOUT_SECONDS
        d.log("clear_probe_sent", target=target, by_sender=caller, probe_id=reservation.get("probe_id"))

        if d.dry_run:
            reservation["phase"] = "probe_finished"
            reservation["probe_prompt_submitted"] = True
            reservation["probe_response_finished"] = True
            reservation["new_session_id"] = reservation.get("old_session_id") or "dry-run-session"

        condition = reservation["condition"]
        while not reservation.get("probe_response_finished") and not reservation.get("failure_reason"):
            remaining = float(reservation.get("deadline_ts") or time.time()) - time.time()
            if remaining <= 0:
                break
            condition.wait(min(remaining, 1.0))
        failure_reason = str(reservation.get("failure_reason") or "")
        if failure_reason:
            return {"ok": False, "error": failure_reason, "error_kind": failure_reason, "forced_leave": bool(reservation.get("forced_leave")), "target": target}
        if not reservation.get("probe_response_finished"):
            d.force_leave_after_clear_failure(target, caller=caller, reason="probe_timeout", reservation=reservation)
            return {"ok": False, "error": "probe_timeout", "error_kind": "probe_timeout", "forced_leave": True, "target": target}
        reservation["phase"] = "finalizing"
        marker_id_value = str(reservation.get("identity_marker_id") or "")
        if marker_id_value:
            update_marker(marker_id_value, lambda current: {**current, "phase": "finalizing"})
    finally:
        state_lock_ctx.__exit__(None, None, None)

    agent_type = str(participant.get("agent_type") or reservation.get("agent") or "")
    new_session_id = str(reservation.get("new_session_id") or "")
    pane = str(reservation.get("pane") or "")
    identity_required = not (d.dry_run and not str(reservation.get("old_session_id") or ""))
    process_identity = (
        d.clear_process_identity_for_replacement(
            agent_type=agent_type,
            session_id=new_session_id,
            pane=pane,
        )
        if identity_required
        else {}
    )

    if not identity_required:
        helper_result = {"ok": True, "dry_run": True}
    else:
        helper_result = d.replace_attached_session_identity_for_clear(
            bridge_session=d.bridge_session,
            alias=target,
            agent_type=agent_type,
            old_session_id=str(reservation.get("old_session_id") or ""),
            new_session_id=new_session_id,
            pane=pane,
            target=str(participant.get("target") or endpoint_detail.get("target") or ""),
            cwd=str(participant.get("cwd") or ""),
            model=str(participant.get("model") or ""),
            process_identity=process_identity,
        )

    try:
        final_lock_ctx = d.command_state_lock(
            post_lock_worst_case=COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS,
            margin=COMMAND_SAFETY_MARGIN_SECONDS,
            command_class="clear_peer",
        )
        final_lock_ctx.__enter__()
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        d.force_leave_after_clear_failure(target, caller=caller, reason="lock_wait_exceeded_after_identity", reservation=reservation)
        response = d.lock_wait_exceeded_response("clear_peer")
        response.update({"forced_leave": True, "target": target})
        return response
    try:
        if not helper_result.get("ok"):
            d.force_leave_after_clear_failure(target, caller=caller, reason=f"identity_replace_failed:{helper_result.get('error')}", reservation=reservation)
            return {
                "ok": False,
                "error": str(helper_result.get("error") or "identity_replace_failed"),
                "error_kind": "identity_replace_failed",
                "forced_leave": True,
                "target": target,
            }
        if identity_required and not verified_process_identity(helper_result.get("live_record") or {}):
            d.force_leave_after_clear_failure(target, caller=caller, reason="clear_process_identity_unverified", reservation=reservation)
            return {
                "ok": False,
                "error": "clear_process_identity_unverified",
                "error_kind": "clear_process_identity_unverified",
                "forced_leave": True,
                "target": target,
            }
        marker_id_value = str(reservation.get("identity_marker_id") or "")
        if marker_id_value:
            remove_marker(marker_id_value)
            reservation["identity_marker_id"] = ""
        if hold_reservation_after_success:
            reservation["phase"] = "batch_hold"
            reservation["batch_hold_complete"] = True
        else:
            d.clear_reservations.pop(target, None)
        d.pending_self_clears.pop(target, None)
        d.busy[target] = False
        d.reserved[target] = None
        d.current_prompt_by_agent.pop(target, None)
        d.session_mtime_ns = None
        d._reload_participants_unlocked()
        d.log(
            "clear_peer_completed",
            target=target,
            by_sender=caller,
            force=bool(force),
            probe_id=reservation.get("probe_id"),
            new_session_id=reservation.get("new_session_id"),
        )
    finally:
        final_lock_ctx.__exit__(None, None, None)
    if not hold_reservation_after_success:
        d.request_and_drain_delivery(target, command_aware=True, reason="clear_success")
    return {"ok": True, "target": target, "cleared": True, "force": bool(force), "new_session_id": reservation.get("new_session_id")}


def handle_clear_prompt_submitted_locked(d, agent: str, record: dict) -> bool:
    reservation = d.clear_reservations.get(agent)
    if not reservation:
        return False
    attach_probe = str(record.get("attach_probe") or "")
    if attach_probe and attach_probe == str(reservation.get("probe_id") or ""):
        marker_id_value = str(reservation.get("identity_marker_id") or "")
        marker = (read_markers().get("markers") or {}).get(marker_id_value) if marker_id_value else None
        marker = marker if isinstance(marker, dict) else {}
        marker_new_session = str(marker.get("new_session_id") or "")
        marker_turn_id = str(marker.get("probe_turn_id") or "")
        record_turn_id = str(record.get("turn_id") or "")
        phase_ok = str(marker.get("phase") or "") == "prompt_seen"
        if marker_turn_id:
            turn_ids_compatible = not record_turn_id or marker_turn_id == record_turn_id
        else:
            turn_ids_compatible = not record_turn_id
        if not (phase_ok and marker_new_session and turn_ids_compatible):
            reservation["phase"] = "failed"
            reservation["failure_reason"] = "clear_marker_phase2_missing"
            condition = reservation.get("condition")
            if isinstance(condition, threading.Condition):
                condition.notify_all()
            d.log(
                "clear_marker_phase2_missing",
                target=agent,
                probe_id=reservation.get("probe_id"),
                marker_id=marker_id_value,
                marker_phase=marker.get("phase"),
                marker_new_session_present=bool(marker_new_session),
                marker_probe_turn_id=marker_turn_id,
                record_turn_id=record_turn_id,
            )
            d.busy[agent] = False
            d.reserved[agent] = None
            if reservation.get("pane_touched"):
                d.force_leave_after_clear_failure(
                    agent,
                    caller=str(reservation.get("caller") or "bridge"),
                    reason="clear_marker_phase2_missing",
                    reservation=reservation,
                )
            return True
        reservation["new_session_id"] = marker_new_session
        reservation["probe_turn_id"] = marker_turn_id
        reservation["probe_prompt_submitted"] = True
        reservation["phase"] = "prompt_seen"
        d.log(
            "clear_probe_prompt_seen",
            target=agent,
            probe_id=reservation.get("probe_id"),
            new_session_id=reservation.get("new_session_id"),
            probe_turn_id=reservation.get("probe_turn_id"),
        )
    else:
        d.log("clear_window_prompt_suppressed", target=agent, attach_probe=attach_probe, turn_id=record.get("turn_id"))
    d.busy[agent] = False
    d.reserved[agent] = None
    return True


def handle_clear_response_finished_locked(d, agent: str, record: dict) -> bool:
    reservation = d.clear_reservations.get(agent)
    if not reservation:
        return False
    response_turn_id = str(record.get("turn_id") or "")
    response_session_id = str(record.get("session_id") or "")
    probe_turn_id = str(reservation.get("probe_turn_id") or "")
    new_session_id = str(reservation.get("new_session_id") or "")
    matches_probe = bool(
        (probe_turn_id and response_turn_id == probe_turn_id)
        or (new_session_id and response_session_id == new_session_id)
    )
    if matches_probe or (d.dry_run and reservation.get("probe_prompt_submitted")):
        reservation["probe_response_finished"] = True
        reservation["phase"] = "probe_finished"
        if response_session_id:
            reservation["new_session_id"] = response_session_id
        if response_turn_id:
            reservation["probe_turn_id"] = response_turn_id
        condition = reservation.get("condition")
        if isinstance(condition, threading.Condition):
            condition.notify_all()
        d.log(
            "clear_probe_response_finished",
            target=agent,
            probe_id=reservation.get("probe_id"),
            new_session_id=reservation.get("new_session_id"),
            probe_turn_id=reservation.get("probe_turn_id"),
        )
    else:
        d.log(
            "clear_window_response_ignored",
            target=agent,
            response_turn_id=response_turn_id,
            response_session_id=response_session_id,
            probe_turn_id=probe_turn_id,
            new_session_id=new_session_id,
        )
    d.busy[agent] = False
    d.reserved[agent] = None
    return True


def promote_pending_self_clear_locked(d, sender: str) -> None:
    pending = d.pending_self_clears.pop(sender, None)
    if not pending:
        return
    force = bool(pending.get("force"))
    d.reload_participants()
    if sender not in d.participants:
        return
    guard = d.clear_guard(sender, force=force)
    if not guard.ok:
        notice = make_message(
            sender="bridge",
            target=sender,
            intent="self_clear_cancelled",
            body=f"[bridge:self_clear_cancelled] Deferred self-clear did not run: {format_clear_guard_result(guard)}.",
            causal_id=short_id("causal"),
            hop_count=0,
            auto_return=False,
            kind="notice",
            source="self_clear_cancelled",
        )

        def prepend(queue: list[dict]) -> None:
            queue.insert(0, notice)

        d.queue.update(prepend)
        d.log("self_clear_cancelled", target=sender, reason=format_clear_guard_result(guard))
        return
    participant = dict(d.participants.get(sender) or {})
    reservation = d._new_clear_reservation(str(pending.get("caller") or sender), sender, force=force, participant=participant)
    reservation["clear_id"] = str(pending.get("clear_id") or reservation.get("clear_id"))
    d.clear_reservations[sender] = reservation
    d.log("self_clear_promoted", target=sender, force=force, clear_id=reservation.get("clear_id"))
    threading.Thread(
        target=d._self_clear_worker,
        args=(sender, reservation),
        name=f"bridge-self-clear-{sender}",
        daemon=True,
    ).start()


def _self_clear_worker(d, target: str, reservation: dict) -> None:
    try:
        d.run_clear_peer(
            str(reservation.get("caller") or target),
            target,
            force=bool(reservation.get("force")),
            existing_reservation=reservation,
        )
    except Exception as exc:
        d.safe_log("self_clear_worker_failed", target=target, error=str(exc))
