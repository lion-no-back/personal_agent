"""
记忆模块 — 跨会话持久化记忆系统

双层架构：
  热层（始终注入）：MEMORY.md（经验）+ USER.md（偏好），硬字符上限约束
  冷层（始终注入）：context.md（情境）+ events.md（事件），Compiled Truth + Timeline

子组件：
  HotMemory          文件读写 + 字符上限执行
  ColdMemory         文件读写 + 事件滚动删除
  MemoryExtractor    对话结束后异步 LLM 抽取，路由到对应文件
  MemoryRetriever    组装 <user_memory> 块注入 system prompt
  MemoryConsolidator 合并 Timeline→Compiled Truth（阈值触发 + APScheduler 夜间任务）
"""

from pathlib import Path
from typing import TYPE_CHECKING

from .cold_memory import ColdMemory
from .consolidator import MemoryConsolidator
from .extractor import MemoryExtractor
from .hot_memory import HotMemory
from .retriever import MemoryRetriever
from .types import MemoryEntry, MemoryType

if TYPE_CHECKING:
    from ..core.llm_router import LLMRouter


class MemoryModule:
    """记忆模块门面，统一初始化和生命周期管理。"""

    def __init__(self, store_dir: Path, llm: "LLMRouter") -> None:
        self.hot = HotMemory(store_dir)
        self.cold = ColdMemory(store_dir)
        self.consolidator = MemoryConsolidator(llm, self.hot, self.cold)
        # extractor 持有 consolidator 引用，在 append 后即时检查阈值
        self.extractor = MemoryExtractor(llm, self.hot, self.cold, self.consolidator)
        self.retriever = MemoryRetriever(self.hot, self.cold)

    def start(self) -> None:
        """启动夜间合并定时任务。"""
        self.consolidator.start()

    def stop(self) -> None:
        self.consolidator.stop()
        self.extractor.stop()


__all__ = [
    "MemoryModule",
    "HotMemory",
    "ColdMemory",
    "MemoryExtractor",
    "MemoryRetriever",
    "MemoryConsolidator",
    "MemoryType",
    "MemoryEntry",
]
