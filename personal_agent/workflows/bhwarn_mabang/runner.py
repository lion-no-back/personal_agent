"""
马帮补货预警 workflow 入口

由 ConversationAgent 按需触发，通过意图关键词匹配（"补货预警"）激活。
返回 {"message": str, "files": [Path]}，调用方负责将文件发回原会话。
"""

import importlib.util
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEP_GROUP = "zxwarn-mabang"
_CHECK_PACKAGES = ["pandas", "openpyxl"]


def _get_project_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return path.parents[3]


def _ensure_deps() -> None:
    missing = [p for p in _CHECK_PACKAGES if importlib.util.find_spec(p) is None]
    if not missing:
        return
    uv = shutil.which("uv") or "uv"
    logger.info("[bhwarn_mabang] 缺少依赖 %s，正在安装 %s...", missing, _DEP_GROUP)
    try:
        subprocess.run([uv, "sync", "--group", _DEP_GROUP], check=True, capture_output=False)
        logger.info("[bhwarn_mabang] 依赖安装完成")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"依赖组 {_DEP_GROUP!r} 安装失败，请手动执行：uv sync --group {_DEP_GROUP}"
        ) from exc


def run() -> dict:
    """
    执行马帮补货预警工作流。
    返回 {"message": str, "files": [Path]}
    """
    for key in ("MABANG_USERNAME", "MABANG_PASSWORD"):
        if not os.environ.get(key):
            raise RuntimeError(f"缺少马帮凭证，请在 .env 中配置 {key}")

    _ensure_deps()

    project_root = _get_project_root()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = project_root / "artifact" / f"bhwarn_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    from . import bhwarn

    start = datetime.now()
    result = bhwarn.run(output_dir=output_dir)
    elapsed = int((datetime.now() - start).total_seconds())

    result["message"] += f"\n\n耗时: {elapsed}s"
    return result
