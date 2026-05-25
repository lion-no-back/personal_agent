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
import os
import shutil
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)

_DEP_GROUP = "costupdate-saihu"
# 多包检查：任一缺失都触发 uv sync，防止单哨兵在部分安装时误判
_CHECK_PACKAGES = ["ddddocr", "pandas", "openpyxl"]


def _ensure_deps() -> None:
    """检查 workflow 依赖是否齐全，有缺失时自动执行 uv sync --group。"""
    missing = [p for p in _CHECK_PACKAGES if importlib.util.find_spec(p) is None]
    if not missing:
        return

    uv = shutil.which("uv") or "uv"
    logger.info(
        "[costupdate_saihu] 缺少依赖 %s，正在安装依赖组 %s，请稍候...",
        missing,
        _DEP_GROUP,
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
    _REQUIRED = ("MABANG_USERNAME", "MABANG_PASSWORD", "SELLFOX_USERNAME", "SELLFOX_PASSWORD")
    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"缺少必要凭证，请在设置中配置后重启 Agent：{', '.join(missing)}")

    _ensure_deps()

    from . import mian_controller

    start = datetime.now()
    mian_controller.main()
    elapsed = int((datetime.now() - start).total_seconds())
    return (
        f"✅ 赛狐成本每周更新完成\n"
        f"耗时: {elapsed}s\n"
        f"时间: {start.strftime('%Y-%m-%d %H:%M:%S')}"
    )
