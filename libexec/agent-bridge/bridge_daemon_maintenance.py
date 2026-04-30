#!/usr/bin/env python3
from dataclasses import dataclass, field
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
