import requests
import time
import json
import base64
import ddddocr
import os
import glob
import re
import logging
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ================= 1. 配置日志系统 =================
logger = logging.getLogger(__name__)
logger.propagate = False

class SellFoxAutoClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.aes_key = b'T3hUI4vWio25jyCBr1IIZ5pIycAxljPf'
        
        self.base_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Connection': 'keep-alive',
            'Origin': 'https://www.sellfox.com',
            'Referer': 'https://www.sellfox.com/amzup-web-main/login.html'
        }
        self.session.headers.update(self.base_headers)

    def close(self):
        if self.session:
            self.session.close()
            logger.info("会话已安全关闭，系统资源已释放。")

    # ---------------- 登录相关方法 ----------------
    def aes_ecb_encrypt(self, raw_text):
        cipher = AES.new(self.aes_key, AES.MODE_ECB)
        padded_text = pad(raw_text.encode('utf-8'), AES.block_size, style='pkcs7')
        cipher_text = cipher.encrypt(padded_text)
        return base64.b64encode(cipher_text).decode('utf-8')

    def get_captcha(self):
        for i in range(3):
            timestamp = int(time.time() * 1000)
            vcode_url = f"https://www.sellfox.com/api/vcode.json?v={timestamp}"
            try:
                response = self.session.get(vcode_url, timeout=10)
                res_data = response.json()
                b64_str = res_data.get('data', '')
                if 'base64,' in b64_str:
                    b64_str = b64_str.split('base64,')[1]
                img_bytes = base64.b64decode(b64_str)
                vcode = self.ocr.classification(img_bytes)
                logger.info(f"第 {i+1} 次获取验证码，识别结果: {vcode}")
                if len(vcode) == 4:
                    return vcode
                time.sleep(1) 
            except Exception as e:
                logger.error(f"验证码异常: {e}")
                time.sleep(1)
        return None

    def get_system_time(self):
        url = "https://www.sellfox.com/api/systemTime.json"
        try:
            res = self.session.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict) and 'data' in data:
                    return int(data['data'])
                elif isinstance(data, (int, str)):
                    return int(data)
        except Exception:
            pass
        return int(time.time() * 1000)

    def login(self):
        logger.info("=== 开始登录流程 ===")
        vcode = self.get_captcha()
        if not vcode:
            logger.error("无法获取或识别验证码，登录中止。")
            return False

        local_timestamp = int(time.time() * 1000)
        password_plain = f"{self.password}:::{local_timestamp}"
        encrypted_password = self.aes_ecb_encrypt(password_plain)

        payload_dict = {
            "sf_u": self.username,
            "sf_c": vcode,
            "sf_p": encrypted_password,
            "sf_t": 7200,
            "sf_pz": "20230310",
            "time": self.get_system_time()
        }
        
        json_payload_str = json.dumps(payload_dict, separators=(',', ':'))
        final_encrypted_payload = self.aes_ecb_encrypt(json_payload_str)
        
        login_url = "https://www.sellfox.com/api/loginEncrypt.json"
        try:
            login_headers = {'Content-Type': 'application/json;charset=UTF-8'}
            payload_with_quotes = f'"{final_encrypted_payload}"'
            
            response = self.session.post(login_url, headers=login_headers, data=payload_with_quotes, timeout=15)
            res_json = response.json()
            
            code = res_json.get('code')
            msg = str(res_json.get('msg') or '')
            
            if code in (200, 0) or '成功' in msg:
                logger.info("登录成功！凭证已自动保存在 Session 中。")
                return True
            else:
                logger.error(f"登录失败，响应信息: {res_json}")
                return False
                
        except Exception as e:
            logger.error(f"登录请求发生异常: {e}")
            return False

    # ---------------- 核心业务流程 ----------------
    def run_import_job(self, file_path, output_dir, max_polls=100):
        """
        处理单个 Excel 切片。
        最大轮询次数 max_polls 默认设为 120，每次等待 5 秒，最多支持长达 10 分钟的处理时间。
        """
        if not os.path.exists(file_path):
            # 🚨 触发硬中断
            raise FileNotFoundError(f"致命错误：找不到需要上传的切片文件: {file_path}")

        file_basename = os.path.basename(file_path)
        logger.info(f"=== 开始上传文件: {file_basename} ===")
        upload_url = "https://www.sellfox.com/excel/import.json"
        upload_data = {
            "fileName": file_basename,
            "type": "commodity_update"
        }
        
        upload_headers = {
            'Content-Type': None, 
            'Referer': 'https://www.sellfox.com/amzup-web-main/web/commodity/index.html'
        }

        try:
            with open(file_path, 'rb') as f:
                files = {
                    "FileData": (file_basename, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                }
                # 针对大文件，将上传请求本身的超时拉长至 300 秒（5分钟）
                upload_resp = self.session.post(upload_url, headers=upload_headers, data=upload_data, files=files, timeout=300)
                
                upload_result = upload_resp.json()
                if upload_result.get("code") != 0:
                    # 🚨 触发硬中断
                    raise RuntimeError(f"文件 {file_basename} 上传失败，服务器返回异常: {upload_result}")
                
                task_uuid = upload_result.get("data")
                logger.info(f"文件上传成功，拿到任务 ID: {task_uuid}")

        except Exception as e:
            # 🚨 触发硬中断
            raise RuntimeError(f"文件 {file_basename} 上传阶段发生致命异常: {e}")

        logger.info(f"=== 开始轮询任务进度 (最大等待时长约 {max_polls * 5 / 60:.1f} 分钟) ===")
        poll_url = "https://www.sellfox.com/checkProcess.json"
        poll_headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://www.sellfox.com/amzup-web-main/web/commodity/index.html'
        }
        poll_data = {"uuid": task_uuid}

        poll_count = 0

        while poll_count < max_polls:
            poll_count += 1
            try:
                poll_resp = self.session.post(poll_url, headers=poll_headers, data=poll_data, timeout=10)
                poll_result = poll_resp.json()
                
                inner_data = poll_result.get("data", {})
                status_code = inner_data.get("code")
                
                if status_code == 0:
                    msg = inner_data.get('msg', '处理中')
                    logger.info(f"[{poll_count}/{max_polls}] {file_basename} 进度: {msg} ...")
                    time.sleep(5)  # 拉长单次休眠时间，降低对服务器的请求频率
                    
                elif status_code == 1:
                    msg = inner_data.get('msg', '完成')
                    fail_num = inner_data.get('failNum', 0)
                    download_url = inner_data.get('data') 
                    
                    logger.info(f"【{file_basename}】导入结束！服务器消息: {msg}")
                    
                    if fail_num > 0 and download_url and str(download_url).startswith("http"):
                        logger.info(f"发现 {fail_num} 条失败记录，准备下载错误表格...")
                        self.download_error_report(download_url, output_dir, file_basename)
                    else:
                        logger.info(f"【{file_basename}】全部导入成功，无需下载错误记录！")
                    
                    # 正常完成，退出函数
                    return 
                    
                else:
                    # 🚨 触发硬中断
                    raise RuntimeError(f"轮询 {file_basename} 时遇到异常状态码: {poll_result}")
                    
            except requests.exceptions.RequestException as req_err:
                logger.warning(f"[{poll_count}/{max_polls}] 网络连接异常，可能发生了抖动: {req_err}。将在 5 秒后重试...")
                time.sleep(5)
                continue 
                
        # 🚨 如果循环结束还没 return，说明彻底超时，触发硬中断
        raise TimeoutError(f"致命错误：文件 {file_basename} 任务未能在 {max_polls} 次轮询内完成（已超时），流水线已被迫中断！")

    def download_error_report(self, url, output_dir, origin_filename):
        try:
            file_name = url.split("/")[-1]
            save_path = os.path.join(output_dir, f"赛狐报错记录_{origin_filename}_{file_name}")
            
            logger.info(f"正在下载错误记录文件...")
            response = self.session.get(url, stream=True, timeout=60)
            response.raise_for_status()
            
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    
            logger.info(f"=== 失败记录下载成功！保存在: {save_path} ===")
        except Exception as e:
            # 下载错误报告失败不影响主流程，仅抛出警告
            logger.error(f"警告：错误报告文件下载失败，但这不影响上传主流程: {e}")

