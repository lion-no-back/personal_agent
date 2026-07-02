from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_TELEMETRY_DIR = Path("logs") / "telemetry"
_ERROR_STATUSES = {"error", "timeout", "blocked", "cancelled"}
_ERROR_ATTR_KEYS = {
    "cron_job_id",
    "delivery_id",
    "error_code",
    "error_type",
    "file_name",
    "heartbeat_check_id",
    "kind",
    "reason",
    "tool_name",
    "workflow_id",
}


def _resolve_dir(telemetry_dir: str | os.PathLike[str] | None = None) -> Path:
    if telemetry_dir is not None:
        return Path(telemetry_dir)
    return Path(os.getenv("TELEMETRY_DIR") or _DEFAULT_TELEMETRY_DIR)


def iter_events(
    telemetry_dir: str | os.PathLike[str] | None = None,
) -> Iterable[dict[str, Any]]:
    root = _resolve_dir(telemetry_dir)
    if not root.exists() or not root.is_dir():
        return
    for path in sorted(root.glob("events*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        event.setdefault("_file", str(path))
                        yield event
        except OSError:
            continue


def query_trace(
    trace_id: str,
    telemetry_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    events = [
        event
        for event in iter_events(telemetry_dir)
        if str(event.get("trace_id") or "") == trace_id
    ]
    return sorted(events, key=lambda event: str(event.get("ts") or ""))


def _success_rate(completed: int, total: int) -> float | None:
    if total <= 0:
        return None
    return completed / total


def _counter() -> dict[str, int | float | None]:
    return {"total": 0, "completed": 0, "failed": 0, "success_rate": None}


def _duration_summary(values: list[float]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "avg": None}
    return {"count": len(values), "min": min(values), "max": max(values), "avg": sum(values) / len(values)}


def _numeric_duration(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _is_error_event(event: dict[str, Any]) -> bool:
    name = str(event.get("event") or "")
    status = str(event.get("status") or "")
    return (
        status in _ERROR_STATUSES
        or name.endswith(".failed")
        or name.endswith(".timeout")
        or name == "guardrail.blocked"
    )


def _error_summary(event: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"ts": str(event.get("ts") or ""), "event": str(event.get("event") or "")}
    for key in (
        "trace_id",
        "source",
        "status",
        "workflow_id",
        "cron_job_id",
        "tool_call_id",
        "delivery_id",
        "heartbeat_check_id",
    ):
        value = event.get(key)
        if value not in (None, ""):
            summary[key] = value
    attrs = event.get("attrs")
    if isinstance(attrs, dict):
        for key in sorted(_ERROR_ATTR_KEYS):
            value = attrs.get(key)
            if value not in (None, ""):
                summary[key] = value
    return summary


def build_statistics(
    telemetry_dir: str | os.PathLike[str] | None = None,
    *,
    recent_errors_limit: int = 10,
) -> dict[str, Any]:
    events = list(iter_events(telemetry_dir))
    workflow = {"completed": 0, "failed": 0, "timeout": 0}
    tools = _counter()
    tools_by_name: dict[str, dict[str, int | float | None]] = {}
    cron = {"total": 0, "completed": 0, "failed": 0, "timeout": 0, "success_rate": None}
    delivery = {"failed": 0}
    llm_durations: list[float] = []
    errors: list[dict[str, Any]] = []

    for event in events:
        name = str(event.get("event") or "")
        if name == "workflow.completed":
            workflow["completed"] += 1
        elif name == "workflow.failed":
            workflow["failed"] += 1
        elif name == "workflow.timeout":
            workflow["timeout"] += 1

        if name in {"tool.completed", "tool.failed"}:
            tools["total"] += 1
            attrs = event.get("attrs")
            tool_name = "unknown"
            if isinstance(attrs, dict) and attrs.get("tool_name"):
                tool_name = str(attrs["tool_name"])
            per_tool = tools_by_name.setdefault(tool_name, _counter())
            per_tool["total"] += 1
            if name == "tool.completed":
                tools["completed"] += 1
                per_tool["completed"] += 1
            else:
                tools["failed"] += 1
                per_tool["failed"] += 1

        if name == "llm.completed":
            duration = _numeric_duration(event.get("duration_ms"))
            if duration is not None:
                llm_durations.append(duration)

        if name in {"cron.completed", "cron.failed", "cron.timeout"}:
            cron["total"] += 1
            if name == "cron.completed":
                cron["completed"] += 1
            elif name == "cron.failed":
                cron["failed"] += 1
            else:
                cron["timeout"] += 1

        if name == "delivery.failed":
            delivery["failed"] += 1
        if _is_error_event(event):
            errors.append(_error_summary(event))

    tools["success_rate"] = _success_rate(int(tools["completed"]), int(tools["total"]))
    for per_tool in tools_by_name.values():
        per_tool["success_rate"] = _success_rate(int(per_tool["completed"]), int(per_tool["total"]))
    tools["by_tool"] = dict(sorted(tools_by_name.items()))
    cron["success_rate"] = _success_rate(cron["completed"], cron["total"])
    recent_errors = sorted(errors, key=lambda item: str(item.get("ts") or ""), reverse=True)
    if recent_errors_limit >= 0:
        recent_errors = recent_errors[:recent_errors_limit]
    return {
        "event_count": len(events),
        "recent_errors": recent_errors,
        "workflow": workflow,
        "tools": tools,
        "llm_duration_ms": _duration_summary(llm_durations),
        "cron": cron,
        "delivery": delivery,
    }


def format_timeline(events: Iterable[dict[str, Any]]) -> str:
    lines: list[str] = []
    for event in events:
        parts = [str(event.get("ts") or ""), str(event.get("event") or "")]
        status = event.get("status")
        if status:
            parts.append(f"status={status}")
        source = event.get("source")
        if source:
            parts.append(f"source={source}")
        duration_ms = event.get("duration_ms")
        if duration_ms is not None:
            parts.append(f"duration_ms={duration_ms}")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _legacy_trace_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Query PersonalAgent telemetry events.")
    parser.add_argument("trace_id", help="Trace id to query")
    parser.add_argument("--dir", dest="telemetry_dir", default=None)
    args = parser.parse_args(argv)
    print(format_timeline(query_trace(args.trace_id, telemetry_dir=args.telemetry_dir)))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = next((arg for arg in argv if not arg.startswith("-")), "")
    if command not in {"trace", "stats", "errors"}:
        return _legacy_trace_main(argv)

    parser = argparse.ArgumentParser(description="Query PersonalAgent telemetry events.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    trace_parser = subparsers.add_parser("trace")
    trace_parser.add_argument("trace_id")
    trace_parser.add_argument("--dir", dest="telemetry_dir", default=None)
    stats_parser = subparsers.add_parser("stats")
    stats_parser.add_argument("--dir", dest="telemetry_dir", default=None)
    stats_parser.add_argument("--errors-limit", dest="recent_errors_limit", type=int, default=10)
    errors_parser = subparsers.add_parser("errors")
    errors_parser.add_argument("--dir", dest="telemetry_dir", default=None)
    errors_parser.add_argument("--limit", dest="recent_errors_limit", type=int, default=10)

    args = parser.parse_args(argv)
    if args.command == "trace":
        print(format_timeline(query_trace(args.trace_id, telemetry_dir=args.telemetry_dir)))
    elif args.command == "stats":
        _print_json(build_statistics(args.telemetry_dir, recent_errors_limit=args.recent_errors_limit))
    else:
        stats = build_statistics(args.telemetry_dir, recent_errors_limit=args.recent_errors_limit)
        _print_json(stats["recent_errors"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
