import requests
import json
import os
import time
from datetime import datetime

# ================= 配置区域 =================
# 1. 你的马帮登录账号和密码
USER_NAME = "15525692520"        # <--- 请替换为真实账号
PASS_WORD = "Tg15525692520&"        # <--- 请替换为真实密码

# 2. 文件路径配置
PAYLOAD_FILE = r"scripts\payload_data.txt"
BASE_URL = "https://private.mabangerp.com"

# 3. 动态生成文件名：精确到年月日_时分秒
time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
SAVE_FILENAME = f"{time_str}_MSKU.xls"
# ===========================================

def mabang_full_process():
    # 记录程序开始的绝对时间
    start_time = time.time()
    
    print("=========================================")
    print("🚀 第一步：正在启动防封锁登录程序...")
    
    session = requests.Session()
    
    # 统一的伪装头，完全模拟真实浏览器
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive"
    }
    
    try:
        # [敲门]：先访问登录页，骗取系统的初始 Cookie 号牌
        print("⏳ 1/3 正在敲门获取初始 Cookie...")
        session.get(f"{BASE_URL}/", headers=base_headers, timeout=15)
        time.sleep(1) 
        
        # 准备提交的请求头和数据
        login_api_url = f"{BASE_URL}/index.php?mod=main.doLogin&lang=undefined"
        login_headers = base_headers.copy()
        login_headers.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/"
        })
        
        login_data = {
            "username": USER_NAME,
            "password": PASS_WORD,
            "verifyType": "1",
            "loginEntrance": "1",
            "timezone": "UTC+8"
        }

        # [提交]：发送真实的登录请求
        print("⏳ 2/3 正在提交账号密码...")
        login_res = session.post(login_api_url, headers=login_headers, data=login_data, timeout=15)
        
        try:
            res_json = login_res.json()
        except json.JSONDecodeError:
            print(f"❌ 登录失败，服务器返回异常数据: {login_res.text[:200]}")
            return
            
        if res_json.get("success") is True:
            print("✅ 登录验证真正通过！")
        else:
            print(f"❌ 登录被拒绝！原因: {res_json.get('message')}")
            return

        # [激活]：登录后必须访问一次后台首页，激活 companyId 环境变量
        print("⏳ 3/3 正在访问首页激活公司环境变量...")
        session.get(BASE_URL, headers=base_headers, timeout=15)
        time.sleep(1.5)

        # --- 提取终极 Cookie ---
        cookies_dict = requests.utils.dict_from_cookiejar(session.cookies)
        cookie_string = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
        print(f"🕵️ 成功提取到授权 Cookie (当前长度: {len(cookie_string)} 字符)")

        # =========================================
        # 📦 第二步：开始导出流程
        # =========================================
        print("\n=========================================")
        print("📦 第二步：准备请求导出接口...")
        
        if not os.path.exists(PAYLOAD_FILE):
            print(f"❌ 找不到 Payload 文件：{PAYLOAD_FILE}")
            print(f"👉 当前脚本运行所在的目录是：{os.getcwd()}")
            return

        with open(PAYLOAD_FILE, "r", encoding="utf-8") as f:
            payload_data = f.read().strip()

        export_url = f"{BASE_URL}/index.php?mod=export.doFbaExportFile"
        
        export_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": base_headers["User-Agent"],
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie_string  # 手动塞入刚刚提取的完整 Cookie
        }

        print("⏳ 正在向马帮服务器下达导出指令 (数据量大，打包需要时间，请耐心等待 1-3 分钟)...")
        export_res = requests.post(export_url, headers=export_headers, data=payload_data, timeout=300)
        
        if export_res.status_code == 200:
            exp_json = export_res.json()
            if exp_json.get("success"):
                download_url = exp_json.get("gourl")
                print(f"✅ 导出指令成功！云端下载链接: {download_url}")
                
                print(f"⬇️ 正在下载文件至: {SAVE_FILENAME} (请稍候)...")
                file_res = requests.get(download_url, headers={"Cookie": cookie_string}, timeout=300)
                
                if file_res.status_code == 200:
                    with open(SAVE_FILENAME, "wb") as f:
                        f.write(file_res.content)
                    print(f"🎉 大功告成！文件已完美保存！快去文件夹里看看吧。")
                else:
                    print(f"❌ 下载文件失败，状态码: {file_res.status_code}")
            else:
                print(f"❌ 导出失败: {exp_json}")
        else:
            print(f"❌ 接口通信失败，状态码: {export_res.status_code}")

    except requests.exceptions.Timeout:
        print("\n❌ 依然超时：马帮服务器处理实在太慢了（超过了5分钟）。")
        print("💡 建议：如果经常这样，你可以把代码里的 timeout=300 改成 timeout=600（10分钟），或者分批次导出数据。")
    except Exception as e:
        print(f"\n❌ 运行过程中发生严重异常: {e}")
        
    finally:
        # 无论成功还是失败，最后都会执行这里的计时逻辑
        end_time = time.time()
        total_seconds = int(end_time - start_time)
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        
        print("\n=========================================")
        print(f"⏱️ 脚本运行结束！总共耗时: {minutes} 分 {seconds} 秒")
        print("=========================================\n")

if __name__ == "__main__":
    mabang_full_process()