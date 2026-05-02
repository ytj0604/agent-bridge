#!/usr/bin/env python3
"""Show the caller's own bridge wait/queue status."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from bridge_daemon_client import ensure_daemon_running
from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session, room_status
from bridge_paths import run_root


WAIT_STATUS_SECTION_NAMES = (
    "pending_inbound",
    "outstanding_requests",
    "aggregate_waits",
    "alarms",
    "watchdogs",
)


def send_command(bridge_session: str, payload: dict) -> tuple[bool, dict, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, {}, f"daemon socket not found: {socket_path}"
    request = json.dumps(payload, ensure_ascii=True) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(str(socket_path))
            client.sendall(request.encode("utf-8"))
            raw = b""
            while b"\n" not in raw and len(raw) < 2_000_000:
                chunk = client.recv(65536)
                if not chunk:
                    break
                raw += chunk
    except OSError as exc:
        return False, {}, f"daemon socket error: {exc}"
    try:
        response = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return False, {}, f"invalid daemon response: {exc}"
    if not response.get("ok"):
        return False, response if isinstance(response, dict) else {}, str(response.get("error") or "daemon rejected request")
    return True, response, ""


def wait_status_error_message(error: str) -> str:
    if error == "unsupported command":
        return (
            "agent_wait_status: connected daemon does not support wait_status yet. "
            "Reload/restart the bridge daemon after updating Agent Bridge."
        )
    return f"agent_wait_status: {error}"


def _section(response: dict, name: str) -> dict:
    value = response.get(name)
    return value if isinstance(value, dict) else {}


def _section_items(response: dict, name: str) -> list[dict]:
    items = _section(response, name).get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _section_total(response: dict, name: str) -> int:
    section = _section(response, name)
    try:
        total = int(section.get("total_count", 0))
    except (TypeError, ValueError):
        total = 0
    return max(total, len(_section_items(response, name)))


def _section_is_truncated(response: dict, name: str) -> bool:
    return bool(_section(response, name).get("truncated"))


def _compact_refs(refs: dict) -> dict:
    compact: dict = {}
    for key, value in refs.items():
        if value is None or value == "":
            continue
        if isinstance(value, (str, int, float, bool)):
            compact[key] = value
        elif isinstance(value, list):
            compact[key] = [str(item) for item in value if str(item or "")]
    return compact


def _safe_display_text(value: object, *, limit: int = 160) -> str:
    text = str(value or "")
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    collapsed = collapsed.replace("[bridge:", "[bridge :")
    if len(collapsed) > limit:
        return collapsed[: max(0, limit - 3)].rstrip() + "..."
    return collapsed


def _why_item(
    *,
    category: str,
    severity: str,
    subject: str,
    reason: str,
    next_action: str,
    refs: dict | None = None,
) -> dict:
    return {
        "category": category,
        "severity": severity,
        "subject": subject,
        "reason": reason,
        "next_action": next_action,
        "refs": _compact_refs(refs or {}),
    }


def _watchdog_index(response: dict) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for item in _section_items(response, "watchdogs"):
        wake_id = str(item.get("wake_id") or "")
        if not wake_id:
            continue
        indexed[wake_id] = {
            "wake_id": wake_id,
            "message_id": str(item.get("message_id") or ""),
            "aggregate_id": str(item.get("aggregate_id") or ""),
            "target": str(item.get("target") or ""),
            "phase": str(item.get("phase") or ""),
            "deadline": str(item.get("deadline") or ""),
            "kind": str(item.get("kind") or ""),
            "intent": str(item.get("intent") or ""),
        }
    return indexed


def _watchdog_sentence(wake_ids: list[str], watchdogs_by_id: dict[str, dict]) -> str:
    safe_ids = [str(wake_id) for wake_id in wake_ids if str(wake_id or "")]
    if not safe_ids:
        return ""
    first = safe_ids[0]
    watchdog = watchdogs_by_id.get(first) or {}
    deadline = str(watchdog.get("deadline") or "")
    if deadline:
        return f" Watchdog wake {first} is scheduled for {deadline}."
    return f" Watchdog wake {first} is scheduled."


def _missing_peers_text(peers: object, *, limit: int = 6) -> tuple[str, list[str], int]:
    if not isinstance(peers, list):
        return "", [], 0
    safe = [str(peer) for peer in peers if str(peer or "")]
    if not safe:
        return "", [], 0
    shown = safe[:limit]
    text = ", ".join(shown)
    extra = len(safe) - len(shown)
    if extra > 0:
        text = f"{text}, and {extra} more"
    return text, shown, len(safe)


def _add_count_only_items(response: dict, items: list[dict], rendered_categories: set[str]) -> None:
    for name in WAIT_STATUS_SECTION_NAMES:
        if name in rendered_categories:
            continue
        total = _section_total(response, name)
        if total <= 0 and not _section_is_truncated(response, name):
            continue
        category_labels = {
            "pending_inbound": "Pending inbound bridge messages",
            "outstanding_requests": "Outstanding requests",
            "aggregate_waits": "Aggregate waits",
            "alarms": "Alarms",
            "watchdogs": "Watchdogs",
        }
        public_categories = {
            "pending_inbound": "pending_inbound",
            "outstanding_requests": "request",
            "aggregate_waits": "aggregate",
            "alarms": "alarm",
            "watchdogs": "watchdog",
        }
        severity = "attention" if name == "pending_inbound" else "waiting"
        if name == "alarms":
            severity = "info"
        items.append(_why_item(
            category=public_categories.get(name, name),
            severity=severity,
            subject=f"{category_labels.get(name, name)} are present but details were not returned",
            reason=f"The daemon reported {total} item(s) in this section.",
            next_action="Use agent_wait_status --json only for a human-prompted debug check; do not poll.",
            refs={"total_count": total},
        ))


def _next_actions_for(status: str, categories: set[str]) -> list[str]:
    if status == "idle":
        return []
    if "pending_inbound" in categories:
        return ["finish the current response and wait for the bridge prompt."]
    if "outstanding_requests" in categories:
        return ["wait for the [bridge:*] result. If a watchdog wake arrives, choose one recovery action from that wake."]
    if "aggregate_waits" in categories:
        return ["wait for the aggregate [bridge:*] result; use agent_aggregate_status only for a human-prompted debug check."]
    if "alarms" in categories:
        return ["wait for the alarm notice or an incoming peer request/notice; do not poll."]
    if "watchdogs" in categories:
        return ["wait for the bridge wake or inspect only if debugging."]
    return ["use agent_wait_status --json only for a human-prompted debug check; do not poll."]


def build_why_model(response: dict) -> dict:
    watchdogs_by_id = _watchdog_index(response)
    referenced_wake_ids: set[str] = set()
    aggregate_wait_ids: set[str] = set()
    rendered_categories: set[str] = set()
    items: list[dict] = []

    for item in _section_items(response, "pending_inbound"):
        message_id = str(item.get("message_id") or "")
        kind = str(item.get("kind") or "notice")
        sender = str(item.get("from") or "")
        body_chars = item.get("body_chars")
        try:
            safe_body_chars = int(body_chars)
        except (TypeError, ValueError):
            safe_body_chars = 0
        rendered_categories.add("pending_inbound")
        items.append(_why_item(
            category="pending_inbound",
            severity="attention",
            subject=f"Pending inbound {kind} {message_id or '(unknown)'} from {sender or '(unknown)'} is queued for this pane",
            reason=f"A bridge message is queued for you ({safe_body_chars} chars).",
            next_action="Finish this turn and wait for the incoming [bridge:*] prompt; do not poll.",
            refs={
                "message_id": message_id,
                "from": sender,
                "kind": kind,
                "intent": item.get("intent"),
                "body_chars": safe_body_chars,
                "ref_message_id": item.get("ref_message_id"),
                "ref_aggregate_id": item.get("ref_aggregate_id"),
            },
        ))

    for item in _section_items(response, "outstanding_requests"):
        message_id = str(item.get("message_id") or "")
        target = str(item.get("target") or "")
        req_status = str(item.get("status") or "unknown")
        raw_wake_ids = item.get("watchdog_wake_ids") if isinstance(item.get("watchdog_wake_ids"), list) else []
        wake_ids = [str(wake_id) for wake_id in raw_wake_ids if str(wake_id or "")]
        referenced_wake_ids.update(wake_ids)
        watchdog_note = _watchdog_sentence(wake_ids, watchdogs_by_id)
        if req_status == "pending":
            reason = f"queued for {target or 'target'} but not active yet.{watchdog_note}"
            next_action = f"Wait for delivery; use agent_cancel_message {message_id} only if the human wants to retract before active delivery."
        elif req_status == "inflight":
            reason = f"delivery to {target or 'target'} is reserved or in progress.{watchdog_note}"
            next_action = f"Wait for prompt submission or a watchdog; inspect with agent_view_peer {target} or interrupt only if a wake shows stuck or wrong active work."
        elif req_status == "submitted":
            reason = f"{target or 'target'} prompt was submitted and response may be starting or active.{watchdog_note}"
            next_action = "Wait for the [bridge:*] result or watchdog; do not send duplicate requests."
        elif req_status == "delivered":
            reason = f"waiting for {target or 'target'} to answer.{watchdog_note}"
            next_action = "Wait for the [bridge:*] result. If a watchdog wake arrives, choose one recovery action from that wake."
        else:
            reason = f"request is in {req_status} state.{watchdog_note}"
            next_action = "Wait for bridge progress or inspect only if debugging; do not poll."
        rendered_categories.add("outstanding_requests")
        items.append(_why_item(
            category="request",
            severity="waiting",
            subject=f"Request {message_id or '(unknown)'} to {target or '(unknown)'} is {req_status}",
            reason=reason,
            next_action=next_action,
            refs={
                "message_id": message_id,
                "target": target,
                "status": req_status,
                "aggregate_id": item.get("aggregate_id"),
                "watchdog_wake_ids": wake_ids,
            },
        ))

    for item in _section_items(response, "aggregate_waits"):
        aggregate_id = str(item.get("aggregate_id") or "")
        if aggregate_id:
            aggregate_wait_ids.add(aggregate_id)
        try:
            replied = int(item.get("replied_count", 0))
        except (TypeError, ValueError):
            replied = 0
        try:
            expected = int(item.get("expected_count", 0))
        except (TypeError, ValueError):
            expected = 0
        missing_text, missing_refs, missing_count = _missing_peers_text(item.get("missing_peers"))
        if missing_text:
            reason = f"waiting for {expected - replied if expected >= replied else missing_count} of {expected} peer(s): {missing_text}."
        else:
            reason = f"waiting for aggregate replies ({replied}/{expected} received)."
        rendered_categories.add("aggregate_waits")
        items.append(_why_item(
            category="aggregate",
            severity="waiting",
            subject=f"Aggregate {aggregate_id or '(unknown)'} has {replied}/{expected} replies",
            reason=reason,
            next_action="Wait for the aggregate [bridge:*] result; use agent_aggregate_status only for a human-prompted debug check.",
            refs={
                "aggregate_id": aggregate_id,
                "replied_count": replied,
                "expected_count": expected,
                "missing_peer_count": missing_count,
                "missing_peers": missing_refs,
            },
        ))

    for item in _section_items(response, "alarms"):
        wake_id = str(item.get("wake_id") or "")
        deadline = str(item.get("deadline") or "")
        note = _safe_display_text(item.get("note"))
        reason = "notice-workflow safety wake"
        if deadline:
            reason = f"scheduled for {deadline}: {reason}"
        if note:
            reason = f"{reason} ({note})"
        rendered_categories.add("alarms")
        items.append(_why_item(
            category="alarm",
            severity="info",
            subject=f"Alarm {wake_id or '(unknown)'} is scheduled",
            reason=reason + ".",
            next_action="It will cancel on an incoming peer request/notice; do not treat it as a request result wait or poll.",
            refs={"wake_id": wake_id, "deadline": deadline},
        ))

    for item in _section_items(response, "watchdogs"):
        wake_id = str(item.get("wake_id") or "")
        aggregate_id = str(item.get("aggregate_id") or "")
        if wake_id in referenced_wake_ids:
            rendered_categories.add("watchdogs")
            continue
        if aggregate_id and aggregate_id in aggregate_wait_ids:
            rendered_categories.add("watchdogs")
            continue
        phase = str(item.get("phase") or "unknown")
        message_id = str(item.get("message_id") or "")
        target = str(item.get("target") or "")
        deadline = str(item.get("deadline") or "")
        reason = f"registered in {phase} phase"
        if deadline:
            reason = f"{reason} until {deadline}"
        rendered_categories.add("watchdogs")
        items.append(_why_item(
            category="watchdog",
            severity="waiting",
            subject=f"Watchdog {wake_id or '(unknown)'} is still registered",
            reason=reason + ". This is diagnostic state.",
            next_action="Wait for a bridge wake or inspect only if debugging; do not poll.",
            refs={
                "wake_id": wake_id,
                "message_id": message_id,
                "aggregate_id": aggregate_id,
                "target": target,
                "phase": phase,
                "deadline": deadline,
            },
        ))

    _add_count_only_items(response, items, rendered_categories)
    count_categories = {name for name in WAIT_STATUS_SECTION_NAMES if _section_total(response, name) > 0 or _section_is_truncated(response, name)}
    categories = rendered_categories | count_categories
    if "pending_inbound" in categories:
        status = "attention"
    elif categories:
        status = "waiting"
    else:
        status = "idle"

    truncated_sections = [name for name in WAIT_STATUS_SECTION_NAMES if _section_is_truncated(response, name)]
    return {
        "status": status,
        "items": items,
        "next_actions": _next_actions_for(status, categories),
        "truncated_sections": truncated_sections,
        "caller": response.get("caller") or "",
        "bridge_session": response.get("bridge_session") or "",
        "generated_ts": response.get("generated_ts") or "",
    }


def format_why_text(model: dict) -> str:
    caller = str(model.get("caller") or "(unknown)")
    generated_ts = str(model.get("generated_ts") or "(unknown time)")
    lines = [f"Bridge wait status for {caller} at {generated_ts}"]
    items = [item for item in model.get("items") or [] if isinstance(item, dict)]
    if not items and model.get("status") == "idle":
        lines.append(
            f"No bridge waits for {caller}. There are no outstanding requests, aggregate waits, "
            "alarms, watchdogs, or pending inbound bridge messages."
        )
    else:
        for item in items:
            subject = str(item.get("subject") or "Bridge wait item")
            reason = str(item.get("reason") or "").strip()
            next_action = str(item.get("next_action") or "").strip()
            detail = subject
            if reason:
                detail = f"{detail}; {reason}"
            if next_action:
                detail = f"{detail} {next_action}"
            lines.append(f"- {detail}")
    for action in model.get("next_actions") or []:
        if str(action or ""):
            lines.append(f"Next: {action}")
            break
    if model.get("truncated_sections"):
        lines.append("Some sections are truncated; rerun agent_wait_status --json for full capped metadata.")
    return "\n".join(lines)


def why_json_output(response: dict, model: dict) -> dict:
    return {
        "ok": True,
        "bridge_session": response.get("bridge_session"),
        "caller": response.get("caller"),
        "generated_ts": response.get("generated_ts"),
        "why": {
            "status": model.get("status"),
            "items": model.get("items") or [],
            "next_actions": model.get("next_actions") or [],
            "truncated_sections": model.get("truncated_sections") or [],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_wait_status",
        description=(
            "Show your own bridge waits and queued inbound messages as JSON, or explain them with --why. "
            "Use only for a human-prompted check, after a watchdog wake, or when debugging suspected bridge state; "
            "do not poll for progress or replace waiting for [bridge:*] prompts."
        ),
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--summary", action="store_true", help="print only summary counts and truncation flags")
    output_group.add_argument("--why", action="store_true", help="explain why you are waiting in a human-readable diagnostic format")
    parser.add_argument("--json", action="store_true", help="print JSON output (default)")
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--allow-spoof", action="store_true")
    args = parser.parse_args()

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""

    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=sender,
        allow_spoof=args.allow_spoof,
        tool_name="agent_wait_status",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_wait_status: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_wait_status: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_read:
        print(f"agent_wait_status: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_wait_status: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    ok, response, error = send_command(session, {"op": "wait_status", "from": sender})
    if not ok:
        print(wait_status_error_message(error), file=sys.stderr)
        return 1

    if args.why:
        why_model = build_why_model(response)
        if args.json:
            print(json.dumps(why_json_output(response, why_model), ensure_ascii=False, indent=2))
        else:
            print(format_why_text(why_model))
        return 0

    output = {
        "ok": True,
        "bridge_session": response.get("bridge_session") or session,
        "caller": response.get("caller") or sender,
        "generated_ts": response.get("generated_ts"),
        "limits": response.get("limits") or {},
        "summary": response.get("summary") or {},
    } if args.summary else response
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
