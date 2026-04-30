#!/usr/bin/env python3
import hashlib
import json
import time

from bridge_daemon_messages import make_message
from bridge_util import normalize_kind, short_id, utc_now


EMPTY_RESPONSE_BODY = "(empty response)"


def find_inflight_candidate(d, agent: str) -> dict | None:
    """Pick the queue item that was just delivered to `agent`.

    Order of resolution:
      1. reserved[agent]: in-memory pointer set when prompt delivery was
         called and not yet cleared by the prompt_submitted that
         confirms submission. Verified against queue (must still be
         status=inflight and to=agent).
      2. queue scan for the unique item with to=agent and
         status=inflight. Covers daemon restart (in-memory reserved
         lost) and any path that bypassed reserved.

    Multiple inflight items for the same target indicate a queue
    invariant violation; we log and return None (fail-closed).

    Caller MUST hold state_lock.
    """
    queue = list(d.queue.read())
    msg_id = d.reserved.get(agent)
    if msg_id:
        for item in queue:
            if item.get("id") != msg_id:
                continue
            if item.get("to") != agent or item.get("status") != "inflight":
                # Stale reserved pointer: the message moved on (cancelled,
                # already delivered, etc.) but reserved was not cleared.
                break
            return dict(item)
    candidates = [
        item for item in queue
        if item.get("to") == agent and item.get("status") == "inflight"
    ]
    if len(candidates) == 1:
        return dict(candidates[0])
    if len(candidates) > 1:
        d.log(
            "ambiguous_inflight",
            agent=agent,
            count=len(candidates),
            message_ids=[c.get("id") for c in candidates],
        )
    return None


