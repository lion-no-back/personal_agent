"""
赛狐成本更新 workflow 入口

由 WorkflowRegistry 翻译为 CronJobConfig 后，调度器通过
  module: personal_agent.workflows.costupdate_saihu.runner
  function: run
调用此模块的 run() 函数。

首次运行时会自动检测并安装所需依赖（dependency group: costupdate-saihu）。
"""

import importlib.util
import logging
import shutil
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)

_DEPS_SENTINEL = "ddddocr"   # 该包不在主项目依赖中，用作安装状态哨兵
_DEP_GROUP = "costupdate-saihu"


def _ensure_deps() -> None:
    """检查 workflow 依赖是否已安装，未安装时自动执行 uv sync --group。"""
    if importlib.util.find_spec(_DEPS_SENTINEL) is not None:
        return

    uv = shutil.which("uv") or "uv"
    logger.info(
        "[costupdate_saihu] 首次运行，正在安装依赖组 %s，请稍候...", _DEP_GROUP
    )
    try:
        subprocess.run(
            [uv, "sync", "--group", _DEP_GROUP],
            check=True,
            capture_output=False,
        )
        logger.info("[costupdate_saihu] 依赖安装完成")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"依赖组 {_DEP_GROUP!r} 安装失败，请手动执行：uv sync --group {_DEP_GROUP}"
        ) from exc


def run() -> str:
    _ensure_deps()

    from . import mian_controller

    start = datetime.now()
    try:
        mian_controller.main()
        elapsed = int((datetime.now() - start).total_seconds())
        return (
            f"✅ 赛狐成本每周更新完成\n"
            f"耗时: {elapsed}s\n"
            f"时间: {start.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    except Exception as exc:
        return (
            f"❌ 赛狐成本每周更新失败\n"
            f"错误: {exc}\n"
            f"时间: {start.strftime('%Y-%m-%d %H:%M:%S')}"
        )
