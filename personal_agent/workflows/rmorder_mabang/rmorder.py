"""
马帮ERP - 订单移除报告自动下载脚本
流程: 登录 → 跳转亚马逊专业版 → 获取token → 筛选AU/JP/DE/US店铺
     → 创建上月移除报告任务 → 轮询等待生成 → 下载CSV文件
"""

import requests
import time
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

# ── 配置 ──────────────────────────────────────────────────────────────────────
TARGET_SUFFIXES = ("AU", "JP", "DE", "US")  # 只处理这些结尾的店铺
POLL_INTERVAL = 30   # 轮询间隔（秒）
POLL_TIMEOUT = 600   # 最长等待时间（秒）

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def last_month_range():
    """返回上个月的第一天和最后一天，格式 YYYY-MM-DD"""
    today = datetime.now()
    last_day = today.replace(day=1) - timedelta(days=1)
    first_day = last_day.replace(day=1)
    return first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ── 认证流程 ──────────────────────────────────────────────────────────────────

def step1_login() -> requests.Session:
    """登录 private.mabangerp.com，返回带 session cookie 的 Session"""
    session = requests.Session()
    session.headers.update({
        **HEADERS_BASE,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://private.mabangerp.com",
        "Referer": "https://private.mabangerp.com/index.htm",
    })

    log("正在登录马帮ERP...")
    session.get("https://private.mabangerp.com/index.htm", timeout=10)
    time.sleep(1)

    resp = session.post(
        "https://private.mabangerp.com/index.php?mod=main.doLogin&lang=undefined",
        data={
            "username": os.environ["MABANG_USERNAME"],
            "password": os.environ["MABANG_PASSWORD"],
            "verifyType": "1",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"登录失败: {data.get('message', data)}")
    log("登录成功")
    session.get("https://private.mabangerp.com/", timeout=10)
    time.sleep(1)
    return session


def step2_get_amz_key(session: requests.Session) -> str:
    """跳转亚马逊专业版，返回 key 字符串"""
    log("获取亚马逊专业版跳转key...")
    resp = session.post(
        "https://private.mabangerp.com/index.php?mod=main.jumpAmzProEdition",
        headers={
            "Origin": "https://private.mabangerp.com",
            "Referer": "https://private.mabangerp.com/",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"获取跳转URL失败: {data}")
    url = data["url"]
    params = parse_qs(urlparse(url).query)
    key = params["key"][0]
    log(f"获取key成功: {key[:20]}...")
    return key


def step3_get_bearer_token(key: str) -> requests.Session:
    """用 key 换取 Bearer JWT token，返回带 Authorization 的 API Session"""
    log("用key换取Bearer token...")
    api_session = requests.Session()
    api_session.headers.update({
        **HEADERS_BASE,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "lang": "zh",
        "projectid": "erp",
        "Origin": "https://amazon-private.mabangerp.com",
        "Referer": "https://amazon-private.mabangerp.com/",
    })

    resp = api_session.post(
        f"https://api-private.mabangerp.com/fba/api/v1/login/Auth/getToken?key={key}",
        json={"key": key},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"获取token失败: {data}")

    token = data["data"]["token"]
    api_session.headers.update({"Authorization": f"Bearer {token}"})
    log("Bearer token获取成功")
    return api_session


# ── 业务逻辑 ──────────────────────────────────────────────────────────────────

def get_target_shops(api_session: requests.Session) -> dict[str, int]:
    """获取所有店铺，筛选名称结尾为 AU/JP/DE/US，返回 {name: id} 字典"""
    log("获取店铺列表...")
    resp = api_session.get("https://api-private.mabangerp.com/fba/api/v1/shop/shopCountry")
    resp.raise_for_status()
    shops = resp.json()["data"]["shop"]

    target = {
        s["name"]: s["id"] for s in shops
        if s["name"].upper().endswith(TARGET_SUFFIXES)
    }
    log(f"筛选到 {len(target)} 个目标店铺: {', '.join(target.keys())}")
    return target


def create_report_tasks(
    api_session: requests.Session,
    shops: dict[str, int],
    start_time: str,
    end_time: str,
) -> None:
    """
    提交报告任务。先查今天已有的任务，跳过已创建的店铺（24小时限制）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    time_slot = f"{start_time}_{end_time}"
    all_shop_ids = list(shops.values())

    all_today = _query_task_list(api_session, all_shop_ids, today)
    existing_tasks = [t for t in all_today if t.get("timeSlot") == time_slot]
    existing_names = {t["name"] for t in existing_tasks}

    if existing_tasks:
        log(f"今天已有 {len(existing_tasks)} 个店铺的任务，跳过重复创建：{', '.join(existing_names)}")

    new_shop_ids = [sid for name, sid in shops.items() if name not in existing_names]

    if new_shop_ids:
        log(f"为 {len(new_shop_ids)} 个店铺创建移除报告任务 [{start_time} ~ {end_time}]...")
        resp = api_session.post(
            "https://api-private.mabangerp.com/fba/api/v1/fbaReport/createReportTask",
            json={
                "shopId": new_shop_ids,
                "enumType": ["GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA"],
                "startTime": start_time,
                "endTime": end_time,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise RuntimeError(f"创建报告任务失败: {data}")
        log("新任务提交成功")
    else:
        log("所有店铺今天已有任务，无需重新创建")


def _query_task_list(api_session: requests.Session, shop_ids: list[int], today: str) -> list[dict]:
    resp = api_session.post(
        "https://api-private.mabangerp.com/fba/api/v1/fbaReport/getReportTaskList",
        json={
            "shopId": shop_ids,
            "state": [],
            "enumType": ["GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA"],
            "employeeId": [],
            "searchType": "zh",
            "searchContent": "",
            "startDate": today,
            "endDate": today,
            "perPage": 100,
            "page": 1,
        },
    )
    resp.raise_for_status()
    return resp.json()["data"]["list"]


def poll_tasks(
    api_session: requests.Session,
    shop_ids: list[int],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    轮询今日所有任务，最多等待 POLL_TIMEOUT 秒。
    返回 (done_tasks, pending_tasks, failed_tasks)：
      - done:    status == 1，可下载
      - pending: status != 1 且无 err_msg，超时后仍未生成
      - failed:  含 err_msg，生成失败
    """
    deadline = time.time() + POLL_TIMEOUT
    today = datetime.now().strftime("%Y-%m-%d")

    while True:
        all_tasks = _query_task_list(api_session, shop_ids, today)
        done    = [t for t in all_tasks if t["status"] == 1]
        failed  = [t for t in all_tasks if t.get("err_msg")]
        pending = [t for t in all_tasks if t["status"] != 1 and not t.get("err_msg")]

        log(f"状态: {len(done)} 已生成 / {len(pending)} 待执行 / {len(failed)} 失败 / 共 {len(all_tasks)} 个")

        if len(done) + len(failed) == len(all_tasks):
            log("全部任务已完成（或已失败）")
            return done, [], failed

        if time.time() >= deadline:
            log(f"等待超时（{POLL_TIMEOUT // 60} 分钟），停止轮询")
            return done, pending, failed

        time.sleep(POLL_INTERVAL)


def write_skip_summary(
    pending: list[dict],
    failed: list[dict],
    output_dir: str,
    run_time: str,
) -> None:
    """将未完成/失败的店铺汇总输出到控制台和日志文件"""
    lines = [
        "",
        "=" * 52,
        f"  跳过店铺汇总   运行时间: {run_time}",
        "=" * 52,
    ]

    if failed:
        lines.append(f"\n  【生成失败】共 {len(failed)} 个店铺")
        lines.append("  " + "-" * 40)
        for t in failed:
            lines.append(f"  ✗  {t['name']}")
            lines.append(f"     原因: {t.get('err_msg', '未知')}")

    if pending:
        lines.append(f"\n  【超时未完成】共 {len(pending)} 个店铺（等待超过 {POLL_TIMEOUT // 60} 分钟）")
        lines.append("  " + "-" * 40)
        for t in pending:
            create_time = t.get("createTime", "未知")
            lines.append(f"  ⏳  {t['name']}")
            lines.append(f"     任务创建于: {create_time}")

    lines += [
        "",
        "  提示: 建议下午3点后重新运行以获取完整数据。",
        "=" * 52,
        "",
    ]

    summary = "\n".join(lines)

    # 输出到控制台
    print(summary)

    # 写入日志文件
    os.makedirs(output_dir, exist_ok=True)
    log_filename = f"skip_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path = os.path.join(output_dir, log_filename)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(summary)
    log(f"跳过记录已写入: {log_path}")


def download_reports(tasks: list[dict], output_dir: str) -> list[str]:
    """下载所有已生成报告的CSV文件"""
    os.makedirs(output_dir, exist_ok=True)
    downloaded = []

    for task in tasks:
        url = task.get("exportUrl", "")
        if not url:
            log(f"跳过 {task['name']}: 无下载链接")
            continue

        filename = url.split("/")[-1]
        shop_name = task["name"]
        filepath = os.path.join(output_dir, f"{shop_name}_{filename}")

        log(f"下载: {shop_name} → {filename}")
        resp = requests.get(url, headers=HEADERS_BASE, timeout=60)
        resp.raise_for_status()

        with open(filepath, "wb") as f:
            f.write(resp.content)
        downloaded.append(filepath)
        log(f"已保存: {filepath}")

    return downloaded


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now()
    run_time = now.strftime("%Y-%m-%d %H:%M:%S")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_dir = os.path.join(
        project_root,
        "artifact",
        f"rmorder{now.year}",
        f"{now.month}.{now.day}",
        now.strftime("%H%M%S"),
    )
    start_time, end_time = last_month_range()
    log(f"报告区间: {start_time} ~ {end_time}")

    # 认证
    erp_session = step1_login()
    key = step2_get_amz_key(erp_session)
    api_session = step3_get_bearer_token(key)

    # 获取目标店铺 {name: id}
    shops = get_target_shops(api_session)
    if not shops:
        raise RuntimeError("未找到目标店铺，请检查店铺授权状态")
    shop_ids = list(shops.values())

    # 提交新任务（已有任务的店铺自动跳过）
    create_report_tasks(api_session, shops, start_time, end_time)

    # 轮询今日全部任务，最多等待 POLL_TIMEOUT 秒
    done_tasks, pending_tasks, failed_tasks = poll_tasks(api_session, shop_ids)

    # 汇总输出跳过的店铺（控制台 + 日志文件）
    if pending_tasks or failed_tasks:
        write_skip_summary(pending_tasks, failed_tasks, output_dir, run_time)

    # 下载已成功生成的报告
    if not done_tasks:
        raise RuntimeError("没有可下载的报告，请检查店铺授权状态或稍后重试")

    files = download_reports(done_tasks, output_dir)
    log(f"\n完成！共下载 {len(files)} 个文件，保存至: {output_dir}")
    for f in files:
        print(f"  {f}")
    return output_dir


if __name__ == "__main__":
    main()
