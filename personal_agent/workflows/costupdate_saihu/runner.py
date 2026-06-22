"""
赛狐成本更新 workflow 入口

由 WorkflowRegistry 翻译为 CronJobConfig 后，调度器通过
  module: personal_agent.workflows.costupdate_saihu.runner
  function: run
调用此模块的 run() 函数。

首次运行时会自动检测并安装所需依赖（dependency group: costupdate-saihu）。
"""

import importlib
import importlib.metadata
import logging
import os
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEP_GROUP = "costupdate-saihu"
# pyproject 解析失败时的兜底哨兵；正常路径下检查清单从依赖组同源推导
_FALLBACK_CHECK = ["ddddocr", "pandas", "openpyxl", "python-calamine"]


def _dist_name(spec: str) -> str:
    """从依赖串取发行名（PyPI 名）：'python-calamine>=0.2' -> 'python-calamine'。"""
    for sep in ("<", ">", "=", "!", "~", "[", " "):
        spec = spec.split(sep, 1)[0]
    return spec.strip()


def _installed(dist: str) -> bool:
    """读 dist-info 判断发行包是否就绪（按 PEP 503 归一化连字符/下划线/大小写）。"""
    try:
        importlib.metadata.version(dist)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _group_packages() -> list[str]:
    """从 pyproject.toml 读取 dependency-group 的包列表，作为 uv pip install 的参数。"""
    with open(_PROJECT_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data.get("dependency-groups", {}).get(_DEP_GROUP, [])


def _ensure_deps() -> None:
    """检查 workflow 依赖是否齐全，有缺失时用 uv pip install 补装。

    检查清单从 pyproject 的依赖组同源推导（按发行名查 dist-info），确保"检查的包"
    与"安装的包"永远一致，不会再漏检单个依赖（如 python-calamine）。

    故意不用 uv sync：sync 是全量同步语义，会删除 lockfile 之外的包（含
    paddle_bootstrap 动态装入的 opencv/torch/paddlepaddle 等），并因锁住的
    DLL（sqlite_vec.dll）而拒绝访问。pip install 是纯加法，不删现有包。
    """
    packages = _group_packages()
    check_names = [_dist_name(p) for p in packages] or _FALLBACK_CHECK

    missing = [n for n in check_names if not _installed(n)]
    if not missing:
        return

    if not packages:
        packages = missing  # pyproject.toml 解析失败时退化到兜底哨兵

    logger.info(
        "[costupdate_saihu] 缺少依赖 %s，正在安装依赖组 %s，请稍候...",
        missing,
        _DEP_GROUP,
    )
    try:
        from personal_agent.workflows.deplock import uv_pip_install
        uv_pip_install(packages)  # 跨进程串行装包 + 刷新导入缓存
        logger.info("[costupdate_saihu] 依赖安装完成")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"依赖组 {_DEP_GROUP!r} 安装失败，请手动执行：uv pip install {' '.join(packages)}"
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
