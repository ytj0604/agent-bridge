#!/usr/bin/env python3
from collections import OrderedDict
from dataclasses import dataclass, field
import threading
from typing import Any


class StateField:
    def __init__(self, state_attr: str, field_name: str) -> None:
        self.state_attr = state_attr
        self.field_name = field_name

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return getattr(getattr(instance, self.state_attr), self.field_name)

    def __set__(self, instance: object, value: Any) -> None:
        setattr(getattr(instance, self.state_attr), self.field_name, value)


class BoundedSet:
    def __init__(self, max_size: int) -> None:
        self.max_size = max(1, int(max_size))
        self.items: OrderedDict[str, None] = OrderedDict()

    def add(self, item: str) -> bool:
        if item in self.items:
            self.items.move_to_end(item)
            return False
        self.items[item] = None
        while len(self.items) > self.max_size:
            self.items.popitem(last=False)
        return True


class TargetLockManager:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def normalize_targets(self, targets) -> list[str]:
        if isinstance(targets, str):
            targets = [targets]
        return sorted({str(target) for target in targets if str(target or "")})

    def acquire(self, targets):
        return _TargetLockContext(self, self.normalize_targets(targets))

    def _locks_for(self, targets: list[str]) -> list[threading.RLock]:
        with self._guard:
            return [
                self._locks.setdefault(target, threading.RLock())
                for target in targets
            ]


class _TargetLockContext:
    def __init__(self, manager: TargetLockManager, targets: list[str]) -> None:
        self.manager = manager
        self.targets = targets
        self.locks: list[threading.RLock] = []

    def __enter__(self) -> list[str]:
        self.locks = self.manager._locks_for(self.targets)
        for lock in self.locks:
            lock.acquire()
        return list(self.targets)

    def __exit__(self, exc_type, exc, tb) -> None:
        for lock in reversed(self.locks):
            lock.release()
        self.locks = []


@dataclass
class LockFacade:
    state_lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class ParticipantCache:
    session_state: dict = field(default_factory=dict)
    participants: dict[str, dict] = field(default_factory=dict)
    panes: dict[str, str] = field(default_factory=dict)
    session_mtime_ns: int | None = None
    startup_backfill_summary: dict[str, dict] = field(default_factory=dict)


@dataclass
class RoutingState:
    busy: dict[str, bool] = field(default_factory=dict)
    reserved: dict[str, str | None] = field(default_factory=dict)
    current_prompt_by_agent: dict[str, dict] = field(default_factory=dict)
    injected_by_nonce: OrderedDict[str, dict] = field(default_factory=OrderedDict)
    last_enter_ts: dict[str, float] = field(default_factory=dict)
    interrupted_turns: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class WatchdogState:
    max_processed_returns: int
    max_processed_capture_requests: int
    processed_returns: BoundedSet = field(init=False)
    processed_capture_requests: BoundedSet = field(init=False)
    watchdogs: dict[str, dict] = field(default_factory=dict)
    alarm_wake_tombstones: OrderedDict[str, dict] = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        self.processed_returns = BoundedSet(self.max_processed_returns)
        self.processed_capture_requests = BoundedSet(self.max_processed_capture_requests)


@dataclass
class ClearState:
    held_interrupt: dict[str, dict] = field(default_factory=dict)
    interrupt_partial_failure_blocks: dict[str, dict] = field(default_factory=dict)
    clear_reservations: dict[str, dict] = field(default_factory=dict)
    pending_self_clears: dict[str, dict] = field(default_factory=dict)


@dataclass
class MaintenanceState:
    last_maintenance: float = 0.0
    last_capture_cleanup: float = 0.0
    last_ingressing_check: float = 0.0
    stop_logged: bool = False
    last_delivery_tick: float = 0.0
