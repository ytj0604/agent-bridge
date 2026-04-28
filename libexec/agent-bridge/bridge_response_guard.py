#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypedDict

from bridge_util import normalize_kind


class ResponseContext(TypedDict, total=False):
    id: str
    from_agent: str
    to: str
    kind: str
    auto_return: bool


@dataclass(frozen=True)
class ResponseSendViolation:
    sender: str
    requester: str
    message_id: str
    outgoing_kind: str
    blocked_targets: tuple[str, ...]
    source: str


def context_from_current_prompt(sender: str, context: dict) -> ResponseContext:
    return {
        "id": str(context.get("id") or ""),
        "from_agent": str(context.get("from") or ""),
        "to": str(sender or ""),
        "kind": normalize_kind(context.get("kind"), "request"),
        "auto_return": bool(context.get("auto_return")),
    }


def contexts_from_queue(sender: str, queue: Iterable[dict]) -> list[ResponseContext]:
    contexts: list[ResponseContext] = []
    for item in queue:
        if item.get("status") != "delivered":
            continue
        if str(item.get("to") or "") != sender:
            continue
        if normalize_kind(item.get("kind"), "notice") != "request":
            continue
        if not bool(item.get("auto_return")):
            continue
        requester = str(item.get("from") or "")
        if not requester:
            continue
        contexts.append(
            {
                "id": str(item.get("id") or ""),
                "from_agent": requester,
                "to": sender,
                "kind": "request",
                "auto_return": True,
            }
        )
    return contexts


def response_send_violation(
    *,
    sender: str,
    targets: Iterable[str],
    outgoing_kind: str,
    force: bool,
    contexts: Iterable[ResponseContext],
    source: str,
) -> ResponseSendViolation | None:
    if force:
        return None
    kind = normalize_kind(outgoing_kind, "request")
    if kind not in {"request", "notice"}:
        return None
    sender = str(sender or "")
    target_tuple = tuple(str(target or "") for target in targets if str(target or ""))
    target_set = set(target_tuple)
    if not sender or not target_set:
        return None
    for context in contexts:
        if normalize_kind(context.get("kind"), "notice") != "request":
            continue
        if not bool(context.get("auto_return")):
            continue
        if str(context.get("to") or "") != sender:
            continue
        requester = str(context.get("from_agent") or "")
        if not requester or requester == sender:
            continue
        if requester not in target_set:
            continue
        return ResponseSendViolation(
            sender=sender,
            requester=requester,
            message_id=str(context.get("id") or ""),
            outgoing_kind=kind,
            blocked_targets=target_tuple,
            source=source,
        )
    return None


def format_response_send_violation(violation: ResponseSendViolation) -> str:
    extra = f"the outgoing target is that requester"
    if len(violation.blocked_targets) > 1:
        extra = f"the outgoing target list includes that requester ({violation.requester})"
    return (
        "agent_send_peer: response-time guard blocked/rejected this separate agent_send_peer "
        f"because you are responding to an auto-return peer request from {violation.requester} "
        f"(current_prompt.from={violation.requester}) and {extra}. Reply to {violation.requester} "
        "in the current response; bridge auto-returns that reply. third-party peer sends for "
        "review/collaboration are not blocked by this response-time guard, but other validations "
        f"still apply. If you intentionally need a separate request/notice to {violation.requester}, "
        "retry with --force."
    )
