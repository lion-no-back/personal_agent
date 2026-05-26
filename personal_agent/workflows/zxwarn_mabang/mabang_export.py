# -*- coding: utf-8 -*-
import requests
import json
import re
import math
import time
import os
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
# 马帮 ERP - MSKU列表导出脚本
# 筛选条件：各亚马逊站点 + 已配对 + 正常销售
# ==========================================

BASE_URL = "https://private.mabangerp.com"
AMZ_URL  = "https://private-amz.mabangerp.com"

SITES = {
    "ca": "加拿大站",
    "us": "美国站",
    "o5": "欧洲站",
    "uk": "英国站",
    "au": "澳大利亚站",
    "jp": "日本站",
}

ROWS_PER_PAGE  = 500
PAGE_WORKERS   = 8   # 单站点内并发页数
SITE_WORKERS   = len(SITES)  # 站点间并发数

base_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}
ajax_headers = {
    **base_headers,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

EXPORT_FIELDS = [
    ("店铺名称",        "uq101"),
    ("MSKU",            "uq102"),
    ("图片链接",        "uq181"),
    ("产品名称",        "uq104"),
    ("父ASIN",          "uq194"),
    ("ASIN",            "uq105"),
    ("可售",            "uq124"),
    ("待入库",          "uq125"),
    ("预留",            "uq126"),
    ("计划入库",        "uq127"),
    ("在途",            "uq128"),
    ("可售天数",        "uq129"),
    ("本地SKU名称",     "uq141"),
    ("售价",            "uq164"),
    ("上架时间",        "uq167"),
    ("91-180天库存数",  "uq147"),
    ("181-330天库存数", "uq229"),
    ("331-365天库存数", "uq230"),
    ("365天以上库存数", "uq199"),
]


# ==========================================
# STEP 1: 登录，返回 (session, cm_key)
# ==========================================
def login() -> tuple:
    username = os.environ["MABANG_USERNAME"]
    password = os.environ["MABANG_PASSWORD"]
    print("[1/3] 初始化会话并登录...")
    s = requests.Session()
    s.get(f"{BASE_URL}/", headers=base_headers, timeout=10)
    time.sleep(0.5)

    r = s.post(
        f"{BASE_URL}/index.php?mod=main.doLogin&lang=undefined",
        headers={**ajax_headers, "Origin": BASE_URL, "Referer": f"{BASE_URL}/"},
        data={"username": username, "password": password, "verifyType": "1"},
        timeout=10,
    )
    try:
        res_json = r.json()
    except json.JSONDecodeError:
        raise SystemExit(f"[ERR] 登录响应非JSON: {r.text[:200]}")

    if res_json.get("success") is not True:
        raise SystemExit(f"[ERR] 登录失败: {res_json.get('message', r.text[:200])}")

    s.get(f"{BASE_URL}/index.htm", headers=base_headers, timeout=10)
    time.sleep(0.5)

    cm_key = s.cookies.get("MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE", "")
    if not cm_key:
        raise SystemExit("[ERR] 未找到登录 Cookie")

    print(f"[OK] 登录成功，cMKey={cm_key[:8]}...")
    return s, cm_key


def clone_session(src: requests.Session) -> requests.Session:
    """共享登录 Cookie，独立线程状态。"""
    s = requests.Session()
    s.cookies.update(src.cookies)
    return s


# ==========================================
# STEP 2: 激活 AMZ 子域名 Session
# ==========================================
def activate_amz(s: requests.Session, cm_key: str) -> str:
    iframe_url = (
        f"{AMZ_URL}/index.php?mod=fbanew.list&platform=amazon&version=1"
        f"&cMKey={cm_key}&fromTest=111&lang=cn&tz=UTC%2B8&DOMAIN=private.mabangerp.com"
    )
    s.get(
        iframe_url,
        headers={**base_headers,
                 "Referer": f"{BASE_URL}/index.php?mod=fbanew.list&platform=amazon&version=1"},
        timeout=15,
    )
    return iframe_url


# ==========================================
# STEP 3: 并发翻页收集 ID
# ==========================================
def _fetch_page(s: requests.Session, search_url: str, search_headers: dict,
                site_code: str, page: int) -> tuple:
    """
    返回 (page, ids, total_pages)
    total_pages 仅在 page==1 时有效，其余为 0。
    """
    resp = s.post(
        search_url,
        headers=search_headers,
        data={
            "shopId": "", "status": "1", "ispair": "1", "isChange": "1",
            "amazonsite": site_code, "stockStatus": "", "stockStatusAmz": "",
            "developerId": "", "saleId": "", "setdataflag": "",
            "searchtexttype": "platformSkuLike", "Orderby": "", "highsearch": "",
            "atn": "list", "searchtext": "", "searchtype": "1",
            "page": str(page), "rowsPerPage": str(ROWS_PER_PAGE),
            "selecttype": "platformSkuIn", "platformSkuData": "",
        },
        timeout=30,
    )
    data = resp.json()
    if not data.get("success"):
        return page, [], 0

    # 直接用 fbastockId 字段（逗号分隔 ID），避免解析 6MB+ HTML
    raw = data.get("fbastockId", "")
    ids = [i for i in raw.split(",") if i.strip()] if raw else []

    # 回退：如果 fbastockId 为空才解析 HTML
    if not ids:
        ids = re.findall(r'name="item"\s+value="(\d+)"', data.get("html", ""))

    total_pages = 0
    if page == 1:
        m = re.search(r'<span[^>]*>(\d+)/(\d+)</span>', data.get("pageHtml", ""))
        if m:
            total_pages = int(m.group(2))
        elif ids:
            total_pages = 1  # 只有1页

    return page, ids, total_pages


def fetch_ids_for_site(s: requests.Session, cm_key: str,
                       site_code: str, site_name: str) -> list:
    iframe_url  = activate_amz(s, cm_key)
    search_url  = f"{AMZ_URL}/index.php?mod=fbanew.listsearch"
    search_hdrs = {**ajax_headers, "Origin": AMZ_URL, "Referer": iframe_url}

    # 第 1 页：拿到 ID + 总页数
    _, ids_p1, total_pages = _fetch_page(s, search_url, search_hdrs, site_code, 1)
    if not ids_p1:
        return []

    if total_pages <= 1:
        print(f"  [{site_name}] 共 1 页: {len(ids_p1)} 条")
        return ids_p1

    print(f"  [{site_name}] 共 {total_pages} 页 / 并发获取中...")

    # 剩余页全部并发
    page_results = {1: ids_p1}
    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
        futs = {
            pool.submit(_fetch_page, s, search_url, search_hdrs, site_code, p): p
            for p in range(2, total_pages + 1)
        }
        for fut in as_completed(futs):
            p, ids, _ = fut.result()
            page_results[p] = ids

    all_ids = []
    for p in range(1, total_pages + 1):
        all_ids.extend(page_results.get(p, []))

    print(f"  [{site_name}] 共收集 {len(all_ids)} 条记录")
    return all_ids


# ==========================================
# STEP 4: 导出 + 下载
# ==========================================
def export_and_download(s: requests.Session, cm_key: str,
                        all_ids: list, site_name: str, output_dir: str) -> str:
    export_params = [
        ("backUrl",  ""),
        ("orderIds", "\r\n".join(all_ids)),
    ]
    for _, uq in EXPORT_FIELDS:
        export_params.append(("fieldlabel", uq))
    for name, uq in EXPORT_FIELDS:
        export_params.append(("map-name[]", name))
        export_params.append(("map-uq[]",   uq))
        export_params.append(("map-text[]", ""))
    export_params += [
        ("templateName", ""), ("templateId", "0"), ("datasOpen", "2"),
        ("memcacheKey", cm_key), ("showRmbColumn", "2"), ("pageSave", "1"),
        ("operateType", "5"), ("params", ""), ("InterfaceUrl", ""),
        ("mainMenu", ""), ("hiddenPage", ""), ("hiddenPageSize", ""),
        ("tableBase", ""), ("isMerage", "2"), ("showRmbColumn", "2"),
    ]

    export_res = s.post(
        f"{BASE_URL}/index.php?mod=export.doFbaExportFile",
        headers={**ajax_headers, "Origin": BASE_URL, "Referer": f"{BASE_URL}/index.php"},
        data=export_params,
        timeout=120,
    )
    try:
        export_json = export_res.json()
    except json.JSONDecodeError:
        print(f"[ERR] [{site_name}] 导出响应非JSON: {export_res.text[:300]}")
        return ""

    if not export_json.get("success"):
        print(f"[ERR] [{site_name}] 导出失败: {export_res.text[:300]}")
        return ""

    download_url = export_json["gourl"]
    local_path   = os.path.join(output_dir, f"{site_name}_{download_url.split('/')[-1]}")

    dl = s.get(download_url, headers=base_headers, timeout=60)
    dl.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(dl.content)

    return local_path


# ==========================================
# 单站点完整流程（供并发调用）
# ==========================================
def process_site(main_session: requests.Session, cm_key: str,
                 site_code: str, site_name: str, output_dir: str) -> tuple:
    s = clone_session(main_session)
    ids = fetch_ids_for_site(s, cm_key, site_code, site_name)
    if not ids:
        print(f"[SKIP] {site_name} 无符合条件的记录")
        return site_name, None  # None = 无数据，非失败

    print(f"[OK] {site_name} 共 {len(ids)} 条，提交导出...")
    path = export_and_download(s, cm_key, ids, site_name, output_dir)
    if path:
        print(f"[OK] {site_name} -> {os.path.basename(path)}")
    return site_name, path


# ==========================================
# 主流程
# ==========================================
def run(output_dir=None) -> tuple:
    """执行导出，返回 (results, failures, output_dir)。
    results:  {站点名: 本地文件路径}
    failures: [导出失败的站点名]（无数据的站点不算失败）
    output_dir: 若为 None，则在脚本同目录下按日期创建
    """
    from pathlib import Path
    if output_dir is None:
        _today = date.today()
        output_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "artifact" / str(_today.year) / f"{_today.month}.{_today.day:02d}"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _output_dir_str = str(output_dir)

    main_session, cm_key = login()

    print("[2/3] 激活 AMZ 会话（主）...")
    activate_amz(main_session, cm_key)
    time.sleep(0.5)

    print(f"[3/3] 并发导出 {len(SITES)} 个站点...")
    results  = {}
    failures = []
    with ThreadPoolExecutor(max_workers=SITE_WORKERS) as pool:
        futs = {
            pool.submit(process_site, main_session, cm_key, code, name, _output_dir_str): name
            for code, name in SITES.items()
        }
        for fut in as_completed(futs):
            name, path = fut.result()
            if path is None:       # 无数据，跳过
                pass
            elif path == "":       # 导出/下载失败
                failures.append(name)
            else:                  # 成功
                results[name] = path

    print("\n========================================")
    print(f"导出完成，共 {len(results)}/{len(SITES)} 个站点成功")
    if failures:
        print(f"失败站点: {', '.join(failures)}")
    print(f"文件目录: {output_dir}")
    for name, path in results.items():
        print(f"  {name}: {os.path.basename(path)}")

    return results, failures, output_dir


if __name__ == "__main__":
    run()
