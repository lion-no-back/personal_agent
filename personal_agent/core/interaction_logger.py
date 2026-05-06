"""
LLM 交互日志记录器

职责：
- 按天写入独立文件（logs/interaction_YYYY-MM-DD.log）
- 完整记录每次 LLM 调用的 messages 数组、工具调用参数、工具结果、最终回复
- 线程安全，多 worker 并发写入不乱序
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 单条消息内容在日志中的最大显示长度（不影响实际调用，只影响记录详细度）
_MSG_PREVIEW_LEN = 2000
# 工具结果在日志中的最大显示长度
_TOOL_RESULT_LEN = 3000
# 最终回复在日志中的最大显示长度
_FINAL_LEN = 3000


def _trunc(text: str, limit: int) -> str:
    """截断超长文本，附加省略提示。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... (省略 {len(text) - limit} 字符)"


def _fmt_content(msg: dict) -> str:
    """格式化单条消息内容用于日志输出。"""
    role = msg.get("role", "?")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")
    tool_call_id = msg.get("tool_call_id")
    name = msg.get("name")

    if role == "tool":
        header = f"[tool:{name or '?'}]"
        body = _trunc(str(content or ""), _MSG_PREVIEW_LEN).replace("\n", "↵")
        return f"{header} {body}"

    if tool_calls:
        calls = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            calls.append(f"{fn.get('name', '?')}({fn.get('arguments', '')[:200]})")
        return f"[{role}] <tool_calls> " + " | ".join(calls)

    body = _trunc(str(content or ""), _MSG_PREVIEW_LEN).replace("\n", "↵")
    return f"[{role}] {body}"


class InteractionLogger:
    """
    按日期分文件写入 LLM 交互日志。

    日志目录结构：
      logs/
        interaction_2026-05-05.log
        interaction_2026-05-06.log
        ...
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ──────────────────────────────────────────────
    #   底层写入
    # ──────────────────────────────────────────────

    def _path(self) -> Path:
        return self._log_dir / f"interaction_{datetime.now():%Y-%m-%d}.log"

    def _write(self, text: str) -> None:
        """线程安全地追加写入当日日志文件。"""
        with self._lock:
            with open(self._path(), "a", encoding="utf-8") as f:
                f.write(text)

    # ──────────────────────────────────────────────
    #   公开日志接口
    # ──────────────────────────────────────────────

    def log_session_start(self, session_key: str, user_input: str) -> None:
        """记录一次对话的开始：时间戳、会话 key、用户输入。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"\n{'═' * 80}\n"
            f"[{ts}] SESSION: {session_key}\n"
            f"USER: {_trunc(user_input, 500)}\n"
            f"{'═' * 80}\n"
        )
        self._write(text)

    def log_llm_call(
        self, session_key: str, round_n: int, messages: List[Dict[str, Any]]
    ) -> None:
        """记录一次 LLM 调用：发送的完整 messages 数组。"""
        lines = [f"\n── LLM CALL #{round_n} SESSION={session_key} {'─' * 40}"]
        lines.append(f"MESSAGES ({len(messages)} 条):")
        for msg in messages:
            lines.append(f"  {_fmt_content(msg)}")
        self._write("\n".join(lines) + "\n")

    def log_llm_response(
        self,
        session_key: str,
        round_n: int,
        text: str,
        tool_calls: Optional[List[Dict[str, Any]]],
    ) -> None:
        """记录 LLM 的回复：文本内容与工具调用列表。"""
        lines = [f"LLM RESPONSE #{round_n} SESSION={session_key}:"]
        if text:
            lines.append(f"  TEXT: {_trunc(text, 1000).replace(chr(10), '↵')}")
        if tool_calls:
            lines.append(f"  TOOL_CALLS ({len(tool_calls)} 个):")
            for i, tc in enumerate(tool_calls, 1):
                fn = tc.get("function", {})
                args_str = fn.get("arguments", "")
                lines.append(f"    #{i} {fn.get('name', '?')}  args: {args_str[:300]}")
        if not text and not tool_calls:
            lines.append("  (空响应)")
        self._write("\n".join(lines) + "\n")

    def log_tool_result(
        self,
        session_key: str,
        tool_name: str,
        args: Dict[str, Any],
        result: str,
    ) -> None:
        """记录单次工具调用的参数与返回结果。"""
        try:
            args_str = json.dumps(args, ensure_ascii=False)
        except Exception:
            args_str = str(args)
        result_preview = _trunc(result, _TOOL_RESULT_LEN).replace("\n", "↵")
        text = (
            f"\n── TOOL: {tool_name} {'─' * 60}\n"
            f"  ARGS:   {args_str[:500]}\n"
            f"  RESULT: {result_preview}\n"
        )
        self._write(text)

    def log_final(self, session_key: str, response: str) -> None:
        """记录本次对话的最终回复，并写入结束分隔线。"""
        preview = _trunc(response, _FINAL_LEN)
        text = (
            f"\n── FINAL RESPONSE {'─' * 60}\n"
            f"{preview}\n"
            f"{'═' * 80}\n"
        )
        self._write(text)
