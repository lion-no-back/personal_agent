import logging
import os
from datetime import datetime
from pathlib import Path
from . import config  # 引入共享配置模块

_WORKFLOW_DIR = Path(__file__).parent
logger = logging.getLogger(__name__)

_LOG_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def _setup_workflow_logger(workspace_dir: str) -> None:
    """将三个编排模块的 logger 都接到同一个 workflow.log，关闭控制台冒泡。"""
    log_path = Path(workspace_dir) / "workflow.log"
    for name in (__name__,
                 "personal_agent.workflows.costupdate_saihu.exportcontroller",
                 "personal_agent.workflows.costupdate_saihu.addcontroller"):
        lg = logging.getLogger(name)
        lg.propagate = False
        lg.setLevel(logging.INFO)
        lg.handlers.clear()
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(_LOG_FMT)
        lg.addHandler(fh)

# ================= 核心配置区 =================
# 您可以放心填写相对路径，例如 '商品导入模板.xlsx'
GLOBAL_TEMPLATE_PATH = '商品导入模板.xlsx'

def get_project_root(markers=(".git", "pyproject.toml", ".venv")) -> Path:
    """向上查找包含指定标记文件/目录的项目根目录"""
    path = Path(__file__).resolve()
    for parent in (path, *path.parents):
        if any((parent / marker).exists() for marker in markers):
            return parent
    raise FileNotFoundError(f"未找到项目根目录，标记: {markers}")

def main():
    logger.info("=" * 60)
    logger.info("🌟 [总控] 开始执行全链路自动化工作流 (更新 + 新增)")
    logger.info("=" * 60)

    # 1. 动态获取项目根目录
    try:
        project_root_path = get_project_root()
    except FileNotFoundError as e:
        logger.error("❌ 致命错误: %s", e)
        return

    project_root = str(project_root_path)
    absolute_template_path = str(_WORKFLOW_DIR / GLOBAL_TEMPLATE_PATH)

    # 2. 创建全局统一沙盒目录
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    workspace_dir_path = project_root_path / "artifact" / f"workflow_{timestamp}"
    workspace_dir_path.mkdir(parents=True, exist_ok=True)
    workspace_dir = str(workspace_dir_path)

    # 3. 日志文件就绪后，统一配置三个编排模块的 logger
    _setup_workflow_logger(workspace_dir)

    # 4. 注入共享配置
    config.WORKSPACE_DIR = workspace_dir
    config.TEMPLATE_PATH = absolute_template_path
    config.ERROR_FILES_PATTERN = str(workspace_dir_path / '赛狐报错记录*.xlsx')

    logger.info("📂 [总控] 统一工作空间已创建并注入配置: %s", workspace_dir)
    logger.info("📄 [总控] 全局模板路径已锁定至: %s", absolute_template_path)

    # 5. 阶段 1: exportcontroller
    logger.info("-" * 60)
    logger.info("▶️ [总控阶段 1/2] 启动 exportcontroller (更新流程)...")
    from . import exportcontroller
    exportcontroller.main()

    # 6. 阶段 2: addcontroller
    logger.info("-" * 60)
    logger.info("▶️ [总控阶段 2/2] 启动 addcontroller (新增流程)...")
    from . import addcontroller
    addcontroller.main()

    logger.info("=" * 60)
    logger.info("🎉 [总控] 所有自动化流程已完美执行完毕！")
    logger.info("📦 最终产物已汇总至: %s", workspace_dir)
    logger.info("=" * 60)

if __name__ == "__main__":
    main()