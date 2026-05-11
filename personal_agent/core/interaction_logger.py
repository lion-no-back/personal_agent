"""
LLM 交互日志记录器 v5

日志目录（按 session + 进程启动时间聚合）：
  logs/2026.5/7/p2p_7f25e707@152001.log  — 同一用户 × 同一次运行的全部对话
  logs/2026.5/7/p2p_7f25e707@163045.log  — 重启后自动新建文件

  每次进程启动生成唯一 Run ID（HHMMss），写入文件顶部 Banner。
  同一运行内的多轮对话仍追加到同一文件，用 ━━━ 分隔。

文件格式（逐字对齐）：
  %(asctime)s.%(msecs)03d  %(levelname)-8s  %(trace)-8s  %(evt)-20s  %(message)s

  2026-05-07 14:23:01.234  INFO      a3f2b1c4  agent.start           🚀 USER: ...
  2026-05-07 14:23:01.456  INFO      a3f2b1c4  llm.request           ▶ LLM CALL #1 · 2 messages
  2026-05-07 14:23:01.457  DEBUG     a3f2b1c4  llm.request           ├─ system: 你是助手...
  2026-05-07 14:23:01.457  DEBUG     a3f2b1c4  llm.request           │  请用简洁语言回答...
  2026-05-07 14:23:01.458  DEBUG     a3f2b1c4  llm.request           └─ user: 查询天气
  2026-05-07 14:23:03.201  INFO      a3f2b1c4  llm.response          ✔ #1 · 1.74s · tool_calls=1
  2026-05-07 14:23:03.202  DEBUG     a3f2b1c4  llm.response          └─ web_fetch({"url":"..."})
  2026-05-07 14:23:04.000  INFO      a3f2b1c4  tool.invoke           🔧 web_fetch  args=...
  2026-05-07 14:23:04.800  INFO      a3f2b1c4  tool.result           ├─ 晴天 25°C
  2026-05-07 14:23:04.800  INFO      a3f2b1c4  tool.result           └─ ...
  2026-05-07 14:23:06.890  INFO      a3f2b1c4  agent.end             🎯 FINAL · 5.66s · 234c

终端（Rich，INFO+，每事件≤3行）：
  trace_id  🚀 USER: ...
  trace_id  ▶ LLM CALL #1 · 2 messages
  trace_id  ✔ #1 · 1.74s · text=234c
  trace_id  🎯 FINAL · 5.66s · 234c

环境变量：
  LOG_FILE_LEVEL     文件级别（默认 DEBUG，与全局 LOG_LEVEL 解耦）
  LOG_CONSOLE_LEVEL  终端级别（默认 INFO）
  LOG_DIR            覆盖日志根目录
"""

import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

try:
    from rich.console import Console as _RichConsole
    _console = _RichConsole(stderr=True, highlight=False)
except ImportError:
    _console = None  # type: ignore[assignment]

logging.captureWarnings(True)

# 进程启动时间 — 每次重启唯一，用于日志文件名和 Banner
_RUN_START_DT = datetime.now()
_RUN_ID = _RUN_START_DT.strftime("%H%M%S")          # 嵌入文件名，如 152001
_RUN_START_TS = _RUN_START_DT.strftime("%Y-%m-%d %H:%M:%S")

# ── 常量 ──────────────────────────────────────────────────────────────────────
_MSG_LINE_LEN = 500    # 每行最多字符数
_MSG_MAX_LINES = 20    # 每条消息最多行数
_RESULT_MAX_LEN = 3000 # 工具结果最大总字符
_RESULT_MAX_LINES = 30 # 工具结果最多行数


# ── 截断辅助 ──────────────────────────────────────────────────────────────────

