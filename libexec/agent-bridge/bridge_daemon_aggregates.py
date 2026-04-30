#!/usr/bin/env python3

from bridge_daemon_messages import make_message
from bridge_util import short_id, utc_now


def aggregate_expected_from_context(d, context: dict) -> list[str]:
    raw = context.get("aggregate_expected") or []
    if not isinstance(raw, list):
        return []
    expected = []
    for item in raw:
        alias = str(item or "")
        if alias and alias not in expected:
            expected.append(alias)
    return expected


def aggregate_message_ids_from_context(d, context: dict) -> dict[str, str]:
    raw = context.get("aggregate_message_ids") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(alias): str(message_id) for alias, message_id in raw.items() if alias and message_id}


def merge_ordered_aliases(d, existing: list[str], incoming: list[str]) -> list[str]:
    merged = []
    for alias in [*existing, *incoming]:
        alias = str(alias or "")
        if alias and alias not in merged:
            merged.append(alias)
    return merged


def aggregate_result_body(d, aggregate: dict) -> str:
    expected = [str(alias) for alias in aggregate.get("expected") or []]
    replies = aggregate.get("replies") or {}
    message_ids = aggregate.get("message_ids") or {}
    lines = [
        f"Aggregated result for broadcast {aggregate.get('id')} from {aggregate.get('requester')}.",
        f"causal_id={aggregate.get('causal_id')} expected={', '.join(expected)}",
        "",
    ]
    for alias in expected:
        reply = replies.get(alias) or {}
        original_id = message_ids.get(alias) or reply.get("reply_to") or ""
        lines.append(f"--- {alias} in_reply_to={original_id} ---")
        body = str(reply.get("body") or "").strip()
        lines.append(body or "(empty response)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _record_aggregate_completion_tombstones(d, aggregate: dict) -> None:
    requester = str(aggregate.get("requester") or "")
    message_ids = aggregate.get("message_ids") or {}
    if not isinstance(message_ids, dict):
        return
    for alias, message_id in message_ids.items():
        alias_str = str(alias or "")
        msg_id = str(message_id or "")
        if not alias_str or not msg_id:
            continue
        d._record_message_tombstone(
            alias_str,
            {"id": msg_id, "from": requester},
            by_sender="bridge",
            reason="aggregate_complete",
            suppress_late_hooks=False,
            prompt_submitted_seen=True,
        )


def _record_aggregate_interrupted_reply(
    d,
    cancelled_msg: dict,
    by_sender: str,
    *,
    reason: str,
    deliver: bool = True,
) -> None:
    # Inject a synthetic "[interrupted]" reply for this peer into the
    # aggregate's reply set so the aggregate can still progress (and
    # eventually complete or hit its watchdog) without depending on a
    # real Stop event from the interrupted peer.
    agg_id = cancelled_msg.get("aggregate_id")
    if not agg_id:
        return
    synthetic_context = {
        "aggregate_id": agg_id,
        "from": cancelled_msg.get("from"),
        "aggregate_expected": cancelled_msg.get("aggregate_expected"),
        "aggregate_message_ids": cancelled_msg.get("aggregate_message_ids"),
        "aggregate_mode": cancelled_msg.get("aggregate_mode"),
        "aggregate_started_ts": cancelled_msg.get("aggregate_started_ts"),
        "aggregate_synthetic": True,
        "aggregate_synthetic_reason": reason,
        "causal_id": cancelled_msg.get("causal_id"),
        "intent": cancelled_msg.get("intent"),
        "hop_count": cancelled_msg.get("hop_count"),
        "id": cancelled_msg.get("id"),
    }
    synthetic_sender = str(cancelled_msg.get("to") or "")
    if reason == "prompt_intercepted":
        synthetic_text = (
            "[intercepted by user prompt: peer accepted a new prompt before this aggregate request finished]"
        )
    elif reason == "cancelled_by_sender":
        synthetic_text = (
            f"[cancelled by {by_sender or 'original sender'}: sender retracted this aggregate request leg before it became active]"
        )
    elif reason == "endpoint_lost":
        synthetic_text = (
            "[bridge:undeliverable] peer endpoint was lost before this aggregate request could complete"
        )
    else:
        synthetic_text = (
            f"[interrupted by {by_sender or 'bridge'}: peer did not respond before being asked to stop]"
        )
    try:
        d.collect_aggregate_response(synthetic_sender, synthetic_text, synthetic_context, deliver=deliver)
    except Exception as exc:
        d.safe_log(
            "aggregate_interrupt_inject_failed",
            aggregate_id=agg_id,
            cancelled_message_id=cancelled_msg.get("id"),
            error=str(exc),
        )


def collect_aggregate_response(d, sender: str, text: str, context: dict, deliver: bool = True) -> None:
    aggregate_id = str(context.get("aggregate_id") or "")
    if not aggregate_id:
        return

    requester = str(context.get("from") or "")
    expected = aggregate_expected_from_context(d, context)
    message_ids = aggregate_message_ids_from_context(d, context)
    causal_id = str(context.get("causal_id") or short_id("causal"))
    original_intent = str(context.get("intent") or "message")
    aggregate_mode = str(context.get("aggregate_mode") or "")
    if aggregate_mode not in {"all", "partial"}:
        aggregate_mode = ""
    aggregate_started_ts = str(context.get("aggregate_started_ts") or "")
    aggregate_synthetic = bool(context.get("aggregate_synthetic"))
    aggregate_synthetic_reason = str(context.get("aggregate_synthetic_reason") or "")
    hop_count = int(context.get("hop_count") or 0)
    reply_to = str(context.get("id") or message_ids.get(sender) or "")
    completed: dict | None = None

    def mutator(data: dict) -> dict | None:
        data.setdefault("version", 1)
        aggregates = data.setdefault("aggregates", {})
        existing = aggregates.get(aggregate_id)
        if isinstance(existing, dict) and existing.get("status") == "cancelled_requester_cleared":
            return {"_cancelled_requester_cleared": True, **dict(existing)}
        aggregate = aggregates.setdefault(
            aggregate_id,
            {
                "id": aggregate_id,
                "created_ts": utc_now(),
                "requester": requester,
                "causal_id": causal_id,
                "intent": original_intent,
                "mode": aggregate_mode or "unknown",
                "started_ts": aggregate_started_ts,
                "hop_count": hop_count,
                "expected": expected,
                "message_ids": message_ids,
                "replies": {},
                "status": "collecting",
                "delivered": False,
            },
        )
        aggregate["updated_ts"] = utc_now()
        aggregate["requester"] = aggregate.get("requester") or requester
        aggregate["causal_id"] = aggregate.get("causal_id") or causal_id
        aggregate["intent"] = aggregate.get("intent") or original_intent
        aggregate["mode"] = aggregate.get("mode") or aggregate_mode or "unknown"
        if aggregate_started_ts and not aggregate.get("started_ts"):
            aggregate["started_ts"] = aggregate_started_ts
        aggregate["hop_count"] = int(aggregate.get("hop_count") or hop_count)
        aggregate["expected"] = merge_ordered_aliases(d, list(aggregate.get("expected") or []), expected)
        aggregate.setdefault("message_ids", {}).update(message_ids)
        replies = aggregate.setdefault("replies", {})
        reply_record = {
            "from": sender,
            "body": str(text),
            "reply_to": reply_to,
            "received_ts": utc_now(),
        }
        if aggregate_synthetic:
            reply_record["synthetic"] = True
            reply_record["synthetic_reason"] = aggregate_synthetic_reason
        replies[sender] = reply_record
        complete = bool(aggregate.get("expected")) and all(alias in replies for alias in aggregate.get("expected") or [])
        if complete and not aggregate.get("delivered"):
            aggregate["status"] = "complete"
            aggregate["delivered"] = True
            aggregate["delivered_at"] = utc_now()
            return dict(aggregate)
        return None

    completed = d.aggregates.update(mutator)

    if completed and completed.get("_cancelled_requester_cleared"):
        d.log(
            "aggregate_reply_ignored_requester_cleared",
            aggregate_id=aggregate_id,
            from_agent=sender,
            requester=requester,
            reply_to=reply_to,
        )
        return

    expected_count = len(expected)
    received_count = 0
    if completed:
        received_count = len(completed.get("replies") or {})
    else:
        aggregate = d.aggregates.get(aggregate_id)
        received_count = len(aggregate.get("replies") or {})
        expected_count = len(aggregate.get("expected") or expected)
    d.log(
        "aggregate_reply_collected",
        aggregate_id=aggregate_id,
        from_agent=sender,
        to=requester,
        reply_to=reply_to,
        causal_id=causal_id,
        received_count=received_count,
        expected_count=expected_count,
    )

    if not completed:
        return

    return_intent = f"{completed.get('intent') or original_intent}_aggregate_result"
    message = make_message(
        sender="bridge",
        target=str(completed.get("requester") or requester),
        intent=return_intent,
        body=aggregate_result_body(d, completed),
        causal_id=str(completed.get("causal_id") or causal_id),
        hop_count=int(completed.get("hop_count") or hop_count),
        auto_return=False,
        kind="result",
        reply_to=aggregate_id,
        source="aggregate_return",
    )
    message["aggregate_id"] = aggregate_id
    message["aggregate_expected"] = completed.get("expected") or expected
    d.redirect_oversized_result_body(message)
    with d.state_lock:
        _record_aggregate_completion_tombstones(d, completed)
        d.cancel_watchdogs_for_aggregate(aggregate_id, reason="aggregate_complete")
        d.suppress_pending_watchdog_wakes(ref_aggregate_id=aggregate_id, reason="aggregate_complete")
        d.queue_message(message, deliver=deliver)
        d.log(
            "aggregate_result_queued",
            message_id=message["id"],
            aggregate_id=aggregate_id,
            to=message["to"],
            kind="result",
            intent=return_intent,
            causal_id=message["causal_id"],
            expected_count=len(completed.get("expected") or []),
        )
