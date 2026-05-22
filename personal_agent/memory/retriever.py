"""
记忆检索器：为每次 LLM 调用组装记忆注入块。

注入策略（两层，确定性，无需向量检索）：
  热层（始终注入）：compiled truth（合并摘要）+ 尚未合并的 timeline 末尾 N 条
  冷层（始终注入）：context.md 同上 + 最近 5 条事件

合并前的新记忆通过 timeline 直接可见，不必等到阈值触发合并。
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hot_memory import HotMemory
    from .cold_memory import ColdMemory

logger = logging.getLogger(__name__)

_HOT_RECENT = 8   # 热层：最多展示 N 条未合并 timeline
_CTX_RECENT = 5   # 情境层：最多展示 N 条未合并 timeline


class MemoryRetriever:
    def __init__(self, hot: "HotMemory", cold: "ColdMemory") -> None:
        self._hot = hot
        self._cold = cold

    def build_memory_block(self, user_id: str) -> str:
        """
        返回格式化的 <user_memory> 块，供注入 system prompt。
        若用户暂无任何记忆内容，返回空字符串。
        """
        parts: list[str] = []

        # 热层：偏好（USER.md）
        pref_truth, pref_tl = self._hot.read_both(user_id, "preference")
        section = self._build_section(pref_truth, pref_tl, _HOT_RECENT)
        if section:
            parts.append(f"[用户偏好]\n{section}")

        # 热层：经验（MEMORY.md）
        exp_truth, exp_tl = self._hot.read_both(user_id, "experience")
        section = self._build_section(exp_truth, exp_tl, _HOT_RECENT)
        if section:
            parts.append(f"[经验记忆]\n{section}")

        # 冷层：情境（context.md）
        ctx_truth, ctx_tl = self._cold.read_context_both(user_id)
        section = self._build_section(ctx_truth, ctx_tl, _CTX_RECENT)
        if section:
            parts.append(f"[当前情境]\n{section}")

        # 冷层：事件（events.md）
        events = self._cold.get_recent_events(user_id, n=5)
        if events:
            parts.append("[近期事件]\n" + "\n".join(f"- {e}" for e in events))

        if not parts:
            return ""

        return "<user_memory>\n" + "\n\n".join(parts) + "\n</user_memory>"

    @staticmethod
    def _build_section(truth: str, timeline: list[str], n: int) -> str:
        """
        合并 compiled truth 与最近 N 条 timeline，返回该 section 的文本。
        两者都为空时返回空字符串。
        """
        lines: list[str] = []
        if truth:
            lines.append(truth)
        recent = timeline[-n:] if len(timeline) > n else timeline
        if recent:
            lines.append("\n".join(f"- {e}" for e in recent))
        return "\n".join(lines)