def _trunc(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... (省略 {len(text) - limit} 字符)"


# ── 消息树形拆分 ──────────────────────────────────────────────────────────────

def _split_message(msg: dict, is_last: bool) -> List[str]:
    """
    将单条消息拆成树形行列表，每行将作为独立 log record 写入。

    首行:  ├─/└─ role: content_first_line
    续行:  │  /    content_next_line
    """
    role = msg.get("role", "?")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")
    name = msg.get("name")

    tree = "└─" if is_last else "├─"
    cont = "   " if is_last else "│  "

    if role == "tool":
        label = f"tool:{name or '?'}"
        body = str(content or "")
    elif role == "assistant":
        label = "assistant"
        parts = []
        if content:
            parts.append(str(content))
        if tool_calls:
            calls = " | ".join(
                f"{tc.get('function', {}).get('name', '?')}"
                f"({tc.get('function', {}).get('arguments', '')[:80]})"
                for tc in tool_calls
            )
            parts.append(f"<tool_calls> {calls}")
        body = "\n".join(parts) if parts else ""
    else:
        label = role
        body = str(content or "")

    # 截断后按换行拆分，忽略空行
    body = _trunc(body, _MSG_LINE_LEN * _MSG_MAX_LINES)
    lines = [l for l in body.split("\n") if l.strip()] or ["(empty)"]

    result: List[str] = []
    result.append(f"{tree} {label}: {lines[0][:_MSG_LINE_LEN]}")
    for line in lines[1:_MSG_MAX_LINES]:
        result.append(f"{cont}{line[:_MSG_LINE_LEN]}")
    if len(lines) > _MSG_MAX_LINES:
        result.append(f"{cont}... (+{len(lines) - _MSG_MAX_LINES} more lines)")
    return result


def _split_result(result_str: str) -> List[str]:
    """将工具结果按行拆分，用于 tool.result 文件输出。"""
    truncated = _trunc(result_str, _RESULT_MAX_LEN)
    lines = [l for l in truncated.split("\n") if l.strip()] or ["(empty)"]
    out = lines[:_RESULT_MAX_LINES]
    if len(lines) > _RESULT_MAX_LINES:
        out.append(f"... (+{len(lines) - _RESULT_MAX_LINES} more lines)")
    return out


# ── 文件 Formatter ────────────────────────────────────────────────────────────

class _TraceFormatter(logging.Formatter):
    """
    格式：YYYY-MM-DD HH:MM:SS.mmm  LEVEL     trace     evt                   message
    """
    _FMT = "%(asctime)s.%(msecs)03d  %(levelname)-8s  %(trace)-8s  %(evt)-20s  %(message)s"

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "trace"):
            record.trace = "--------"  # type: ignore[attr-defined]
        if not hasattr(record, "evt"):
            record.evt = ""  # type: ignore[attr-defined]
        record.message = record.getMessage()
        record.asctime = self.formatTime(record, self.datefmt)
        s = self._FMT % record.__dict__
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            s = s + "\n" + record.exc_text
        return s


# ── 终端输出 ──────────────────────────────────────────────────────────────────

_LEVEL_STYLE: Dict[str, str] = {
    "INFO":     "cyan",
    "WARNING":  "yellow",
    "ERROR":    "bold red",
    "CRITICAL": "bold red on white",
}


def _term(level_name: str, trace: str, line: str) -> None:
    if _console is None:
        return
    style = _LEVEL_STYLE.get(level_name, "white")
    _console.print(
        f"[dim]{trace}[/dim]  [{style}]{line}[/{style}]",
        soft_wrap=True,
    )


# ── 会话状态 ──────────────────────────────────────────────────────────────────

class _SessionState:
    __slots__ = ("path", "trace_id", "logger", "handler", "llm_starts", "start_time")

    def __init__(
        self,
        path: Path,
        trace_id: str,
        logger: logging.Logger,
        handler: logging.FileHandler,
    ) -> None:
        self.path = path
        self.trace_id = trace_id
        self.logger = logger
        self.handler = handler
        self.llm_starts: Dict[int, float] = {}
        self.start_time: float = time.monotonic()


# ── 主类 ──────────────────────────────────────────────────────────────────────

