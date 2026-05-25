# addcontroller.py
import logging
import os
import sys
from . import config  # 导入全局配置

from .name_export import run_export
from .add_ready import process_and_split_data
from .add_run import run_sellfox_upload

logger = logging.getLogger(__name__)

def main():
    logger.info("=" * 60)
    logger.info("🚀 [新增主控] 开始执行自动化工作流")
    logger.info("=" * 60)

    sandbox_dir = config.WORKSPACE_DIR
    template_path = config.TEMPLATE_PATH
    error_files_pattern = config.ERROR_FILES_PATTERN

    logger.info("[+] 读取统一沙盒目录: %s", sandbox_dir)
    logger.info("[+] 监听报错文件规则: %s", error_files_pattern)

    logger.info("▶ [新增阶段 1/3] 正在运行 name_export 导出最新映射表...")
    name_csv_path = run_export(sandbox_dir, output_filename="name.csv")

    if not name_csv_path or not os.path.exists(name_csv_path):
        logger.error("❌ 致命错误: name_export 执行失败或未生成 name.csv，流程终止。")
        sys.exit(1)

    logger.info("▶ [新增阶段 2/3] 正在运行 add_ready 进行数据清洗与拆分...")
    log_file_path = os.path.join(sandbox_dir, 'add_ready_process.log')

    split_files = process_and_split_data(
        template_path=template_path,
        name_csv_path=name_csv_path,
        error_files_pattern=error_files_pattern,
        output_dir=sandbox_dir,
        log_file=log_file_path
    )

    if not split_files:
        logger.error("❌ 致命错误: add_ready 未生成任何拆分文件（可能无报错记录或匹配失败），流程终止。")
        sys.exit(1)

    logger.info("✔ 成功生成 %d 个拆分文件，准备交接给上传模块。", len(split_files))

    logger.info("▶ [新增阶段 3/3] 正在运行 add_run 进行赛狐批量上传...")
    upload_success = run_sellfox_upload(upload_dir=sandbox_dir)

    if not upload_success:
        logger.error("❌ 致命错误: 赛狐批量上传阶段发生系统级中断，流水线被迫终止。")
        sys.exit(1)

    logger.info("✅ [新增主控] 执行完毕！")

if __name__ == "__main__":
    main()