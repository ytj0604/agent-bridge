#!/usr/bin/env python3
import time
import uuid
from datetime import datetime, timezone

from bridge_daemon_messages import build_peer_prompt, make_message, normalize_prompt_body_text
from bridge_daemon_watchdogs import WATCHDOG_PHASE_DELIVERY, WATCHDOG_PHASE_RESPONSE
from bridge_util import MAX_PEER_BODY_CHARS, RESTART_PRESERVED_INFLIGHT_KEY, normalize_kind, short_id, utc_now


PANE_MODE_FORCE_CANCEL_MODES = {"copy-mode", "copy-mode-vi", "view-mode"}
TMUX_DELIVERY_WORST_CASE_SECONDS = 20.0
PANE_MODE_METADATA_KEYS = (
    "pane_mode_blocked_since",
    "pane_mode_blocked_since_ts",
    "pane_mode_blocked_mode",
    "pane_mode_block_count",
    "pane_mode_unforceable_logged",
    "pane_mode_cancel_failed_logged",
    "pane_mode_probe_failed_logged",
    "last_pane_mode_cancel_error",
    "last_pane_mode_probe_error",
)
PANE_MODE_ENTER_DEFER_KEYS = (
    "pane_mode_enter_deferred_since",
    "pane_mode_enter_deferred_since_ts",
    "pane_mode_enter_deferred_mode",
)
RESTART_INFLIGHT_METADATA_KEYS = (
    RESTART_PRESERVED_INFLIGHT_KEY,
    "restart_preserved_inflight_at",
)


