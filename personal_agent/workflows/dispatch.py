"""在独立子进程里运行工作流。

主进程（网关/调度器/知识库）只负责派活和收结果，自己永不 import 工作流的重原生库、
永不在运行时装包，从根上消除多线程并发首次导入的竞态。

对外只暴露 run_in_subprocess()，返回的 dict 与原先 run() 的返回结构兼容
（files 为 Path、segments 的 image 为 Path），调用方的投递逻辑无需改动。
"""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .retry import WORKFLOW_RETRY_DELAYS

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SUB_LOG_DIR = _PROJECT_ROOT / "logs" / "app" / "sub"

# 装包宽限：首次运行需现装大依赖（如 cloakbrowser+Chromium）。按需入口在工作流执行超时
# 之外再叠加这段预算作为子进程硬总额，保证"正常装包"不会被执行超时杀断。
# （定时入口因有外层 asyncio 超时兜底，由调用方自行换算，不在此叠加。）
INSTALL_GRACE_SECONDS = 1200

# 超时后温和终止与强杀之间的宽限秒数。
_KILL_GRACE_SECONDS = 5

_WORKFLOW_RETRY_MODULES = {
    "personal_agent.workflows.bhwarn_mabang.runner",
    "personal_agent.workflows.qhwarn_mabang.runner",
    "personal_agent.workflows.rankbb_amazon.runner",
    "personal_agent.workflows.zxwarn_mabang.runner",
}


def _new_log_file(module: str) -> Path:
    _SUB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    tag = module.split(".")[-2] if module.endswith(".runner") else module.split(".")[-1]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _SUB_LOG_DIR / f"{tag}_{ts}.log"


def _kill_tree(proc: subprocess.Popen) -> None:
    """先温和终止，宽限后强杀整棵进程树（含工作流派生的浏览器/uv 等子进程）。"""
    try:
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # 需配合 CREATE_NEW_PROCESS_GROUP
            except Exception:
                pass
            try:
                proc.wait(timeout=_KILL_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                pass
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, capture_output=True,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=_KILL_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        logger.exception("[dispatch] 杀进程树失败 | pid=%s", proc.pid)


def _append_child_output(log_file: Path, stdout: Optional[str], stderr: Optional[str]) -> None:
    chunks = []
    if stdout:
        chunks.append("----- captured stdout -----\n" + stdout.rstrip())
    if stderr:
        chunks.append("----- captured stderr -----\n" + stderr.rstrip())
    if not chunks:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8", errors="replace") as fp:
            fp.write("\n[dispatch] 子进程未能写入标准结果，补充捕获输出：\n")
            fp.write("\n".join(chunks))
            fp.write("\n")
    except Exception:
        logger.exception("[dispatch] 写入子进程捕获输出失败 | log=%s", log_file)


def _safe_stream_write(stream, text: str) -> None:
    try:
        stream.write(text)
        stream.flush()
        return
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe_text)
        stream.flush()


