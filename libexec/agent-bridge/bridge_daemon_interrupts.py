#!/usr/bin/env python3
import time

from bridge_daemon_watchdogs import WATCHDOG_PHASE_DELIVERY
from bridge_participants import PHYSICAL_AGENT_TYPES
from bridge_util import short_id, utc_now


INTERRUPT_KEY_DELAY_DEFAULT_SECONDS = 0.15
INTERRUPT_KEY_DELAY_MIN_SECONDS = 0.05
INTERRUPT_KEY_DELAY_MAX_SECONDS = 1.0
INTERRUPT_SEND_KEY_TIMEOUT_SECONDS = 1.0
CLAUDE_INTERRUPT_KEYS_DEFAULT = ("Escape", "C-c")
INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT = 16
INTERRUPTED_TOMBSTONE_TTL_SECONDS = 600.0


def _is_command_lock_wait_exceeded(exc: Exception) -> bool:
    return exc.__class__.__name__ == "CommandLockWaitExceeded"


def _prune_interrupted_turns(d, agent: str, now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    rows = list(d.interrupted_turns.get(agent) or [])
    kept: list[dict] = []
    for row in rows:
        try:
            interrupted_ts = float(row.get("interrupted_ts") or 0.0)
        except (TypeError, ValueError):
            interrupted_ts = 0.0
        if now - interrupted_ts > INTERRUPTED_TOMBSTONE_TTL_SECONDS:
            d.log(
                "interrupted_tombstone_pruned",
                target=agent,
                reason="ttl",
                message_id=row.get("message_id"),
                turn_id=row.get("turn_id"),
                nonce=row.get("nonce"),
            )
            continue
        kept.append(row)
    overflow = max(0, len(kept) - INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT)
    if overflow:
        for row in kept[:overflow]:
            d.log(
                "interrupted_tombstone_pruned",
                target=agent,
                reason="cap",
                message_id=row.get("message_id"),
                turn_id=row.get("turn_id"),
                nonce=row.get("nonce"),
            )
        kept = kept[overflow:]
    if kept:
        d.interrupted_turns[agent] = kept
    else:
        d.interrupted_turns.pop(agent, None)


def _prune_interrupted_turns_for_all(d) -> None:
    now = time.time()
    for agent in list(d.interrupted_turns.keys()):
        d._prune_interrupted_turns(agent, now)


def _find_message_tombstone(d, message_id: str) -> dict | None:
    if not message_id:
        return None
    d._prune_interrupted_turns_for_all()
    for target, rows in d.interrupted_turns.items():
        for row in rows or []:
            if str(row.get("message_id") or "") == message_id:
                found = dict(row)
                found.setdefault("target", target)
                return found
    return None


def _record_message_tombstone(
    d,
    target: str,
    message: dict,
    *,
    by_sender: str,
    reason: str,
    suppress_late_hooks: bool,
    prompt_submitted_seen: bool,
) -> dict | None:
    message_id = str(message.get("id") or "")
    if not message_id:
        return None
    now = time.time()
    d._prune_interrupted_turns(target, now)
    rows = d.interrupted_turns.setdefault(target, [])
    for row in rows:
        if str(row.get("message_id") or "") != message_id:
            continue
        row["reason"] = row.get("reason") or reason
        row["prior_sender"] = row.get("prior_sender") or str(message.get("from") or "")
        row["by_sender"] = row.get("by_sender") or by_sender
        row["target"] = row.get("target") or target
        row["suppress_late_hooks"] = bool(row.get("suppress_late_hooks", True) or suppress_late_hooks)
        row["prompt_submitted_seen"] = bool(row.get("prompt_submitted_seen") or prompt_submitted_seen)
        return row
    tombstone = {
        "message_id": message_id,
        "turn_id": str(message.get("turn_id") or ""),
        "nonce": str(message.get("nonce") or ""),
        "prior_sender": str(message.get("from") or ""),
        "by_sender": by_sender,
        "target": target,
        "cancelled_message_ids": [message_id] if reason == "cancelled_by_sender" else [],
        "interrupted_ts": now,
        "prompt_submitted_seen": bool(prompt_submitted_seen),
        "superseded_by_prompt": False,
        "reason": reason,
        "suppress_late_hooks": bool(suppress_late_hooks),
    }
    rows.append(tombstone)
    d._prune_interrupted_turns(target, now)
    d.log(
        "message_tombstone_recorded",
        target=target,
        message_id=message_id,
        nonce=tombstone.get("nonce"),
        prompt_submitted_seen=tombstone.get("prompt_submitted_seen"),
        by_sender=by_sender,
        reason=reason,
        suppress_late_hooks=suppress_late_hooks,
    )
    return tombstone


def _record_interrupted_turns(d, target: str, active_context: dict, cancelled: list[dict], by_sender: str) -> list[dict]:
    now = time.time()
    cancelled_ids = [str(cm.get("id") or "") for cm in cancelled if cm.get("id")]
    by_id = {str(cm.get("id") or ""): dict(cm) for cm in cancelled if cm.get("id")}
    active_id = str(active_context.get("id") or "")
    if active_id and active_id not in by_id:
        by_id[active_id] = {}
    recorded: list[dict] = []
    for message_id, row in by_id.items():
        if not message_id:
            continue
        is_active = bool(active_id and message_id == active_id)
        status = str(row.get("status") or "")
        prompt_submitted_seen = bool(is_active and active_context.get("id")) or status in {"delivered", "submitted"}
        tombstone = {
            "message_id": message_id,
            "turn_id": str(active_context.get("turn_id") or "") if is_active else "",
            "nonce": str(row.get("nonce") or active_context.get("nonce") or ""),
            "prior_sender": str(row.get("from") or active_context.get("from") or ""),
            "by_sender": by_sender,
            "cancelled_message_ids": cancelled_ids,
            "interrupted_ts": now,
            "prompt_submitted_seen": prompt_submitted_seen,
            "superseded_by_prompt": False,
            "reason": "interrupted",
            "target": target,
            "suppress_late_hooks": True,
        }
        recorded.append(tombstone)
    if not recorded and active_context.get("id"):
        recorded.append({
            "message_id": str(active_context.get("id") or ""),
            "turn_id": str(active_context.get("turn_id") or ""),
            "nonce": str(active_context.get("nonce") or ""),
            "prior_sender": str(active_context.get("from") or ""),
            "by_sender": by_sender,
            "cancelled_message_ids": cancelled_ids,
            "interrupted_ts": now,
            "prompt_submitted_seen": True,
            "superseded_by_prompt": False,
            "reason": "interrupted",
            "target": target,
            "suppress_late_hooks": True,
        })
    if recorded:
        d.interrupted_turns.setdefault(target, []).extend(recorded)
        d._prune_interrupted_turns(target, now)
        for row in recorded:
            d.log(
                "interrupted_tombstone_recorded",
                target=target,
                message_id=row.get("message_id"),
                turn_id=row.get("turn_id"),
                nonce=row.get("nonce"),
                prompt_submitted_seen=row.get("prompt_submitted_seen"),
                by_sender=by_sender,
            )
    return recorded


def _match_interrupted_prompt(d, agent: str, observed_nonce: object, record_turn_id: object) -> dict | None:
    d._prune_interrupted_turns(agent)
    nonce = str(observed_nonce or "")
    turn_id = str(record_turn_id or "")
    if not nonce and not turn_id:
        return None
    for row in d.interrupted_turns.get(agent) or []:
        if not bool(row.get("suppress_late_hooks", True)):
            continue
        row_nonce = str(row.get("nonce") or "")
        row_turn_id = str(row.get("turn_id") or "")
        if (nonce and row_nonce and nonce == row_nonce) or (turn_id and row_turn_id and turn_id == row_turn_id):
            return row
    return None


def _supersede_interrupted_no_turn_suppression(d, agent: str, *, turn_id: object = None, message_id: object = None) -> None:
    rows = d.interrupted_turns.get(agent) or []
    changed = 0
    for row in rows:
        if bool(row.get("suppress_late_hooks", True)) and not row.get("superseded_by_prompt"):
            row["superseded_by_prompt"] = True
            changed += 1
    if changed:
        d.log(
            "interrupted_tombstones_superseded_by_prompt",
            target=agent,
            count=changed,
            turn_id=turn_id,
            message_id=message_id,
        )


def _pop_interrupted_tombstone(d, agent: str, row: dict) -> None:
    rows = list(d.interrupted_turns.get(agent) or [])
    rows = [item for item in rows if item is not row]
    if rows:
        d.interrupted_turns[agent] = rows
    else:
        d.interrupted_turns.pop(agent, None)


def _match_interrupted_response(d, agent: str, response_turn_id: object, has_current_context: bool) -> tuple[dict | None, str]:
    d._prune_interrupted_turns(agent)
    rows = [row for row in list(d.interrupted_turns.get(agent) or []) if bool(row.get("suppress_late_hooks", True))]
    turn_id = str(response_turn_id or "")
    if turn_id:
        for row in rows:
            row_turn_id = str(row.get("turn_id") or "")
            if row_turn_id and row_turn_id == turn_id:
                return row, "interrupted_drain"
        return None, ""
    if not rows:
        return None, ""
    if not has_current_context:
        for row in rows:
            if row.get("prompt_submitted_seen"):
                return row, "interrupted_drain_no_context"
        return None, "interrupted_drain_no_context_unmatched"
    for row in rows:
        if row.get("prompt_submitted_seen") and not row.get("superseded_by_prompt"):
            return row, "interrupted_drain_no_turn_after_prompt_submit"
    return None, "interrupted_drain_ambiguous_no_turn"


def target_has_active_interrupt_work(d, target: str, active_context: dict | None = None) -> bool:
    if active_context and active_context.get("id"):
        return True
    if d.interrupt_partial_failure_blocks.get(target):
        return True
    if d.reserved.get(target):
        return True
    active_statuses = {"inflight", "submitted", "delivered"}
    for item in d.queue.read():
        if item.get("to") == target and item.get("status") in active_statuses:
            return True
    return False


def _lock_wait_response(command_class: str, *, interrupt_shape: bool = False) -> dict:
    if not interrupt_shape:
        return {"error": "lock_wait_exceeded", "error_kind": "lock_wait_exceeded", "_lock_wait_exceeded": True}
    return {
        "esc_sent": False,
        "esc_error": "lock_wait_exceeded",
        "interrupt_ok": False,
        "interrupt_keys": [],
        "cc_sent": None,
        "cc_error": None,
        "held": False,
        "cancelled_message_ids": [],
        "error_kind": "lock_wait_exceeded",
    }


def handle_interrupt(d, sender: str, target: str) -> dict:
    # Interrupt semantics:
    #   1. ESC fail-closed: if tmux send-keys fails, no state mutation.
    #   2. Cancel (not requeue) the active in-flight message and any
    #      delivered/inflight/submitted messages for this target.
    #      Pending replacement messages are allowed to deliver once the
    #      configured interrupt key sequence succeeds.
    #   3. Record interrupted-turn tombstones so identifiable late
    #      prompt_submitted / response_finished events from the
    #      cancelled turn are suppressed without blocking replacements.
    #   4. Aggregate-bearing cancelled messages get a synthetic
    #      "[interrupted]" reply recorded into the aggregate, so the
    #      aggregate can still complete from the remaining peers'
    #      replies (and so the aggregate watchdog isn't dropped).
    # IMPORTANT: pane resolve, key dispatch, and state mutation all run
    # inside state_lock. Otherwise a prompt_submitted/response_finished
    # event or queued replacement delivery could interleave between ESC
    # and Claude's follow-up C-c, causing stale ctx mutation or clearing
    # a fresh replacement prompt. The bounded key delay is the v1
    # correctness/perf trade-off and is documented in the lock ordering
    # comment in __init__.
    default_post_lock, default_margin = d.command_budget("interrupt")
    try:
        d.reload_participants()
        lock_ctx = d.command_state_lock(
            post_lock_worst_case=INTERRUPT_SEND_KEY_TIMEOUT_SECONDS,
            margin=default_margin,
            command_class="interrupt",
        )
        lock_ctx.__enter__()
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        return _lock_wait_response("interrupt", interrupt_shape=True)
    try:
        if not d.command_deadline_ok(
            post_lock_worst_case=default_post_lock,
            margin=default_margin,
            command_class="interrupt",
        ):
            return _lock_wait_response("interrupt", interrupt_shape=True)
        endpoint_detail = d.resolve_endpoint_detail(target, purpose="write")
        pane = str(endpoint_detail.get("pane") or "") if endpoint_detail.get("ok") else ""
        if not pane:
            reason = str(endpoint_detail.get("reason") or "no_pane")
            finalized: list[dict] = []
            if endpoint_detail.get("should_detach") or endpoint_detail.get("probe_status") == "mismatch":
                for item in list(d.queue.read()):
                    if item.get("to") == target and item.get("status") in {"delivered", "inflight", "submitted"}:
                        removed = d.finalize_undeliverable_message(item, endpoint_detail, phase="interrupt_endpoint_lost")
                        if removed:
                            finalized.append(removed)
                d.busy[target] = False
                d.reserved[target] = None
            d.log(
                "esc_failed",
                target=target,
                by_sender=sender,
                error=reason,
                probe_status=endpoint_detail.get("probe_status"),
                finalized_message_ids=[item.get("id") for item in finalized],
            )
            return {
                "esc_sent": False,
                "esc_error": reason,
                "interrupt_ok": False,
                "interrupt_keys": [],
                "cc_sent": None,
                "cc_error": None,
                "held": False,
                "cancelled_message_ids": [item.get("id") for item in finalized],
            }
        participant = d.participants.get(target) or {}
        agent_type = str(participant.get("agent_type") or "")
        if agent_type not in PHYSICAL_AGENT_TYPES:
            d.safe_log("interrupt_unknown_agent_type", target=target, agent_type=agent_type)

        active_context_before = d.current_prompt_by_agent.get(target) or {}
        had_active = d.target_has_active_interrupt_work(target, active_context_before)

        interrupt_keys: list[str] = ["Escape"]
        if not d.command_deadline_ok(
            post_lock_worst_case=INTERRUPT_SEND_KEY_TIMEOUT_SECONDS,
            margin=default_margin,
            command_class="interrupt",
        ):
            return _lock_wait_response("interrupt", interrupt_shape=True)
        esc_ok, esc_error = d.send_interrupt_key(pane, "Escape")
        if not esc_ok:
            d.log("esc_failed", target=target, by_sender=sender, error=esc_error)
            return {
                "esc_sent": False,
                "esc_error": esc_error,
                "interrupt_ok": False,
                "interrupt_keys": interrupt_keys,
                "cc_sent": None,
                "cc_error": None,
                "held": False,
                "cancelled_message_ids": [],
            }

        active_context = d.current_prompt_by_agent.pop(target, {}) or {}
        cancelled = d._cancel_active_messages_for_target(
            target,
            active_context=active_context,
            reason="interrupted",
            by_sender=sender,
            cancel_statuses={"delivered", "inflight", "submitted"},
            notify_sources=True,
        )

        tombstones = d._record_interrupted_turns(target, active_context, cancelled, sender)
        prior_active_message_id = active_context.get("id")
        should_send_cc = (
            agent_type == "claude"
            and "C-c" in d.claude_interrupt_keys
            and had_active
        )
        cc_sent: bool | None = None
        cc_error: str | None = None
        if should_send_cc:
            if not d.dry_run and d.interrupt_key_delay_seconds > 0:
                time.sleep(d.interrupt_key_delay_seconds)
            interrupt_keys.append("C-c")
            if not d.command_deadline_ok(
                post_lock_worst_case=INTERRUPT_SEND_KEY_TIMEOUT_SECONDS,
                margin=default_margin,
                command_class="interrupt",
            ):
                cc_sent = False
                cc_error = "lock_wait_exceeded"
                d.log("cc_send_failed", target=target, by_sender=sender, error=cc_error)
            else:
                cc_ok, cc_error_text = d.send_interrupt_key(pane, "C-c")
                cc_sent = cc_ok
                if not cc_ok:
                    cc_error = cc_error_text
                    d.log("cc_send_failed", target=target, by_sender=sender, error=cc_error)
        interrupt_ok = cc_sent is not False
        d.busy[target] = False
        d.reserved[target] = None
        if interrupt_ok:
            prior_block = d.interrupt_partial_failure_blocks.pop(target, None)
            if prior_block:
                d.log(
                    "interrupt_partial_failure_block_cleared",
                    target=target,
                    by_sender=sender,
                    reason="interrupt_completed",
                    prior_error=prior_block.get("cc_error"),
                )
        else:
            block_info = {
                "since": utc_now(),
                "since_ts": time.time(),
                "by_sender": sender,
                "agent_type": agent_type,
                "prior_message_id": prior_active_message_id,
                "cancelled_message_ids": [cm.get("id") for cm in cancelled],
                "cc_error": cc_error,
                "reason": "interrupt_key_sequence_failed",
            }
            d.interrupt_partial_failure_blocks[target] = block_info
            d.log(
                "interrupt_partial_failure_blocked",
                target=target,
                by_sender=sender,
                prior_message_id=prior_active_message_id,
                cc_error=cc_error,
            )
        d.log(
            "interrupt_replacement_unblocked" if interrupt_ok else "interrupt_replacement_blocked",
            target=target,
            by_sender=sender,
            prior_message_id=prior_active_message_id,
            prior_sender=active_context.get("from"),
            cancelled_count=len(cancelled),
            cancelled_message_ids=[cm.get("id") for cm in cancelled],
            tombstone_count=len(tombstones),
        )
        d.log(
            "interrupt_keys_sent",
            target=target,
            by_sender=sender,
            agent_type=agent_type,
            keys=interrupt_keys,
            delay_sec=d.interrupt_key_delay_seconds if should_send_cc else 0.0,
            had_active=had_active,
            interrupt_ok=interrupt_ok,
            cc_sent=cc_sent,
        )
    finally:
        lock_ctx.__exit__(None, None, None)

    # Kick delivery to the interrupted target so queued corrections run
    # promptly. If the configured key sequence only partially completed,
    # leave queued work pending rather than appending to a possibly dirty
    # prompt buffer.
    if interrupt_ok:
        d.try_deliver_command_aware(target)
    else:
        d.log(
            "interrupt_delivery_skipped",
            target=target,
            by_sender=sender,
            reason="interrupt_key_sequence_failed",
        )
    return {
        "esc_sent": True,
        "esc_error": None,
        "interrupt_ok": interrupt_ok,
        "interrupt_keys": interrupt_keys,
        "cc_sent": cc_sent,
        "cc_error": cc_error,
        "cancelled_message_ids": [cm.get("id") for cm in cancelled],
        "prior_active_message_id": prior_active_message_id,
        "held": False,
    }


def _build_interrupt_notice(
    d,
    recipient: str,
    target: str,
    by_sender: str,
    cancelled: list[dict],
    active_context: dict,
    *,
    reason: str,
) -> dict:
    cancelled_ids = ", ".join(str(cm.get("id") or "") for cm in cancelled if cm.get("id")) or "(none)"
    if reason == "prompt_intercepted":
        body = (
            f"[bridge:interrupted] Your active message to {target} was cancelled because "
            f"{target} started a new prompt before responding. The peer is processing "
            "that new prompt now; pending messages will continue to deliver normally. "
            f"Affected message_ids: {cancelled_ids}."
        )
    else:
        body = (
            f"[bridge:interrupted] Your active message to {target} was cancelled by "
            f"{by_sender or 'bridge'}. Pending messages to that peer may continue after "
            "ESC succeeds. Identifiable late output from the interrupted turn will be "
            f"ignored. Affected message_ids: {cancelled_ids}."
        )
    return {
        "id": short_id("msg"),
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": "bridge",
        "to": recipient,
        "kind": "notice",
        "intent": "interrupt_notice",
        "body": body,
        "causal_id": short_id("causal"),
        "hop_count": 0,
        "auto_return": False,
        "reply_to": active_context.get("id"),
        "source": "interrupt_notice",
        "bridge_session": d.bridge_session,
        "status": "pending",
        "nonce": None,
        "delivery_attempts": 0,
    }


def release_hold(d, target: str, reason: str, by_sender: str | None = None) -> dict | None:
    try:
        lock_ctx = d.command_state_lock(command_class="clear_hold")
        lock_ctx.__enter__()
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        return {"error": "lock_wait_exceeded", "error_kind": "lock_wait_exceeded", "_lock_wait_exceeded": True}
    try:
        info = d.held_interrupt.pop(target, None)
        partial_block = d.interrupt_partial_failure_blocks.pop(target, None)
    finally:
        lock_ctx.__exit__(None, None, None)
    had_held = info is not None
    if not info and not partial_block:
        return None
    if partial_block:
        if info:
            info = {**info, "interrupt_partial_failure": partial_block}
        else:
            info = {
                "reason": "interrupt_partial_failure",
                "target": target,
                "prior_message_id": partial_block.get("prior_message_id"),
                "since_ts": partial_block.get("since_ts"),
                "interrupt_partial_failure": partial_block,
            }
    hold_duration_ms = None
    since_ts = info.get("since_ts")
    if isinstance(since_ts, (int, float)):
        hold_duration_ms = int(max(0.0, time.time() - float(since_ts)) * 1000)
    if partial_block and not had_held and reason.startswith("manual_clear"):
        event_name = "interrupt_partial_failure_block_cleared"
    else:
        event_name = "hold_force_resumed" if reason.startswith("manual_clear") else "hold_released"
    d.log(
        event_name,
        target=target,
        reason=reason,
        by_sender=by_sender,
        prior_message_id=info.get("prior_message_id"),
        partial_cc_error=(partial_block or {}).get("cc_error"),
        hold_duration_ms=hold_duration_ms,
    )
    d.try_deliver_command_aware(target)
    d.try_deliver_command_aware()
    return info
