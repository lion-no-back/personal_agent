# -*- coding: utf-8 -*-
"""
清货预警主控脚本：导出 → 筛选。
可作为模块调用（runner.py 导入），也可直接执行（uv run python -m ...）。
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def run(output_dir: Optional[Path] = None) -> dict:
    """
    执行完整流程：马帮导出 → 清货筛选。

    Returns:
        {"message": str, "files": [Path]}
    """
    from ..export import run as export_run
    from ..errors import mabang_export_failed_error, mabang_network_failed_error
    from ..retry import is_retryable_request_error
    from .filter_qh import run as filter_run

    logger.info("=" * 50)
    logger.info("Step 1 / 2  导出各站点 MSKU 数据（清货）")
    logger.info("=" * 50)

    try:
        results, failures, artifact_dir = export_run(output_dir=output_dir)
    except Exception as exc:
        if is_retryable_request_error(exc):
            raise mabang_network_failed_error("马帮清货预警", exc) from exc
        raise
    if failures:
        raise mabang_export_failed_error("马帮清货预警", failures)

    logger.info("=" * 50)
    logger.info("Step 2 / 2  筛选清货预警数据")
    logger.info("=" * 50)

    output_file, hit_count = filter_run(artifact_dir, list(results.values()))

    if output_file is None:
        return {"message": "✅ 清货预警执行完成，无符合条件的记录", "files": []}

    return {
        "message": f"✅ 清货预警完成，共命中 {hit_count} 个父体",
        "files": [output_file],
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = run()
        print(result["message"])
        for f in result["files"]:
            print(f"  文件: {f}")
    except RuntimeError as e:
        print(f"[ERR] {e}", file=sys.stderr)
        sys.exit(1)
