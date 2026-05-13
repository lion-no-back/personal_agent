# config.py
# ==========================================
# ⚙️ 全局上下文配置模块 (Shared Context Configuration)
# 
# 作用：
# 作为内存中的数据仓库。
# 由主控脚本 (main_controller.py) 在运行时动态赋值
# 下游子脚本 (exportcontroller.py 和 addcontroller.py) 引入本模块读取
# 实现子脚本之间零侵入式的数据交接和配置共享。
# ==========================================

# 统一的沙盒工作目录绝对路径（由总控按本次任务时间戳生成并注入）
WORKSPACE_DIR = ""         

# 商品导入模板的绝对路径（由总控统一定义并注入）
TEMPLATE_PATH = ""         

# 赛狐报错记录匹配模式，确保只匹配当前沙盒目录下的文件（由总控拼接后注入）
ERROR_FILES_PATTERN = ""