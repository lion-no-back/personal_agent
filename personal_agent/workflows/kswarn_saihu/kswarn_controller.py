"""
主控脚本：先运行 export.py 导出数据，再用 ksprocess.py 打亏损标签

手动运行时需提前设置环境变量（或在 .env 中配置）：
  SAIHU_USERNAME  赛狐账号
  SAIHU_PASSWORD  赛狐密码
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from export import SellFoxExporter, last_week_range
from ksprocess import process_dir

if __name__ == "__main__":
    username = os.environ.get("SAIHU_USERNAME") or ""
    password = os.environ.get("SAIHU_PASSWORD") or ""
    if not username or not password:
        print("[!] 缺少赛狐凭证，请在 .env 中配置 SAIHU_USERNAME / SAIHU_PASSWORD")
        sys.exit(1)

    START_DATE, END_DATE = last_week_range()
    print(f"[*] 导出日期范围：{START_DATE} ~ {END_DATE}")

    exporter = SellFoxExporter(username, password)
    exporter.run(start_date=START_DATE, end_date=END_DATE)

    output_dir = SellFoxExporter._output_dir()
    print(f"\n[*] 开始处理 {output_dir} 中的文件...")
    process_dir(output_dir)
