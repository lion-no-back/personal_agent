import requests
import time
import os
import pandas as pd

BASE_URL = "https://private.mabangerp.com"
AMZ_BASE_URL = "https://private-amz.mabangerp.com"
USERNAME = "15525692520"
PASSWORD = "Tg15525692520&"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

session = requests.Session()


def login_and_get_strict_cookies() -> tuple[str | None, str | None]:
    print("  ➤ [1/5] 正在登录主站，提取基础 Cookie 与 SSO 票据...")
    session.get(f"{BASE_URL}/", headers={"User-Agent": UA}, timeout=10)
    
    r1 = session.post(
        f"{BASE_URL}/index.php?mod=main.doLogin&lang=undefined",
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL, "Referer": f"{BASE_URL}/"},
        data={"username": USERNAME, "password": PASSWORD, "verifyType": "1"},
        timeout=10,
    )
    
    if not r1.json().get("success"):
        print(f"  ❌ 主站登录失败: {r1.text}")
        return None, None

    pure_cookies = {}
    target_keys_main = [
        "MULTI_LANGUAGE_TYPE", "MABANG_ERP_PRO_UNIQUE_ID",
        "CRAWL_KANDENG_KEY", "MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE"
    ]
    for key in target_keys_main:
        pure_cookies[key] = r1.cookies.get(key) or session.cookies.get(key)

    memcache_key = pure_cookies.get("MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE", "")
    if not memcache_key:
        print("  ❌ 未获取到 SSO 票据，流程终止")
        return None, None

    print(f"  ➤ [2/5] 携带票据访问子站 SSO 接口，获取专属 PLUS Cookie...")
    sso_url = (f"{AMZ_BASE_URL}/index.php?mod=fbanew.list&platform=amazon&version=1"
               f"&cMKey={memcache_key}&fromTest=111&lang=cn&tz=UTC+8&DOMAIN=private.mabangerp.com")
    
    r2 = session.get(sso_url, headers={"User-Agent": UA}, timeout=10)
    
    target_keys_sub = ["PHPSESSID", "MABANG_ERP_PRO_MEMBERINFO_LOGIN_PLUS"]
    for key in target_keys_sub:
        val = session.cookies.get(key, domain="private-amz.mabangerp.com") or r2.cookies.get(key) or session.cookies.get(key)
        pure_cookies[key] = val

    ordered_keys = target_keys_main + target_keys_sub
    amz_cookie_str = "; ".join([f"{k}={pure_cookies[k]}" for k in ordered_keys if pure_cookies.get(k)])
    
    print("  ✅ 纯净跨域 Cookie 拼接完成！")
    return memcache_key, amz_cookie_str


def fetch_ids(amz_cookie_str: str) -> str | None:
    print("  ➤ [3/5] 正在向子站请求导出数据 ID...")
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
        print(f"  ❌ 子站请求失败: {data.get('message', '')}")
        return None

    ids = data.get("id", "")
    id_count = len(ids.split(',')) if ids else 0
    print(f"  ✅ 获取到 {id_count} 条准确的数据 ID")
    return ids


def export_and_download(ids: str, key: str, output_dir: str) -> str | None:
    print("  ➤ [4/5] 正在触发导出任务...")
    r = session.post(
        f"{BASE_URL}/index.php?mod=export.doFbaExportFile",
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL,
                 "Referer": f"{BASE_URL}/index.php?mod=fbanew.list&platform=amazon&version=1"},
        data={"orderIds": ids.replace(",", "\r\n"), "fieldlabel": ["uq102", "uq141"],
              "map-name[]": ["MSKU", "本地SKU名称"], "map-uq[]": ["uq102", "uq141"],
              "map-text[]": ["", ""], "templateId": "0", "datasOpen": "2",
              "memcacheKey": key, "showRmbColumn": ["0", "0"], "pageSave": "1",
              "operateType": "5", "isMerage": "2"},
        timeout=300,
    )
    j = r.json()
    if not j.get("success"):
        print(f"  ❌ 导出失败: {j}")
        return None

    url = j["gourl"]
    
    xls_path = os.path.join(output_dir, "export.xls")
    print("  ➤ 正在下载文件...")
    resp = session.get(url, stream=True, timeout=300)
    with open(xls_path, "wb") as f:
        for chunk in resp.iter_content(8192):
            if chunk:
                f.write(chunk)
    print(f"  ✅ Excel 文件已保存: {xls_path}")
    return xls_path


def convert_to_csv(xls_path: str, output_dir: str, output_filename: str = "export.csv") -> str | None:
    print("  ➤ [5/5] 正在将 Excel 转换为 CSV...")
    csv_path = os.path.join(output_dir, output_filename)
    try:
        df = pd.read_excel(xls_path, engine='calamine')
        df = df.apply(lambda col: col.str.strip() if col.dtype == object else col)
        df.to_csv(csv_path, index=False, encoding='utf-8-sig', sep=',')
        print(f"  ✅ 最终 CSV 基础文件已保存: {csv_path}")
        return csv_path
    except Exception as e:
        print(f"  ❌ Excel 转 CSV 失败: {e}")
        return None

def run_export(output_dir: str, output_filename: str = "export.csv") -> str | None:
    """暴露给主控调用的主函数，返回最终的 csv 路径"""
    t = time.time()
    memcache_key, auto_amz_cookie = login_and_get_strict_cookies()

    if memcache_key and auto_amz_cookie:
        data_ids = fetch_ids(auto_amz_cookie)
        if data_ids:
            xls_file = export_and_download(data_ids, memcache_key, output_dir)
            if xls_file:
                csv_file = convert_to_csv(xls_file, output_dir, output_filename)
                print(f"⏱️ 导出流程结束，共耗时 {time.time()-t:.1f}s")
                return csv_file
    return None

# 保留独立运行能力供测试
if __name__ == "__main__":
    os.makedirs("test_dir", exist_ok=True)
    run_export("test_dir")