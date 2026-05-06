import requests
import time
import os
import logging
import pandas as pd

BASE_URL = "https://private.mabangerp.com"
AMZ_BASE_URL = "https://private-amz.mabangerp.com"
USERNAME = "15525692520"
PASSWORD = "Tg15525692520&"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

session = requests.Session()
logger = logging.getLogger(__name__)


def login_and_get_strict_cookies() -> tuple[str | None, str | None]:
    logger.info("  ➤ [1/5] 正在登录主站，提取基础 Cookie 与 SSO 票据...")
    session.get(f"{BASE_URL}/", headers={"User-Agent": UA}, timeout=10)

    r1 = session.post(
        f"{BASE_URL}/index.php?mod=main.doLogin&lang=undefined",
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL, "Referer": f"{BASE_URL}/"},
        data={"username": USERNAME, "password": PASSWORD, "verifyType": "1"},
        timeout=10,
    )

    if not r1.json().get("success"):
        logger.error("  ❌ 主站登录失败: %s", r1.text)
        return None, None

    pure_cookies = {}
    target_keys_main = [
        "MULTI_LANGUAGE_TYPE", "MABANG_ERP_PRO_UNIQUE_ID",
        "CRAWL_KANDENG_KEY", "MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE"
    ]
    for key in target_keys_main:
        pure_cookies[key] = r1.cookies.get(key) or session.cookies.get(key)
    logger.debug("  ├─ 主站 Cookie 提取: %s", {k: bool(v) for k, v in pure_cookies.items()})

    memcache_key = pure_cookies.get("MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE", "")
    if not memcache_key:
        logger.error("  ❌ 未获取到 SSO 票据，流程终止")
        return None, None

    logger.info("  ➤ [2/5] 携带票据访问子站 SSO 接口，获取专属 PLUS Cookie...")
    sso_url = (f"{AMZ_BASE_URL}/index.php?mod=fbanew.list&platform=amazon&version=1"
               f"&cMKey={memcache_key}&fromTest=111&lang=cn&tz=UTC+8&DOMAIN=private.mabangerp.com")

    r2 = session.get(sso_url, headers={"User-Agent": UA}, timeout=10)

    target_keys_sub = ["PHPSESSID", "MABANG_ERP_PRO_MEMBERINFO_LOGIN_PLUS"]
    for key in target_keys_sub:
        val = session.cookies.get(key, domain="private-amz.mabangerp.com") or r2.cookies.get(key) or session.cookies.get(key)
        pure_cookies[key] = val

    ordered_keys = target_keys_main + target_keys_sub
    amz_cookie_str = "; ".join([f"{k}={pure_cookies[k]}" for k in ordered_keys if pure_cookies.get(k)])
    valid_count = len([k for k in ordered_keys if pure_cookies.get(k)])
    logger.debug("  └─ 跨域 Cookie 拼接完成，有效字段: %d / %d", valid_count, len(ordered_keys))
    logger.info("  ✅ 纯净跨域 Cookie 拼接完成！")
    return memcache_key, amz_cookie_str


def fetch_ids(amz_cookie_str: str) -> str | None:
    logger.info("  ➤ [3/5] 正在向子站请求导出数据 ID...")
    r = requests.post(
        f"{AMZ_BASE_URL}/index.php?mod=fbanew.listsearch",
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": AMZ_BASE_URL, "Referer": f"{AMZ_BASE_URL}/index.php?mod=fbanew.list",
                 "Cookie": amz_cookie_str},
        data=("shopId=&status=1&ispair=1&isChange=1&amazonsite=&stockStatus="
              "&stockStatusAmz=&developerId=&saleId=&setdataflag="
              "&searchtexttype=platformSkuLike&Orderby=&highsearch=&atn=list"
              "&searchtext=&searchtype=4&selecttype=platformSkuIn&platformSkuData="),
        timeout=30,
    )
    data = r.json()
    if not data.get("success"):
        logger.error("  ❌ 子站请求失败: %s", data.get('message', ''))
        return None

    ids = data.get("id", "")
    id_count = len(ids.split(',')) if ids else 0
    logger.info("  ✅ 获取到 %d 条准确的数据 ID", id_count)
    return ids


def export_and_download(ids: str, key: str, output_dir: str) -> str | None:
    logger.info("  ➤ [4/5] 正在触发导出任务...")
    r = session.post(
        f"{BASE_URL}/index.php?mod=export.doFbaExportFile",
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL,
                 "Referer": f"{BASE_URL}/index.php?mod=fbanew.list&platform=amazon&version=1"},
        data={"orderIds": ids.replace(",", "\r\n"), "fieldlabel": ["uq102", "uq106", "uq103"],
              "map-name[]": ["MSKU", "库存状态", "本地SKU"], "map-uq[]": ["uq102", "uq106", "uq103"],
              "map-text[]": ["", ""], "templateId": "0", "datasOpen": "2",
              "memcacheKey": key, "showRmbColumn": ["0", "0"], "pageSave": "1",
              "operateType": "5", "isMerage": "2"},
        timeout=60,
    )
    j = r.json()
    if not j.get("success"):
        logger.error("  ❌ 导出失败: %s", j)
        return None

    url = j["gourl"]
    xls_path = os.path.join(output_dir, "export.xls")
    logger.info("  ➤ 正在下载文件...")
    logger.debug("  ├─ 下载 URL: %s", url)
    resp = session.get(url, stream=True, timeout=300)
    total_bytes = 0
    with open(xls_path, "wb") as f:
        for chunk in resp.iter_content(8192):
            if chunk:
                f.write(chunk)
                total_bytes += len(chunk)
    logger.debug("  └─ 下载完成，共 %.1f KB", total_bytes / 1024)
    logger.info("  ✅ Excel 文件已保存: %s", xls_path)
    return xls_path


def convert_to_csv(xls_path: str, output_dir: str) -> str | None:
    logger.info("  ➤ [5/5] 正在将 Excel 转换为 CSV...")
    csv_path = os.path.join(output_dir, "export.csv")
    try:
        df = pd.read_excel(xls_path, engine='calamine')
        df = df.apply(lambda col: col.str.strip() if col.dtype == object else col)
        df.to_csv(csv_path, index=False, encoding='utf-8-sig', sep=',')
        logger.info("  ✅ 最终 CSV 基础文件已保存: %s", csv_path)
        return csv_path
    except Exception as e:
        logger.error("  ❌ Excel 转 CSV 失败: %s", e)
        return None


def run_export(output_dir: str) -> str | None:
    """暴露给主控调用的主函数，返回最终的 export.csv 路径"""
    t = time.time()
    memcache_key, auto_amz_cookie = login_and_get_strict_cookies()

    if memcache_key and auto_amz_cookie:
        data_ids = fetch_ids(auto_amz_cookie)
        if data_ids:
            xls_file = export_and_download(data_ids, memcache_key, output_dir)
            if xls_file:
                csv_file = convert_to_csv(xls_file, output_dir)
                logger.info("⏱️ 导出流程结束，共耗时 %.1fs", time.time() - t)
                return csv_file
    return None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logging.captureWarnings(True)
    os.makedirs("test_dir", exist_ok=True)
    run_export("test_dir")
