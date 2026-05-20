"""L4 Tool Output Guard — sanitize tool results before writing to LLM context."""
import logging

from .audit_logger import get_audit_logger
from .patterns import redact_credentials

logger = logging.getLogger(__name__)


def sanitize_tool_result(tool_name: str, result: str, session_key: str = "") -> str:
    """
    L4: Scan tool execution results for credentials and redact them
    before the content is added to the LLM context window.
    """
    sanitized, triggered_rules = redact_credentials(result)
    if triggered_rules:
        for rule in triggered_rules:
            get_audit_logger().emit(
                session_key=session_key,
                layer="L4_ToolOutputGuard",
                rule=rule,
                action="REDACTED",
                confidence="HIGH",
                raw_text=result[:200],
                sanitized_preview=sanitized[:120],
            )
        logger.warning(
            "[GUARDRAIL] L4 REDACTED tool=%s rules=%s | session=%s",
            tool_name, triggered_rules, session_key,
        )
    return sanitized
