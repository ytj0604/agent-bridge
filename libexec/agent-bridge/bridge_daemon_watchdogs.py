#!/usr/bin/env python3
import math
import re
import time
from datetime import datetime, timedelta, timezone

from bridge_util import MAX_PEER_BODY_CHARS, normalize_kind, short_id, utc_now


WATCHDOG_PHASE_DELIVERY = "delivery"
WATCHDOG_PHASE_RESPONSE = "response"
WATCHDOG_PHASE_ALARM = "alarm"
ALARM_CLIENT_WAKE_ID_RE = re.compile(r"^wake-[a-f0-9]{12}$")
ALARM_WAKE_TOMBSTONE_TTL_SECONDS = 60 * 60
ALARM_WAKE_TOMBSTONE_LIMIT = 4096
RESPONSE_LIKE_TOMBSTONE_REASONS = {
    "terminal_response",
    "reply_received",
    "interrupted_tombstone_terminal",
    "aggregate_complete",
}
EXTEND_WATCHDOG_HINTS = {
    "message_recently_responded": "A [bridge:result] may already be queued or arriving; do not keep extending this id.",
    "message_already_terminal": "No active watchdog remains; do not retry this id.",
    "message_unknown": "Verify the id; old or restart-lost ids cannot be extended.",
    "message_not_found": (
        "A [bridge:result] may already be queued or arriving; do not keep extending this id."
    ),
    "not_owner": "Only the original sender can extend; check the id you intended.",
    "aggregate_extend_not_supported": (
        "Per-message extend is not supported for delivered aggregate members; wait for the broadcast result."
    ),
    "watchdog_requires_auto_return": "This request has no automatic return route; the bridge cannot extend its watchdog.",
    "message_not_in_delivered_state": "Only inflight/submitted delivery and delivered response waits can be extended.",
    "message_not_extendable_state": "Only inflight/submitted delivery and delivered response waits can be extended.",
}
WATCHDOG_REQUIRES_AUTO_RETURN_ERROR = "watchdog_requires_auto_return"
WATCHDOG_REQUIRES_AUTO_RETURN_TEXT = (
    "watchdog requires auto_return; use --no-auto-return without --watchdog or set --watchdog 0"
)
ALARM_NOTICE_MAX_NOTES = 3
ALARM_NOTICE_NOTE_CHARS = 80
ALARM_NOTICE_TOTAL_CHARS = 400


def _is_command_lock_wait_exceeded(exc: Exception) -> bool:
    return exc.__class__.__name__ == "CommandLockWaitExceeded"


