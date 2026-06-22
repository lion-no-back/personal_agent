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
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SUB_LOG_DIR = _PROJECT_ROOT / "logs" / "app" / "sub"

# 装包宽限：首次运行需现装大依赖（如 cloakbrowser+Chromium）。按需入口在工作流执行超时
# 之外再叠加这段预算作为子进程硬总额，保证"正常装包"不会被执行超时杀断。
# （定时入口因有外层 asyncio 超时兜底，由调用方自行换算，不在此叠加。）
INSTALL_GRACE_SECONDS = 1200

# 超时后温和终止与强杀之间的宽限秒数。
_KILL_GRACE_SECONDS = 5


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


def run_in_subprocess(
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
        sys.executable, "-m", "personal_agent.workflows.subprocess_runner",
        "--module", module, "--function", function,
        "--result-file", result_path, "--log-file", str(log_file),
    ]
    env = {**os.environ}
    if content is not None:
        env["WF_CONTENT"] = content

    total_timeout = timeout  # 调用方传入的即为子进程硬总额（含装包预算）
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    logger.info("[dispatch] 启动工作流子进程 | module=%s 日志=%s", module, log_file)
    proc = subprocess.Popen(
        cmd, cwd=str(_PROJECT_ROOT), env=env,
        stdin=subprocess.DEVNULL, creationflags=creationflags,
    )
    try:
        proc.wait(timeout=total_timeout)
    except subprocess.TimeoutExpired:
        logger.error("[dispatch] 工作流超时，强制中止 | module=%s pid=%s", module, proc.pid)
        _kill_tree(proc)
        _cleanup(result_path)
        return {"ok": False, "message": "❌ 执行超时，已强制中止",
                "files": [], "segments": [], "error": "timeout"}

    try:
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("[dispatch] 读取子进程结果失败 | module=%s rc=%s err=%s",
                     module, proc.returncode, exc)
        return {"ok": False, "message": f"❌ 执行失败：无法读取子进程结果（{exc}），详见日志 {log_file}",
                "files": [], "segments": [], "error": str(exc)}
    finally:
        _cleanup(result_path)

    payload["files"] = [Path(f) for f in payload.get("files", [])]
    for seg in payload.get("segments", []):
        if seg.get("image"):
            seg["image"] = Path(seg["image"])
    return payload


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
