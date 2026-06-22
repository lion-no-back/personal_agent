"""工作流子进程入口。

在一个全新的、单线程的独立进程里运行单个工作流的 run()，把结果写入 --result-file。

为什么要独立进程：常驻主进程是多线程的（调度器/网关/知识库），若在其中并发地
首次 import numpy/pandas/torch 等原生库，或在运行时 uv pip install / del sys.modules
改写导入状态，会把这些库导成半成品（典型症状：`module 'numpy' has no attribute 'matrix'`）。
每个工作流独占一个进程后：单线程从零导入，按需装包/卸载的污染随进程退出蒸发，
工作流之间互不干涉。

调用方式（由 dispatch.run_in_subprocess 构造）：
    python -m personal_agent.workflows.subprocess_runner \
        --module personal_agent.workflows.zxwarn_mabang.runner \
        --function run \
        --result-file <json路径> \
        --log-file <log路径>
用户原始消息（如含 ASIN）通过环境变量 WF_CONTENT 传入，避免命令行转义/长度限制。
"""

import argparse
import importlib
import inspect
import json
import logging
import os
import sys
from pathlib import Path


def _setup_child_logging(log_file: str) -> None:
    """子进程独立日志：写自己的文件 + 继承的 stdout（控制台仍可见进度）。

    刻意不复用 main.setup_logging：它用 RotatingFileHandler 写同一个 agent.log，
    多进程同写+轮转在 Windows 上会冲突。每个子进程写独立文件最干净。
    """
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)
    for noisy in ("httpx", "httpcore", "openai", "lark_oapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _serialize(result) -> dict:
    """把 run() 的返回统一成 {message, files[str], segments[{image,text}]}。"""
    if isinstance(result, str):
        return {"message": result, "files": [], "segments": []}
    if isinstance(result, dict):
        files = [str(f) for f in (result.get("files") or [])]
        segments = []
        for seg in (result.get("segments") or []):
            img = seg.get("image")
            segments.append({"image": str(img) if img else None, "text": seg.get("text", "")})
        return {"message": result.get("message", ""), "files": files, "segments": segments}
    return {"message": "" if result is None else str(result), "files": [], "segments": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--function", default="run")
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--log-file", default="")
    args = parser.parse_args()

    if args.log_file:
        _setup_child_logging(args.log_file)
    logger = logging.getLogger(__name__)

    payload = {"ok": False, "message": "", "files": [], "segments": [], "error": None}
    try:
        mod = importlib.import_module(args.module)
        fn = getattr(mod, args.function)
        params = inspect.signature(fn).parameters
        if "content" in params:
            result = fn(content=os.environ.get("WF_CONTENT", ""))
        else:
            result = fn()
        payload.update(_serialize(result))
        payload["ok"] = True
    except Exception as exc:
        logger.exception("[subprocess] 工作流执行失败 | module=%s", args.module)
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["ok"] = False

    try:
        Path(args.result_file).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        logger.exception("[subprocess] 写结果文件失败 | %s", args.result_file)
        return 2
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
