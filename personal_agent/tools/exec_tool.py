import logging
import os
import platform
import subprocess
import threading
import time
from typing import Callable, Optional

from .types import ToolDef
from ..guardrails.patterns import get_exec_denylist
from ..guardrails.audit_logger import get_audit_logger

logger = logging.getLogger(__name__)

# 在 Unix 系统上需要净化的危险环境变量，防止通过动态链接器劫持二进制
_UNIX_DANGEROUS_ENV = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "LD_ORIGIN_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
})

_DEFAULT_YIELD_MS: int = int(os.getenv("EXEC_YIELD_MS", "5000"))
_DEFAULT_TIMEOUT: int = int(os.getenv("EXEC_TIMEOUT", "30"))
_MAX_OUTPUT_CHARS: int = 8000


def _get_allowlist() -> Optional[list]:
    """
    读取 EXEC_ALLOWLIST 环境变量，返回允许的命令名列表。
    未设置时返回 None，表示放行所有命令。
    格式：逗号分隔的命令名，如 "python,git,npm"
    """
    raw = os.getenv("EXEC_ALLOWLIST", "").strip()
    if not raw:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_allowed(command: str, allowlist: Optional[list]) -> bool:
    """检查命令的第一个 token 是否在 allowlist 中。"""
    if allowlist is None:
        return True
    stripped = command.strip()
    if not stripped:
        return False
    first_token = stripped.split()[0]
    exe = os.path.basename(first_token).lower()
    if exe.endswith(".exe"):
        exe = exe[:-4]
    for pattern in allowlist:
        p = pattern.strip().lower()
        if p.endswith(".exe"):
            p = p[:-4]
        if exe == p:
            return True
    return False


def _sanitize_env(extra: Optional[dict]) -> dict:
    """
    构建净化后的环境变量字典：
    - 以 os.environ 为基础
    - Unix 上移除危险变量（防止 LD_PRELOAD 等劫持）
    - 合并调用方传入的 extra 覆盖项
    """
    env = dict(os.environ)
    if platform.system() != "Windows":
        for var in _UNIX_DANGEROUS_ENV:
            env.pop(var, None)
    # 强制子进程以 UTF-8 读写 stdin/stdout/stderr，避免中文 Windows 下
    # GBK 无法编码 emoji 等字符导致 UnicodeEncodeError
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _detect_encoding() -> str:
    # 始终使用 utf-8，与 _sanitize_env 注入的 PYTHONIOENCODING=utf-8 保持一致
    return os.getenv("EXEC_ENCODING", "utf-8")


def _format_result(command: str, returncode: int, stdout: str, stderr: str, elapsed: float) -> str:
    """将命令执行结果格式化为 Markdown 字符串。"""
    parts = [f"**$ {command}**"]
    if stdout.strip():
        parts.append(f"**stdout:**\n```\n{stdout.rstrip()}\n```")
    if stderr.strip():
        parts.append(f"**stderr:**\n```\n{stderr.rstrip()}\n```")
    if not stdout.strip() and not stderr.strip():
        parts.append("_(无输出)_")
    icon = "✅" if returncode == 0 else "❌"
    parts.append(f"{icon} 退出码: {returncode} | 耗时: {elapsed:.2f}s")
    result = "\n\n".join(parts)
    if len(result) > _MAX_OUTPUT_CHARS:
        result = result[:_MAX_OUTPUT_CHARS] + f"\n\n_(输出已截断，完整长度 {len(result)} 字符)_"
    return result


_FEISHU_PREFIX = "[飞书]"


def _extract_feishu_summary(stdout: str) -> Optional[str]:
    """从 stdout 中提取最后一个 [飞书] 前缀行作为飞书通知摘要。"""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith(_FEISHU_PREFIX):
            return stripped[len(_FEISHU_PREFIX):].strip()
    return None


def _notify_complete(push_fn: Optional[Callable], result: str) -> None:
    """发送后台命令完成通知，无 push_fn 时仅记录日志。"""
    if push_fn:
        try:
            push_fn(f"🔔 **后台命令已完成**\n\n{result}")
        except Exception as e:
            logger.error("后台命令通知失败: %s", e)
    else:
        logger.info("后台命令完成（无 push_fn，跳过飞书通知）")


def _spawn_background_watcher(
    command: str,
    workdir: Optional[str],
    env: dict,
    timeout: float,
    push_fn: Optional[Callable],
    *,
    start_time: Optional[float] = None,
    proc: Optional[subprocess.Popen] = None,
) -> None:
    """
    在守护线程中运行或接管一个命令，完成后调用 push_fn 通知。

    - proc=None：重新启动命令（background=True 强制后台模式）
    - proc 不为 None：接管已启动但超过 yieldMs 的进程（yieldMs 自动后台模式）
    """
    def _target() -> None:
        t0 = start_time if start_time is not None else time.monotonic()
        enc = _detect_encoding()

        if proc is None:
            # 全新启动
            try:
                _proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=workdir,
                    env=env,
                    text=True,
                    encoding=enc,
                    errors="replace",
                )
            except Exception as e:
                _notify_complete(push_fn, f"**$ {command}**\n\nError: 启动进程失败 - {e}")
                return
            wait_proc = _proc
        else:
            wait_proc = proc

        try:
            stdout, stderr = wait_proc.communicate(timeout=max(1.0, timeout))
            rc = wait_proc.returncode
        except subprocess.TimeoutExpired:
            wait_proc.kill()
            stdout, stderr = wait_proc.communicate()
            rc = -1
            stderr = f"[后台超时: 总执行时间超过 {timeout:.0f}s]\n{stderr or ''}"

        elapsed = time.monotonic() - t0
        stdout_str = stdout or ""
        summary = _extract_feishu_summary(stdout_str)
        if summary:
            icon = "✅" if rc == 0 else "❌"
            _notify_complete(push_fn, f"{icon} {summary}")
        else:
            result = _format_result(command, rc, stdout_str, stderr or "", elapsed)
            _notify_complete(push_fn, result)

    threading.Thread(target=_target, daemon=True, name="exec-bg").start()


