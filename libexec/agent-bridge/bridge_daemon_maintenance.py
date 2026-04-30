#!/usr/bin/env python3
from dataclasses import dataclass, field
import threading
import time


@dataclass
class DeliveryRequest:
    target: str | None = None
    message_id: str = ""
    command_aware: bool = False
    reason: str = ""
    created_ts: float = field(default_factory=time.time)
    drained: bool = False


class DeliveryScheduler:
    def request_delivery(
        self,
        target: str | None = None,
        *,
        message_id: str = "",
        command_aware: bool = False,
        reason: str = "",
    ) -> DeliveryRequest:
        normalized_target = str(target) if target is not None else None
        return DeliveryRequest(
            target=normalized_target,
            message_id=str(message_id or ""),
            command_aware=bool(command_aware),
            reason=str(reason or ""),
        )

    def drain_inline(self, d, request: DeliveryRequest) -> None:
        if request.command_aware:
            d.try_deliver_command_aware(request.target, message_id=request.message_id)
        else:
            d.try_deliver(request.target)
        request.drained = True


class MaintenanceScheduler:
    TASKS = (
        ("stale_inflight", "requeue_stale_inflight"),
        ("watchdogs", "check_watchdogs"),
        ("turn_id_mismatch", "expire_turn_id_mismatch_contexts"),
        ("aged_ingressing", "_promote_aged_ingressing"),
        ("retry_enter", "retry_enter_for_inflight"),
        ("delivery_tick", "maintenance_delivery_tick"),
        ("capture_cleanup", "cleanup_capture_responses"),
    )

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self.start_count = 0
        self.stop_count = 0

    def start(self, d) -> None:
        if self.is_running():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(
            target=self.loop,
            args=(d,),
            name="bridge-maintenance-scheduler",
            daemon=True,
        )
        self.start_count += 1
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            if timeout is None:
                thread.join()
            else:
                thread.join(timeout=max(0.0, float(timeout)))
        if thread is None or not thread.is_alive():
            self._thread = None
        self.stop_count += 1

    def wake(self) -> None:
        self._wake_event.set()

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def run_once(self, d) -> bool:
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            for task_name, method_name in self.TASKS:
                try:
                    getattr(d, method_name)()
                except Exception as exc:
                    d.safe_log("maintenance_task_failed", task=task_name, error=repr(exc))
        finally:
            self._run_lock.release()
        return True

    def loop(self, d) -> None:
        while not self._stop_event.is_set():
            self.run_once(d)
            if self._stop_event.is_set():
                break
            self._wake_event.wait(0.25)
            self._wake_event.clear()
