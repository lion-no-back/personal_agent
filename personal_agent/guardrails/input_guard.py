"""L1 Input Guard — applied to user messages before they reach the LLM."""
import logging
import os

from .audit_logger import get_audit_logger
from .patterns import check_input_injection
from .types import GuardResult

logger = logging.getLogger(__name__)

_MAX_INPUT_CHARS = int(os.getenv("GUARDRAIL_MAX_INPUT_CHARS", "4000"))
_ENABLED = os.getenv("GUARDRAIL_INPUT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")

_BLOCKED_MSG = (
    "⚠️ **请求被安全策略拦截**\n\n"
    "检测到此消息包含可能影响 AI 行为的特殊指令模式，已拒绝处理。\n"
    "如认为这是误报，请换一种方式描述您的需求。"
)

_TOO_LONG_MSG = (
    "⚠️ **消息过长**\n\n"
    f"当前消息超过 {_MAX_INPUT_CHARS} 字符限制，请拆分为多条消息发送。"
)


def check_input(text: str, session_key: str = "") -> GuardResult:
    """
    L1 Input guard: validate user message before it reaches the LLM.
    Returns GuardResult with passed=False to block the request.
    """
    if not _ENABLED:
        return GuardResult(passed=True, layer="L1_InputGuard", action="PASSED")

    # ── Length check ─────────────────────────────────────────────────────────
    if len(text) > _MAX_INPUT_CHARS:
        get_audit_logger().emit(
            session_key=session_key,
            layer="L1_InputGuard",
            rule="INPUT_TOO_LONG",
            action="BLOCKED",
            confidence="HIGH",
            raw_text=text[:200],
            sanitized_preview=f"len={len(text)} limit={_MAX_INPUT_CHARS}",
        )
        logger.warning(
            "[GUARDRAIL] L1 BLOCKED INPUT_TOO_LONG | session=%s len=%d",
            session_key, len(text),
        )
        return GuardResult(
            passed=False,
            layer="L1_InputGuard",
            action="BLOCKED",
            rule="INPUT_TOO_LONG",
            confidence="HIGH",
            user_message=_TOO_LONG_MSG,
        )

    # ── Prompt injection / jailbreak check ───────────────────────────────────
    match = check_input_injection(text)
    if match:
        rule, matched_text = match
        get_audit_logger().emit(
            session_key=session_key,
            layer="L1_InputGuard",
            rule=rule,
            action="BLOCKED",
            confidence="HIGH",
            raw_text=text[:200],
            sanitized_preview=f"matched: {matched_text[:80]}",
        )
        logger.warning(
            "[GUARDRAIL] L1 BLOCKED %s | session=%s matched=%r",
            rule, session_key, matched_text[:50],
        )
        return GuardResult(
            passed=False,
            layer="L1_InputGuard",
            action="BLOCKED",
            rule=rule,
            confidence="HIGH",
            user_message=_BLOCKED_MSG,
        )

    return GuardResult(passed=True, layer="L1_InputGuard", action="PASSED")