def pane_mode_block_since_ts(item: dict) -> float | None:
    raw_ts = item.get("pane_mode_blocked_since_ts")
    try:
        return float(raw_ts)
    except (TypeError, ValueError):
        pass
    raw_iso = item.get("pane_mode_blocked_since")
    if not raw_iso:
        return None
    try:
        return datetime.fromisoformat(str(raw_iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def try_deliver_command_aware(
    d,
    target: str | None = None,
    *,
    message_id: str = "",
    use_target_locks: bool = True,
) -> None:
    if target and not d.command_delivery_allowed(str(target), message_id):
        return
    if target is None and not d.command_delivery_allowed("", message_id):
        return
    try:
        if use_target_locks:
            d.try_deliver(target)
        else:
            d.try_deliver_legacy_state_locked(target)
    except Exception as exc:
        if exc.__class__.__name__ != "CommandLockWaitExceeded":
            raise
        remaining = d.command_remaining_budget()
        d.log(
            "command_delivery_deferred_lock_wait",
            command_class=getattr(exc, "command_class", "") or d.command_context_info().get("op") or "",
            message_id=message_id,
            target=str(target or ""),
            remaining_budget_ms=int((remaining or 0.0) * 1000),
        )


def next_pending_candidate(d, target: str) -> dict | None:
    if (
        d.busy.get(target)
        or d.reserved.get(target)
        or d.interrupt_partial_failure_blocks.get(target)
        or d.clear_reservations.get(target)
    ):
        return None

    def mutator(queue: list[dict]) -> dict | None:
        if any(item.get("to") == target and item.get("status") in {"inflight", "submitted", "delivered"} for item in queue):
            return None
        for item in queue:
            if item.get("to") == target and item.get("status") == "pending":
                return dict(item)
        return None

    return d.queue.update(mutator)


def annotate_pending_pane_mode_block(d, target: str, message_id: str, mode: str) -> dict | None:
    now_ts = time.time()
    now_iso = utc_now()

    def mutator(queue: list[dict]) -> dict | None:
        for item in queue:
            if item.get("id") != message_id or item.get("to") != target or item.get("status") != "pending":
                continue
            prior_mode = str(item.get("pane_mode_blocked_mode") or "")
            since_ts = pane_mode_block_since_ts(item)
            started = since_ts is None
            if since_ts is None:
                since_ts = now_ts
                item["pane_mode_blocked_since"] = now_iso
                item["pane_mode_blocked_since_ts"] = now_ts
            if prior_mode and prior_mode != mode:
                item.pop("pane_mode_unforceable_logged", None)
                item.pop("pane_mode_cancel_failed_logged", None)
            item.pop("pane_mode_probe_failed_logged", None)
            item.pop("last_pane_mode_probe_error", None)
            item["pane_mode_blocked_mode"] = mode
            item["pane_mode_block_count"] = int(item.get("pane_mode_block_count") or 0) + 1
            item["last_error"] = "pane_in_mode"
            item["updated_ts"] = now_iso
            result = dict(item)
            result["_pane_mode_started"] = started
            result["_pane_mode_blocked_age"] = max(0.0, now_ts - float(since_ts))
            return result
        return None

    return d.queue.update(mutator)


def clear_pane_mode_metadata(d, message_id: str) -> dict | None:
    def mutator(queue: list[dict]) -> dict | None:
        for item in queue:
            if item.get("id") != message_id:
                continue
            had_metadata = any(key in item for key in PANE_MODE_METADATA_KEYS)
            if not had_metadata:
                return None
            prior = dict(item)
            for key in PANE_MODE_METADATA_KEYS:
                item.pop(key, None)
            if item.get("last_error") in {"pane_in_mode", "pane_mode_unforceable", "pane_mode_cancel_failed", "pane_mode_probe_failed"}:
                item.pop("last_error", None)
            item["updated_ts"] = utc_now()
            return prior
        return None

    return d.queue.update(mutator)


def mark_pane_mode_unforceable(d, message_id: str) -> bool:
    def mutator(queue: list[dict]) -> bool:
        for item in queue:
            if item.get("id") != message_id:
                continue
            already = bool(item.get("pane_mode_unforceable_logged"))
            item["pane_mode_unforceable_logged"] = True
            item["last_error"] = "pane_mode_unforceable"
            item["updated_ts"] = utc_now()
            return not already
        return False

    return bool(d.queue.update(mutator))


def mark_pane_mode_cancel_failed(d, message_id: str, error: str) -> bool:
    def mutator(queue: list[dict]) -> bool:
        for item in queue:
            if item.get("id") != message_id:
                continue
            already = bool(item.get("pane_mode_cancel_failed_logged"))
            item["pane_mode_cancel_failed_logged"] = True
            item["last_error"] = "pane_mode_cancel_failed"
            item["last_pane_mode_cancel_error"] = error
            item["updated_ts"] = utc_now()
            return not already
        return False

    return bool(d.queue.update(mutator))


def mark_pane_mode_probe_failed(d, message_id: str, error: str) -> bool:
    def mutator(queue: list[dict]) -> bool:
        for item in queue:
            if item.get("id") != message_id:
                continue
            already = bool(item.get("pane_mode_probe_failed_logged"))
            item["pane_mode_probe_failed_logged"] = True
            item["last_error"] = "pane_mode_probe_failed"
            item["last_pane_mode_probe_error"] = error
            item["updated_ts"] = utc_now()
            return not already
        return False

    return bool(d.queue.update(mutator))


def defer_inflight_for_pane_mode_probe_failed(d, message: dict, error: str) -> dict | None:
    message_id = str(message.get("id") or "")
    target = str(message.get("to") or "")
    now_iso = utc_now()

    def mutator(queue: list[dict]) -> dict | None:
        for item in queue:
            if item.get("id") != message_id or item.get("to") != target or item.get("status") != "inflight":
                continue
            already = bool(item.get("pane_mode_probe_failed_logged"))
            item["status"] = "pending"
            item["nonce"] = None
            item["delivery_attempts"] = max(0, int(item.get("delivery_attempts") or 0) - 1)
            item["pane_mode_probe_failed_logged"] = True
            item["last_error"] = "pane_mode_probe_failed"
            item["last_pane_mode_probe_error"] = error
            item["updated_ts"] = now_iso
            result = dict(item)
            result["_pane_mode_probe_failed_first"] = not already
            return result
        return None

    result = d.queue.update(mutator)
    if result:
        d.cancel_watchdogs_for_message(message_id, reason="pane_mode_probe_failed", phase=WATCHDOG_PHASE_DELIVERY)
    return result


def blocked_duration(d, item: dict | None) -> float | None:
    if not item:
        return None
    since_ts = pane_mode_block_since_ts(item)
    if since_ts is None:
        return None
    return max(0.0, time.time() - since_ts)


def maybe_defer_for_pane_mode(d, target: str, pane: str, message: dict) -> bool:
    status = d.pane_mode_status(pane)
    if status.get("error"):
        if d.mark_pane_mode_probe_failed(str(message.get("id") or ""), str(status.get("error") or "")):
            d.safe_log(
                "pane_mode_probe_failed",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                error=status.get("error"),
                phase="pre_reserve",
            )
        return True
    if not status.get("in_mode"):
        cleared = d.clear_pane_mode_metadata(str(message.get("id") or ""))
        if cleared:
            d.log(
                "pane_mode_block_cleared",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                mode=cleared.get("pane_mode_blocked_mode"),
                blocked_duration_sec=d.blocked_duration(cleared),
            )
        return False

    mode = str(status.get("mode") or "")
    blocked = d.annotate_pending_pane_mode_block(target, str(message.get("id") or ""), mode)
    if not blocked:
        return True
    if blocked.get("_pane_mode_started"):
        d.log(
            "pane_mode_block_started",
            target=target,
            pane=pane,
            message_id=message.get("id"),
            mode=mode,
        )
    age = float(blocked.get("_pane_mode_blocked_age") or 0.0)
    if d.pane_mode_grace_seconds is None or age < d.pane_mode_grace_seconds:
        return True
    if mode not in PANE_MODE_FORCE_CANCEL_MODES:
        if d.mark_pane_mode_unforceable(str(message.get("id") or "")):
            d.log(
                "pane_mode_block_unforceable",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                mode=mode,
                blocked_duration_sec=age,
            )
        return True

    ok, error = d.force_cancel_pane_mode(pane, mode)
    if not ok:
        if d.mark_pane_mode_cancel_failed(str(message.get("id") or ""), error):
            d.log(
                "pane_mode_force_cancelled",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                mode=mode,
                blocked_duration_sec=age,
                success=False,
                error=error,
            )
        return True

    after = d.pane_mode_status(pane)
    if after.get("error"):
        error = str(after.get("error"))
        if d.mark_pane_mode_cancel_failed(str(message.get("id") or ""), error):
            d.log(
                "pane_mode_force_cancelled",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                mode=mode,
                blocked_duration_sec=age,
                success=False,
                error=error,
            )
        return True
    if after.get("in_mode"):
        error = f"pane still in mode {after.get('mode') or ''}".strip()
        if d.mark_pane_mode_cancel_failed(str(message.get("id") or ""), error):
            d.log(
                "pane_mode_force_cancelled",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                mode=mode,
                blocked_duration_sec=age,
                success=False,
                error=error,
            )
        return True

    d.clear_pane_mode_metadata(str(message.get("id") or ""))
    d.log(
        "pane_mode_force_cancelled",
        target=target,
        pane=pane,
        message_id=message.get("id"),
        mode=mode,
        blocked_duration_sec=age,
        success=True,
    )
    return False


def defer_inflight_for_pane_mode(d, message: dict, mode: str) -> dict | None:
    message_id = str(message.get("id") or "")
    target = str(message.get("to") or "")
    now_ts = time.time()
    now_iso = utc_now()

    def mutator(queue: list[dict]) -> dict | None:
        for item in queue:
            if item.get("id") != message_id or item.get("to") != target or item.get("status") != "inflight":
                continue
            since_ts = pane_mode_block_since_ts(item)
            started = since_ts is None
            if since_ts is None:
                since_ts = pane_mode_block_since_ts(message) or now_ts
                item["pane_mode_blocked_since"] = message.get("pane_mode_blocked_since") or now_iso
                item["pane_mode_blocked_since_ts"] = since_ts
            item["status"] = "pending"
            item["nonce"] = None
            item["pane_mode_blocked_mode"] = mode
            item["pane_mode_block_count"] = int(item.get("pane_mode_block_count") or 0) + 1
            item["delivery_attempts"] = max(0, int(item.get("delivery_attempts") or 0) - 1)
            item["last_error"] = "pane_in_mode"
            item["updated_ts"] = now_iso
            result = dict(item)
            result["_pane_mode_started"] = started
            result["_pane_mode_blocked_age"] = max(0.0, now_ts - float(since_ts))
            return result
        return None

    result = d.queue.update(mutator)
    if result:
        d.cancel_watchdogs_for_message(message_id, reason="pane_mode_deferred", phase=WATCHDOG_PHASE_DELIVERY)
    return result


def mark_enter_deferred_for_pane_mode(d, message_id: str, target: str, mode: str, error: str = "") -> dict | None:
    now_ts = time.time()
    now_iso = utc_now()

    def mutator(queue: list[dict]) -> dict | None:
        for item in queue:
            if item.get("id") != message_id or item.get("to") != target or item.get("status") != "inflight":
                continue
            started = "pane_mode_enter_deferred_since_ts" not in item
            if started:
                item["pane_mode_enter_deferred_since"] = now_iso
                item["pane_mode_enter_deferred_since_ts"] = now_ts
            item["pane_mode_enter_deferred_mode"] = mode
            if error:
                item["last_pane_mode_probe_error"] = error
                item["last_error"] = "pane_mode_probe_failed_waiting_enter"
            else:
                item.pop("last_pane_mode_probe_error", None)
                item["last_error"] = "pane_in_mode_waiting_enter"
            item["updated_ts"] = now_iso
            result = dict(item)
            result["_pane_mode_enter_defer_started"] = started
            return result
        return None

    return d.queue.update(mutator)


def clear_enter_deferred_metadata(d, message_id: str) -> None:
    def mutator(queue: list[dict]) -> None:
        for item in queue:
            if item.get("id") != message_id:
                continue
            for key in PANE_MODE_ENTER_DEFER_KEYS:
                item.pop(key, None)
            item.pop("last_pane_mode_probe_error", None)
            if item.get("last_error") in {"pane_in_mode_waiting_enter", "pane_mode_probe_failed_waiting_enter"}:
                item.pop("last_error", None)
            item["updated_ts"] = utc_now()

    d.queue.update(mutator)


def queue_message(d, message: dict, log_event: bool = True, deliver: bool = True) -> None:
    def mutator(queue: list[dict]) -> None:
        if not any(item.get("id") == message["id"] for item in queue):
            queue.append(message)

    d.queue.update(mutator)
    if log_event:
        d.log(
            "message_queued",
            message_id=message["id"],
            from_agent=message["from"],
            to=message["to"],
            kind=message.get("kind"),
            intent=message["intent"],
            causal_id=message["causal_id"],
            hop_count=message["hop_count"],
            auto_return=message["auto_return"],
            reply_to=message.get("reply_to"),
            aggregate_id=message.get("aggregate_id"),
            aggregate_expected=message.get("aggregate_expected"),
            source=message.get("source"),
        )
    if deliver:
        d.request_and_drain_delivery(
            str(message["to"]),
            message_id=str(message.get("id") or ""),
            command_aware=True,
            reason="queue_message",
        )


def reserve_next(d, target: str) -> dict | None:
    with d.state_lock:
        if (
            d.busy.get(target)
            or d.reserved.get(target)
            or d.interrupt_partial_failure_blocks.get(target)
            or d.clear_reservations.get(target)
        ):
            return None

        def mutator(queue: list[dict]) -> dict | None:
            # `delivered` is also a delivery blocker so that, even after a
            # daemon restart that wipes in-memory busy/reserved state, a
            # message that was already injected into the peer's pane is
            # not redelivered nor allowed to be overlaid by a fresh prompt
            # before its terminal response_finished is observed.
            if any(item.get("to") == target and item.get("status") in {"inflight", "submitted", "delivered"} for item in queue):
                return None
            for item in queue:
                if item.get("to") != target or item.get("status") != "pending":
                    continue
                now_iso = utc_now()
                item["status"] = "inflight"
                item["nonce"] = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
                item["inflight_ts"] = now_iso
                item["updated_ts"] = now_iso
                item["delivery_attempts"] = int(item.get("delivery_attempts") or 0) + 1
                return dict(item)
            return None

        message = d.queue.update(mutator)
        if message:
            d.arm_message_watchdog(message, WATCHDOG_PHASE_DELIVERY)
        return message


def _try_deliver_targets_locked(d, targets: list[str]) -> None:
    for agent in targets:
        if agent not in d.participants:
            continue
        candidate = d.next_pending_candidate(agent)
        if not candidate:
            continue
        pane = d.resolve_target_pane(agent)
        if pane and d.maybe_defer_for_pane_mode(agent, pane, candidate):
            continue
        message = d.reserve_next(agent)
        if not message:
            continue
        d.deliver_reserved(message)


def try_deliver(d, target: str | None = None, *, use_target_locks: bool = True) -> None:
    d.reload_participants()
    targets = [str(target)] if target else sorted(str(alias) for alias in d.participants)
    if use_target_locks:
        if d._state_lock_owned_by_current_thread():
            raise RuntimeError("target-locking delivery cannot run while state_lock is owned")
        with d.target_locks_for(targets):
            with d.state_lock:
                _try_deliver_targets_locked(d, targets)
        return
    with d.state_lock:
        _try_deliver_targets_locked(d, targets)


def deliver_reserved(d, message: dict) -> None:
    target = str(message["to"])
    nonce = str(message["nonce"])
    endpoint_detail = d.resolve_endpoint_detail(target, purpose="write")
    if not endpoint_detail.get("ok"):
        d.finalize_undeliverable_message(message, endpoint_detail, phase="pre_send")
        return
    pane = str(endpoint_detail.get("pane") or "")
    mode_status = d.pane_mode_status(pane)
    if mode_status.get("error"):
        deferred = d.defer_inflight_for_pane_mode_probe_failed(message, str(mode_status.get("error") or ""))
        if deferred and deferred.get("_pane_mode_probe_failed_first"):
            d.safe_log(
                "pane_mode_probe_failed",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                error=mode_status.get("error"),
                phase="pre_send",
            )
        return
    elif mode_status.get("in_mode"):
        mode = str(mode_status.get("mode") or "")
        blocked = d.defer_inflight_for_pane_mode(message, mode)
        if blocked and blocked.get("_pane_mode_started"):
            d.log(
                "pane_mode_block_started",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                mode=mode,
                phase="pre_send",
            )
        d.last_enter_ts.pop(str(message.get("id") or ""), None)
        return
    else:
        d.clear_pane_mode_metadata(str(message.get("id") or ""))

    d.reserved[target] = str(message["id"])
    d.remember_nonce(nonce, message)
    body_text = str(message.get("body") or "")
    normalized_body_text = normalize_prompt_body_text(body_text)
    if len(normalized_body_text) > MAX_PEER_BODY_CHARS:
        d.log(
            "body_truncated",
            message_id=message.get("id"),
            from_agent=message.get("from"),
            to=target,
            kind=message.get("kind"),
            intent=message.get("intent"),
            original_chars=len(body_text),
            normalized_chars=len(normalized_body_text),
            limit_chars=MAX_PEER_BODY_CHARS,
        )
    prompt = build_peer_prompt(message, nonce)
    enter_deferred = False

    try:
        if not d.dry_run:
            d.run_tmux_send_literal(
                pane,
                prompt,
                bridge_session=d.bridge_session,
                target_alias=target,
                message_id=str(message["id"]),
                nonce=nonce,
            )
            time.sleep(d.submit_delay)
            enter_status = d.pane_mode_status(pane)
            if enter_status.get("error"):
                enter_deferred = True
                error = str(enter_status.get("error") or "")
                defer_info = d.mark_enter_deferred_for_pane_mode(str(message["id"]), target, "probe_error", error=error)
                if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                    d.safe_log(
                        "pane_mode_probe_failed",
                        target=target,
                        pane=pane,
                        message_id=message.get("id"),
                        error=error,
                        phase="pre_enter",
                    )
            elif enter_status.get("in_mode"):
                enter_deferred = True
                mode = str(enter_status.get("mode") or "")
                defer_info = d.mark_enter_deferred_for_pane_mode(str(message["id"]), target, mode)
                if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                    d.log(
                        "enter_deferred_pane_mode",
                        message_id=message["id"],
                        to=target,
                        mode=mode,
                    )
            else:
                d.clear_enter_deferred_metadata(str(message["id"]))
                d.run_tmux_enter(pane)
    except Exception as exc:
        d.mark_message_pending(str(message["id"]), str(exc))
        d.reserved[target] = None
        d.discard_nonce(nonce)
        d.log(
            "message_delivery_failed",
            message_id=message["id"],
            to=target,
            nonce=nonce,
            error=str(exc),
        )
        return

    d.last_enter_ts[str(message["id"])] = time.time()
    d.log(
        "message_delivery_attempted",
        message_id=message["id"],
        from_agent=message["from"],
        to=target,
        kind=message.get("kind"),
        intent=message["intent"],
        nonce=nonce,
        causal_id=message["causal_id"],
        hop_count=message["hop_count"],
        auto_return=message["auto_return"],
        aggregate_id=message.get("aggregate_id"),
        dry_run=d.dry_run,
        enter_deferred=enter_deferred,
    )


def mark_message_pending(d, message_id: str, error: str | None = None) -> None:
    def mutator(queue: list[dict]) -> None:
        for item in queue:
            if item.get("id") != message_id:
                continue
            item["status"] = "pending"
            item["nonce"] = None
            item["updated_ts"] = utc_now()
            if error:
                item["last_error"] = error
                if not str(error).startswith("pane_"):
                    for key in PANE_MODE_METADATA_KEYS + PANE_MODE_ENTER_DEFER_KEYS:
                        item.pop(key, None)

    d.queue.update(mutator)
    d.cancel_watchdogs_for_message(message_id, reason="delivery_requeued", phase=WATCHDOG_PHASE_DELIVERY)


def mark_message_submitted(d, message_id: str) -> None:
    def mutator(queue: list[dict]) -> None:
        for item in queue:
            if item.get("id") != message_id:
                continue
            item["status"] = "submitted"
            item["updated_ts"] = utc_now()
            item.pop("last_error", None)

    d.queue.update(mutator)


def mark_message_delivered_by_id(d, agent: str, message_id: str) -> dict | None:
    """Mark message status=delivered using id+recipient as the primary key.

    v1.5.2 authoritative path. Verifies recipient and status before
    flipping state — never trust hook-extracted nonce alone.
    """
    with d.state_lock:
        def mutator(queue: list[dict]) -> dict | None:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                if item.get("to") != agent or item.get("status") != "inflight":
                    return None
                now_iso = utc_now()
                item["status"] = "delivered"
                item["delivered_ts"] = now_iso
                item["updated_ts"] = now_iso
                item.pop("last_error", None)
                for key in PANE_MODE_METADATA_KEYS + PANE_MODE_ENTER_DEFER_KEYS:
                    item.pop(key, None)
                for key in RESTART_INFLIGHT_METADATA_KEYS:
                    item.pop(key, None)
                return dict(item)
            return None

        message = d.queue.update(mutator)
        if message:
            d.cancel_watchdogs_for_message(str(message_id), reason="delivered", phase=WATCHDOG_PHASE_DELIVERY)
            d.arm_message_watchdog(message, WATCHDOG_PHASE_RESPONSE)
        return message


def remove_delivered_message(d, target: str, message_id: str) -> dict | None:
    def mutator(queue: list[dict]) -> dict | None:
        found = None
        kept = []
        for item in queue:
            if item.get("id") == message_id and item.get("to") == target:
                found = dict(item)
                continue
            kept.append(item)
        queue[:] = kept
        return found

    return d.queue.update(mutator)


def finalize_undeliverable_message(d, message: dict, endpoint_detail: dict, *, phase: str) -> dict | None:
    message_id = str(message.get("id") or "")
    target = str(message.get("to") or "")
    if not message_id:
        return None

    def mutator(queue: list[dict]) -> dict | None:
        found = None
        kept = []
        for item in queue:
            if item.get("id") == message_id:
                found = dict(item)
                continue
            kept.append(item)
        queue[:] = kept
        return found

    removed = d.queue.update(mutator) or dict(message)
    nonce = str(removed.get("nonce") or message.get("nonce") or "")
    if nonce:
        d.discard_nonce(nonce)
    if d.reserved.get(target) == message_id:
        d.reserved[target] = None
    d.last_enter_ts.pop(message_id, None)
    active = d.current_prompt_by_agent.get(target) or {}
    if str(active.get("id") or "") == message_id:
        d.current_prompt_by_agent.pop(target, None)
        d.busy[target] = False
    d.cancel_watchdogs_for_message(message_id, reason="endpoint_lost")
    d.suppress_pending_watchdog_wakes(ref_message_id=message_id, reason="endpoint_lost")
    d._record_message_tombstone(
        target,
        removed,
        by_sender="bridge",
        reason="endpoint_lost",
        suppress_late_hooks=False,
        prompt_submitted_seen=bool(removed.get("status") in {"submitted", "delivered"}),
    )

    reason = str(endpoint_detail.get("reason") or "endpoint_lost")
    probe_status = str(endpoint_detail.get("probe_status") or "")
    d.log(
        "message_undeliverable",
        message_id=message_id,
        from_agent=removed.get("from"),
        to=target,
        kind=removed.get("kind"),
        aggregate_id=removed.get("aggregate_id"),
        reason=reason,
        probe_status=probe_status,
        detail=endpoint_detail.get("detail"),
        phase=phase,
    )

    if removed.get("aggregate_id"):
        d._record_aggregate_interrupted_reply(removed, by_sender="bridge", reason="endpoint_lost")
        return removed

    kind = normalize_kind(removed.get("kind"), "request")
    requester = str(removed.get("from") or "")
    if kind == "request" and bool(removed.get("auto_return")) and requester and requester != "bridge" and requester in d.participants:
        body = (
            f"[bridge:undeliverable] Message {message_id} to {target} could not be delivered because "
            f"the target has no verified live endpoint ({reason}). The target may have exited, been killed, "
            "or the tmux pane may have been reused. If the agent was resumed in another pane, ask the human "
            "to submit any short prompt there so the bridge hook can re-attach it. Otherwise reattach/join "
            "the target, or remove it from the room."
        )
        result = make_message(
            sender="bridge",
            target=requester,
            intent="undeliverable_result",
            body=body,
            causal_id=str(removed.get("causal_id") or short_id("causal")),
            hop_count=int(removed.get("hop_count") or 0),
            auto_return=False,
            kind="result",
            reply_to=message_id,
            source="endpoint_lost",
        )
        d.queue_message(result)
    return removed


def retry_enter_for_inflight(d) -> None:
    now = time.time()
    with d.state_lock:
        for item in list(d.queue.read()):
            if item.get("status") != "inflight":
                continue
            msg_id = str(item.get("id") or "")
            if not msg_id:
                continue
            last = d.last_enter_ts.get(msg_id)
            enter_deferred = bool(item.get("pane_mode_enter_deferred_since_ts"))
            if last is None and not enter_deferred:
                continue
            if last is not None and now - last < 1.0:
                continue
            target = str(item.get("to") or "")
            if target in d.clear_reservations:
                continue
            endpoint_detail = d.resolve_endpoint_detail(target, purpose="write")
            if not endpoint_detail.get("ok"):
                d.finalize_undeliverable_message(item, endpoint_detail, phase="enter_retry")
                d.last_enter_ts.pop(msg_id, None)
                continue
            pane = str(endpoint_detail.get("pane") or "")
            mode_status = d.pane_mode_status(pane)
            if mode_status.get("error"):
                error = str(mode_status.get("error") or "")
                defer_info = d.mark_enter_deferred_for_pane_mode(msg_id, target, "probe_error", error=error)
                d.last_enter_ts[msg_id] = now
                if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                    d.safe_log(
                        "pane_mode_probe_failed",
                        target=target,
                        pane=pane,
                        message_id=msg_id,
                        error=error,
                        phase="enter_retry",
                    )
                continue
            elif mode_status.get("in_mode"):
                mode = str(mode_status.get("mode") or "")
                defer_info = d.mark_enter_deferred_for_pane_mode(msg_id, target, mode)
                d.last_enter_ts[msg_id] = now
                if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                    d.log(
                        "enter_retry_deferred_pane_mode",
                        message_id=msg_id,
                        to=target,
                        mode=mode,
                    )
                continue
            else:
                d.clear_enter_deferred_metadata(msg_id)
            try:
                if not d.dry_run:
                    d.run_tmux_enter(pane)
            except Exception as exc:
                d.safe_log(
                    "enter_retry_failed",
                    message_id=msg_id,
                    to=target,
                    error=str(exc),
                )
                continue
            d.last_enter_ts[msg_id] = now
            d.log(
                "enter_retry",
                message_id=msg_id,
                to=target,
                attempts=int(item.get("delivery_attempts") or 0),
            )
