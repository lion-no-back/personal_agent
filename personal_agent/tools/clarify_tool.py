"""Clarification tool for pausing ambiguous tasks and asking the user."""

from __future__ import annotations

import json
from typing import Any

from .types import ToolDef

MAX_CHOICES = 4


def _flatten_choice(choice: Any) -> str:
    """Normalize model-emitted choice shapes into user-facing text."""
    if choice is None:
        return ""
    if isinstance(choice, str):
        return choice.strip()
    if isinstance(choice, dict):
        for key in ("label", "description", "text", "title"):
            value = choice.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(choice, (list, tuple)):
        return " ".join(_flatten_choice(item) for item in choice).strip()
    return str(choice).strip()


def build_clarify_tool() -> ToolDef:
    def clarify_call(question: str, choices: list | None = None, **kwargs) -> str:
        question = (question or "").strip()
        if not question:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Question text is required.",
                },
                ensure_ascii=False,
            )

        normalized_choices = None
        if choices is not None:
            if not isinstance(choices, list):
                return json.dumps(
                    {
                        "status": "error",
                        "error": "choices must be a list.",
                    },
                    ensure_ascii=False,
                )
            normalized = [_flatten_choice(choice) for choice in choices]
            normalized = [choice for choice in normalized if choice]
            normalized_choices = normalized[:MAX_CHOICES] or None

        tool_ctx = kwargs.get("_tool_ctx") or {}
        callback = tool_ctx.get("clarify_callback")
        if not callback:
            return json.dumps(
                {
                    "status": "error",
                    "error": "clarify callback is not configured.",
                    "question": question,
                    "choices_offered": normalized_choices or [],
                },
                ensure_ascii=False,
            )

        return callback(question, normalized_choices)

    return ToolDef(
        name="clarify",
        description=(
            "当用户任务模糊、缺少关键信息、存在多种处理方式，或继续执行会产生明显成本/副作用时，"
            "调用此工具向用户提问并等待用户补充；用户补充会作为工具结果返回给当前轮继续处理。"
            "question 必须简短、具体，最多两句，只问继续处理所需的最关键问题。"
            "存在明确处理路径时优先提供 choices；必须把每个选项放入 choices 数组，不要把选项编号写进 question。"
            "不要用它确认危险命令；命令审批由执行工具处理。"
            "工作流仍只能由固定触发词触发；疑似工作流但未使用触发词时，用本工具说明触发词并询问用户。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要直接展示给用户的澄清问题，不要在这里枚举 choices。",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CHOICES,
                    "description": (
                        "可选。给用户选择的 1 到 4 个选项；开放式问题可省略。"
                    ),
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        call=clarify_call,
    )
