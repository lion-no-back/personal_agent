"""L5 Output Guard — sanitize the final LLM response before displaying to user."""
import logging
import os

from .audit_logger import get_audit_logger
from .patterns import redact_credentials

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = int(os.getenv("GUARDRAIL_MAX_OUTPUT_CHARS", "8000"))
_ENABLED = os.getenv("GUARDRAIL_OUTPUT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
_REDACTED_NOTICE = "\n\n> ⚠️ _部分内容因包含敏感信息已被自动屏蔽_"


def sanitize_output(text: str, session_key: str = "") -> str:
    """
    L5: Scan the final LLM response for credentials and redact them
    before the text is pushed to the Feishu card.
    """
    if not _ENABLED or not text:
        return text

    sanitized, triggered_rules = redact_credentials(text)
    if triggered_rules:
        for rule in triggered_rules:
            get_audit_logger().emit(
                session_key=session_key,
                layer="L5_OutputGuard",
                rule=rule,
                action="REDACTED",
                confidence="HIGH",
                raw_text=text[:200],
                sanitized_preview=sanitized[:120],
            )
        logger.warning(
            "[GUARDRAIL] L5 REDACTED rules=%s | session=%s",
            triggered_rules, session_key,
        )
        sanitized += _REDACTED_NOTICE
    else:
        sanitized = text

    if len(sanitized) > _MAX_OUTPUT_CHARS:
        sanitized = sanitized[:_MAX_OUTPUT_CHARS] + (
            f"\n\n> _(输出已截断，原长 {len(sanitized)} 字符)_"
        )

    return sanitized
