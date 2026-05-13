import os
import requests
import time
import json
import base64
import ddddocr
import logging
import glob
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ================= 核心配置区 =================
ACCOUNT = "18379067507"
PASSWORD = "Leixiuer@20260418"

# ================= 极简双轨制日志体系 =================
class ConsoleFilter(logging.Filter):
    def filter(self, record):
        if record.name == 'py.warnings' or 'UserWarning' in record.getMessage():
            return False
        return True

def setup_logger(log_dir):
    logging.captureWarnings(True)
    logger = logging.getLogger('SellFox')
    logger.setLevel(logging.DEBUG)

    if logger.hasHandlers():
        logger.handlers.clear()

    c_handler = logging.StreamHandler()
    c_handler.setLevel(logging.INFO)
    c_handler.addFilter(ConsoleFilter())
    c_handler.setFormatter(logging.Formatter('%(message)s'))

    log_file = os.path.join(log_dir, 'sellfox_run.log')
    f_handler = logging.FileHandler(log_file, encoding='utf-8', mode='w')
    f_handler.setLevel(logging.DEBUG)
    f_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

    logger.addHandler(c_handler)
    logger.addHandler(f_handler)
    logger.propagate = False 
    return logger

# 声明全局 logger 占位符
logger = None

# ================= 核心业务类 =================
class SellFoxLogin:
    def __init__(self, username, password, upload_dir):
        self.username = username
        self.password = password
        self.upload_dir = upload_dir 
        self.session = requests.Session()
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.aes_key = b'T3hUI4vWio25jyCBr1IIZ5pIycAxljPf'
        
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Connection': 'keep-alive',
            'Origin': 'https://www.sellfox.com',
            'Referer': 'https://www.sellfox.com/amzup-web-main/web/commodity/index.html',
            'Content-Type': 'application/json;charset=UTF-8',
        })

        # 战报：回归本质，只有成功处理和系统级崩溃
        self.stats = {"total": 0, "success": 0, "sys_fail": 0}

    def aes_ecb_encrypt(self, raw_text):
        cipher = AES.new(self.aes_key, AES.MODE_ECB)
        return base64.b64encode(cipher.encrypt(pad(raw_text.encode('utf-8'), AES.block_size, style='pkcs7'))).decode('utf-8')

    def login(self):
        logger.info("▶ 阶段 1/3：正在执行账户登录与身份鉴权...")
        vcode = None
        for i in range(3):
            try:
                res = self.session.get(f"https://www.sellfox.com/api/vcode.json?v={int(time.time()*1000)}", timeout=5)
                b64_str = res.json().get('data', '').split('base64,')[-1]
                vcode = self.ocr.classification(base64.b64decode(b64_str))
                logger.debug(f"  ├─ 验证码识别尝试 ({i+1}/3): {vcode}")
                if len(vcode) == 4: break
            except Exception as e:
                logger.debug(f"  ├─ 验证码获取异常: {e}")
                time.sleep(1)
        
        if not vcode:
            logger.error("❌ 身份鉴权失败：验证码识别多次未果。")
            return False

        try:
            sys_time = int(time.time()*1000)
            try:
                t_res = self.session.get("https://www.sellfox.com/api/systemTime.json", timeout=5).json()
                if t_res.get('code') == 200: sys_time = int(t_res.get('data', sys_time))
            except: pass

            pwd_plain = f"{self.password}:::{int(time.time()*1000)}"
            payload = json.dumps({
                "sf_u": self.username, "sf_c": vcode,
                "sf_p": self.aes_ecb_encrypt(pwd_plain),
                "sf_t": 7200, "sf_pz": "20230310", "time": sys_time
            }, separators=(',', ':'))
            
            res = self.session.post("https://www.sellfox.com/api/loginEncrypt.json", data=f'"{self.aes_ecb_encrypt(payload)}"', timeout=10).json()

            if res.get('code') in (200, 0):
                logger.info("✔ 身份鉴权成功！已成功建立安全会话。")
                return True
            else:
                logger.error(f"❌ 登录被拒：{res.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"❌ 登录请求发生代码层面崩溃: {e}")
            return False

    def download_error_file(self, url, original_filename):
        """流式下载导入失败记录"""
        try:
            save_path = os.path.join(self.upload_dir, f"赛狐导入失败记录_{original_filename}")
            logger.info("  ├─ 正在下载导入失败记录表格...")
            res = self.session.get(url, stream=True, timeout=60)
            res.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in res.iter_content(8192):
                    f.write(chunk)
            logger.info(f"  └─ 📥 失败记录已自动保存至: {os.path.basename(save_path)}")
        except Exception as e:
            logger.error(f"  └─ ❌ 失败记录表格下载异常: {e}")

    def import_products(self, file_path):
        if not os.path.exists(file_path):
            logger.error(f"[-] 文件不存在: {file_path}")
            return "sys_fail"

        filename = os.path.basename(file_path)
        logger.info(f"\n⏳ 正在处理: {filename}")
        
        upload_url = "https://www.sellfox.com/excel/import.json"
        data = {'fileName': filename, 'type': 'commodity_create'}
        upload_headers = self.session.headers.copy()
        upload_headers['Content-Type'] = None

        try:
            with open(file_path, 'rb') as f:
                files = {'FileData': (filename, f, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
                res = self.session.post(upload_url, data=data, files=files, headers=upload_headers, timeout=120).json()
                
                if res.get('code') == 0 and res.get('data'):
                    task_uuid = res.get('data')
                    logger.debug(f"  ├─ 文件流传输成功，任务 UUID: {task_uuid}")
                    return self.poll_progress(task_uuid, filename)
                else:
                    logger.error(f"  └─ ❌ 上传接口拒绝，服务器返回: {res}")
                    return "sys_fail"
        except Exception as e:
            logger.error(f"  └─ ❌ 网络传输崩溃或文件读取异常: {e}")
            return "sys_fail"

    def poll_progress(self, uuid, filename):
        poll_url = "https://www.sellfox.com/checkProcess.json"
        poll_headers = self.session.headers.copy()
        poll_headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
        
        while True:
            try:
                res = self.session.post(poll_url, data={'uuid': uuid}, headers=poll_headers, timeout=10).json()
                
                if res.get('code') == 0 and res.get('data'):
                    d = res['data']
                    num, total = d.get('num', 0), d.get('totalNum', 0)
                    logger.debug(f"  ├─ 服务器处理进度: {num}/{total} ({d.get('msg', '执行中')})")
                    
                    if total > 0 and num >= total:
                        fail_num = d.get('failNum', 0)
                        
                        # 【核心修正】：视作正常业务流，使用 INFO 级别输出
                        if fail_num > 0:
                            logger.info(f"  ├─ ℹ️ 正常业务过滤：赛狐系统判定 {fail_num} 条数据为重复或失败。")
                            error_url = d.get('data')
                            if error_url and str(error_url).startswith("http"):
                                self.download_error_file(error_url, filename)
                            else:
                                logger.info(f"  └─ ✔ 处理完毕！成功入库 {d.get('succNum')} 条。")
                            return "success" 
                            
                        logger.info(f"  └─ ✔ 处理完毕！全部成功入库 {d.get('succNum')} 条。")
                        return "success"
                time.sleep(2)
            except Exception as e:
                logger.debug(f"  ├─ 轮询发生网络抖动 (尝试重连中): {e}")
                time.sleep(2)

# ================= 供主控调用的入口 =================
def run_sellfox_upload(upload_dir: str):
    """
    供 addcontroller.py 调用的唯一入口。
    必须传入由主控分配的沙盒目录路径。
    """
    global logger
    logger = setup_logger(upload_dir)
    logger.info("🚀 启动赛狐商品自动导入模块...")
    
    client = SellFoxLogin(ACCOUNT, PASSWORD, upload_dir)
    
    if not client.login():
        return False

    logger.info("\n▶ 阶段 2/3：正在检索待上传的分割文件...")
    search_pattern = os.path.join(upload_dir, "批量导入_分割结果_第*部分.xlsx")
    files = sorted(glob.glob(search_pattern))
    if not files:
        logger.error(f"❌ 未在 {upload_dir} 发现分割产物，流水线停止。")
        return False
    logger.info(f"✔ 检索完毕，共发现 {len(files)} 个待处理文件。")

    logger.info("\n▶ 阶段 3/3：开始执行批量上传与同步任务...")
    client.stats["total"] = len(files)
    
    for f in files:
        status = client.import_products(f)
        if status == "success":
            client.stats["success"] += 1
        else:
            client.stats["sys_fail"] += 1
            # 只有遇到 HTTP 500、网络断开这种系统级崩溃，才会触发退出
            logger.error("\n⚠️ 检测到系统级异常，根据容错协议停止后续所有上传任务。")
            break 
        time.sleep(2) 

    s = client.stats
    logger.info("\n🎯 最终执行战报统计")
    logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(f"  总处理文件数：{s['total']} 个")
    logger.info(f"  成功执行完毕：{s['success']} 个 (含自动生成失败记录)")
    logger.info(f"  遭遇系统崩溃：{s['sys_fail']} 个")
    logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(f"📂 详尽底稿及失败记录表已存证至: {upload_dir}\n")
    
    return s['sys_fail'] == 0

if __name__ == "__main__":
    # 若需单独调试，可临时指定一个存在数据的测试目录
    run_sellfox_upload("split_results")