import os
from pathlib import Path

# 把 Playwright 浏览器缓存放到本工作流目录内（而非机器级 ~/AppData/.../ms-playwright），
# 删除 chromium/ 后下次运行会重新下载到这里。必须在 import scrapling（其底层 playwright）
# 之前设置，下载与启动才都落到本地目录。仿 fxreview_amazon 的 CLOAKBROWSER_CACHE_DIR 思路。
BROWSERS_DIR = Path(__file__).resolve().parent / "chromium"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_DIR))
