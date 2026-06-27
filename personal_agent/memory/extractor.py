"""
记忆抽取器：对话结束后异步调用 LLM，判断哪些内容值得持久化。

写入路径：
  preference / experience → HotMemory.append_timeline（热层）
  context                → ColdMemory.append_context_timeline（冷层）
  event                  → ColdMemory.append_event（冷层）

合并检查：每次 append 后检查 timeline 条数，超过阈值时触发合并。
"""

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..core.llm_router import LLMRouter
    from .hot_memory import HotMemory
    from .cold_memory import ColdMemory
    from .consolidator import MemoryConsolidator

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = """\
你是记忆提取助手，从对话中提取值得长期记忆的信息。

## 记忆类型
- preference：用户稳定的偏好、沟通风格、工作习惯。示例：不用 emoji、后台执行长命令、喜欢表格格式
- experience：工具执行知识或任务流程经验。示例：某命令必须在特定目录执行、某技能的正确执行顺序
- context：正在进行的项目/长期任务背景。示例：用户做电商选品项目、服务器在某路径
- event：已发生的具体操作事件。示例：导出了某报告、清空了某会话、安装了某依赖

## 判断标准
- 只提取明确、有长期价值的信息，忽略一次性或模糊内容
- importance：0.3（一般参考）/ 0.6（有用信息）/ 0.9（重要且稳定）
- 无值得记忆的内容时返回空数组

返回 JSON 数组（不加 markdown 代码块）：
[{"type": "preference|experience|context|event", "content": "简洁描述", "importance": 0.6}]
"""


class MemoryExtractor:
    """
    对话结束后异步 LLM 抽取记忆。
    通过 extract_async() 提交后台任务，不阻塞主线程。
    """

    def __init__(
        self,
        llm: "LLMRouter",
        hot: "HotMemory",
        cold: "ColdMemory",
        consolidator: Optional["MemoryConsolidator"] = None,
    ) -> None:
        self._llm = llm
        self._hot = hot
        self._cold = cold
        self._consolidator = consolidator
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mem-extract")

    # ── 公开接口 ───────────────────────────────────────────────────

    def extract_async(self, user_id: str, conversation: list[dict]) -> None:
        """对话结束后异步触发，不阻塞飞书回复链路。

        用 copy_context() 把当前请求的 usage 归属（current_user/source）带进后台线程，
        否则线程池 submit 不会继承 contextvar，记忆抽取的 token 将无法归账到发送人。
        """
        ctx = contextvars.copy_context()
        self._executor.submit(ctx.run, self._safe_extract, user_id, conversation)

    def save_direct(self, user_id: str, content: str, memory_type: str = "experience") -> None:
        """
        用户主动 /remember 时直接写入，跳过 LLM 抽取。
        默认写入经验记忆（MEMORY.md）。
        """
        self._save(user_id, memory_type, content.strip())
        logger.info("MemoryExtractor: direct save | user=%s type=%s", user_id, memory_type)

    def stop(self) -> None:
        """关闭线程池，服务重启时调用。"""
        self._executor.shutdown(wait=False)

    # ── 内部抽取 ───────────────────────────────────────────────────

    def _safe_extract(self, user_id: str, conversation: list[dict]) -> None:
        from ..core.usage_tracker import usage_source
        try:
            with usage_source("memory"):  # 标记本块内 LLM 调用归到 memory 来源
                self._extract(user_id, conversation)
        except Exception as e:
            logger.warning("MemoryExtractor: 抽取失败 | user=%s err=%s", user_id, e)

    def _extract(self, user_id: str, conversation: list[dict]) -> None:
        # 只取 user/assistant 的纯文本轮次，排除 system / tool
        turns = [
            m for m in conversation
            if m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and m.get("content", "").strip()
        ]
        if not turns:
            return

        # 取最近 6 条（3 轮对话），避免 token 过多
        recent = turns[-6:]
        dialog = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {str(m['content'])[:300]}"
            for m in recent
        )

        messages = [
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": f"请从以下对话提取记忆：\n\n{dialog}"},
        ]

        raw = self._llm.think(messages, temperature=0, task_type="fast")
        if not raw:
            return

        entries = self._parse_json(raw)
        if not entries:
            return

        for item in entries:
            if not isinstance(item, dict):
                continue
            mem_type = item.get("type", "")
            content = str(item.get("content", "")).strip()
            if not content or mem_type not in ("preference", "experience", "context", "event"):
                continue
            self._save(user_id, mem_type, content)
            logger.info(
                "MemoryExtractor: 已保存 | user=%s type=%s content=%s",
                user_id, mem_type, content[:60],
            )

    @staticmethod
    def _parse_json(raw: str) -> list:
        raw = raw.strip()
        # 清理 LLM 可能包裹的 markdown 代码块
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
        try:
            result = json.loads(raw)
            return result if isinstance(result, list) else []
        except json.JSONDecodeError:
            logger.debug("MemoryExtractor: JSON 解析失败 | raw=%s", raw[:100])
            return []

    def _save(self, user_id: str, mem_type: str, content: str) -> None:
        if mem_type in ("preference", "experience"):
            self._hot.append_timeline(user_id, mem_type, content)
            if self._consolidator:
                self._consolidator.check_and_consolidate_hot(user_id, mem_type)
        elif mem_type == "context":
            self._cold.append_context_timeline(user_id, content)
            if self._consolidator:
                self._consolidator.check_and_consolidate_cold(user_id)
        elif mem_type == "event":
            self._cold.append_event(user_id, content)
