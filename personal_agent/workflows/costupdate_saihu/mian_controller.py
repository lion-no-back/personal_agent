import os
from datetime import datetime
from pathlib import Path
from . import config  # 引入共享配置模块

_WORKFLOW_DIR = Path(__file__).parent

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
    print("="*60)
    print("🌟 [总控] 开始执行全链路自动化工作流 (更新 + 新增)")
    print("="*60)

    # 1. 【优雅定位】动态且精准地获取项目根目录
    try:
        project_root_path = get_project_root()
    except FileNotFoundError as e:
        print(f"❌ 致命错误: {e}")
        return
        
    # 为了兼容下游可能还在使用 os 模块的子脚本，这里统一转为字符串形式
    project_root = str(project_root_path)

    # 2. 利用 pathlib 的 "/" 操作符优雅地拼接模板绝对路径（模板与脚本同目录）
    absolute_template_path = str(_WORKFLOW_DIR / GLOBAL_TEMPLATE_PATH)

    # 3. 创建全局统一的运行沙盒目录 (以本次任务时间戳为准)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    workspace_dir_path = project_root_path / "artifact" / f"workflow_{timestamp}"
    workspace_dir_path.mkdir(parents=True, exist_ok=True) # 替代 os.makedirs
    workspace_dir = str(workspace_dir_path)
    
    # 4. 将动态路径注入到全局 config 模块中，供下游脚本共享读取
    config.WORKSPACE_DIR = workspace_dir
    config.TEMPLATE_PATH = absolute_template_path
    # 动态拼接报错文件的匹配模式
    config.ERROR_FILES_PATTERN = str(workspace_dir_path / '赛狐报错记录*.xlsx')

    print(f"📂 [总控] 统一工作空间已创建并注入配置: {workspace_dir}")
    print(f"📄 [总控] 全局模板路径已锁定至: {absolute_template_path}")

    # 5. 进程内调度阶段 1: 负责更新及产出报错数据的 exportcontroller
    print("\n" + "-"*60)
    print("▶️ [总控阶段 1/2] 启动 exportcontroller (更新流程)...")
    from . import exportcontroller
    exportcontroller.main()  # 无缝调用原函数

    # 6. 进程内调度阶段 2: 负责抓取刚才的报错数据并执行新增的 addcontroller
    print("\n" + "-"*60)
    print("▶️ [总控阶段 2/2] 启动 addcontroller (新增流程)...")
    from . import addcontroller
    addcontroller.main()     # 无缝调用原函数

    # ================= 流程完美结束 =================
    print("\n" + "="*60)
    print("🎉 [总控] 所有自动化流程已完美执行完毕！")
    print(f"📦 最终产物 (底稿、CSV、报错日志、拆分Excel) 已全部汇总至: \n   -> {workspace_dir}")
    print("="*60)

if __name__ == "__main__":
    main()