def handle_prompt_submitted(d, record: dict) -> None:
    d.reload_participants()
    agent = d.participant_alias(record)
    if agent not in d.participants:
        return

    intercepted = False
    suppressed_interrupted_prompt = False
    message: dict = {}
    with d.target_state_lock([agent]):
        if d.handle_clear_prompt_submitted_locked(agent, record):
            return
        observed_nonce = record.get("nonce")
        record_turn_id = record.get("turn_id")
        interrupted_prompt = d._match_interrupted_prompt(agent, observed_nonce, record_turn_id)
        if interrupted_prompt:
            interrupted_prompt["prompt_submitted_seen"] = True
            if record_turn_id and not interrupted_prompt.get("turn_id"):
                interrupted_prompt["turn_id"] = str(record_turn_id)
            suppressed_interrupted_prompt = True
            d.log(
                "interrupted_prompt_submitted_suppressed",
                agent=agent,
                nonce=observed_nonce,
                turn_id=record_turn_id,
                interrupted_message_id=interrupted_prompt.get("message_id"),
                superseded_by_prompt=interrupted_prompt.get("superseded_by_prompt"),
            )
        else:
            current_context = d.current_prompt_by_agent.get(agent, {}) or {}
            current_context_id = str(current_context.get("id") or "")
            current_context_turn_id = current_context.get("turn_id")
            duplicate_by_nonce = (
                observed_nonce
                and current_context.get("nonce")
                and observed_nonce == current_context.get("nonce")
                and record_turn_id == current_context_turn_id
            )
            duplicate_by_turn = False
            if current_context_id and record_turn_id is not None and record_turn_id == current_context_turn_id:
                duplicate_by_turn = any(
                    item.get("id") == current_context_id
                    and item.get("to") == agent
                    and item.get("status") == "delivered"
                    for item in d.queue.read()
                )
            if duplicate_by_nonce or duplicate_by_turn:
                d.log(
                    "duplicate_prompt_submitted",
                    agent=agent,
                    nonce=observed_nonce,
                    turn_id=record_turn_id,
                    existing_message_id=current_context.get("id"),
                    duplicate_match="nonce_turn" if duplicate_by_nonce else "turn_delivered",
                )
                return

            # v1.5.2: daemon state is the authoritative identity. Find the
            # message the daemon believes was just delivered to this agent;
            # the hook-extracted nonce is treated as a hint that must
            # cross-check against the candidate's nonce. Anything that
            # doesn't match cleanly is fail-closed (no delivered mutation,
            # treated as a user-typed prompt for ctx purposes).
            candidate = d.find_inflight_candidate(agent)

            if candidate:
                candidate_nonce = candidate.get("nonce")
                candidate_id = str(candidate.get("id") or "")
                if not observed_nonce:
                    # User typing collided with bridge inject (or hook
                    # payload arrived without the [bridge:nonce] prefix).
                    # Do NOT mark delivered. Leave the candidate in
                    # `inflight`; `requeue_stale_inflight` will revert it
                    # to `pending` after submit_timeout. Cancel the
                    # retry-enter loop now so the daemon doesn't spam
                    # `Enter` into a pane the human is typing in.
                    if candidate_id:
                        d.last_enter_ts.pop(candidate_id, None)
                    d.log(
                        "nonce_missing_for_candidate",
                        agent=agent,
                        candidate_message_id=candidate.get("id"),
                        candidate_nonce=candidate_nonce,
                    )
                elif candidate_nonce and observed_nonce != candidate_nonce:
                    if candidate_id:
                        d.last_enter_ts.pop(candidate_id, None)
                    d.log(
                        "nonce_mismatch",
                        agent=agent,
                        observed_nonce=observed_nonce,
                        candidate_message_id=candidate.get("id"),
                        candidate_nonce=candidate_nonce,
                    )
                else:
                    marked = d.mark_message_delivered_by_id(agent, str(candidate["id"]))
                    if marked:
                        message = marked
                    else:
                        # find_inflight_candidate already filtered by
                        # to=agent and status=inflight; reaching here means
                        # the queue moved between the two reads. Defensive
                        # log only.
                        if candidate_id:
                            d.last_enter_ts.pop(candidate_id, None)
                        d.log(
                            "delivery_recipient_mismatch",
                            agent=agent,
                            candidate_message_id=candidate.get("id"),
                            observed_nonce=observed_nonce,
                        )
            else:
                # No daemon-side candidate. This is a user-typed prompt.
                # An observed_nonce here means the user (or some quoted
                # text) included a [bridge:...] prefix — log for diagnostics
                # but do not bind the ctx to it.
                if observed_nonce:
                    d.log(
                        "orphan_nonce_in_user_prompt",
                        agent=agent,
                        observed_nonce=observed_nonce,
                    )

            if message.get("id"):
                d.last_enter_ts.pop(str(message["id"]), None)

            if not message.get("id"):
                queue_now = list(d.queue.read())
                delivered_rows = [
                    item for item in queue_now
                    if item.get("to") == agent and item.get("status") == "delivered"
                ]
                active_context = d.current_prompt_by_agent.get(agent, {}) or {}
                if active_context.get("id") or delivered_rows:
                    active_context = d.current_prompt_by_agent.pop(agent, {}) or {}
                    cancelled = d._cancel_active_messages_for_target(
                        agent,
                        active_context=active_context,
                        reason="prompt_intercepted",
                        by_sender="bridge",
                        cancel_statuses={"delivered"},
                        notify_sources=True,
                    )
                    intercepted = True
                    d.log(
                        "active_prompt_intercepted",
                        agent=agent,
                        prior_message_id=active_context.get("id"),
                        turn_id=record_turn_id,
                        reason="prompt_intercepted",
                        cancelled_message_ids=[cm.get("id") for cm in cancelled],
                        cancelled_count=len(cancelled),
                        observed_nonce_present=bool(observed_nonce),
                        candidate_message_id=candidate.get("id") if candidate else None,
                    )

            d._supersede_interrupted_no_turn_suppression(
                agent,
                turn_id=record_turn_id,
                message_id=message.get("id"),
            )
            d.busy[agent] = True
            d.reserved[agent] = None
            d.current_prompt_by_agent[agent] = {
                "id": message.get("id"),
                # ctx nonce comes from the matched message only; orphan
                # nonces from user prompts must NOT be stored here, or
                # discard_nonce at terminal time would clear unrelated
                # cache entries.
                "nonce": message.get("nonce"),
                "causal_id": message.get("causal_id"),
                "hop_count": int(message.get("hop_count") or 0),
                "from": message.get("from"),
                "kind": normalize_kind(message.get("kind"), "notice") if message else None,
                "intent": message.get("intent"),
                "auto_return": bool(message.get("auto_return")),
                "aggregate_id": message.get("aggregate_id"),
                "aggregate_expected": message.get("aggregate_expected"),
                "aggregate_message_ids": message.get("aggregate_message_ids"),
                "aggregate_mode": message.get("aggregate_mode"),
                "aggregate_started_ts": message.get("aggregate_started_ts"),
                "requester_cleared": bool(message.get("requester_cleared")),
                "requester_cleared_alias": message.get("requester_cleared_alias"),
                "requester_cleared_at": message.get("requester_cleared_at"),
                "requester_cleared_by": message.get("requester_cleared_by"),
                "turn_id": record.get("turn_id"),
            }

    if suppressed_interrupted_prompt:
        d.try_deliver(agent)
        return
    if message.get("id"):
        d.log(
            "message_delivered",
            message_id=message.get("id"),
            to=agent,
            nonce=message.get("nonce"),
            from_agent=message.get("from"),
            kind=message.get("kind"),
            intent=message.get("intent"),
            causal_id=message.get("causal_id"),
            hop_count=message.get("hop_count"),
            auto_return=message.get("auto_return"),
            aggregate_id=message.get("aggregate_id"),
        )
    if intercepted:
        d.try_deliver()


