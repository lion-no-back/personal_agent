"""Telemetry helpers for the in-process message bus."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..core.telemetry import emit_event
from .events import (
    DeliveryReport,
    OutboundEvent,
    OutboundFile,
    OutboundImageText,
    OutboundText,
    ProgressUpdate,
)

_SIGNIFICANT_PROGRESS = {"processing", "tool", "done", "error", "cancelled"}


def should_trace_progress(status: str) -> bool:
    return status in _SIGNIFICANT_PROGRESS


def value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def masked_value(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def target_preview(target: str) -> str:
    target = str(target or "")
    if ":" not in target:
        return masked_value(target)
    parts = target.split(":")
    parts[-1] = masked_value(parts[-1])
    return ":".join(parts)


def target_attrs(targets: tuple[str, ...]) -> dict[str, Any]:
    return {
        "target_count": len(targets),
        "target_hashes": [value_hash(target) for target in targets],
        "target_previews": [target_preview(target) for target in targets],
    }


def outbound_attrs(event: OutboundEvent) -> dict[str, Any]:
    attrs: dict[str, Any] = {"kind": event.kind}
    if isinstance(event, ProgressUpdate):
        attrs.update(
            {
                "status": event.status,
                "target_hash": value_hash(event.target),
                "target_preview": target_preview(event.target),
                "progress_chars": len(event.content or ""),
            }
        )
    elif isinstance(event, OutboundText):
        attrs.update(target_attrs(event.targets))
        attrs["text_chars"] = len(event.text or "")
    elif isinstance(event, OutboundFile):
        attrs.update(target_attrs(event.targets))
        attrs["file_ext"] = Path(event.file_path).suffix.lower()
        attrs["filename_ext"] = Path(event.filename).suffix.lower()
    elif isinstance(event, OutboundImageText):
        attrs.update(target_attrs(event.targets))
        attrs["image_ext"] = Path(event.image_path).suffix.lower()
        attrs["text_chars"] = len(event.text or "")
    return attrs


def delivery_attrs(report: DeliveryReport) -> dict[str, Any]:
    return {
        "kind": report.event_kind,
        "matched": report.matched,
        "succeeded": report.succeeded,
        "failed": report.failed,
        "attempts": [
            {
                "target_hash": value_hash(attempt.target),
                "target_preview": target_preview(attempt.target),
                "gateway": attempt.gateway,
                "matched": attempt.matched,
                "succeeded": attempt.succeeded,
                "error": attempt.error[:200] if attempt.error else "",
            }
            for attempt in report.attempts
        ],
    }


def emit_bus_event(
    event_name: str,
    *,
    status: str | None = None,
    attrs: dict[str, Any] | None = None,
    **context: Any,
) -> None:
    emit_event(event_name, source="message_bus", status=status, attrs=attrs, **context)
