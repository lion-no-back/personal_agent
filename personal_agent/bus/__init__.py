"""In-process message bus for PersonalAgent runtime traffic."""

from .dispatcher import GatewayDispatcher
from .events import (
    DeliveryAttempt,
    DeliveryReport,
    InboundMessage,
    OutboundEvent,
    OutboundFile,
    OutboundImageText,
    OutboundText,
    ProgressUpdate,
    normalize_targets,
)
from .message_bus import MessageBus

__all__ = [
    "GatewayDispatcher",
    "DeliveryAttempt",
    "DeliveryReport",
    "InboundMessage",
    "MessageBus",
    "OutboundEvent",
    "OutboundFile",
    "OutboundImageText",
    "OutboundText",
    "ProgressUpdate",
    "normalize_targets",
]
