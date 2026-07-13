from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..bus import InboundMessage


class BaseAgent(ABC):
    """
    Agent 抽象基类。

    所有具体 Agent 必须继承此类并实现消息处理方法。
    设计原则：
    - Agent 只负责业务逻辑（理解消息、调用 LLM、管理上下文）
    - Agent 不直接依赖任何具体的消息平台（飞书、微信等）
    - 平台消息先由 Gateway 转换为标准 InboundMessage，再由 Agent 消费
    - 流式更新和主动投递发布为标准 OutboundEvent，由 GatewayDispatcher 投递
    """

    @abstractmethod
    def process_inbound(
        self,
        message: InboundMessage,
        *,
        publish_progress: bool = False,
        stream_updates_sync_with_llm: bool = False,
    ) -> str:
        """
        处理一条标准入站消息，生成并返回回复内容。

        Args:
            message: 标准 InboundMessage，由 Gateway / scheduler 适配生成。
            publish_progress: 为 True 时将流式进度发布为 ProgressUpdate。

            stream_updates_sync_with_llm:
                       为 True 时，在 LLM 每收到一段流式增量后立刻发布进度（由平台侧
                       再做流式上屏，例如飞书 CardKit）。为 False 时由 Agent 内部按
                       CARD_UPDATE_INTERVAL 节流并模拟打字机（用于旧版卡片 PATCH）。

        Returns:
            完整的回复文本字符串
        """
        ...

    @abstractmethod
    def process(
        self,
        event,
        on_update: Optional[Callable[[str, str], None]] = None,
        *,
        stream_updates_sync_with_llm: bool = False,
    ) -> str:
        """兼容旧调用；新代码应优先使用 process_inbound()."""
        ...