def response_fingerprint(d, record: dict) -> str:
    material = json.dumps(
        {
            "agent": record.get("agent"),
            "ts": record.get("ts"),
            "session_id": record.get("session_id"),
            "turn_id": record.get("turn_id"),
            "kind": "auto_return",
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def maybe_return_response(d, sender: str, text: str, context: dict) -> None:
    requester = context.get("from")
    d.reload_participants()
    if context.get("requester_cleared"):
        message_id = str(context.get("id") or "")
        aggregate_id = str(context.get("aggregate_id") or "")
        cleared = str(context.get("requester_cleared_alias") or requester or "")
        if message_id:
            d.suppress_pending_watchdog_wakes(ref_message_id=message_id, reason="requester_cleared")
            d.cancel_watchdogs_for_message(message_id, reason="requester_cleared")
        if aggregate_id:
            d.cancel_watchdogs_for_aggregate(aggregate_id, reason="requester_cleared")
            d.suppress_pending_watchdog_wakes(ref_aggregate_id=aggregate_id, reason="requester_cleared")
        body = (
            f"[bridge:requester_cleared] Requester {cleared or '(unknown)'} was cleared before "
            "your response could be returned. The bridge did not deliver this response back."
        )
        notice = make_message(
            sender="bridge",
            target=sender,
            intent="requester_cleared_response_notice",
            body=body,
            causal_id=str(context.get("causal_id") or short_id("causal")),
            hop_count=int(context.get("hop_count") or 0),
            auto_return=False,
            kind="notice",
            reply_to=message_id,
            source="requester_cleared",
        )
        d.queue_message(notice)
        d.log(
            "requester_cleared_response_suppressed",
            responder=sender,
            requester=requester,
            requester_cleared_alias=cleared,
            message_id=message_id,
            aggregate_id=aggregate_id,
        )
        return
    if requester not in d.participants or requester == sender:
        return
    if not context.get("auto_return"):
        return

    message_id = str(context.get("id") or "")
    if message_id:
        d.suppress_pending_watchdog_wakes(ref_message_id=message_id, reason="terminal_response")

    if context.get("aggregate_id"):
        d.collect_aggregate_response(sender, text, context)
        return

    causal_id = context.get("causal_id") or short_id("causal")
    hop_count = int(context.get("hop_count") or 0)
    original_intent = context.get("intent") or "message"
    return_intent = f"{original_intent}_result"
    response_text = text if text.strip() else EMPTY_RESPONSE_BODY
    body = (
        f"Result from {sender}:\n"
        f"{response_text}"
    )
    message = make_message(
        sender=sender,
        target=str(requester),
        intent=return_intent,
        body=body,
        causal_id=causal_id,
        hop_count=hop_count,
        auto_return=False,
        kind="result",
        reply_to=context.get("id"),
        source="auto_return",
    )
    d.redirect_oversized_result_body(message)
    d.queue_message(message)
    d.log(
        "response_return_queued",
        message_id=message["id"],
        from_agent=sender,
        to=requester,
        kind="result",
        intent=return_intent,
        causal_id=causal_id,
        hop_count=hop_count,
    )
    d.cancel_watchdogs_for_message(context.get("id"), reason="reply_received")


def _cleanup_terminal_context_locked(d, sender: str, context: dict, *, watchdog_reason: str) -> None:
    msg_id = context.get("id")
    if msg_id:
        d.suppress_pending_watchdog_wakes(ref_message_id=str(msg_id), reason=watchdog_reason)
        d.remove_delivered_message(sender, msg_id)
        d.last_enter_ts.pop(str(msg_id), None)
        # Aggregate watchdogs are cancelled inside collect_aggregate_response
        # when the aggregate completes; only cancel the per-message
        # watchdog here for non-aggregate messages.
        if not context.get("aggregate_id"):
            d.cancel_watchdogs_for_message(str(msg_id), reason=watchdog_reason)
        if watchdog_reason != "interrupted_tombstone_terminal":
            d._record_message_tombstone(
                sender,
                {
                    "id": msg_id,
                    "from": context.get("from"),
                    "nonce": context.get("nonce"),
                    "turn_id": context.get("turn_id"),
                },
                by_sender="bridge",
                reason=watchdog_reason,
                suppress_late_hooks=False,
                prompt_submitted_seen=True,
            )
    ctx_nonce = context.get("nonce")
    if ctx_nonce:
        d.discard_nonce(str(ctx_nonce))
    d.busy[sender] = False
    d.reserved[sender] = None
    # v1.5.2 consume-once: a peer request gets exactly one terminal
    # response path. Held-drain skips this helper. turn_id_mismatch keeps
    # ctx only until bounded maintenance expiry, unless the matching Stop
    # arrives first and reaches terminal cleanup.
    d.current_prompt_by_agent.pop(sender, None)


def handle_response_finished(d, record: dict) -> None:
    d.reload_participants()
    sender = d.participant_alias(record)
    if sender not in d.participants:
        return

    # IMPORTANT: stale judgement runs BEFORE any state mutation. If the
    # response is stale, we must not flip busy/reserved/current_prompt
    # nor trigger try_deliver against an unrelated active turn — doing
    # so could cause the bridge to inject a fresh prompt on top of a
    # peer that is still mid-turn.
    run_delivery_after_lock = False
    post_lock_delivery_requests: list[tuple[str | None, bool, str]] = []
    return_after_post_lock_delivery = False
    with d.target_state_lock([sender]):
        text = record.get("last_assistant_message") or ""
        if d.handle_clear_response_finished_locked(sender, record):
            return
        context = d.current_prompt_by_agent.get(sender) or {}
        response_turn_id = record.get("turn_id")
        context_turn_id = context.get("turn_id") if context else None
        held_info = d.held_interrupt.get(sender)
        held_stale_turn_mismatch = False
        # held_interrupt is now a legacy marker, not a delivery gate.
        # Classify it before interrupted-tombstone matching so stale
        # residue cannot survive an early tombstone return:
        # - no marker: normal path
        # - marker + no active ctx id: legacy held-drain
        # - marker prior_id == active id and no active turn-id conflict:
        #   legacy held-drain. A matching id is enough when the active ctx
        #   has no turn_id; otherwise the Stop turn must match.
        # - marker prior_id == active id but active turn-id conflicts:
        #   stale residue; pop it and let turn_id_mismatch handling run
        # - marker prior_id absent/different from active id: stale
        #   residue; pop it and continue with normal stale/terminal rules
        if held_info is not None:
            active_message_id = str(context.get("id") or "")
            held_prior_message_id = str(held_info.get("prior_message_id") or "")
            held_matches_active = bool(
                active_message_id
                and held_prior_message_id
                and held_prior_message_id == active_message_id
            )
            held_turns_conflict = context_turn_id is not None and response_turn_id != context_turn_id
            legacy_held_drain = not active_message_id or (held_matches_active and not held_turns_conflict)
            if not legacy_held_drain:
                stale_info = d.held_interrupt.pop(sender, None) or held_info
                held_info = None
                held_stale_turn_mismatch = bool(active_message_id and held_turns_conflict)
                hold_duration_ms = None
                since_ts = stale_info.get("since_ts")
                if isinstance(since_ts, (int, float)):
                    hold_duration_ms = int(max(0.0, time.time() - float(since_ts)) * 1000)
                d.log(
                    "stale_hold_ignored_active_context",
                    target=sender,
                    prior_message_id=stale_info.get("prior_message_id"),
                    active_message_id=active_message_id,
                    active_turn_id=context_turn_id,
                    response_turn_id=response_turn_id,
                    hold_age_ms=hold_duration_ms,
                )
        in_held = held_info is not None
        turn_id_mismatch = (
            held_stale_turn_mismatch
            or (
                response_turn_id is not None
                and context_turn_id is not None
                and response_turn_id != context_turn_id
            )
        )
        fingerprint = d.response_fingerprint(record)
        first_time = d.processed_returns.add(fingerprint)

        interrupted_tombstone, interrupted_reason = d._match_interrupted_response(
            sender,
            response_turn_id,
            has_current_context=bool(context),
        )
        if interrupted_tombstone:
            active_id = str(context.get("id") or "")
            tombstone_message_id = str(interrupted_tombstone.get("message_id") or "")
            id_match = bool(active_id and tombstone_message_id and active_id == tombstone_message_id)
            turn_match = bool(response_turn_id and context_turn_id and response_turn_id == context_turn_id)
            is_concrete_current = id_match or turn_match
            d._pop_interrupted_tombstone(sender, interrupted_tombstone)
            if first_time:
                d.log(
                    "response_skipped_stale",
                    agent=sender,
                    reason=interrupted_reason,
                    response_turn_id=response_turn_id,
                    active_turn_id=context_turn_id,
                    active_message_id=context.get("id"),
                    interrupted_message_id=interrupted_tombstone.get("message_id"),
                    interrupted_turn_id=interrupted_tombstone.get("turn_id"),
                    interrupted_nonce=interrupted_tombstone.get("nonce"),
                )
            if is_concrete_current:
                d._cleanup_terminal_context_locked(
                    sender,
                    context,
                    watchdog_reason="interrupted_tombstone_terminal",
                )
                d.promote_pending_self_clear_locked(sender)
                run_delivery_after_lock = True
            else:
                return
        if not run_delivery_after_lock and interrupted_reason and first_time:
            d.log(
                "interrupted_response_ambiguous",
                agent=sender,
                reason=interrupted_reason,
                response_turn_id=response_turn_id,
                active_turn_id=context_turn_id,
                active_message_id=context.get("id"),
                tombstone_message_ids=[
                    row.get("message_id") for row in (d.interrupted_turns.get(sender) or [])
                ],
            )

        if not run_delivery_after_lock and in_held:
            # The held-target interrupt is being drained: this Stop
            # event confirms the peer aborted the interrupted turn
            # and is now idle. Release hold but do NOT route the
            # (possibly partial) text. If a new prompt_submitted
            # arrived while held, a late Stop for the old turn must
            # release the hold without clobbering the new active ctx.
            active_message_id = str(context.get("id") or "")
            held_prior_message_id = str(held_info.get("prior_message_id") or "")
            message_id_matches = bool(
                active_message_id
                and held_prior_message_id
                and active_message_id == held_prior_message_id
            )
            turn_id_matches = bool(response_turn_id and context_turn_id and response_turn_id == context_turn_id)
            has_active_context = bool(context)
            # An empty ctx is not a real active prompt in normal daemon
            # state; preserve that legacy manual-recovery drain, but
            # require a concrete id or turn-id match for any present ctx.
            held_drain_matches_current = (
                not has_active_context
                or message_id_matches
                or turn_id_matches
            )
            if first_time:
                if held_drain_matches_current:
                    d.log(
                        "response_skipped_stale",
                        agent=sender,
                        reason="held_drain",
                        response_turn_id=response_turn_id,
                        active_turn_id=context_turn_id,
                        prior_message_id=held_info.get("prior_message_id"),
                    )
                else:
                    d.log(
                        "held_drain_stale_stop",
                        agent=sender,
                        response_turn_id=response_turn_id,
                        active_turn_id=context_turn_id,
                        active_message_id=context.get("id"),
                        prior_message_id=held_info.get("prior_message_id"),
                    )
            d.held_interrupt.pop(sender, None)
            hold_duration_ms = None
            since_ts = held_info.get("since_ts")
            if isinstance(since_ts, (int, float)):
                hold_duration_ms = int(max(0.0, time.time() - float(since_ts)) * 1000)
            d.log(
                "hold_released",
                target=sender,
                reason="stop_event_received",
                prior_message_id=held_info.get("prior_message_id"),
                hold_duration_ms=hold_duration_ms,
            )
            if held_drain_matches_current:
                d.busy[sender] = False
                d.reserved[sender] = None
                d.current_prompt_by_agent.pop(sender, None)
                post_lock_delivery_requests.append((sender, False, "held_stop_sender"))
            post_lock_delivery_requests.append((None, False, "held_stop_global"))
            return_after_post_lock_delivery = True

        if not return_after_post_lock_delivery and not run_delivery_after_lock and turn_id_mismatch:
            # A late Stop event for an old turn arrived while the peer
            # is processing a new turn. Skip routing now; a maintenance
            # sweep expires the active context later if the matching Stop
            # never arrives. The first mismatch pins the grace clock so a
            # stream of stale Stops cannot keep the target busy forever.
            now_ts = time.time()
            if context.get("turn_id_mismatch_since_ts") is None:
                context["turn_id_mismatch_since_ts"] = now_ts
            context["turn_id_mismatch_last_ts"] = now_ts
            context["turn_id_mismatch_response_turn_id"] = response_turn_id
            context["turn_id_mismatch_count"] = int(context.get("turn_id_mismatch_count") or 0) + 1
            if first_time:
                d.log(
                    "response_skipped_stale",
                    agent=sender,
                    reason="turn_id_mismatch",
                    response_turn_id=response_turn_id,
                    active_turn_id=context_turn_id,
                    active_message_id=context.get("id"),
                )
            return

        if not return_after_post_lock_delivery and not run_delivery_after_lock:
            # Normal terminal path: route (if applicable) and remove the
            # delivered message from queue regardless of whether routing
            # actually delivered text (notices, empty responses, inactive
            # requesters all still terminate the message). Watchdog cancel
            # also happens here, decoupled from routing success — otherwise
            # a request whose response was empty / had no active requester
            # would leave its watchdog alive and fire a bogus wake later.
            if first_time:
                d.maybe_return_response(sender, text, context)
            d._cleanup_terminal_context_locked(sender, context, watchdog_reason="terminal_response")
            d.promote_pending_self_clear_locked(sender)
    for target, command_aware, reason in post_lock_delivery_requests:
        d.request_and_drain_delivery(target, command_aware=command_aware, reason=reason)
    if return_after_post_lock_delivery:
        return
    d.request_and_drain_delivery(sender, command_aware=False, reason="response_finished_sender")
    d.request_and_drain_delivery(command_aware=False, reason="response_finished_global")


def handle_external_message_queued(d, record: dict) -> None:
    # File-fallback ingress: bridge_enqueue.py wrote the message
    # directly to queue.json + events.raw.jsonl because the daemon
    # socket was not reachable from the caller's sandbox (e.g. codex).
    # The daemon's main follow loop dispatches us when it sees the
    # message_queued event. Always finalize the queue item via the
    # shared helper so that:
    #   - status is promoted ingressing -> pending (regardless of
    #     sender, including bridge-origin synthetic messages that
    #     happened to take the fallback path), and
    #   - alarm cancel + body prepend runs (the helper itself is
    #     gated on sender != "bridge" via _maybe_cancel_alarms_for_incoming).
    # The helper is idempotent (status==ingressing gate), so replay
    # of the same event cannot re-cancel a fresh alarm.
    target = record.get("to")
    d.reload_participants()
    deliver_target = ""
    with d.state_lock:
        msg_id = str(record.get("message_id") or "")
        if msg_id:
            d._apply_alarm_cancel_to_queued_message(msg_id)
        if target in d.participants:
            deliver_target = str(target)
    if deliver_target:
        d.try_deliver(deliver_target)


def _drop_ingress_row_from_blocked_sender(d, message_id: str, *, phase: str) -> bool:
    if not message_id:
        return False
    removed: dict | None = None

    def mutator(queue: list[dict]) -> bool:
        nonlocal removed
        kept: list[dict] = []
        dropped = False
        for item in queue:
            if item.get("id") == message_id and item.get("status") == "ingressing":
                sender = str(item.get("from") or "")
                if d.sender_blocked_by_clear(sender):
                    removed = dict(item)
                    dropped = True
                    continue
            kept.append(item)
        if dropped:
            queue[:] = kept
        return dropped

    dropped = bool(d.queue.update(mutator))
    if dropped and removed:
        sender = str(removed.get("from") or "")
        d._record_message_tombstone(
            str(removed.get("to") or ""),
            removed,
            by_sender="bridge",
            reason="sender_clear_blocked",
            suppress_late_hooks=False,
            prompt_submitted_seen=False,
        )
        d.log(
            "ingress_dropped_sender_clear_blocked",
            message_id=message_id,
            from_agent=sender,
            to=removed.get("to"),
            phase=phase,
            error_kind="self_clear_pending" if sender in d.pending_self_clears else "clear_in_progress",
        )
    return dropped


def _apply_alarm_cancel_to_queued_message(d, message_id: str) -> None:
    # Single source of truth for finalizing ingestion of a message
    # placed into queue.json by either ingress path:
    #   1. Daemon-socket ingress (enqueue_ipc_message): bridge_enqueue
    #      sent the message via the unix socket; daemon appended it
    #      to queue.json with status="ingressing".
    #   2. File-fallback ingress: bridge_enqueue.py wrote queue.json
    #      directly because its sandbox could not connect to the
    #      socket. Daemon dispatches handle_external_message_queued
    #      which calls this.
    # Steps performed (atomic under state_lock that the caller holds):
    #   a) Run alarm-cancel-on-incoming, which may prepend a notice
    #      to the body in place if any alarms qualify (gated on
    #      sender != bridge inside the helper).
    #   b) Promote status from "ingressing" to "pending" so
    #      reserve_next can pick the message up.
    # Idempotent: gated by `status == "ingressing"`. The same event
    # may be observed multiple times (socket path enqueues then
    # daemon's tail loop replays the message_queued record; or
    # fallback writes both queue and event and the event later
    # gets duplicated). After the first finalize the status is
    # promoted to "pending" and any subsequent invocation no-ops,
    # so a newer alarm registered between replays cannot be
    # accidentally cancelled by a stale message.
    if not message_id:
        return
    snapshot = list(d.queue.read())
    item = next((it for it in snapshot if it.get("id") == message_id), None)
    if not item:
        return
    if item.get("status") != "ingressing":
        return
    if d._drop_ingress_row_from_blocked_sender(message_id, phase="event"):
        return
    original_body = item.get("body")
    d._maybe_cancel_alarms_for_incoming(item)
    body_changed = item.get("body") != original_body
    new_body = item.get("body") if body_changed else None
    stripped_logs: list[dict] = []

    def update_mut(queue: list[dict]) -> None:
        for q in queue:
            if q.get("id") == message_id:
                if body_changed:
                    q["body"] = new_body
                stripped_log = d._strip_no_auto_return_watchdog_metadata(
                    q,
                    phase="ingress",
                    reason="ingress_finalize",
                )
                if stripped_log:
                    stripped_logs.append(stripped_log)
                q["status"] = "pending"
                q["updated_ts"] = utc_now()
                return

    d.queue.update(update_mut)
    for fields in stripped_logs:
        d.log("watchdog_stripped_no_auto_return", **fields)
