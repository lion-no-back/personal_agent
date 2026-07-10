from __future__ import annotations

from datetime import datetime

from openai import APIStatusError, AuthenticationError


FORCE_REASONING_PATTERNS = ("换推理模型", "用推理模型", "切换推理模型", "重新推理", "深度思考")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def build_runtime_context_block(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    timezone_name = getattr(current.tzinfo, "key", None) or current.tzname() or "local"
    return (
        "[Runtime context]\n"
        f"Current date: {current:%Y-%m-%d}\n"
        f"Current weekday: {WEEKDAYS[current.weekday()]}\n"
        f"Current time: {current:%H:%M:%S}\n"
        f"Current timezone: {timezone_name}\n"
        "Interpret relative time words such as today, tomorrow, yesterday, this week, "
        "last week, this year, recently, and their Chinese equivalents against this date.\n"
        "For time-sensitive current-world facts such as sports events, odds, schedules, "
        "prices, laws, rankings, releases, or public roles, do not rely on stale model "
        "memory. Use available search or knowledge tools when current precision matters; "
        "if no current source is available, say what is uncertain."
    )


def inject_runtime_context(
    messages: list[dict],
    now: datetime | None = None,
) -> list[dict]:
    block = build_runtime_context_block(now)
    copied = [dict(message) for message in messages]
    if copied and copied[0].get("role") == "system":
        copied[0]["content"] = f"{copied[0].get('content') or ''}\n\n{block}"
        return copied
    return [{"role": "system", "content": block}, *copied]


def is_llm_auth_error(exc: BaseException) -> bool:
    """OpenAI SDK: 401 usually means an authentication failure."""
    if isinstance(exc, AuthenticationError):
        return True
    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None) == 401
    return False
