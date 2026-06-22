"""
马帮滞销预警 workflow 入口

由 ConversationAgent 按需触发，通过意图关键词匹配（"滞销预警"/"滞销"/"马帮预警"）激活。
返回 {"message": str, "files": [Path]}，调用方负责将文件发回原会话。
"""

import importlib.metadata
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEP_GROUP = "zxwarn-mabang"


def _get_project_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return path.parents[3]


_INSTALL_PACKAGES = ["openpyxl>=3.1", "pandas>=2.0", "python-calamine>=0.2"]


def _dist_name(spec: str) -> str:
    """从 pip 依赖串取出发行名（PyPI 名）：'python-calamine>=0.2' -> 'python-calamine'。"""
    for sep in ("<", ">", "=", "!", "~", "[", " "):
        spec = spec.split(sep, 1)[0]
    return spec.strip()


# 检查清单从 _INSTALL_PACKAGES 自动推导，确保两份清单永不漏项
_CHECK_PACKAGES = [_dist_name(p) for p in _INSTALL_PACKAGES]


def _ensure_deps() -> None:
    def _installed(dist: str) -> bool:
        """读 dist-info 判断发行包是否就绪（自动归一化连字符/下划线、大小写）。"""
        try:
            importlib.metadata.version(dist)
            return True
        except importlib.metadata.PackageNotFoundError:
            return False

    missing = [p for p in _CHECK_PACKAGES if not _installed(p)]
    if not missing:
        return
    logger.info("[zxwarn_mabang] 缺少依赖 %s，正在安装...", missing)
    try:
        from personal_agent.workflows.deplock import uv_pip_install
        uv_pip_install(_INSTALL_PACKAGES)  # 跨进程串行装包 + 刷新导入缓存
        logger.info("[zxwarn_mabang] 依赖安装完成")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"依赖安装失败，请手动执行：uv pip install {' '.join(_INSTALL_PACKAGES)}"
        ) from exc


def run() -> dict:
    """
    执行马帮滞销预警工作流。
    返回 {"message": str, "files": [Path]}
    """
    for key in ("MABANG_USERNAME", "MABANG_PASSWORD"):
        if not os.environ.get(key):
            raise RuntimeError(f"缺少马帮凭证，请在 .env 中配置 {key}")

    _ensure_deps()

    project_root = _get_project_root()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = project_root / "artifact" / f"zxwarn_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    from . import zhixiaowarn

    start = datetime.now()
    result = zhixiaowarn.run(output_dir=output_dir)
    elapsed = int((datetime.now() - start).total_seconds())

    result["message"] += f"\n\n耗时: {elapsed}s"
    return result
