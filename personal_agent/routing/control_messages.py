from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_workflow_help_request(content: str) -> bool:
    text = (content or "").strip()
    return text == "工作流使用说明"


def is_control_message(content: str) -> bool:
    """Messages that should start a fresh turn instead of answering clarify."""
    text = (content or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return True
    if is_workflow_help_request(text):
        return True
    return _matches_fixed_workflow_trigger(text)


def _matches_fixed_workflow_trigger(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    return any(trigger and trigger in text for trigger in _workflow_triggers())


def _workflow_triggers() -> tuple[str, ...]:
    try:
        from ..workflows import WorkflowRegistry

        triggers: list[str] = []
        for meta in WorkflowRegistry().get_ondemand_workflows():
            triggers.extend(str(item).strip() for item in meta.intent_triggers)
        return tuple(item for item in triggers if item)
    except Exception:
        logger.debug("加载工作流触发词失败，跳过控制消息匹配", exc_info=True)
        return ()
