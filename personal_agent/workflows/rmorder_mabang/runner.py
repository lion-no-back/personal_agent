import importlib.util
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_CHECK_PACKAGES = ["aiohttp", "pandas", "lxml", "bs4", "openpyxl", "tqdm"]
# uv pip install（纯加法）而非 uv sync：sync 会尝试删除 sqlite_vec.dll 等被进程锁住的文件，
# 导致 OS error 5（拒绝访问）。pip install 只增量补装，不触碰现有包。
_INSTALL_PACKAGES = [
    "aiohttp>=3.9",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
    "openpyxl>=3.1",
    "pandas>=2.0",
    "tqdm>=4.66",
]


def _ensure_deps() -> None:
    missing = [p for p in _CHECK_PACKAGES if importlib.util.find_spec(p) is None]
    if not missing:
        return
    uv = shutil.which("uv") or "uv"
    logger.info("[rmorder_mabang] 缺少依赖 %s，正在安装...", missing)
    try:
        subprocess.run([uv, "pip", "install"] + _INSTALL_PACKAGES, check=True, capture_output=False)
        importlib.invalidate_caches()
        logger.info("[rmorder_mabang] 依赖安装完成")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"依赖安装失败，请手动执行：uv pip install {' '.join(_INSTALL_PACKAGES)}"
        ) from exc


def run() -> dict:
    for key in ("MABANG_USERNAME", "MABANG_PASSWORD"):
        if not os.environ.get(key):
            raise RuntimeError(f"缺少马帮凭证，请在 .env 中配置 {key}")

    _ensure_deps()

    from . import rmorder_controller

    start = datetime.now()
    output_dir = rmorder_controller.main()
    elapsed = int((datetime.now() - start).total_seconds())

    merged_csv = Path(output_dir) / "merged.csv"
    if not merged_csv.exists():
        raise RuntimeError(f"merged.csv 未生成，请检查日志: {output_dir}")

    import pandas as pd

    df = pd.read_csv(merged_csv, encoding="utf-8-sig")
    row_count = len(df)
    merged_xlsx = Path(output_dir) / "merged.xlsx"
    df.to_excel(merged_xlsx, index=False)

    message = (
        f"✅ 移除订单报告已生成\n"
        f"共 {row_count} 条记录\n"
        f"耗时: {elapsed}s\n"
        f"时间: {start.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return {"message": message, "files": [merged_xlsx]}
