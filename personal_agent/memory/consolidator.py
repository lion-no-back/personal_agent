"""
记忆合并器（Dream Cycle）：
  1. 即时触发：extractor 每次 append 后检查阈值，超出则同步合并（在后台线程中执行）
  2. 夜间定时：APScheduler 每日凌晨扫描所有用户，合并积压的 timeline

合并逻辑：
  Timeline 全量 + 旧 Compiled Truth → LLM → 新 Compiled Truth
  合并后只保留最近 5 条 timeline（作为原始证据），其余压缩进摘要
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from ..core.llm_router import LLMRouter
    from .hot_memory import HotMemory
    from .cold_memory import ColdMemory

logger = logging.getLogger(__name__)

_HOT_TYPES = ("preference", "experience")

_CONSOLIDATE_SYSTEM = """\
你是记忆整理助手。将以下历史记忆条目整理为简洁的「当前认知摘要」。

要求：
1. 只保留仍然成立的、有长期价值的信息
2. 去除重复、过时、矛盾的条目，合并相似条目
3. 以简洁要点格式输出，每条一行，不加序号和 bullet
4. 严格控制总字符数在 {max_chars} 字以内

直接输出摘要内容，不加标题、解释或 markdown 格式：
"""


class MemoryConsolidator:
    """
    合并 Timeline → 更新 Compiled Truth。
    """

    HOT_THRESHOLD = 15   # 热层 timeline 超过此数时合并
    COLD_THRESHOLD = 15  # 情境 timeline 超过此数时合并
    KEEP_RECENT = 5      # 合并后保留最近几条 timeline 作为原始证据

    def __init__(
        self,
        llm: "LLMRouter",
        hot: "HotMemory",
        cold: "ColdMemory",
        cron_hour: int = 3,
        cron_minute: int = 0,
    ) -> None:
        self._llm = llm
        self._hot = hot
        self._cold = cold
        self._cron_hour = cron_hour
        self._cron_minute = cron_minute
        self._scheduler: Optional[BackgroundScheduler] = None

    # ── 生命周期 ───────────────────────────────────────────────────

    def start(self) -> None:
        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self._nightly_job,
            CronTrigger(hour=self._cron_hour, minute=self._cron_minute),
            id="memory_consolidate",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(
            "MemoryConsolidator: 已启动 | 夜间任务 cron=%02d:%02d",
            self._cron_hour, self._cron_minute,
        )

    def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ── 即时阈值检查（由 extractor 在后台线程中调用） ──────────────

    def check_and_consolidate_hot(self, user_id: str, memory_type: str) -> None:
        if self._hot.timeline_count(user_id, memory_type) >= self.HOT_THRESHOLD:
            self._consolidate_hot(user_id, memory_type)

    def check_and_consolidate_cold(self, user_id: str) -> None:
        if self._cold.context_timeline_count(user_id) >= self.COLD_THRESHOLD:
            self._consolidate_context(user_id)

    # ── 合并逻辑 ───────────────────────────────────────────────────

    def _consolidate_hot(self, user_id: str, memory_type: str) -> None:
        timeline = self._hot.get_timeline(user_id, memory_type)
        if not timeline:
            return

        max_chars = self._hot.max_chars(memory_type)
        old_truth = self._hot.read_compiled_truth(user_id, memory_type)
        context = self._build_context(old_truth, timeline)

        new_truth = self._call_llm(context, max_chars)
        if not new_truth:
            return

        kept = timeline[-self.KEEP_RECENT:]
        self._hot.update_compiled_truth(user_id, memory_type, new_truth)
        self._hot.replace_timeline(user_id, memory_type, kept)
        logger.info(
            "MemoryConsolidator: 热层合并完成 | user=%s type=%s %d→%d条",
            user_id, memory_type, len(timeline), len(kept),
        )

    def _consolidate_context(self, user_id: str) -> None:
        timeline = self._cold.get_context_timeline(user_id)
        if not timeline:
            return

        old_truth = self._cold.read_context_compiled(user_id)
        context = self._build_context(old_truth, timeline)

        new_truth = self._call_llm(context, max_chars=1500)
        if not new_truth:
            return

        kept = timeline[-self.KEEP_RECENT:]
        self._cold.update_context_compiled(user_id, new_truth)
        self._cold.replace_context_timeline(user_id, kept)
        logger.info(
            "MemoryConsolidator: 情境合并完成 | user=%s %d→%d条",
            user_id, len(timeline), len(kept),
        )

    @staticmethod
    def _build_context(old_truth: str, timeline: list[str]) -> str:
        items = "\n".join(f"- {e}" for e in timeline)
        if old_truth:
            return f"现有摘要：\n{old_truth}\n\n新增条目：\n{items}"
        return f"待整理条目：\n{items}"

    def _call_llm(self, context: str, max_chars: int) -> str:
        messages = [
            {"role": "system", "content": _CONSOLIDATE_SYSTEM.format(max_chars=max_chars)},
            {"role": "user", "content": context},
        ]
        result = self._llm.think(messages, temperature=0, task_type="fast")
        return (result or "").strip()

    # ── 夜间全量任务 ───────────────────────────────────────────────

    def _nightly_job(self) -> None:
        logger.info(
            "MemoryConsolidator: 夜间维护开始 | %s",
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        for user_id in self._hot.list_user_ids():
            try:
                for mem_type in _HOT_TYPES:
                    self._consolidate_hot(user_id, mem_type)
                self._consolidate_context(user_id)
            except Exception as e:
                logger.warning(
                    "MemoryConsolidator: 夜间任务异常 | user=%s err=%s", user_id, e
                )
        logger.info("MemoryConsolidator: 夜间维护完成")
