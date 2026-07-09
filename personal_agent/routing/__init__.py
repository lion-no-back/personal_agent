"""routing —— 消息进入 Agent 前的并发控制与准入决策。

把原先散落在各网关里、且互相重复/不一致的两块关注点抽出来统一：
- SessionRunLocks：同会话请求串行（sessions 层并发控制）
- AccessPolicy：谁能触发机器人（routing 层准入）

会话历史主体仍在 context/conversation_context.py。
"""

from .access_policy import AccessPolicy
from .agent_bindings import (
    AgentBindingConfig,
    AgentConfig,
    DingTalkAccountConfig,
    load_agent_binding_config,
)
from .session_locks import SessionRunLocks

__all__ = [
    "AccessPolicy",
    "AgentBindingConfig",
    "AgentConfig",
    "DingTalkAccountConfig",
    "SessionRunLocks",
    "load_agent_binding_config",
]
