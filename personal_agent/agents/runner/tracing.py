from __future__ import annotations

import hashlib
from typing import Any

from ...core.telemetry import emit_event


def value_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()[:12]


def event_attrs(event: object) -> dict[str, Any]:
    content = getattr(event, "content", "") or ""
    images = getattr(event, "images", None) or []
    return {
        "input_chars": len(content),
        "has_images": bool(images),
        "image_count": len(images),
        "is_scheduled": bool(getattr(event, "is_scheduled", False)),
    }


def tool_call_attrs(tool_call: dict) -> dict[str, Any]:
    function = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    args = function.get("arguments", "") or ""
    tool_call_id = tool_call.get("id", "") if isinstance(tool_call, dict) else ""
    return {
        "tool_name": function.get("name", "") or "",
        "tool_call_id_hash": value_hash(tool_call_id) if tool_call_id else "",
        "args_chars": len(args),
    }


def emit_runner_event(
    event_name: str,
    *,
    status: str | None = None,
    duration_ms: int | float | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    emit_event(
        event_name,
        source="agent_runner",
        status=status,
        duration_ms=duration_ms,
        attrs=attrs,
    )
