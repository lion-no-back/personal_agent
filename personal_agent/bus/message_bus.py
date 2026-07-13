"""Thread-safe in-process message bus."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .events import InboundMessage, OutboundEvent, ProgressUpdate

logger = logging.getLogger(__name__)

InboundSubscriber = Callable[[InboundMessage], Any]
OutboundSubscriber = Callable[[OutboundEvent], Any]
ProgressSink = Callable[[str, str], None]


class MessageBus:
    """Small in-process bus for runtime message handoff.

    The bus deliberately stays synchronous and in-memory for the first version.
    Publishers keep today's delivery semantics, while the routing logic moves
    behind one stable interface.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._inbound_subscribers: list[InboundSubscriber] = []
        self._outbound_subscribers: list[OutboundSubscriber] = []
        self._progress_sinks: dict[str, ProgressSink] = {}

    def subscribe_inbound(self, subscriber: InboundSubscriber) -> Callable[[], None]:
        with self._lock:
            self._inbound_subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._inbound_subscribers:
                    self._inbound_subscribers.remove(subscriber)

        return unsubscribe

    def subscribe_outbound(self, subscriber: OutboundSubscriber) -> Callable[[], None]:
        with self._lock:
            self._outbound_subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._outbound_subscribers:
                    self._outbound_subscribers.remove(subscriber)

        return unsubscribe

    def register_progress_sink(
        self,
        target: str,
        sink: ProgressSink,
    ) -> Callable[[], None]:
        """Register a runtime UI sink for ProgressUpdate events."""
        if not target:
            raise ValueError("progress sink target must not be empty")
        with self._lock:
            self._progress_sinks[target] = sink

        def unregister() -> None:
            with self._lock:
                if self._progress_sinks.get(target) is sink:
                    self._progress_sinks.pop(target, None)

        return unregister

    def dispatch_progress(self, event: ProgressUpdate) -> bool:
        """Deliver a ProgressUpdate to its registered runtime sink."""
        with self._lock:
            sink = self._progress_sinks.get(event.target)
        if sink is None:
            logger.debug("ProgressUpdate has no active sink | target=%s", event.target)
            return False

        try:
            sink(event.content, event.status)
            return True
        finally:
            if event.status in {"done", "error", "cancelled"}:
                with self._lock:
                    if self._progress_sinks.get(event.target) is sink:
                        self._progress_sinks.pop(event.target, None)

    def publish_inbound(self, message: InboundMessage) -> list[Any]:
        with self._lock:
            subscribers = list(self._inbound_subscribers)
        if not subscribers:
            logger.debug("Inbound message has no subscribers | source=%s", message.source)
            return []

        results: list[Any] = []
        for subscriber in subscribers:
            try:
                results.append(subscriber(message))
            except Exception:
                logger.exception(
                    "Inbound subscriber failed | source=%s message_id=%s",
                    message.source,
                    message.message_id,
                )
                raise
        return results

    def publish_outbound(self, event: OutboundEvent) -> list[Any]:
        with self._lock:
            subscribers = list(self._outbound_subscribers)
        if not subscribers:
            logger.debug("Outbound event has no subscribers | kind=%s", event.kind)
            return []

        results: list[Any] = []
        for subscriber in subscribers:
            try:
                results.append(subscriber(event))
            except Exception:
                logger.exception("Outbound subscriber failed | kind=%s", event.kind)
                if isinstance(event, ProgressUpdate):
                    raise
        return results
