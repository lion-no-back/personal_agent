"""Dispatch outbound bus events to the currently running gateways."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from .events import (
    DeliveryAttempt,
    DeliveryReport,
    OutboundEvent,
    OutboundFile,
    OutboundImageText,
    OutboundText,
    ProgressUpdate,
)
from .message_bus import MessageBus

logger = logging.getLogger(__name__)


class GatewayDispatcher:
    """Translate outbound events into the existing gateway delivery interface."""

    def __init__(self, gateways: Iterable[Any], message_bus: MessageBus | None = None) -> None:
        self._gateways = list(gateways)
        self._message_bus = message_bus

    def dispatch(self, event: OutboundEvent) -> DeliveryReport:
        if isinstance(event, OutboundText):
            return self._dispatch_text(event)
        elif isinstance(event, OutboundFile):
            return self._dispatch_file(event)
        elif isinstance(event, OutboundImageText):
            return self._dispatch_image_text(event)
        elif isinstance(event, ProgressUpdate):
            self._dispatch_progress(event)
            return DeliveryReport(event_kind=event.kind)
        else:
            logger.warning("Unknown outbound event type: %s", type(event).__name__)
            return DeliveryReport(event_kind=getattr(event, "kind", type(event).__name__))

    def _dispatch_text(self, event: OutboundText) -> DeliveryReport:
        attempts: list[DeliveryAttempt] = []
        for target in event.targets:
            for gateway in self._gateways:
                gateway_name = getattr(gateway, "platform", type(gateway).__name__)
                if not self._gateway_accepts_target(gateway, target):
                    continue
                try:
                    gateway.send_text(target, event.text)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, True))
                except NotImplementedError:
                    logger.debug("%s.send_text not implemented, skipped", gateway_name)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, False, "not implemented"))
                except Exception as exc:
                    logger.error("%s.send_text failed: %s", gateway_name, exc)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, False, str(exc)))
        return DeliveryReport(event_kind=event.kind, attempts=tuple(attempts))

    def _dispatch_file(self, event: OutboundFile) -> DeliveryReport:
        attempts: list[DeliveryAttempt] = []
        for target in event.targets:
            for gateway in self._gateways:
                gateway_name = getattr(gateway, "platform", type(gateway).__name__)
                if not self._gateway_accepts_target(gateway, target):
                    continue
                try:
                    gateway.send_file(target, event.file_path, event.filename)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, True))
                except NotImplementedError:
                    logger.debug("%s.send_file not implemented, skipped", gateway_name)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, False, "not implemented"))
                except Exception as exc:
                    logger.error("%s.send_file failed: %s", gateway_name, exc)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, False, str(exc)))
        return DeliveryReport(event_kind=event.kind, attempts=tuple(attempts))

    def _dispatch_image_text(self, event: OutboundImageText) -> DeliveryReport:
        attempts: list[DeliveryAttempt] = []
        for target in event.targets:
            for gateway in self._gateways:
                gateway_name = getattr(gateway, "platform", type(gateway).__name__)
                if not self._gateway_accepts_target(gateway, target):
                    continue
                try:
                    gateway.send_image_text(target, event.image_path, event.text)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, True))
                except NotImplementedError:
                    logger.debug(
                        "%s.send_image_text not implemented, skipped",
                        gateway_name,
                    )
                    attempts.append(DeliveryAttempt(target, gateway_name, True, False, "not implemented"))
                except Exception as exc:
                    logger.error("%s.send_image_text failed: %s", gateway_name, exc)
                    attempts.append(DeliveryAttempt(target, gateway_name, True, False, str(exc)))
        return DeliveryReport(event_kind=event.kind, attempts=tuple(attempts))

    def _dispatch_progress(self, event: ProgressUpdate) -> None:
        if self._message_bus is None:
            logger.debug("ProgressUpdate has no message bus registry | target=%s", event.target)
            return
        self._message_bus.dispatch_progress(event)

    @staticmethod
    def _gateway_accepts_target(gateway: Any, target: str) -> bool:
        platform = getattr(gateway, "platform", "")
        if target.startswith("dingtalk:"):
            parts = target.split(":", 3)
            if len(parts) == 3:
                return platform == "dingtalk:default"
            if len(parts) == 4:
                return platform == f"dingtalk:{parts[1]}"
            return False
        if target.startswith("feishu:"):
            return platform == "feishu"
        if ":" in target:
            return False
        return platform == "feishu" and target.startswith(("ou_", "oc_", "p2p_"))
