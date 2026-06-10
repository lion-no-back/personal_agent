"""
赛狐产品分析 - 按店铺导出 Excel（父ASIN汇总维度）

流程：
  1. 登录：AES 加密 + 验证码 OCR
  2. GET /api/index/customShops.json 获取全部店铺
  3. 逐店铺 POST /api/productAnalyze/new/export.json → 获得 taskId
  4. 轮询 POST /api/report/center/task/download.json → 获得签名 URL
  5. GET 签名 URL 下载 xlsx 文件
"""

import base64
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import ddddocr
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE_URL = "https://www.sellfox.com"
AES_KEY = b"T3hUI4vWio25jyCBr1IIZ5pIycAxljPf"

# 导出字段顺序（与页面「已选」列表一致）
INCLUDE_LIST = [
    "parentAsin",           # 父ASIN
    "mskus",                # MSKU
    "productName",          # 品名
    "shopName",             # 店铺
    "labelName",            # 产品标签
    "profitPriceThis",      # 毛利润
    "profitRateThis",       # 毛利率
    "acoasThis",            # ACoAS
    "refundRateThis",       # 退款率
    "adConversionRateThis", # 广告转化率
    "adOrderRateThis",      # 广告订单量占比
    "acosThis",             # ACoS
    "cpcThis",              # CPC
    "salePriceThis",        # 销售额
    "productTotalNumThis",  # 销量
    "sessionsThis",         # Sessions
    "conversionRateThis",   # 转化率
    "rank",                 # 小类目排名
    "ratingThis",           # 星级评分
    "adImpressionsThis",    # 广告曝光量
    "adClicksThis",         # 广告点击量
    "adClickRateThis",      # 广告点击率
    "adCostThis",           # 广告花费
    "adSaleNumThis",        # 广告销量
]


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def aes_ecb_encrypt(raw_text: str) -> str:
    cipher = AES.new(AES_KEY, AES.MODE_ECB)
    padded = pad(raw_text.encode("utf-8"), AES.block_size, style="pkcs7")
    return base64.b64encode(cipher.encrypt(padded)).decode("utf-8")


def last_week_range() -> tuple[str, str]:
    """返回上周一和上周日，例如今天 6.9(周二) → ('2026-06-01', '2026-06-07')"""
    today = datetime.now()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)
    return last_monday.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d")


# ── 主类 ──────────────────────────────────────────────────────────────────────