class InteractionLogger:
    def __init__(self, log_dir: Optional[Path] = None) -> None:
        env_dir = os.getenv("LOG_DIR")
        self._log_dir = Path(env_dir) if env_dir else (log_dir or Path("logs"))
        self._log_dir.mkdir(parents=True, exist_ok=True)

        # LOG_FILE_LEVEL 独立于全局 LOG_LEVEL，默认 DEBUG（记录完整调用链）
        self._file_level = self._parse_level(os.getenv("LOG_FILE_LEVEL", "DEBUG"))
        self._console_level = self._parse_level(os.getenv("LOG_CONSOLE_LEVEL", "INFO"))

        self._dict_lock = threading.Lock()
        self._sessions: Dict[str, _SessionState] = {}
        self._suppressed_sessions: set[str] = set()

        self._cleanup_legacy_logs()

    @staticmethod
    def _parse_level(name: str) -> int:
        return getattr(logging, name.upper(), logging.DEBUG)

    # ── 初始化清理 ────────────────────────────────────────────────────────────

    def _cleanup_legacy_logs(self) -> None:
        """删除旧格式文件：interaction_*.log 和时间戳命名的 HH.MM.SS.log。"""
        for f in self._log_dir.glob("interaction_*.log"):
            try:
                f.unlink()
            except Exception:
                pass
        _ts_pat = re.compile(r"^\d{2}\.\d{2}\.\d{2}(_\d+)?\.log$")
        for f in self._log_dir.rglob("*.log"):
            if _ts_pat.match(f.name):
                try:
                    f.unlink()
                except Exception:
                    pass

    # ── 路径与 Logger ─────────────────────────────────────────────────────────

    @staticmethod
    def _session_slug(session_key: str) -> str:
        """p2p_ou_...7f25e707  →  p2p_7f25e707（稳定短名，用作文件名）"""
        first = session_key.split("_")[0]
        prefix = first if first in ("p2p", "group", "thread") else "sess"
        return f"{prefix}_{session_key[-8:]}"

    def _make_log_path(self, session_key: str) -> Path:
        """同 session × 同次运行返回同一路径；重启后 _RUN_ID 变化，自动新建文件。"""
        dir_path = self._log_dir / f"{_RUN_START_DT.year}.{_RUN_START_DT.month}" / str(_RUN_START_DT.day)
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path / f"{self._session_slug(session_key)}@{_RUN_ID}.log"

    def _make_session_logger(
        self, trace_id: str, path: Path
    ) -> tuple[logging.Logger, logging.FileHandler]:
        logger = logging.getLogger(f"interaction.{trace_id}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        handler.setLevel(self._file_level)
        handler.setFormatter(_TraceFormatter())
        logger.addHandler(handler)
        return logger, handler

    # ── 内部写入 ──────────────────────────────────────────────────────────────

    def _log(
        self,
        session_key: str,
        level: int,
        evt: str,
        msg: str,
        *,
        to_console: bool = False,
    ) -> None:
        with self._dict_lock:
            state = self._sessions.get(session_key)
            suppressed = session_key in self._suppressed_sessions
        if state is None:
            return
        extra = {"trace": state.trace_id, "evt": evt}
        state.logger.log(level, msg, extra=extra)
        if to_console and not suppressed and level >= self._console_level:
            _term(logging.getLevelName(level), state.trace_id, msg)

    # ── 公开日志接口 ──────────────────────────────────────────────────────────

    def log_session_start(self, session_key: str, user_input: str) -> None:
        """每次用户消息开始时调用：分配 trace_id，写入分隔线，记录 agent.start。"""
        path = self._make_log_path(session_key)
        is_new_file = not path.exists()          # 文件不存在 = 本次运行首次写入
        trace_id = secrets.token_hex(4)
        logger, handler = self._make_session_logger(trace_id, path)
        state = _SessionState(path, trace_id, logger, handler)
        with self._dict_lock:
            self._sessions[session_key] = state

        # 新文件顶部写入启动 Banner（标明是哪次重启产生的日志）
        if is_new_file:
            bar = "═" * 60
            handler.stream.write(
                f"{bar}\n"
                f"  PersonalAgent  启动: {_RUN_START_TS}  run={_RUN_ID}\n"
                f"  session: {session_key}\n"
                f"{bar}\n\n"
            )
            handler.stream.flush()

        # 交互分隔线（直接写流，绕过 formatter）
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sep = "━" * 80
        handler.stream.write(
            f"\n{sep}\n[{ts}]  trace={trace_id}  session={session_key}\n{sep}\n"
        )
        handler.stream.flush()

        preview = _trunc(user_input, 200).replace("\n", " ")
        self._log(session_key, logging.INFO, "agent.start",
                  f"🚀 USER: {preview}", to_console=True)

    def log_llm_call(
        self, session_key: str, round_n: int, messages: List[Dict[str, Any]]
    ) -> None:
        with self._dict_lock:
            state = self._sessions.get(session_key)
        if state:
            state.llm_starts[round_n] = time.monotonic()

        summary = f"▶ LLM CALL #{round_n} · {len(messages)} messages"
        # INFO：同时写文件和终端（一行摘要）
        self._log(session_key, logging.INFO, "llm.request", summary, to_console=True)
        # DEBUG：消息明细（仅文件）
        for i, msg in enumerate(messages):
            for line in _split_message(msg, is_last=(i == len(messages) - 1)):
                self._log(session_key, logging.DEBUG, "llm.request", line)

    def log_llm_response(
        self,
        session_key: str,
        round_n: int,
        text: str,
        tool_calls: Optional[List[Dict[str, Any]]],
    ) -> None:
        with self._dict_lock:
            state = self._sessions.get(session_key)

        elapsed_str = ""
        if state and round_n in state.llm_starts:
            elapsed = time.monotonic() - state.llm_starts.pop(round_n)
            elapsed_str = f" · {elapsed:.2f}s"

        if text:
            preview = _trunc(text, 300).replace("\n", " ")
            self._log(session_key, logging.INFO, "llm.response",
                      f"✔ #{round_n}{elapsed_str} · text={len(text)}c  {preview}",
                      to_console=True)
        if tool_calls:
            self._log(session_key, logging.INFO, "llm.response",
                      f"✔ #{round_n}{elapsed_str} · {len(tool_calls)} tool_calls",
                      to_console=True)
            for i, tc in enumerate(tool_calls, 1):
                fn = tc.get("function", {})
                prefix = "└─" if i == len(tool_calls) else "├─"
                args = fn.get("arguments", "")[:200]
                self._log(session_key, logging.DEBUG, "llm.response",
                          f"{prefix} {fn.get('name', '?')}({args})")
        if not text and not tool_calls:
            self._log(session_key, logging.WARNING, "llm.response",
                      f"⚠️  #{round_n}{elapsed_str} · (空响应)", to_console=True)

    def log_tool_result(
        self,
        session_key: str,
        tool_name: str,
        args: Dict[str, Any],
        result: str,
    ) -> None:
        try:
            args_str = json.dumps(args, ensure_ascii=False)[:300]
        except Exception:
            args_str = str(args)[:300]

        # 根据内容判断是成功还是失败
        is_error = result.startswith("Error:") or result.startswith("错误")
        invoke_evt = "tool.error" if is_error else "tool.invoke"
        result_evt = "tool.error" if is_error else "tool.result"
        icon = "⚠️ " if is_error else "🔧"

        self._log(session_key, logging.INFO, invoke_evt,
                  f"{icon} {tool_name}  args={args_str}", to_console=True)

        result_lines = _split_result(result)
        for i, line in enumerate(result_lines):
            prefix = "└─" if i == len(result_lines) - 1 else "├─"
            self._log(session_key, logging.INFO, result_evt, f"{prefix} {line}")

    def log_final(self, session_key: str, response: str) -> None:
        """记录最终回复，关闭本次交互的 FileHandler（文件留存，下次 append）。"""
        with self._dict_lock:
            state = self._sessions.get(session_key)

        elapsed_str = ""
        if state:
            elapsed = time.monotonic() - state.start_time
            elapsed_str = f" · {elapsed:.2f}s"

        # 终端：一行摘要
        term_preview = _trunc(response, 120).replace("\n", " ")
        self._log(session_key, logging.INFO, "agent.end",
                  f"🎯 FINAL{elapsed_str} · {len(response)}c  {term_preview}",
                  to_console=True)
        # 文件：完整内容按行拆分
        result_lines = _split_result(response)
        for i, line in enumerate(result_lines):
            prefix = "└─" if i == len(result_lines) - 1 else "├─"
            self._log(session_key, logging.INFO, "agent.end", f"{prefix} {line}")

        with self._dict_lock:
            state = self._sessions.pop(session_key, None)
        if state:
            state.handler.flush()
            state.handler.close()
            state.logger.removeHandler(state.handler)

    # ── 定时任务控制台静默 ────────────────────────────────────────────────────

    def suppress_console(self, session_key: str) -> None:
        """静默指定会话的终端输出（文件日志不受影响）。"""
        with self._dict_lock:
            self._suppressed_sessions.add(session_key)

    def restore_console(self, session_key: str) -> None:
        """恢复指定会话的终端输出。"""
        with self._dict_lock:
            self._suppressed_sessions.discard(session_key)

    # ── 装饰器 & 上下文管理器 ─────────────────────────────────────────────────

    def log_agent(self, session_key_arg: str = "session_key") -> Callable:
        """
        方法装饰器：自动记录函数入口/出口。

        用法：
            @interaction_logger.log_agent()
            def process(self, event, ..., session_key: str): ...
        """
        def decorator(fn: Callable) -> Callable:
            @wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                sk = kwargs.get(session_key_arg, "?")
                self._log(sk, logging.INFO, "agent.enter",
                          f"▶ {fn.__qualname__}", to_console=True)
                try:
                    result = fn(*args, **kwargs)
                    self._log(sk, logging.INFO, "agent.exit",
                              f"✔ {fn.__qualname__}", to_console=True)
                    return result
                except Exception as exc:
                    self._log(sk, logging.ERROR, "agent.exit",
                              f"❌ {fn.__qualname__}  err={exc}", to_console=True)
                    raise
            return wrapper
        return decorator

    @contextmanager
    def log_span(self, session_key: str, name: str) -> Generator[None, None, None]:
        """上下文管理器：测量并记录代码块耗时。"""
        self._log(session_key, logging.DEBUG, f"span.{name}", f"▶ {name} start")
        t0 = time.monotonic()
        try:
            yield
        except Exception as exc:
            elapsed = time.monotonic() - t0
            self._log(session_key, logging.ERROR, f"span.{name}",
                      f"❌ {name} failed · {elapsed:.3f}s · err={exc}",
                      to_console=True)
            raise
        else:
            elapsed = time.monotonic() - t0
            self._log(session_key, logging.DEBUG, f"span.{name}",
                      f"✔ {name} done · {elapsed:.3f}s")