# ================= 供主控调用的入口函数 =================
def run_upload(output_dir: str, username: str, password: str):
    """供主控调用的主函数，扫描目录下的所有 Excel 拆分件进行上传"""
    client = None
    try:
        log_file = os.path.join(output_dir, "sellfox_upload.log")
        if logger.hasHandlers():
            logger.handlers.clear()
        fh = logging.FileHandler(log_file, encoding='utf-8', mode='w')
        ch = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        logger.addHandler(fh)
        logger.addHandler(ch)

        search_pattern = os.path.join(output_dir, "上传文件_第*部分.xlsx")
        excel_files = glob.glob(search_pattern)

        if not excel_files:
            logger.warning(f"目录 {output_dir} 中未找到符合条件的 '上传文件_第X部分.xlsx'，中止上传流程。")
            return

        def sort_by_part(filepath):
            match = re.search(r"第(\d+)部分", os.path.basename(filepath))
            return int(match.group(1)) if match else 0
            
        excel_files.sort(key=sort_by_part)
        logger.info(f"共发现 {len(excel_files)} 个分片文件待上传。")

        client = SellFoxAutoClient(username, password)
        if not client.login():
            raise PermissionError("赛狐登录失败，凭证错误或验证码识别失败，流水线中断！")

        for idx, file_path in enumerate(excel_files, start=1):
            logger.info(f"\n" + "="*40)
            logger.info(f"⏳ 开始处理队列第 {idx}/{len(excel_files)} 个任务")
            logger.info("="*40)
            
            # 此处如果发生错误，内部会抛出异常，直接跳到下面的 except 块
            client.run_import_job(file_path, output_dir=output_dir)
            
            if idx < len(excel_files):
                logger.info("当前切片处理完毕，进入 5 秒防并发缓冲期...\n")
                time.sleep(5)

    except Exception as e:
        logger.error(f"🚨 批量上传流水线遭遇严重异常，已强制终止程序执行: {e}", exc_info=True)
        raise  # 【关键修改】：向上抛出异常，让主控脚本（mcontroller.py）也随之崩溃退出
    finally:
        if client:
            client.close()