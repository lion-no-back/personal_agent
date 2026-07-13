from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_DEFAULT_TELEMETRY_DIR = Path("logs") / "telemetry"
_RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
_TRACE_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "personal_agent_trace_context",
    default=None,
)
_WRITE_LOCK = threading.Lock()
_CLEANED_DIRS: set[Path] = set()

_CONTEXT_FIELDS = {
    "trace_id",
    "parent_trace_id",
    "run_id",
    "session_key",
    "platform",
    "source",
    "user_id_hash",
    "task_id",
    "message_id",
    "workflow_id",
    "cron_job_id",
    "heartbeat_check_id",
    "tool_call_id",
    "delivery_id",
}
_SENSITIVE_KEYS = ("token", "secret", "password", "authorization", "cookie", "apikey", "api_key")
_TEXT_KEYS = (
    "content",
    "raw_text",
    "user_input",
    "model_output",
    "document_body",
    "page_content",
    "html",
)


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = True
    telemetry_dir: Path = field(default_factory=lambda: _DEFAULT_TELEMETRY_DIR)
    include_text: bool = False
    max_attr_chars: int = 500
    max_event_bytes: int = 4096
    retention_days: int = 14
    sample_success: float = 1.0


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _parse_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def load_config() -> TelemetryConfig:
    return TelemetryConfig(
        enabled=_parse_bool(os.getenv("TELEMETRY_ENABLED"), True),
        telemetry_dir=Path(os.getenv("TELEMETRY_DIR") or _DEFAULT_TELEMETRY_DIR),
        include_text=_parse_bool(os.getenv("TELEMETRY_INCLUDE_TEXT"), False),
        max_attr_chars=max(1, _parse_int(os.getenv("TELEMETRY_MAX_ATTR_CHARS"), 500)),
        max_event_bytes=max(256, _parse_int(os.getenv("TELEMETRY_MAX_EVENT_BYTES"), 4096)),
        retention_days=max(0, _parse_int(os.getenv("TELEMETRY_RETENTION_DAYS"), 14)),
        sample_success=max(0.0, min(1.0, _parse_float(os.getenv("TELEMETRY_SAMPLE_SUCCESS"), 1.0))),
    )


def _new_trace_id() -> str:
    return secrets.token_hex(12)


def _context_from_env() -> dict[str, Any] | None:
    mapping = {
        "TELEMETRY_TRACE_ID": "trace_id",
        "TELEMETRY_PARENT_TRACE_ID": "parent_trace_id",
        "TELEMETRY_RUN_ID": "run_id",
        "TELEMETRY_SESSION_KEY": "session_key",
        "TELEMETRY_PLATFORM": "platform",
        "TELEMETRY_USER_ID_HASH": "user_id_hash",
        "TELEMETRY_TASK_ID": "task_id",
        "TELEMETRY_MESSAGE_ID": "message_id",
        "TELEMETRY_WORKFLOW_ID": "workflow_id",
        "TELEMETRY_CRON_JOB_ID": "cron_job_id",
        "TELEMETRY_HEARTBEAT_CHECK_ID": "heartbeat_check_id",
        "TELEMETRY_TOOL_CALL_ID": "tool_call_id",
        "TELEMETRY_DELIVERY_ID": "delivery_id",
    }
    context = {field: os.environ[name] for name, field in mapping.items() if os.environ.get(name)}
    if not context:
        return None
    context.setdefault("run_id", _RUN_ID)
    return context


def get_trace_context() -> dict[str, Any] | None:
    context = _TRACE_CONTEXT.get()
    if context is None:
        context = _context_from_env()
        if context is not None:
            _TRACE_CONTEXT.set(dict(context))
    return dict(context) if context is not None else None


def set_trace_context(context: Mapping[str, Any] | None = None, **updates: Any) -> dict[str, Any] | None:
    if context is None and not updates:
        _TRACE_CONTEXT.set(None)
        return None
    current = dict(context or get_trace_context() or {})
    for key, value in updates.items():
        if key in _CONTEXT_FIELDS and value is not None:
            current[key] = value
    if "trace_id" not in current:
        current["trace_id"] = _new_trace_id()
    current.setdefault("run_id", _RUN_ID)
    _TRACE_CONTEXT.set(current)
    return dict(current)


def start_trace(parent_trace_id: str | None = None, **fields: Any) -> dict[str, Any]:
    context = {key: value for key, value in fields.items() if key in _CONTEXT_FIELDS and value is not None}
    context["trace_id"] = fields.get("trace_id") or _new_trace_id()
    context["run_id"] = fields.get("run_id") or _RUN_ID
    if parent_trace_id is not None:
        context["parent_trace_id"] = parent_trace_id
    _TRACE_CONTEXT.set(context)
    return dict(context)


