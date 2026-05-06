import os
import sys
import logging
import traceback
from datetime import datetime
from export import run_export
from cost import run_cost
from pathlib import Path


def get_project_root(markers=(".git", "pyproject.toml")) -> Path:
    """向上查找包含指定标记文件/目录的项目根目录"""
    path = Path(__file__).resolve()
    for parent in (path, *path.parents):
        if any((parent / marker).exists() for marker in markers):
            return parent
    raise FileNotFoundError(f"未找到项目根目录，标记: {markers}")


def _setup_logging(log_path: str) -> logging.Logger:
    logging.captureWarnings(True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(ch)

    return logging.getLogger(__name__)


def main():
    project_root = get_project_root()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(project_root, "artifact", f"cost_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)

    log_path = os.path.join(output_dir, f"run_{timestamp}.log")
    logger = _setup_logging(log_path)

    logger.info("=" * 60)
    logger.info(f"🚀 [主控] 初始化完成，本次任务统一输出目录: {output_dir}")
    logger.debug("项目根目录: %s | 日志文件: %s", project_root, log_path)
    logger.info("=" * 60)

    logger.info("\n▶ [主控阶段 1/2] 开始执行导出流程...")
    export_csv_path = run_export(output_dir)

    if not export_csv_path or not os.path.exists(export_csv_path):
        logger.error("❌ [主控] 导出阶段失败或未生成 CSV 文件，流程已终止。")
        logger.error("[飞书] 采购成本导出失败：数据导出阶段未成功，请查看服务器日志。")
        return

    logger.info("\n▶ [主控阶段 2/2] 开始执行成本计算流程...")
    run_cost(input_csv=export_csv_path, output_dir=output_dir)

    logger.info("\n" + "=" * 60)
    logger.info("🎯 [主控] 所有流程执行完毕！")
    logger.info(f"   最终产物已汇总至: {output_dir}")
    logger.info("=" * 60)
    logger.info(f"[飞书] 采购成本导出完成，产物已汇总至：{output_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        project_root = get_project_root()
        artifact_dir = os.path.join(project_root, "artifact")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        error_log = os.path.join(artifact_dir, f"error_{ts}.log")
        os.makedirs(artifact_dir, exist_ok=True)
        with open(error_log, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        _log = logging.getLogger(__name__)
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.ERROR, format="%(message)s")
        _log.critical("❌ [主控] 任务意外终止: %s: %s", type(e).__name__, e)
        _log.critical("[飞书] 采购成本导出异常终止：%s，技术详情已记录至 %s", type(e).__name__, error_log)
        sys.exit(1)