def build_exec_tool() -> ToolDef:
    """构建 exec 命令执行工具。"""

    def exec_call(
        command: str,
        workdir: Optional[str] = None,
        timeout: Optional[int] = None,
        background: bool = False,
        yield_ms: Optional[int] = None,
        env: Optional[dict] = None,
        **kwargs,
    ) -> str:
        # 从 _tool_ctx 中取出飞书通知回调（由 ConversationAgent 注入）
        tool_ctx: dict = kwargs.pop("_tool_ctx", {})
        push_fn: Optional[Callable] = tool_ctx.get("push_fn")

        _timeout = float(timeout if timeout is not None else _DEFAULT_TIMEOUT)
        _yield_ms = yield_ms if yield_ms is not None else _DEFAULT_YIELD_MS

        # ── Allowlist 校验 ────────────────────────────────────
        allowlist = _get_allowlist()
        if not _is_allowed(command, allowlist):
            allowed_str = "、".join(f"`{p}`" for p in (allowlist or []))
            return f"Error: 命令被 Allowlist 拒绝，当前仅允许: {allowed_str}"

        # ── L2 ExecPreGuard Denylist（subprocess 执行前拦截）──
        for _dp, _dr in get_exec_denylist():
            if _dp.search(command):
                get_audit_logger().emit_blocked(
                    layer="L2_ExecPreGuard",
                    rule=_dr,
                    raw_text=command,
                    sanitized_preview=command[:80],
                )
                return f"Error: 命令被安全策略拦截（规则: {_dr}）。此操作不被允许。"

        # ── 环境变量净化 ──────────────────────────────────────
        sanitized_env = _sanitize_env(env)

        # ── workdir 校验 ──────────────────────────────────────
        if workdir:
            # normpath(abspath()) 同时处理：相对路径展开、末尾分隔符（Windows CreateProcess 遇到尾部 \ 返回 WinError 5）
            workdir = os.path.normpath(os.path.abspath(workdir))
            if not os.path.isdir(workdir):
                return f"Error: workdir 不存在或不是目录: '{workdir}'"

        # ── 强制后台模式 ──────────────────────────────────────
        if background:
            _spawn_background_watcher(command, workdir, sanitized_env, _timeout, push_fn)
            return f"⏳ 命令已在后台启动，完成后将通过飞书通知您。\n\n`$ {command}`"

        # ── 前台执行（带 yieldMs 自动后台机制） ───────────────
        enc = _detect_encoding()
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=sanitized_env,
                text=True,
                encoding=enc,
                errors="replace",
            )
        except Exception as e:
            return f"Error: 启动进程失败 - {e}"

        yield_s = _yield_ms / 1000.0
        try:
            stdout, stderr = proc.communicate(timeout=yield_s)
            elapsed = time.monotonic() - start
            return _format_result(command, proc.returncode, stdout, stderr, elapsed)
        except subprocess.TimeoutExpired:
            # 命令超过 yieldMs 还未完成 → 自动转后台，接管已运行的进程
            remaining_timeout = _timeout - yield_s
            _spawn_background_watcher(
                command, workdir, sanitized_env, remaining_timeout, push_fn,
                start_time=start, proc=proc,
            )
            return (
                f"⏳ 命令执行超过 {_yield_ms}ms 阈值，已自动转入后台，"
                f"完成后将通过飞书通知您。\n\n`$ {command}`"
            )

    return ToolDef(
        name="exec",
        description=(
            "在服务器 Shell 中执行命令并返回输出（stdout/stderr/退出码）。"
            "支持前台执行（直接返回结果）与后台执行（立即返回、完成后飞书通知）。"
            "适合运行脚本、查询系统信息、文件处理、调用 CLI 工具等任务。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 Shell 命令，支持管道（|）和重定向（>）语法",
                },
                "workdir": {
                    "type": "string",
                    "description": "命令的工作目录（绝对或相对路径），默认为当前目录",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"命令总超时秒数，超时后强制终止进程。"
                        f"默认值由环境变量 EXEC_TIMEOUT 控制（当前 {_DEFAULT_TIMEOUT}s）"
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": "true 时立即在后台启动命令，工具即刻返回，命令完成后通过飞书通知",
                },
                "yield_ms": {
                    "type": "integer",
                    "description": (
                        f"自动后台阈值（毫秒）：前台等待超过此时长后命令自动转入后台。"
                        f"默认值由环境变量 EXEC_YIELD_MS 控制（当前 {_DEFAULT_YIELD_MS}ms）"
                    ),
                },
                "env": {
                    "type": "object",
                    "description": "额外注入的环境变量（key-value 字符串对），会覆盖同名系统变量",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        call=exec_call,
    )