def telemetry_env(context: Mapping[str, Any] | None = None) -> dict[str, str]:
    source = dict(context or get_trace_context() or {})
    mapping = {
        "trace_id": "TELEMETRY_TRACE_ID",
        "parent_trace_id": "TELEMETRY_PARENT_TRACE_ID",
        "run_id": "TELEMETRY_RUN_ID",
        "session_key": "TELEMETRY_SESSION_KEY",
        "platform": "TELEMETRY_PLATFORM",
        "user_id_hash": "TELEMETRY_USER_ID_HASH",
        "task_id": "TELEMETRY_TASK_ID",
        "message_id": "TELEMETRY_MESSAGE_ID",
        "workflow_id": "TELEMETRY_WORKFLOW_ID",
        "cron_job_id": "TELEMETRY_CRON_JOB_ID",
        "heartbeat_check_id": "TELEMETRY_HEARTBEAT_CHECK_ID",
        "tool_call_id": "TELEMETRY_TOOL_CALL_ID",
        "delivery_id": "TELEMETRY_DELIVERY_ID",
    }
    env = {env_name: str(source[key]) for key, env_name in mapping.items() if source.get(key) is not None}
    env["TELEMETRY_PROCESS_KIND"] = "sub"
    return env


def _safe_json_value(value: Any, max_chars: int) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _safe_json_value(v, max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item, max_chars) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        result = value
    else:
        result = str(value)
    if isinstance(result, str) and len(result) > max_chars:
        return result[:max_chars] + "...[truncated]"
    return result


def sanitize_attrs(attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    config = load_config()
    if not attrs:
        return {}
    result: dict[str, Any] = {}
    for key, value in attrs.items():
        key_str = str(key)
        lower = key_str.lower()
        if any(sensitive in lower for sensitive in _SENSITIVE_KEYS):
            result[key_str] = "[REDACTED]"
        elif not config.include_text and any(text_key in lower for text_key in _TEXT_KEYS):
            result[key_str] = "[REDACTED]"
        else:
            result[key_str] = _safe_json_value(value, config.max_attr_chars)
    return result


def _event_path(config: TelemetryConfig) -> Path:
    kind = os.getenv("TELEMETRY_PROCESS_KIND", "main")
    pid = os.getpid()
    if kind == "sub":
        return config.telemetry_dir / f"events.sub.{pid}.jsonl"
    return config.telemetry_dir / "events.main.jsonl"


def _cleanup_old_files(config: TelemetryConfig) -> None:
    root = config.telemetry_dir
    if root in _CLEANED_DIRS or config.retention_days <= 0:
        return
    _CLEANED_DIRS.add(root)
    cutoff = time.time() - timedelta(days=config.retention_days).total_seconds()
    try:
        for path in root.glob("events*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        return


def _truncate_event(event: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    payload = json.dumps(event, ensure_ascii=False, default=str)
    if len(payload.encode("utf-8")) <= max_bytes:
        return event
    attrs = dict(event.get("attrs") or {})
    attrs["truncated"] = True
    event = dict(event)
    event["attrs"] = attrs
    while attrs and len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")) > max_bytes:
        key = next(reversed(attrs))
        attrs.pop(key, None)
    return event


def emit_event(
    event: str,
    *,
    source: str | None = None,
    status: str | None = None,
    duration_ms: int | float | None = None,
    attrs: Mapping[str, Any] | None = None,
    **context_updates: Any,
) -> None:
    try:
        config = load_config()
        if not config.enabled:
            return
        context = (
            set_trace_context(**context_updates)
            if context_updates
            else get_trace_context()
        )
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            "trace_id": context.get("trace_id") if context else _new_trace_id(),
            "parent_trace_id": context.get("parent_trace_id") if context else None,
            "run_id": context.get("run_id") if context else _RUN_ID,
            "session_key": context.get("session_key") if context else None,
            "platform": context.get("platform") if context else None,
            "source": source or (context.get("source") if context else None),
            "task_id": context.get("task_id") if context else None,
            "message_id": context.get("message_id") if context else None,
            "workflow_id": context.get("workflow_id") if context else None,
            "cron_job_id": context.get("cron_job_id") if context else None,
            "heartbeat_check_id": context.get("heartbeat_check_id") if context else None,
            "tool_call_id": context.get("tool_call_id") if context else None,
            "delivery_id": context.get("delivery_id") if context else None,
            "process_id": os.getpid(),
            "thread_name": threading.current_thread().name,
            "status": status,
            "duration_ms": duration_ms,
            "attrs": sanitize_attrs(attrs),
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        payload = _truncate_event(payload, config.max_event_bytes)
        path = _event_path(config)
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            _cleanup_old_files(config)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        try:
            logger.warning("Telemetry emit failed: %s", exc)
        except Exception:
            pass


def _reset_for_tests() -> None:
    _TRACE_CONTEXT.set(None)
    _CLEANED_DIRS.clear()
