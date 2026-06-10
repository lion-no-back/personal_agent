"""
赛狐亏损预警 workflow 入口

定时（每周二北京时间 10:00）自动触发：
  1. 登录赛狐，按品牌导出上周产品分析 Excel
  2. 用 ksprocess 打亏损标签，仅保留亏损行
  3. 返回 {"message": str, "files": [Path]}，由调度器投递到钉钉群
"""

import importlib.util
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_CHECK_PACKAGES = ["ddddocr", "Crypto", "openpyxl", "requests"]
_INSTALL_PACKAGES = [
    "ddddocr>=1.5",
    "pycryptodome>=3.20",
    "openpyxl>=3.1",
    "requests>=2.31",
]


def _get_project_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return path.parents[4]


def _ensure_deps() -> None:
    missing = [p for p in _CHECK_PACKAGES if importlib.util.find_spec(p) is None]
    if not missing:
        return
    uv = shutil.which("uv") or "uv"
    logger.info("[kswarn_saihu] 缺少依赖 %s，正在安装...", missing)
    try:
        subprocess.run(
            [uv, "pip", "install"] + _INSTALL_PACKAGES,
            check=True,
            capture_output=False,
        )
        importlib.invalidate_caches()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"依赖安装失败，请手动执行：uv pip install {' '.join(_INSTALL_PACKAGES)}"
        ) from exc


def run() -> dict:
    """
    执行赛狐亏损预警工作流。
    返回 {"message": str, "files": [Path]}
    """
    username = os.environ.get("SELLFOX_USERNAME")
    password = os.environ.get("SELLFOX_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "缺少赛狐凭证，请在 .env 中配置 SELLFOX_USERNAME / SELLFOX_PASSWORD"
        )

    _ensure_deps()

    project_root = _get_project_root()

    from .export import SellFoxExporter, last_week_range
    from .ksprocess import process_dir

    t0 = datetime.now()
    start_date, end_date = last_week_range()
    logger.info("[kswarn_saihu] 导出日期范围：%s ~ %s", start_date, end_date)

    exporter = SellFoxExporter(username, password)
    exporter.run(start_date=start_date, end_date=end_date, base_dir=str(project_root))

    output_dir = SellFoxExporter._output_dir(base_dir=str(project_root))
    processed_dir = Path(process_dir(output_dir))
    files = sorted(processed_dir.glob("*.xlsx")) if processed_dir.exists() else []

    elapsed = int((datetime.now() - t0).total_seconds())
    brand_count = len(files)

    message = (
        f"✅ 赛狐亏损预警完成（{start_date} ~ {end_date}）\n"
        f"共处理 {brand_count} 个品牌，耗时 {elapsed}s"
    )
    if not files:
        message += "\n（无亏损数据）"

    return {"message": message, "files": files}