def _stream_child_pipe(pipe, stream, chunks: list[str]) -> None:
    if pipe is None:
        return
    try:
        while True:
            line = pipe.readline()
            if line == "":
                break
            chunks.append(line)
            _safe_stream_write(stream, line)
    except Exception:
        logger.exception("[dispatch] 读取工作流子进程输出失败")
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _run_in_subprocess_once(
    module: str,
    function: str = "run",
    content: str | None = None,
    timeout: int | None = None,
) -> dict:
    """在新进程里运行 module.function()，返回 {ok, message, files[Path], segments, error}。"""
    fd, result_path = tempfile.mkstemp(prefix="wf_result_", suffix=".json")
    os.close(fd)
    log_file = _new_log_file(module)

    cmd = [
        sys.executable, "-u", "-m", "personal_agent.workflows.subprocess_runner",
        "--module", module, "--function", function,
        "--result-file", result_path, "--log-file", str(log_file),
    ]
    env = {**os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        from personal_agent.core.telemetry import telemetry_env
        env.update(telemetry_env())
    except Exception:
        logger.debug("[dispatch] telemetry context export skipped", exc_info=True)
    if content is not None:
        env["WF_CONTENT"] = content

    total_timeout = timeout  # 调用方传入的即为子进程硬总额（含装包预算）
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    logger.info("[dispatch] 启动工作流子进程 | module=%s 日志=%s", module, log_file)
    proc = subprocess.Popen(
        cmd, cwd=str(_PROJECT_ROOT), env=env,
        stdin=subprocess.DEVNULL, creationflags=creationflags,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(
        target=_stream_child_pipe,
        args=(proc.stdout, sys.stdout, stdout_chunks),
        name=f"wf-stdout-{proc.pid}",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stream_child_pipe,
        args=(proc.stderr, sys.stderr, stderr_chunks),
        name=f"wf-stderr-{proc.pid}",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        proc.wait(timeout=total_timeout)
    except subprocess.TimeoutExpired:
        logger.error("[dispatch] 工作流超时，强制中止 | module=%s pid=%s", module, proc.pid)
        _kill_tree(proc)
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        _append_child_output(log_file, stdout, stderr)
        _cleanup(result_path)
        return {"ok": False, "message": "❌ 执行超时，已强制中止",
                "files": [], "segments": [], "error": "timeout"}
    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)

    try:
        raw_result = Path(result_path).read_text(encoding="utf-8")
        if not raw_result.strip():
            raise ValueError(f"子进程结果文件为空，退出码 {proc.returncode}")
        payload = json.loads(raw_result)
    except Exception as exc:
        _append_child_output(log_file, stdout, stderr)
        logger.error("[dispatch] 读取子进程结果失败 | module=%s rc=%s err=%s",
                     module, proc.returncode, exc)
        return {"ok": False, "message": f"❌ 执行失败：无法读取子进程结果（退出码 {proc.returncode}，{exc}），详见日志 {log_file}",
                "files": [], "segments": [], "error": f"rc={proc.returncode}; {exc}"}
    finally:
        _cleanup(result_path)

    payload.setdefault("log_file", str(log_file))
    payload["files"] = [Path(f) for f in payload.get("files", [])]
    for seg in payload.get("segments", []):
        if seg.get("image"):
            seg["image"] = Path(seg["image"])
    return payload


def _should_retry_workflow(module: str, payload: dict) -> bool:
    return (
        module in _WORKFLOW_RETRY_MODULES
        and not payload.get("ok")
        and payload.get("retryable") is True
    )


def _finalize_failure_message(payload: dict, workflow_retry_count: int) -> dict:
    if payload.get("ok"):
        return payload
    message = payload.get("user_message") or payload.get("message")
    if not message:
        error_type = payload.get("error_type") or payload.get("error_code") or "UNKNOWN_ERROR"
        log_file = payload.get("log_file")
        message = f"❌ 工作流执行失败：系统内部异常（{error_type}）。底层错误详情已写入日志，避免直接发送给用户。"
        if log_file:
            message += f"\n日志：{log_file}"
    if workflow_retry_count:
        message = (
            f"{message}\n"
            f"系统已处理：workflow 级已整体重试 {workflow_retry_count} 次，仍未成功。"
        )
    payload["message"] = message
    payload["user_message"] = message
    return payload


def run_in_subprocess(
    module: str,
    function: str = "run",
    content: str | None = None,
    timeout: int | None = None,
) -> dict:
    """Run a workflow subprocess, with configured workflow-level retry."""
    max_workflow_retries = len(WORKFLOW_RETRY_DELAYS) if module in _WORKFLOW_RETRY_MODULES else 0
    for attempt in range(max_workflow_retries + 1):
        payload = _run_in_subprocess_once(module, function=function, content=content, timeout=timeout)
        payload["workflow_attempt"] = attempt + 1
        payload["workflow_max_attempts"] = max_workflow_retries + 1
        if payload.get("ok") or attempt >= max_workflow_retries or not _should_retry_workflow(module, payload):
            return _finalize_failure_message(payload, attempt)
        delay = WORKFLOW_RETRY_DELAYS[attempt]
        logger.warning(
            "[dispatch] workflow failed with retryable business error; retrying | module=%s attempt=%s/%s delay=%ss error=%s",
            module,
            attempt + 1,
            max_workflow_retries + 1,
            delay,
            payload.get("error") or payload.get("message"),
        )
        time.sleep(delay)

    return _finalize_failure_message(payload, max_workflow_retries)


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