class SellFoxExporter:

    def __init__(self, username: str = "", password: str = ""):
        self.username = username or os.environ.get("SAIHU_USERNAME", "")
        self.password = password or os.environ.get("SAIHU_PASSWORD", "")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/amzup-web-main/login.html",
        })
        self._ocr = ddddocr.DdddOcr(show_ad=False)

    # ── 登录 ─────────────────────────────────────────────────────────────────

    def _get_captcha(self) -> Optional[str]:
        for i in range(3):
            ts = int(time.time() * 1000)
            resp = self.session.get(f"{BASE_URL}/api/vcode.json?v={ts}", timeout=5)
            b64 = resp.json().get("data", "")
            if "base64," in b64:
                b64 = b64.split("base64,")[1]
            vcode = self._ocr.classification(base64.b64decode(b64))
            print(f"[*] 验证码识别（第{i+1}次）: {vcode}")
            if len(vcode) == 4:
                return vcode
            time.sleep(1)
        return None

    def _get_system_time(self) -> int:
        try:
            resp = self.session.get(f"{BASE_URL}/api/systemTime.json", timeout=5)
            data = resp.json()
            if isinstance(data, dict) and "data" in data:
                return int(data["data"])
            return int(data)
        except Exception:
            return int(time.time() * 1000)

    def login(self):
        vcode = self._get_captcha()
        if not vcode:
            raise RuntimeError("验证码识别失败，已重试 3 次")

        ts = int(time.time() * 1000)
        enc_password = aes_ecb_encrypt(f"{self.password}:::{ts}")

        payload_dict = {
            "sf_u": self.username,
            "sf_c": vcode,
            "sf_p": enc_password,
            "sf_t": 7200,
            "sf_pz": "20230310",
            "time": self._get_system_time(),
        }
        enc_payload = aes_ecb_encrypt(json.dumps(payload_dict, separators=(",", ":")))

        resp = self.session.post(
            f"{BASE_URL}/api/loginEncrypt.json",
            data=f'"{enc_payload}"',
            timeout=10,
        )
        result = resp.json()
        print(f"[*] 登录响应: {result}")

        code = result.get("code")
        msg = str(result.get("msg") or "")
        if code in (0, 200) or "成功" in msg:
            print("[+] 登录成功")
        else:
            raise RuntimeError(f"登录失败: {result}")

    # ── 店铺列表 ──────────────────────────────────────────────────────────────

    def get_shops(self) -> list[dict]:
        """
        返回 [{id, shopName, marketplace, ...}, ...]
        数据来源：/shop/selectAllPlatformShopInfo.json
        结构为 [{platform, shopInfoList:[{id, shopName, ...}]}, ...]
        """
        resp = self.session.post(
            f"{BASE_URL}/shop/selectAllPlatformShopInfo.json",
            json={},
            timeout=10,
        )
        resp.raise_for_status()
        groups = resp.json().get("data") or []
        shops = []
        for group in groups:
            shops.extend(group.get("shopInfoList") or [])
        return shops

    def print_shops(self):
        """打印店铺列表，用于确认 shopId / shopName 字段名"""
        shops = self.get_shops()
        print(f"\n共 {len(shops)} 个店铺：")
        for s in shops:
            print(f"  {s}")

    # ── 提交导出任务 ──────────────────────────────────────────────────────────

    def submit_export(self, shop_ids: list[int], start_date: str, end_date: str) -> int:
        payload = {
            "pageNo": 1, "pageSize": 20,
            "tagId": "", "asinType": 2,
            "currency": "USD",
            "devIds": "", "operatorIds": "",
            "marketplaceIds": "", "searchContent": "",
            "searchType": "parentAsin", "searchMode": "exact",
            "shopIds": ",".join(str(i) for i in shop_ids),
            "startDate": start_date,
            "endDate": end_date,
            "fullCid": "", "brandIds": [],
            "compareType": 0, "labelIds": "",
            "userIdList": [], "shopIdList": shop_ids,
            "marketplaceIdList": [],
            "preStartDate": "", "preEndDate": "",
            "isNewOrMovingOrInStock": 0,
            "newDevIds": [], "newOperatorIds": [],
            "mergeAsin": False,
            "orderBy": "productTotalNumThis", "desc": True,
            "includeList": INCLUDE_LIST,
            "exportYearOverYear": 0,
            "exportType": 2,
            "exportLabelGroup": False,
        }
        resp = self.session.post(
            f"{BASE_URL}/api/productAnalyze/new/export.json",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"提交导出失败: {result}")
        return result["data"]["taskId"]

    # ── 轮询下载链接 ──────────────────────────────────────────────────────────

    def _check_task_status(self, task_id: int) -> str:
        """查报告中心，返回任务状态：COMPLETE / FATAL / PROCESSING"""
        resp = self.session.post(
            f"{BASE_URL}/api/report/center/task/pageList.json",
            json={"pageNo": 1, "pageSize": 50},
            timeout=10,
        )
        rows = resp.json().get("data", {}).get("rows") or []
        for row in rows:
            if row.get("id") == task_id:
                return row.get("status", "PROCESSING")
        return "PROCESSING"

    def poll_download_url(self, task_id: int, max_wait: int = 180) -> str:
        """轮询直到文件生成，返回签名下载 URL；任务 FATAL 时立即抛出"""
        dl_url = f"{BASE_URL}/api/report/center/task/download.json"
        deadline = time.time() + max_wait
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            status = self._check_task_status(task_id)
            if status == "FATAL":
                raise RuntimeError(f"导出数据为空 (taskId={task_id})")
            urls: list = self.session.post(
                dl_url, json={"ids": [task_id]}, timeout=10
            ).json().get("data") or []
            if urls:
                return urls[0]
            print(f"    [{attempt}] 文件生成中，5s 后重试...")
            time.sleep(5)
        raise TimeoutError(f"任务 {task_id} 在 {max_wait}s 内未完成")

    # ── 下载文件 ──────────────────────────────────────────────────────────────

    @staticmethod
    def download_file(url: str, save_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_kb = os.path.getsize(save_path) // 1024
        print(f"    已保存 → {save_path}  ({size_kb} KB)")

    # ── 主流程 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _brand_name(shop_name: str) -> str:
        """
        提取店铺名（倒数第二段）：
          Amazon-HSP-BR  → HSP   （去掉国家BR，再去掉平台Amazon）
          HSP-JP         → HSP   （去掉国家JP，无平台前缀）
          LEIXIUER-US    → LEIXIUER
        """
        without_country, _, _ = shop_name.rpartition("-")   # 去掉最后一段（国家）
        if "-" in without_country:
            _, _, brand = without_country.rpartition("-")    # 再去掉倒数第二个-之前的部分
            return brand
        return without_country

    @staticmethod
    def _output_dir(base_dir: str = "") -> str:
        """生成保存目录：{base_dir}/artifact/kswarn/{年}/{月.日}，日期为运行当天"""
        today = datetime.now()
        rel = os.path.join(
            "artifact", "kswarn",
            str(today.year),
            f"{today.month}.{today.day}",
        )
        return os.path.join(base_dir, rel) if base_dir else rel

    def run(
        self,
        start_date: str,
        end_date: str,
        brand_filter: Optional[list[str]] = None,
        base_dir: str = "",
    ):
        """
        按品牌分组导出产品分析 Excel（每组一次批量导出）。

        Args:
            start_date:    "YYYY-MM-DD"
            end_date:      "YYYY-MM-DD"
            brand_filter:  指定品牌名列表（最后'-'前的名称）；None 表示全部
            base_dir:      输出目录前缀；空串表示相对当前工作目录
        """
        print("[*] 登录中...")
        self.login()

        print("[*] 获取店铺列表...")
        shops = self.get_shops()
        if not shops:
            raise RuntimeError("未获取到店铺，请先调用 print_shops() 确认接口返回结构")

        # 按品牌分组（大小写不敏感，保留首次出现的大小写作为文件名）
        canonical: dict[str, str] = {}   # lower_key → first-seen brand name
        groups: dict[str, list[int]] = {}
        for s in shops:
            brand = self._brand_name(s.get("shopName", ""))
            lower = brand.lower()
            if lower not in canonical:
                canonical[lower] = brand
            key = canonical[lower]
            groups.setdefault(key, []).append(s["id"])

        SKIP_BRANDS = {"欧洲本土1店", "欧洲本土2店"}

        if brand_filter:
            groups = {k: v for k, v in groups.items() if k in brand_filter}

        groups = {k: v for k, v in groups.items() if k not in SKIP_BRANDS}

        output_dir = self._output_dir(base_dir=base_dir)
        print(f"[*] 共 {len(groups)} 个品牌分组，文件将保存到 {output_dir}")

        ok, fail = 0, 0
        for brand, ids in groups.items():
            print(f"\n[>] {brand}  ({len(ids)} 个站点, ids={ids})")
            try:
                task_id = self.submit_export(ids, start_date, end_date)
                print(f"    taskId={task_id}，等待文件生成...")
                time.sleep(3)
                dl_url = self.poll_download_url(task_id)
                fname = f"{brand}_{start_date}_{end_date}.xlsx"
                self.download_file(dl_url, os.path.join(output_dir, fname))
                ok += 1
            except Exception as e:
                print(f"    [!] 失败: {e}")
                fail += 1

        print(f"\n[+] 完成：成功 {ok} 个，失败 {fail} 个")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    START_DATE, END_DATE = last_week_range()
    print(f"[*] 导出日期范围：{START_DATE} ~ {END_DATE}")

    exporter = SellFoxExporter("18379067507", "Leixiuer@20260418")

    exporter.run(
        start_date=START_DATE,
        end_date=END_DATE,
        brand_filter=None,  # None=全部品牌；指定如 ["Amazon-HSP", "YRZ"]
    )
