# -*- coding: utf-8 -*-
"""
马帮 ERP - MSKU列表导出（共享模块）

三个预警工作流（滞销/补货/清货）共用此导出逻辑。
凭证从环境变量读取：MABANG_USERNAME / MABANG_PASSWORD
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import requests

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

ROWS_PER_PAGE = 500
PAGE_WORKERS  = 8
SITE_WORKERS  = len(SITES)

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
    ("图片链接",        "uq181"),
    ("产品名称",        "uq104"),
    ("父ASIN",          "uq194"),
    ("ASIN",            "uq105"),
    ("7天销量",         "uq107"),
    ("14天销量",        "uq108"),
    ("30天销量",        "uq109"),
    ("90天销量",        "uq110"),
    ("日均销量",        "uq114"),
    ("可售",            "uq124"),
    ("待入库",          "uq125"),
    ("预留",            "uq126"),
    ("计划入库",        "uq127"),
    ("在途",            "uq128"),
    ("可售天数",        "uq129"),
    ("备注",            "uq136"),
    ("本地SKU名称",     "uq141"),
    ("售价",            "uq164"),
    ("上架时间",        "uq167"),
    ("91-180天库存数",  "uq147"),
    ("181-330天库存数", "uq229"),
    ("331-365天库存数", "uq230"),
    ("365天以上库存数", "uq199"),
]


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
        raise RuntimeError(f"[ERR] 登录响应非JSON: {r.text[:200]}")

    if res_json.get("success") is not True:
        raise RuntimeError(f"[ERR] 登录失败: {res_json.get('message', r.text[:200])}")

    s.get(f"{BASE_URL}/index.htm", headers=base_headers, timeout=10)
    time.sleep(0.5)

    cm_key = s.cookies.get("MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE", "")
    if not cm_key:
        raise RuntimeError("[ERR] 未找到登录 Cookie")

    print(f"[OK] 登录成功，cMKey={cm_key[:8]}...")
    return s, cm_key


def clone_session(src: requests.Session) -> requests.Session:
    s = requests.Session()
    s.cookies.update(src.cookies)
    return s


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


def _fetch_page(s: requests.Session, search_url: str, search_headers: dict,
                site_code: str, page: int) -> tuple:
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

    raw = data.get("fbastockId", "")
    ids = [i for i in raw.split(",") if i.strip()] if raw else []

    if not ids:
        ids = re.findall(r'name="item"\s+value="(\d+)"', data.get("html", ""))

    total_pages = 0
    if page == 1:
        m = re.search(r'<span[^>]*>(\d+)/(\d+)</span>', data.get("pageHtml", ""))
        if m:
            total_pages = int(m.group(2))
        elif ids:
            total_pages = 1

    return page, ids, total_pages


def fetch_ids_for_site(s: requests.Session, cm_key: str,
                       site_code: str, site_name: str) -> list:
    iframe_url  = activate_amz(s, cm_key)
    search_url  = f"{AMZ_URL}/index.php?mod=fbanew.listsearch"
    search_hdrs = {**ajax_headers, "Origin": AMZ_URL, "Referer": iframe_url}

    _, ids_p1, total_pages = _fetch_page(s, search_url, search_hdrs, site_code, 1)
    if not ids_p1:
        return []

    if total_pages <= 1:
        print(f"  [{site_name}] 共 1 页: {len(ids_p1)} 条")
        return ids_p1

    print(f"  [{site_name}] 共 {total_pages} 页 / 并发获取中...")

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


def process_site(main_session: requests.Session, cm_key: str,
                 site_code: str, site_name: str, output_dir: str) -> tuple:
    s = clone_session(main_session)
    ids = fetch_ids_for_site(s, cm_key, site_code, site_name)
    if not ids:
        print(f"[SKIP] {site_name} 无符合条件的记录")
        return site_name, None

    print(f"[OK] {site_name} 共 {len(ids)} 条，提交导出...")
    path = export_and_download(s, cm_key, ids, site_name, output_dir)
    if path:
        print(f"[OK] {site_name} -> {os.path.basename(path)}")
    return site_name, path


def run(output_dir=None) -> tuple:
    """执行导出，返回 (results, failures, output_dir)。
    results:  {站点名: 本地文件路径}
    failures: [导出失败的站点名]（无数据的站点不算失败）
    """
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
            if path is None:
                pass
            elif path == "":
                failures.append(name)
            else:
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
