#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from bridge_util import classify_prior_for_hint


@dataclass
class ClearViolation:
    code: str
    message: str
    hard: bool = True
    refs: list[str] = field(default_factory=list)
    target: str = ""


@dataclass
class ClearGuardResult:
    ok: bool
    hard_blockers: list[ClearViolation] = field(default_factory=list)
    soft_blockers: list[ClearViolation] = field(default_factory=list)
    force_attempted: bool = False

    @property
    def has_soft(self) -> bool:
        return bool(self.soft_blockers)

    @property
    def has_hard(self) -> bool:
        return bool(self.hard_blockers)


def cancellable_queue_rows(
    queue: Iterable[dict],
    *,
    target: str,
    last_enter_ts: dict[str, float] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for item in queue:
        if str(item.get("to") or "") != target:
            continue
        if classify_prior_for_hint(item, last_enter_ts) == "cancel":
            rows.append(dict(item))
    return rows


def active_queue_rows(
    queue: Iterable[dict],
    *,
    target: str,
    last_enter_ts: dict[str, float] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for item in queue:
        if str(item.get("to") or "") != target:
            continue
        if classify_prior_for_hint(item, last_enter_ts) == "interrupt":
            rows.append(dict(item))
    return rows


def target_originated_requests(queue: Iterable[dict], *, target: str) -> list[dict]:
    out: list[dict] = []
    for item in queue:
        if str(item.get("from") or "") == target and str(item.get("kind") or "request") == "request":
            out.append(dict(item))
    return out


_SOFT_FORCE_ACTIONS = {
    "target_cancellable_messages": "cancel cancellable inbound work",
    "target_owned_alarms": "cancel target-owned alarms",
    "target_originated_requests": "disable auto-return for target-originated requests",
    "target_aggregate_requester": "cancel incomplete aggregate waits",
}


def _force_actions_for(violations: Iterable[ClearViolation]) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    for violation in violations:
        action = _SOFT_FORCE_ACTIONS.get(violation.code)
        if not action or action in seen:
            continue
        seen.add(action)
        actions.append(action)
    return actions


def format_clear_guard_result(
    result: ClearGuardResult,
    *,
    force_attempted: bool | None = None,
    suppress_force_hint: bool = False,
    include_targets: bool = False,
) -> str:
    force_was_attempted = result.force_attempted if force_attempted is None else force_attempted
    parts: list[str] = []
    for violation in result.hard_blockers:
        refs = f" refs={','.join(violation.refs)}" if violation.refs else ""
        prefix = "hard" if violation.hard else "soft"
        target_prefix = f"{violation.target} " if include_targets and violation.target else ""
        parts.append(f"{target_prefix}{prefix}:{violation.code}: {violation.message}{refs}")
    for violation in result.soft_blockers:
        refs = f" refs={','.join(violation.refs)}" if violation.refs else ""
        prefix = "hard" if violation.hard else "soft"
        target_prefix = f"{violation.target} " if include_targets and violation.target else ""
        parts.append(f"{target_prefix}{prefix}:{violation.code}: {violation.message}{refs}")
    actions = _force_actions_for(result.soft_blockers)
    if actions and not force_was_attempted and not suppress_force_hint:
        parts.append(f"Retry with --force to {'; '.join(actions)}.")
    return "; ".join(parts) if parts else "clear allowed"
