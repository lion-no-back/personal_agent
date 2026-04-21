from abc import ABC, abstractmethod
from typing import Callable, Optional


class BaseAgent(ABC):
    """
    Agent 抽象基类。

    所有具体 Agent 必须继承此类并实现 process() 方法。
    设计原则：
    - Agent 只负责业务逻辑（理解消息、调用 LLM、管理上下文）
    - Agent 不直接依赖任何具体的消息平台（飞书、微信等）
    - 平台适配通过 on_update 回调注入，实现解耦
    """

    @abstractmethod
    def process(
        self,
        event,
        on_update: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """
        处理一条消息事件，生成并返回回复内容。

        Args:
            event:     消息事件对象（由具体 Gateway 层解析后传入）
                       至少包含以下属性：
                       - message_id:      消息唯一 ID
                       - chat_id:         会话/群聊 ID
                       - chat_type:       "p2p" 或 "group"
                       - content:         消息纯文本内容（已去除 @提及）
                       - sender_open_id:  发送者 open_id
                       - thread_id:       话题 ID（可选）

            on_update: 平台层注入的卡片更新回调，签名为：
                       on_update(content: str, status: str) -> None
                       - content: 当前已生成的文本内容
                       - status:  "generating" | "done" | "error"
                       为 None 时表示无需实时更新（纯文本模式或测试场景）

        Returns:
            完整的回复文本字符串
        """
        ...