def _finite_watchdog_delay(d, message: dict) -> float | None:
    if "watchdog_delay_sec" not in message:
        return None
    try:
        delay = float(message.get("watchdog_delay_sec"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(delay):
        return None
    return delay


def _watchdog_strip_log_fields(d, message: dict, *, phase: str, delay: float, reason: str) -> dict:
    return {
        "message_id": message.get("id"),
        "from": message.get("from"),
        "from_agent": message.get("from"),
        "to": message.get("to"),
        "kind": message.get("kind"),
        "status": message.get("status"),
        "phase": phase,
        "watchdog_phase": phase,
        "delay_sec": delay,
        "watchdog_delay_sec": delay,
        "reason": reason,
    }


def _strip_no_auto_return_watchdog_metadata(d, message: dict, *, phase: str, reason: str) -> dict | None:
    if bool(message.get("auto_return")) or "watchdog_delay_sec" not in message:
        return None
    raw_value = message.pop("watchdog_delay_sec", None)
    try:
        delay = float(raw_value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(delay) and delay > 0:
        return d._watchdog_strip_log_fields(message, phase=phase, delay=delay, reason=reason)
    return None


def validate_enqueue_watchdog_metadata(d, message: dict) -> dict | None:
    delay = d._finite_watchdog_delay(message)
    if delay is None:
        return None
    if delay <= 0:
        message.pop("watchdog_delay_sec", None)
        return None
    if not bool(message.get("auto_return")):
        return {
            "ok": False,
            "error": WATCHDOG_REQUIRES_AUTO_RETURN_TEXT,
            "error_kind": WATCHDOG_REQUIRES_AUTO_RETURN_ERROR,
            "message_id": message.get("id"),
        }
    return None


def _maybe_cancel_alarms_for_incoming(d, message: dict) -> None:
    # Normalize kind defensively: malformed direct IPC could pass
    # arbitrary strings, but qualifying logic must be exact.
    kind = normalize_kind(message.get("kind"), "request")
    sender = str(message.get("from") or "")
    target = str(message.get("to") or "")
    if kind == "result":
        return
    if not target or not sender:
        return
    if sender == "bridge" or sender == target:
        return
    cancelled: list[tuple[str, dict]] = []
    for wake_id in list(d.watchdogs.keys()):
        wd = d.watchdogs.get(wake_id)
        if wd and wd.get("is_alarm") and wd.get("sender") == target:
            removed = d.watchdogs.pop(wake_id)
            d._record_alarm_wake_tombstone(wake_id, removed, "registered_then_cancelled")
            cancelled.append((wake_id, removed))
    if not cancelled:
        return
    notes: list[str] = []
    for _wake_id, wd in cancelled[:ALARM_NOTICE_MAX_NOTES]:
        note_raw = str(wd.get("alarm_body") or "").strip()
        if note_raw:
            if len(note_raw) > ALARM_NOTICE_NOTE_CHARS:
                note_raw = note_raw[: ALARM_NOTICE_NOTE_CHARS - 1] + "…"
            notes.append(f'"{note_raw}"')
        else:
            notes.append("(no note)")
    omitted = max(0, len(cancelled) - ALARM_NOTICE_MAX_NOTES)
    if omitted:
        notes.append(f"+{omitted} more")
    notice_text = (
        f"[bridge:alarm_cancelled] {len(cancelled)} alarm(s) cancelled by this incoming "
        f"message: {', '.join(notes)}. Re-arm with `agent_alarm <sec> --note '<desc>'` "
        "if this is NOT what you were waiting for."
    )
    if len(notice_text) > ALARM_NOTICE_TOTAL_CHARS:
        notice_text = notice_text[: ALARM_NOTICE_TOTAL_CHARS - 1] + "…"
    compact_notice_text = f"[bridge:alarm_cancelled] {len(cancelled)} alarm(s) cancelled by this message."
    original_body = str(message.get("body") or "")
    notice_truncated = False
    notice_omitted = False
    notice_compacted = False
    if original_body:
        # Normal external sends reserve headroom below this guard. Legacy
        # queue items and internal synthetic bodies may still be near the
        # guard, so compact or omit the notice instead of emitting garbage.
        max_notice_chars = MAX_PEER_BODY_CHARS - len(original_body) - 2
        if max_notice_chars <= 0:
            notice_text = ""
            notice_omitted = True
        elif len(notice_text) + 2 + len(original_body) > MAX_PEER_BODY_CHARS:
            if len(compact_notice_text) <= max_notice_chars:
                notice_text = compact_notice_text
                notice_compacted = True
            else:
                notice_text = ""
                notice_omitted = True
            notice_truncated = notice_omitted or notice_compacted
    message["body"] = f"{notice_text}\n\n{original_body}" if notice_text and original_body else (notice_text or original_body)
    d.log(
        "alarm_cancelled_by_message",
        target=target,
        trigger_message_id=message.get("id"),
        trigger_from=sender,
        trigger_kind=kind,
        cancelled_count=len(cancelled),
        cancelled_wake_ids=[wid for wid, _ in cancelled],
        notice_truncated=notice_truncated,
        notice_omitted=notice_omitted,
        notice_compacted=notice_compacted,
        original_body_chars=len(original_body),
    )


def suppress_pending_watchdog_wakes(
    d,
    *,
    ref_message_id: str | None = None,
    ref_aggregate_id: str | None = None,
    reason: str,
) -> list[dict]:
    message_id = str(ref_message_id or "")
    aggregate_id = str(ref_aggregate_id or "")
    if bool(message_id) == bool(aggregate_id):
        d.log(
            "watchdog_wake_suppression_skipped",
            reason="invalid_ref",
            ref_message_id=message_id,
            ref_aggregate_id=aggregate_id,
        )
        return []
    key = "ref_message_id" if message_id else "ref_aggregate_id"
    expected = message_id or aggregate_id

    def mutator(queue: list[dict]) -> list[dict]:
        removed: list[dict] = []
        kept: list[dict] = []
        for item in queue:
            if (
                item.get("from") == "bridge"
                and item.get("intent") == "watchdog_wake"
                and item.get("status") == "pending"
                and str(item.get(key) or "") == expected
            ):
                removed.append(dict(item))
                continue
            kept.append(item)
        queue[:] = kept
        return removed

    removed = d.queue.update(mutator)
    for item in removed:
        d.log(
            "watchdog_wake_suppressed",
            message_id=item.get("id"),
            ref_message_id=item.get("ref_message_id"),
            ref_aggregate_id=item.get("ref_aggregate_id"),
            reason=reason,
        )
    return removed


def arm_message_watchdog(d, message: dict, phase: str) -> None:
    if message.get("watchdog_delay_sec") is None:
        return
    try:
        raw_delay = float(message["watchdog_delay_sec"])
    except (TypeError, ValueError):
        return
    if not math.isfinite(raw_delay) or raw_delay <= 0:
        return
    if not bool(message.get("auto_return")):
        message_id = str(message.get("id") or "")
        stripped_log = d._watchdog_strip_log_fields(
            message,
            phase=phase,
            delay=raw_delay,
            reason="arm_guard",
        )

        def strip_mutator(queue: list[dict]) -> None:
            for item in queue:
                if item.get("id") == message_id and not bool(item.get("auto_return")):
                    item.pop("watchdog_delay_sec", None)
                    return

        if message_id:
            d.queue.update(strip_mutator)
        d.log("watchdog_stripped_no_auto_return", **stripped_log)
        return
    deadline = datetime.now(timezone.utc) + timedelta(seconds=raw_delay)
    arm_msg = dict(message)
    arm_msg["watchdog_phase"] = phase
    arm_msg["watchdog_phase_started_ts"] = (
        message.get("inflight_ts")
        if phase == WATCHDOG_PHASE_DELIVERY
        else message.get("delivered_ts")
    ) or message.get("updated_ts") or message.get("created_ts")
    arm_msg["watchdog_at"] = deadline.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    d.register_watchdog(arm_msg)


def tombstone_extend_error(d, tombstone: dict) -> str:
    reason = str(tombstone.get("reason") or "")
    if reason in RESPONSE_LIKE_TOMBSTONE_REASONS:
        return "message_recently_responded"
    return "message_already_terminal"


def extend_watchdog_error_hint(d, error: str | None) -> str:
    return EXTEND_WATCHDOG_HINTS.get(str(error or ""), "")


def upsert_message_watchdog(d, sender: str, message_id: str, additional_sec: float) -> tuple[bool, str | None, str | None]:
    # All validation + upsert inside a single state_lock acquire so the
    # checks and the registration cannot interleave with delivery,
    # cancellation, or termination of the same message.
    # Returns (ok, error_code, new_deadline_iso).
    post_lock_worst_case, margin = d.command_budget("extend_watchdog")
    try:
        lock_ctx = d.command_state_lock(
            post_lock_worst_case=post_lock_worst_case,
            margin=margin,
            command_class="extend_watchdog",
        )
        lock_ctx.__enter__()
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        return False, "lock_wait_exceeded", None
    try:
        if not d.command_deadline_ok(
            post_lock_worst_case=post_lock_worst_case,
            margin=margin,
            command_class="extend_watchdog",
        ):
            return False, "lock_wait_exceeded", None
        queue = list(d.queue.read())
        item = next((it for it in queue if it.get("id") == message_id), None)
        if not item:
            tombstone = d._find_message_tombstone(message_id)
            if tombstone:
                owner = str(tombstone.get("prior_sender") or "")
                if owner and owner != sender:
                    return False, "not_owner", None
                return False, d.tombstone_extend_error(tombstone), None
            return False, "message_unknown", None
        if str(item.get("from") or "") != sender:
            return False, "not_owner", None
        status = str(item.get("status") or "")
        if status not in {"inflight", "submitted", "delivered"}:
            return False, "message_not_extendable_state", None
        if not bool(item.get("auto_return")):
            return False, WATCHDOG_REQUIRES_AUTO_RETURN_ERROR, None
        if item.get("aggregate_id") and status == "delivered":
            return False, "aggregate_extend_not_supported", None
        try:
            additional_value = float(additional_sec)
        except (TypeError, ValueError):
            return False, "seconds_must_be_positive", None
        if not math.isfinite(additional_value) or additional_value <= 0:
            return False, "seconds_must_be_positive", None
        new_deadline = datetime.now(timezone.utc) + timedelta(seconds=additional_value)
        new_deadline_iso = new_deadline.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        phase = WATCHDOG_PHASE_RESPONSE if status == "delivered" else WATCHDOG_PHASE_DELIVERY
        arm_msg = dict(item)
        arm_msg["watchdog_phase"] = phase
        arm_msg["watchdog_phase_started_ts"] = (
            item.get("delivered_ts") if phase == WATCHDOG_PHASE_RESPONSE else item.get("inflight_ts")
        ) or item.get("updated_ts") or item.get("created_ts")
        arm_msg["watchdog_at"] = new_deadline_iso
        d.register_watchdog(arm_msg)
        d.log(
            "watchdog_extended",
            message_id=message_id,
            new_deadline=new_deadline_iso,
            additional_sec=additional_value,
            status=status,
            watchdog_phase=phase,
        )
        return True, None, new_deadline_iso
    finally:
        lock_ctx.__exit__(None, None, None)


def register_watchdog(d, message: dict) -> None:
    deadline_iso = message.get("watchdog_at")
    if not deadline_iso:
        return
    try:
        deadline = datetime.fromisoformat(str(deadline_iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        d.safe_log(
            "watchdog_register_failed",
            message_id=message.get("id"),
            watchdog_at=deadline_iso,
            error="invalid_watchdog_at",
        )
        return
    sender = str(message.get("from") or "")
    if not sender or sender == "bridge":
        return
    aggregate_id = message.get("aggregate_id")
    message_id = message.get("id")
    phase = d.normalize_watchdog_phase(message)
    with d.state_lock:
        if aggregate_id and phase == WATCHDOG_PHASE_RESPONSE:
            # Aggregate response watches are aggregate-wide: one wake tells
            # the requester that the broadcast has not completed.
            for existing in d.watchdogs.values():
                if (
                    existing
                    and existing.get("ref_aggregate_id") == aggregate_id
                    and d.normalize_watchdog_phase(existing) == WATCHDOG_PHASE_RESPONSE
                    and not existing.get("is_alarm")
                ):
                    return
        elif message_id:
            # Delivery watches are per leg, including aggregate legs. For
            # non-aggregate requests the response watch replaces the
            # delivery watch by message id.
            for wake_id_existing in list(d.watchdogs.keys()):
                existing = d.watchdogs.get(wake_id_existing)
                if not existing or existing.get("is_alarm") or existing.get("ref_message_id") != message_id:
                    continue
                existing_phase = d.normalize_watchdog_phase(existing)
                if aggregate_id and phase == WATCHDOG_PHASE_DELIVERY and existing_phase != WATCHDOG_PHASE_DELIVERY:
                    continue
                d.watchdogs.pop(wake_id_existing, None)
        wake_id = short_id("wake")
        d.watchdogs[wake_id] = {
            "sender": sender,
            "deadline": deadline,
            "watchdog_phase": phase,
            "ref_message_id": message.get("id"),
            "ref_aggregate_id": message.get("aggregate_id"),
            "ref_to": message.get("to"),
            "ref_kind": normalize_kind(message.get("kind"), "request"),
            "ref_intent": message.get("intent"),
            "ref_causal_id": message.get("causal_id"),
            "ref_aggregate_expected": list(message.get("aggregate_expected") or []),
            "ref_created_ts": message.get("created_ts"),
            "ref_phase_started_ts": message.get("watchdog_phase_started_ts"),
            "is_alarm": bool(message.get("alarm")),
        }
        d.log(
            "watchdog_registered",
            wake_id=wake_id,
            sender=sender,
            ref_message_id=message.get("id"),
            ref_aggregate_id=message.get("aggregate_id"),
            deadline=deadline_iso,
            kind=message.get("kind"),
            watchdog_phase=phase,
            is_alarm=bool(message.get("alarm")),
        )


def normalize_watchdog_phase(d, wd: dict) -> str:
    if wd.get("is_alarm") or wd.get("alarm"):
        return WATCHDOG_PHASE_ALARM
    raw = str(wd.get("watchdog_phase") or "")
    if raw in {WATCHDOG_PHASE_DELIVERY, WATCHDOG_PHASE_RESPONSE}:
        return raw
    return WATCHDOG_PHASE_RESPONSE


def _prune_alarm_wake_tombstones(d, now: float | None = None) -> None:
    now_value = time.time() if now is None else now
    for wake_id in list(d.alarm_wake_tombstones.keys()):
        try:
            expires_at = float(d.alarm_wake_tombstones[wake_id].get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at and expires_at > now_value:
            continue
        d.alarm_wake_tombstones.pop(wake_id, None)
    while len(d.alarm_wake_tombstones) > ALARM_WAKE_TOMBSTONE_LIMIT:
        d.alarm_wake_tombstones.popitem(last=False)


def _record_alarm_wake_tombstone(d, wake_id: str, wd: dict, status: str) -> None:
    if not ALARM_CLIENT_WAKE_ID_RE.fullmatch(str(wake_id or "")):
        return
    if not wd.get("is_alarm"):
        return
    now = time.time()
    d.alarm_wake_tombstones[str(wake_id)] = {
        "sender": str(wd.get("sender") or ""),
        "status": status,
        "alarm_body": str(wd.get("alarm_body") or ""),
        "alarm_delay_seconds": wd.get("alarm_delay_seconds"),
        "recorded_ts": now,
        "expires_at": now + ALARM_WAKE_TOMBSTONE_TTL_SECONDS,
    }
    d.alarm_wake_tombstones.move_to_end(str(wake_id))
    d._prune_alarm_wake_tombstones(now)


def _same_alarm_request(d, existing: dict, sender: str, delay: float, body: str | None) -> bool:
    if str(existing.get("sender") or "") != sender:
        return False
    if str(existing.get("alarm_body") or "") != (body or ""):
        return False
    try:
        existing_delay = float(existing.get("alarm_delay_seconds"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(existing_delay) and existing_delay == delay


def _alarm_conflict_reason(d, existing: dict, sender: str, delay: float, body: str | None) -> str:
    if existing.get("is_alarm") is False:
        return "existing_non_alarm"
    if str(existing.get("sender") or "") != sender:
        return "sender_mismatch"
    if str(existing.get("alarm_body") or "") != (body or ""):
        return "metadata_mismatch"
    try:
        existing_delay = float(existing.get("alarm_delay_seconds"))
    except (TypeError, ValueError):
        return "metadata_mismatch"
    if not math.isfinite(existing_delay) or existing_delay != delay:
        return "metadata_mismatch"
    return "metadata_mismatch"


def _log_alarm_register_conflict(d, wake_id: str, sender: str, existing: dict, reason: str) -> None:
    d.log(
        "alarm_register_conflict",
        wake_id=wake_id,
        sender=sender,
        reason=reason,
        existing_sender=str(existing.get("sender") or ""),
        existing_is_alarm=existing.get("is_alarm"),
        existing_status=existing.get("status"),
    )


def register_alarm_result(
    d,
    sender: str,
    delay_seconds: float,
    body: str | None = None,
    *,
    wake_id: str | None = None,
) -> dict:
    if not sender or sender == "bridge":
        return {"ok": False, "error": "invalid_sender"}
    try:
        delay = float(delay_seconds)
    except (TypeError, ValueError):
        return {"ok": False, "error": "delay_seconds must be a number"}
    if not math.isfinite(delay) or delay < 0:
        return {"ok": False, "error": "delay_seconds must be a finite non-negative number"}
    client_supplied_wake_id = wake_id is not None
    requested_wake_id = short_id("wake") if wake_id is None else wake_id
    if not ALARM_CLIENT_WAKE_ID_RE.fullmatch(requested_wake_id):
        return {"ok": False, "error": "wake_id must match wake-[a-f0-9]{12}"}
    deadline = time.time() + delay
    post_lock_worst_case, margin = d.command_budget("alarm")
    try:
        lock_ctx = d.command_state_lock(
            post_lock_worst_case=post_lock_worst_case,
            margin=margin,
            command_class="alarm",
        )
        lock_ctx.__enter__()
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        return d.lock_wait_exceeded_response("alarm")
    try:
        if not d.command_deadline_ok(
            post_lock_worst_case=post_lock_worst_case,
            margin=margin,
            command_class="alarm",
        ):
            return d.lock_wait_exceeded_response("alarm")
        d._prune_alarm_wake_tombstones()
        if not client_supplied_wake_id:
            while requested_wake_id in d.watchdogs or requested_wake_id in d.alarm_wake_tombstones:
                requested_wake_id = short_id("wake")
        existing = d.watchdogs.get(requested_wake_id)
        if existing:
            if existing.get("is_alarm") and d._same_alarm_request(existing, sender, delay, body):
                d.log(
                    "alarm_register_idempotent_replay",
                    wake_id=requested_wake_id,
                    sender=sender,
                    alarm_status="active",
                )
                return {"ok": True, "wake_id": requested_wake_id, "alarm_status": "active", "duplicate": True}
            d._log_alarm_register_conflict(
                requested_wake_id,
                sender,
                existing,
                d._alarm_conflict_reason(existing, sender, delay, body),
            )
            return {"ok": False, "error": "wake_id conflict"}
        tombstone = d.alarm_wake_tombstones.get(requested_wake_id)
        if tombstone:
            if d._same_alarm_request(tombstone, sender, delay, body):
                d.alarm_wake_tombstones.move_to_end(requested_wake_id)
                alarm_status = str(tombstone.get("status") or "removed")
                d.log(
                    "alarm_register_idempotent_replay",
                    wake_id=requested_wake_id,
                    sender=sender,
                    alarm_status=alarm_status,
                )
                return {"ok": True, "wake_id": requested_wake_id, "alarm_status": alarm_status, "duplicate": True}
            d._log_alarm_register_conflict(
                requested_wake_id,
                sender,
                tombstone,
                d._alarm_conflict_reason(tombstone, sender, delay, body),
            )
            return {"ok": False, "error": "wake_id conflict"}
        d.watchdogs[requested_wake_id] = {
            "sender": sender,
            "deadline": deadline,
            "watchdog_phase": WATCHDOG_PHASE_ALARM,
            "ref_message_id": None,
            "ref_aggregate_id": None,
            "ref_to": None,
            "ref_kind": "alarm",
            "ref_intent": "alarm",
            "ref_causal_id": None,
            "is_alarm": True,
            "alarm_body": body or "",
            "alarm_delay_seconds": delay,
        }
        d.log(
            "alarm_registered",
            wake_id=requested_wake_id,
            sender=sender,
            delay_seconds=delay,
        )
    finally:
        lock_ctx.__exit__(None, None, None)
    return {"ok": True, "wake_id": requested_wake_id, "alarm_status": "registered", "duplicate": False}


def register_alarm(d, sender: str, delay_seconds: float, body: str | None = None, wake_id: str | None = None) -> str | None:
    result = d.register_alarm_result(sender, delay_seconds, body, wake_id=wake_id)
    if not result.get("ok"):
        return None
    return str(result.get("wake_id") or "")


def check_watchdogs(d) -> None:
    with d.state_lock:
        if not d.watchdogs:
            return
        now = time.time()
        due = [(wake_id, wd) for wake_id, wd in list(d.watchdogs.items()) if now >= wd.get("deadline", now + 1)]
        for wake_id, wd in due:
            d.fire_watchdog(wake_id, wd)


def stamp_turn_id_mismatch_post_watchdog_unblock(d, wd: dict) -> None:
    if wd.get("is_alarm"):
        return
    if d.normalize_watchdog_phase(wd) != WATCHDOG_PHASE_RESPONSE:
        return
    try:
        deadline = float(wd.get("deadline"))
    except (TypeError, ValueError):
        return
    unblock_ts = deadline + max(0.0, float(d.turn_id_mismatch_post_watchdog_grace_seconds))
    message_id = str(wd.get("ref_message_id") or "")
    aggregate_id = str(wd.get("ref_aggregate_id") or "")
    for context in d.current_prompt_by_agent.values():
        if not isinstance(context, dict) or context.get("turn_id_mismatch_since_ts") is None:
            continue
        matches_message = bool(message_id and context.get("id") == message_id)
        matches_aggregate = bool(aggregate_id and context.get("aggregate_id") == aggregate_id)
        if not matches_message and not matches_aggregate:
            continue
        try:
            existing = float(context.get("turn_id_mismatch_post_watchdog_unblock_ts") or 0.0)
        except (TypeError, ValueError):
            existing = 0.0
        if unblock_ts > existing:
            context["turn_id_mismatch_post_watchdog_unblock_ts"] = unblock_ts


def build_watchdog_fire_text(d, wd: dict) -> str:
    if wd.get("is_alarm"):
        custom = str(wd.get("alarm_body") or "").strip()
        base = "[bridge:alarm] Wake elapsed."
        hint = "Re-arm via agent_alarm <sec> if still waiting."
        if custom:
            return f"{base} Note: {custom}. {hint}"
        return f"{base} {hint}"
    phase = d.normalize_watchdog_phase(wd)
    agg = wd.get("ref_aggregate_id")
    if agg and phase == WATCHDOG_PHASE_RESPONSE:
        return (
            f"[bridge:watchdog] aggregate {agg} has not completed within its deadline. "
            f"{d._aggregate_watchdog_progress_text(agg, wd)} "
            "Inspect with agent_view_peer or stop a slow peer via agent_interrupt_peer."
        )
    kind = str(wd.get("ref_kind") or "request")
    to = str(wd.get("ref_to") or "(unknown)")
    msg_id = wd.get("ref_message_id") or "(unknown)"
    item = d._lookup_queue_item(str(msg_id)) if msg_id != "(unknown)" else None
    elapsed_text = d._watchdog_elapsed_text(item, wd)
    if not item:
        return (
            f"[bridge:watchdog] {kind} {msg_id} to {to}: queue item missing "
            "(stale entry). No action required."
        )
    status = str(item.get("status") or "")
    if phase == WATCHDOG_PHASE_RESPONSE and status == "delivered":
        return (
            f"[bridge:watchdog] Your {kind} {msg_id} to {to} has been processing for "
            f"{elapsed_text} without a response. Choose ONE of:\n"
            f"  agent_extend_wait {msg_id} <sec>   (keep waiting on this same request)\n"
            f"  agent_interrupt_peer {to}          (cancel and stop the peer)\n"
            f"  agent_view_peer {to}               (inspect what the peer is doing)"
        )
    if phase == WATCHDOG_PHASE_DELIVERY and status in {"inflight", "submitted"}:
        return (
            f"[bridge:watchdog] Your {kind} {msg_id} to {to} has not completed bridge delivery for "
            f"{elapsed_text} (status={status}). Choose ONE of:\n"
            f"  agent_extend_wait {msg_id} <sec>   (keep waiting on this same delivery attempt)\n"
            f"  agent_interrupt_peer {to}          (cancel and stop the peer)\n"
            f"  agent_view_peer {to}               (inspect the peer pane)"
        )
    return (
        f"[bridge:watchdog] Your {kind} {msg_id} to {to} is in unexpected watchdog phase "
        f"{phase!r} with queue status={status} after {elapsed_text}. "
        f"Inspect with agent_interrupt_peer {to} --status or agent_view_peer {to}."
    )


def _lookup_queue_item(d, message_id: str) -> dict | None:
    if not message_id:
        return None
    for item in d.queue.read():
        if item.get("id") == message_id:
            return dict(item)
    return None


def _watchdog_elapsed_text(d, item: dict | None, wd: dict) -> str:
    ref_ts = None
    if item:
        phase = d.normalize_watchdog_phase(wd)
        if phase == WATCHDOG_PHASE_DELIVERY:
            ref_ts = item.get("inflight_ts") or item.get("updated_ts") or item.get("created_ts")
        elif phase == WATCHDOG_PHASE_RESPONSE:
            ref_ts = item.get("delivered_ts") or item.get("created_ts")
        else:
            ref_ts = item.get("created_ts")
    if not ref_ts:
        ref_ts = wd.get("ref_phase_started_ts") or wd.get("ref_created_ts")
    if not ref_ts:
        return "an unknown duration"
    try:
        origin = datetime.fromisoformat(str(ref_ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return "an unknown duration"
    delta = max(0.0, time.time() - origin)
    if delta < 60.0:
        return f"~{int(delta)}s"
    if delta < 3600.0:
        return f"~{int(delta / 60)}m{int(delta % 60)}s"
    return f"~{int(delta / 3600)}h{int((delta % 3600) / 60)}m"


def _aggregate_watchdog_progress_text(d, aggregate_id: str, wd: dict) -> str:
    try:
        agg = d.aggregates.get(aggregate_id)
        replies = agg.get("replies") or {}
        expected = agg.get("expected") or wd.get("ref_aggregate_expected") or []
    except Exception:
        replies = {}
        expected = wd.get("ref_aggregate_expected") or []
    got_count = len(replies)
    expected_count = len(expected)
    missing = sorted(set(expected) - set(replies.keys())) if expected else []
    elapsed = d._watchdog_elapsed_text(None, wd)
    text = f"Replied: {got_count}/{expected_count} after {elapsed}."
    if missing:
        text += f" Missing peers: {', '.join(missing)}."
    return text


def watchdog_fire_skip_reason(d, wd: dict) -> str:
    if wd.get("is_alarm"):
        return ""
    phase = d.normalize_watchdog_phase(wd)
    message_id = str(wd.get("ref_message_id") or "")
    aggregate_id = str(wd.get("ref_aggregate_id") or "")
    if phase == WATCHDOG_PHASE_DELIVERY:
        if not message_id:
            return "delivery_missing_message_id"
        item = d._lookup_queue_item(message_id)
        if not item:
            tombstone = d._find_message_tombstone(message_id)
            if tombstone:
                return f"delivery_message_terminal_{tombstone.get('reason') or 'unknown'}"
            return "delivery_message_missing"
        status = str(item.get("status") or "")
        if status not in {"inflight", "submitted"}:
            return f"delivery_status_{status or 'missing'}"
        return ""
    if phase == WATCHDOG_PHASE_RESPONSE and aggregate_id:
        try:
            aggregate = d.aggregates.get(aggregate_id)
        except Exception:
            aggregate = {}
        if aggregate and (aggregate.get("delivered") or aggregate.get("status") == "complete"):
            return "aggregate_complete"
        return ""
    if phase == WATCHDOG_PHASE_RESPONSE:
        if not message_id:
            return "response_missing_message_id"
        item = d._lookup_queue_item(message_id)
        if not item:
            tombstone = d._find_message_tombstone(message_id)
            if tombstone:
                return f"response_message_terminal_{tombstone.get('reason') or 'unknown'}"
            return "response_message_missing"
        status = str(item.get("status") or "")
        if status != "delivered":
            return f"response_status_{status or 'missing'}"
        return ""
    return f"unknown_phase_{phase}"


def fire_watchdog(d, wake_id: str, wd: dict) -> None:
    sender = str(wd.get("sender") or "")
    d.reload_participants()
    if not sender or sender not in d.participants:
        removed = d.watchdogs.pop(wake_id, None)
        if removed and removed.get("is_alarm"):
            d._record_alarm_wake_tombstone(wake_id, removed, "sender_inactive")
        d.log(
            "watchdog_fire_skipped",
            wake_id=wake_id,
            sender=sender,
            reason="sender_not_active",
        )
        return
    skip_reason = d.watchdog_fire_skip_reason(wd)
    if skip_reason:
        removed = d.watchdogs.pop(wake_id, None)
        if removed and removed.get("is_alarm"):
            d._record_alarm_wake_tombstone(wake_id, removed, f"skipped_{skip_reason}")
        d.log(
            "watchdog_skipped_stale",
            wake_id=wake_id,
            sender=sender,
            ref_message_id=wd.get("ref_message_id"),
            ref_aggregate_id=wd.get("ref_aggregate_id"),
            watchdog_phase=d.normalize_watchdog_phase(wd),
            reason=skip_reason,
        )
        return
    d.stamp_turn_id_mismatch_post_watchdog_unblock(wd)
    removed = d.watchdogs.pop(wake_id, None)
    if removed and removed.get("is_alarm"):
        d._record_alarm_wake_tombstone(wake_id, removed, "fired")
    body = d.build_watchdog_fire_text(wd)
    synthetic = {
        "id": short_id("msg"),
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": "bridge",
        "to": sender,
        "kind": "notice",
        "intent": "watchdog_wake",
        "body": body,
        "causal_id": wd.get("ref_causal_id") or short_id("causal"),
        "hop_count": 0,
        "auto_return": False,
        "reply_to": wd.get("ref_message_id"),
        "ref_message_id": wd.get("ref_message_id"),
        "ref_aggregate_id": wd.get("ref_aggregate_id"),
        "watchdog_phase": d.normalize_watchdog_phase(wd),
        "source": "watchdog_fire",
        "bridge_session": d.bridge_session,
        "status": "pending",
        "nonce": None,
        "delivery_attempts": 0,
    }
    d.queue_message(synthetic, log_event=True)
    d.log(
        "watchdog_fired",
        wake_id=wake_id,
        sender=sender,
        ref_message_id=wd.get("ref_message_id"),
        ref_aggregate_id=wd.get("ref_aggregate_id"),
        watchdog_phase=d.normalize_watchdog_phase(wd),
        is_alarm=bool(wd.get("is_alarm")),
        synthetic_message_id=synthetic["id"],
    )


def cancel_watchdogs_for_message(
    d,
    message_id: str | None,
    reason: str = "reply_received",
    *,
    phase: str | None = None,
) -> None:
    if not message_id:
        return
    for wake_id in list(d.watchdogs.keys()):
        wd = d.watchdogs.get(wake_id)
        if (
            wd
            and wd.get("ref_message_id") == message_id
            and not wd.get("is_alarm")
            and (phase is None or d.normalize_watchdog_phase(wd) == phase)
        ):
            d.watchdogs.pop(wake_id, None)
            d.log(
                "watchdog_cancelled",
                wake_id=wake_id,
                reason=reason,
                ref_message_id=message_id,
                watchdog_phase=d.normalize_watchdog_phase(wd),
            )


def cancel_watchdogs_for_aggregate(d, aggregate_id: str | None, reason: str = "aggregate_complete") -> None:
    if not aggregate_id:
        return
    for wake_id in list(d.watchdogs.keys()):
        wd = d.watchdogs.get(wake_id)
        if (
            wd
            and wd.get("ref_aggregate_id") == aggregate_id
            and d.normalize_watchdog_phase(wd) == WATCHDOG_PHASE_RESPONSE
            and not wd.get("is_alarm")
        ):
            d.watchdogs.pop(wake_id, None)
            d.log(
                "watchdog_cancelled",
                wake_id=wake_id,
                reason=reason,
                ref_aggregate_id=aggregate_id,
                watchdog_phase=d.normalize_watchdog_phase(wd),
            )
