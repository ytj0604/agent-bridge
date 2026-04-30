#!/usr/bin/env python3
from datetime import datetime, timezone
import math

from bridge_util import normalize_kind, utc_now


WAIT_STATUS_SECTION_LIMIT = 50
AGGREGATE_STATUS_LEG_LIMIT = 100
WATCHDOG_PHASE_RESPONSE = "response"
RESPONSE_LIKE_TOMBSTONE_REASONS = {
    "terminal_response",
    "reply_received",
    "interrupted_tombstone_terminal",
    "aggregate_complete",
}


def _is_command_lock_wait_exceeded(exc: Exception) -> bool:
    return exc.__class__.__name__ == "CommandLockWaitExceeded"


def _wait_status_section(d, items: list[dict], *, limit: int = WAIT_STATUS_SECTION_LIMIT) -> dict:
    limited = items[:limit]
    total = len(items)
    return {
        "total_count": total,
        "returned_count": len(limited),
        "truncated": total > len(limited),
        "items": limited,
    }

def _wait_status_deadline_iso(d, deadline: object) -> str:
    try:
        value = float(deadline)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value):
        return ""
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def _wait_status_message_watchdog_index(d, caller: str, watchdogs: dict[str, dict]) -> dict[str, list[dict]]:
    by_message: dict[str, list[dict]] = {}
    for wake_id, wd in watchdogs.items():
        if wd.get("is_alarm"):
            continue
        if str(wd.get("sender") or "") != caller:
            continue
        message_id = str(wd.get("ref_message_id") or "")
        if not message_id:
            continue
        by_message.setdefault(message_id, []).append({
            "wake_id": wake_id,
            "phase": d.normalize_watchdog_phase(wd),
            "deadline": _wait_status_deadline_iso(d, wd.get("deadline")),
        })
    return by_message

def _wait_status_counts(d, sections: dict[str, dict]) -> dict:
    return {
        name: {
            "total_count": section.get("total_count", 0),
            "returned_count": section.get("returned_count", 0),
            "truncated": bool(section.get("truncated")),
        }
        for name, section in sections.items()
    }

def _build_outstanding_requests(d, caller: str, queue: list[dict], watchdogs_by_message: dict[str, list[dict]]) -> list[dict]:
    rows: list[dict] = []
    active_statuses = {"pending", "inflight", "submitted", "delivered"}
    for item in queue:
        if str(item.get("from") or "") != caller:
            continue
        if normalize_kind(item.get("kind"), "request") != "request":
            continue
        if not bool(item.get("auto_return")):
            continue
        status = str(item.get("status") or "")
        if status not in active_statuses:
            continue
        message_id = str(item.get("id") or "")
        rows.append({
            "message_id": message_id,
            "target": item.get("to"),
            "status": status,
            "kind": normalize_kind(item.get("kind"), "request"),
            "intent": item.get("intent"),
            "created_ts": item.get("created_ts"),
            "updated_ts": item.get("updated_ts"),
            "inflight_ts": item.get("inflight_ts"),
            "delivered_ts": item.get("delivered_ts"),
            "aggregate_id": item.get("aggregate_id") or "",
            "watchdog_wake_ids": [wd.get("wake_id") for wd in watchdogs_by_message.get(message_id, [])],
        })
    return rows

