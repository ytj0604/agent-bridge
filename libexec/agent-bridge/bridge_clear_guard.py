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


@dataclass
class ClearGuardResult:
    ok: bool
    hard_blockers: list[ClearViolation] = field(default_factory=list)
    soft_blockers: list[ClearViolation] = field(default_factory=list)

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


def format_clear_guard_result(result: ClearGuardResult) -> str:
    parts: list[str] = []
    for violation in result.hard_blockers:
        refs = f" refs={','.join(violation.refs)}" if violation.refs else ""
        parts.append(f"hard:{violation.code}: {violation.message}{refs}")
    for violation in result.soft_blockers:
        refs = f" refs={','.join(violation.refs)}" if violation.refs else ""
        parts.append(f"soft:{violation.code}: {violation.message}{refs}")
    return "; ".join(parts) if parts else "clear allowed"