def _build_wait_status_watchdogs(d, caller: str, watchdogs: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for wake_id, wd in watchdogs.items():
        if wd.get("is_alarm"):
            continue
        if str(wd.get("sender") or "") != caller:
            continue
        rows.append({
            "wake_id": wake_id,
            "message_id": wd.get("ref_message_id") or "",
            "aggregate_id": wd.get("ref_aggregate_id") or "",
            "target": wd.get("ref_to") or "",
            "phase": d.normalize_watchdog_phase(wd),
            "deadline": _wait_status_deadline_iso(d, wd.get("deadline")),
            "kind": wd.get("ref_kind") or "",
            "intent": wd.get("ref_intent") or "",
        })
    return rows

def _build_wait_status_alarms(d, caller: str, watchdogs: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for wake_id, wd in watchdogs.items():
        if not wd.get("is_alarm"):
            continue
        if str(wd.get("sender") or "") != caller:
            continue
        rows.append({
            "wake_id": wake_id,
            "deadline": _wait_status_deadline_iso(d, wd.get("deadline")),
            "note": str(wd.get("alarm_body") or ""),
        })
    return rows

def _build_wait_status_pending_inbound(d, caller: str, queue: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in queue:
        if str(item.get("to") or "") != caller:
            continue
        if str(item.get("status") or "") != "pending":
            continue
        body = str(item.get("body") or "")
        rows.append({
            "message_id": item.get("id"),
            "kind": normalize_kind(item.get("kind"), "notice"),
            "from": item.get("from"),
            "intent": item.get("intent"),
            "created_ts": item.get("created_ts"),
            "reply_to": item.get("reply_to"),
            "ref_message_id": item.get("ref_message_id") or "",
            "ref_aggregate_id": item.get("ref_aggregate_id") or "",
            "source": item.get("source") or "",
            "body_chars": len(body),
        })
    return rows

def _build_wait_status_aggregates(d, caller: str, aggregates: dict) -> list[dict]:
    rows: list[dict] = []
    for aggregate_id, aggregate in (aggregates or {}).items():
        if str(aggregate.get("requester") or "") != caller:
            continue
        if aggregate.get("delivered") or aggregate.get("status") == "complete":
            continue
        expected = [str(alias) for alias in aggregate.get("expected") or []]
        replies = aggregate.get("replies") or {}
        replied = sorted(str(alias) for alias in replies.keys())
        missing = sorted(set(expected) - set(replied))
        rows.append({
            "aggregate_id": str(aggregate.get("id") or aggregate_id),
            "causal_id": aggregate.get("causal_id") or "",
            "intent": aggregate.get("intent") or "",
            "expected_count": len(expected),
            "replied_count": len(replied),
            "missing_peers": missing,
            "expected": expected,
            "replied_peers": replied,
            "message_ids": aggregate.get("message_ids") or {},
            "updated_ts": aggregate.get("updated_ts") or "",
        })
    return rows

def build_wait_status(d, caller: str) -> dict:
    try:
        with d.command_state_lock(command_class="wait_status"):
            watchdog_snapshot = d.watchdog_snapshot()
            queue_snapshot = list(d.queue.read())
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        return d.lock_wait_exceeded_response("wait_status")
    try:
        aggregates = d.aggregates.read_aggregates()
    except Exception:
        aggregates = {}

    # Best-effort debug snapshot: watchdogs are snapshotted under their
    # physical lock while state_lock is held, then aggregate JSON is read
    # after releasing daemon locks to preserve Stage 14 ordering.
    watchdogs_by_message = _wait_status_message_watchdog_index(d, caller, watchdog_snapshot)
    sections = {
        "outstanding_requests": _wait_status_section(d, 
            _build_outstanding_requests(d, caller, queue_snapshot, watchdogs_by_message)
        ),
        "aggregate_waits": _wait_status_section(d, _build_wait_status_aggregates(d, caller, aggregates)),
        "alarms": _wait_status_section(d, _build_wait_status_alarms(d, caller, watchdog_snapshot)),
        "watchdogs": _wait_status_section(d, _build_wait_status_watchdogs(d, caller, watchdog_snapshot)),
        "pending_inbound": _wait_status_section(d, _build_wait_status_pending_inbound(d, caller, queue_snapshot)),
    }
    return {
        "ok": True,
        "bridge_session": d.bridge_session,
        "caller": caller,
        "generated_ts": utc_now(),
        "limits": {"per_section": WAIT_STATUS_SECTION_LIMIT},
        "summary": _wait_status_counts(d, sections),
        **sections,
    }

def _aggregate_status_not_found(d, caller: str, aggregate_id: str, reason: str, **details) -> dict:
    d.safe_log(
        "aggregate_status_not_found",
        caller=caller,
        aggregate_id=aggregate_id,
        reason=reason,
        **details,
    )
    return {"ok": False, "error": "aggregate_not_found", "aggregate_id": aggregate_id}

def _aggregate_status_legacy_min_ts(d, values: list[object]) -> str:
    candidates: list[tuple[float, str]] = []
    for raw in values:
        text = str(raw or "")
        if not text:
            continue
        try:
            ts = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        candidates.append((ts, text))
    if not candidates:
        return ""
    return min(candidates, key=lambda item: item[0])[1]

def _aggregate_status_section(d, legs: list[dict]) -> dict:
    limited = legs[:AGGREGATE_STATUS_LEG_LIMIT]
    return {
        "total_count": len(legs),
        "returned_count": len(limited),
        "truncated": len(legs) > len(limited),
        "items": limited,
    }

def _aggregate_status_alias_list(d, raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(alias) for alias in raw if str(alias or "")]

def _aggregate_status_tombstone_for_message(
    d,
    tombstones: dict[str, list[dict]],
    message_id: str,
    caller: str,
) -> dict | None:
    if not message_id:
        return None
    for entries in tombstones.values():
        for tombstone in entries:
            if str(tombstone.get("message_id") or "") != message_id:
                continue
            if str(tombstone.get("prior_sender") or "") != caller:
                continue
            return tombstone
    return None

def _aggregate_terminal_status_from_reason(d, reason: str) -> str:
    if reason in {"cancelled_by_sender", "prompt_intercepted", "interrupted"}:
        return "cancelled"
    if reason in {"endpoint_lost", "turn_id_mismatch_expired", "daemon_restart_lost_routing_ctx"}:
        return "timeout"
    return "timeout"

def _aggregate_status_response_watchdog(
    d,
    caller: str,
    aggregate_id: str,
    watchdogs: dict[str, dict],
) -> dict | None:
    matches: list[tuple[float, str, dict]] = []
    for wake_id, wd in watchdogs.items():
        if wd.get("is_alarm"):
            continue
        if str(wd.get("sender") or "") != caller:
            continue
        if str(wd.get("ref_aggregate_id") or "") != aggregate_id:
            continue
        if d.normalize_watchdog_phase(wd) != WATCHDOG_PHASE_RESPONSE:
            continue
        try:
            deadline = float(wd.get("deadline"))
        except (TypeError, ValueError):
            deadline = 0.0
        matches.append((deadline, wake_id, wd))
    if not matches:
        return None
    _deadline, wake_id, wd = sorted(matches, key=lambda item: (item[0], item[1]))[0]
    return {
        "wake_id": wake_id,
        "phase": WATCHDOG_PHASE_RESPONSE,
        "deadline": _wait_status_deadline_iso(d, wd.get("deadline")),
    }

def _aggregate_status_reply_leg(d, alias: str, message_id: str, reply: dict, tombstone: dict | None = None) -> dict:
    body = str(reply.get("body") or "")
    synthetic = bool(reply.get("synthetic"))
    reason = str(reply.get("synthetic_reason") or "")
    if not synthetic and tombstone:
        tombstone_reason = str(tombstone.get("reason") or "")
        if tombstone_reason and tombstone_reason not in RESPONSE_LIKE_TOMBSTONE_REASONS:
            synthetic = True
            reason = tombstone_reason
    status = _aggregate_terminal_status_from_reason(d, reason) if synthetic else "responded"
    leg = {
        "target": alias,
        "msg_id": message_id,
        "status": status,
        "response_received": not synthetic,
        "response_chars": len(body),
        "received_ts": reply.get("received_ts") or "",
        "synthetic": synthetic,
        "terminal_reason": reason,
        "status_source": "aggregate_reply",
    }
    return leg

def _aggregate_status_build_legs(
    d,
    caller: str,
    expected: list[str],
    message_ids: dict[str, str],
    replies: dict,
    rows_by_alias: dict[str, dict],
    tombstones: dict[str, list[dict]],
) -> list[dict]:
    legs: list[dict] = []
    for alias in expected:
        message_id = str(message_ids.get(alias) or (rows_by_alias.get(alias) or {}).get("id") or "")
        reply = replies.get(alias)
        tombstone = _aggregate_status_tombstone_for_message(d, tombstones, message_id, caller)
        if isinstance(reply, dict):
            legs.append(_aggregate_status_reply_leg(d, alias, message_id, reply, tombstone))
            continue
        row = rows_by_alias.get(alias)
        if row:
            legs.append({
                "target": alias,
                "msg_id": message_id,
                "status": str(row.get("status") or "pending"),
                "response_received": False,
                "response_chars": 0,
                "received_ts": "",
                "synthetic": False,
                "terminal_reason": "",
                "status_source": "queue",
            })
            continue
        if tombstone:
            reason = str(tombstone.get("reason") or "")
            legs.append({
                "target": alias,
                "msg_id": message_id,
                "status": _aggregate_terminal_status_from_reason(d, reason),
                "response_received": False,
                "response_chars": 0,
                "received_ts": "",
                "synthetic": True,
                "terminal_reason": reason,
                "status_source": "tombstone",
            })
            continue
        legs.append({
            "target": alias,
            "msg_id": message_id,
            "status": "pending",
            "response_received": False,
            "response_chars": 0,
            "received_ts": "",
            "synthetic": False,
            "terminal_reason": "",
            "status_source": "fallback",
        })
    return legs

def build_aggregate_status(d, caller: str, aggregate_id: str) -> dict:
    # Best-effort debug snapshot: watchdogs are snapshotted under their
    # physical lock while state_lock is held; aggregate JSON is read
    # afterwards to preserve Stage 14 lock order.
    try:
        with d.command_state_lock(command_class="aggregate_status"):
            watchdog_snapshot = d.watchdog_snapshot()
            queue_snapshot = list(d.queue.read())
            tombstone_snapshot = {
                alias: [dict(entry) for entry in entries]
                for alias, entries in d.interrupted_turns.items()
            }
    except Exception as exc:
        if not _is_command_lock_wait_exceeded(exc):
            raise
        return d.lock_wait_exceeded_response("aggregate_status")
    try:
        aggregate = d.aggregates.get(aggregate_id)
    except Exception:
        aggregate = {}

    matching_rows = [
        dict(item)
        for item in queue_snapshot
        if str(item.get("aggregate_id") or "") == aggregate_id
        and normalize_kind(item.get("kind"), "notice") == "request"
        and bool(item.get("auto_return"))
    ]
    json_owner = str(aggregate.get("requester") or "")
    queue_owners = sorted({str(item.get("from") or "") for item in matching_rows if str(item.get("from") or "")})
    caller_watchdogs = [
        wd for wd in watchdog_snapshot.values()
        if str(wd.get("sender") or "") == caller
        and str(wd.get("ref_aggregate_id") or "") == aggregate_id
        and d.normalize_watchdog_phase(wd) == WATCHDOG_PHASE_RESPONSE
        and not wd.get("is_alarm")
    ]
    watchdog_owner = caller if caller_watchdogs else ""
    conflict = False
    if len(queue_owners) > 1:
        conflict = True
    if json_owner and queue_owners and any(owner != json_owner for owner in queue_owners):
        conflict = True
    if json_owner and watchdog_owner and watchdog_owner != json_owner:
        conflict = True
    if not json_owner and queue_owners and watchdog_owner and queue_owners[0] != watchdog_owner:
        conflict = True
    if conflict:
        return _aggregate_status_not_found(d, 
            caller,
            aggregate_id,
            "source_owner_conflict",
            json_owner=json_owner,
            queue_owners=queue_owners,
            watchdog_owner=watchdog_owner,
        )

    owner = json_owner or (queue_owners[0] if queue_owners else "") or watchdog_owner
    if not owner:
        return _aggregate_status_not_found(d, caller, aggregate_id, "missing")
    if owner != caller:
        return _aggregate_status_not_found(d, caller, aggregate_id, "foreign_owner")

    owned_rows = [item for item in matching_rows if str(item.get("from") or "") == owner]
    rows_by_alias = {
        str(item.get("to") or ""): item
        for item in owned_rows
        if str(item.get("to") or "")
    }
    expected: list[str] = []
    expected = d.merge_ordered_aliases(expected, _aggregate_status_alias_list(d, aggregate.get("expected")))
    for item in owned_rows:
        expected = d.merge_ordered_aliases(expected, _aggregate_status_alias_list(d, item.get("aggregate_expected")))
    for wd in caller_watchdogs:
        expected = d.merge_ordered_aliases(expected, _aggregate_status_alias_list(d, wd.get("ref_aggregate_expected")))

    message_ids: dict[str, str] = {}
    raw_message_ids = aggregate.get("message_ids") or {}
    if isinstance(raw_message_ids, dict):
        message_ids.update({str(alias): str(message_id) for alias, message_id in raw_message_ids.items() if alias and message_id})
    for item in owned_rows:
        raw = item.get("aggregate_message_ids") or {}
        if isinstance(raw, dict):
            message_ids.update({str(alias): str(message_id) for alias, message_id in raw.items() if alias and message_id})
        alias = str(item.get("to") or "")
        if alias:
            message_ids.setdefault(alias, str(item.get("id") or ""))
    if not expected:
        expected = d.merge_ordered_aliases(expected, list(message_ids.keys()))
        expected = d.merge_ordered_aliases(expected, list(rows_by_alias.keys()))
        replies_for_expected = aggregate.get("replies") or {}
        if isinstance(replies_for_expected, dict):
            expected = d.merge_ordered_aliases(expected, list(replies_for_expected.keys()))

    replies = aggregate.get("replies") or {}
    if not isinstance(replies, dict):
        replies = {}
    mode = str(aggregate.get("mode") or "")
    if mode not in {"all", "partial"}:
        row_modes = sorted({str(item.get("aggregate_mode") or "") for item in owned_rows if str(item.get("aggregate_mode") or "") in {"all", "partial"}})
        mode = row_modes[0] if len(row_modes) == 1 else "unknown"
    started_ts = str(aggregate.get("started_ts") or "")
    if not started_ts:
        started_ts = _aggregate_status_legacy_min_ts(d, 
            [item.get("aggregate_started_ts") for item in owned_rows]
            + [item.get("created_ts") for item in owned_rows]
            + [aggregate.get("created_ts")]
        )
    legs = _aggregate_status_build_legs(d, caller, expected, message_ids, replies, rows_by_alias, tombstone_snapshot)
    replied_count = len([alias for alias in expected if alias in replies])
    total_count = len(expected)
    missing_count = max(0, total_count - replied_count)
    status = str(aggregate.get("status") or "")
    if aggregate.get("delivered") or status == "complete" or (total_count > 0 and replied_count >= total_count):
        status = "complete"
    else:
        status = "collecting"

    return {
        "ok": True,
        "bridge_session": d.bridge_session,
        "caller": caller,
        "aggregate_id": aggregate_id,
        "generated_ts": utc_now(),
        "status": status,
        "mode": mode,
        "started_ts": started_ts,
        "completed_ts": aggregate.get("delivered_at") or "",
        "replied_count": replied_count,
        "total_count": total_count,
        "missing_count": missing_count,
        "limits": {"legs": AGGREGATE_STATUS_LEG_LIMIT},
        "legs": _aggregate_status_section(d, legs),
        "aggregate_response_watchdog": _aggregate_status_response_watchdog(d, 
            caller,
            aggregate_id,
            watchdog_snapshot,
        ),
    }
