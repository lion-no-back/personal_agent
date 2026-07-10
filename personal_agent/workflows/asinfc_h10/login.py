"""
Helium10 自动登录脚本 (DrissionPage)
- 从 .env 读取账号密码
- 使用 ChromiumPage 的 anti-detect 能力绕过机器人检测
- 自动填写表单、提交、等待跳转到 dashboard
- 保存 cookies 为 JSON
"""
import json
import os
import random
import re
import time
from pathlib import Path

import requests


# ---------- 路径常量 ----------
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"
COOKIES_PATH = Path(__file__).resolve().parent / "cookies.json"
AUTH_STATE_PATH = Path(__file__).resolve().parent / "auth_state.json"
SCRIPT_DIR = Path(__file__).resolve().parent
BROWSER_PROFILE = PROJECT_ROOT / ".browser_profile"
DEFAULT_ENV_PATH = ENV_PATH
DEFAULT_COOKIES_PATH = COOKIES_PATH
DEFAULT_AUTH_STATE_PATH = AUTH_STATE_PATH
REQUIRED_SESSION_COOKIE_NAMES = {"sid"}
AUTH_STATE_KEYS = ["CURRENT_ACCOUNT_ID", "CORE_BEARER_TOKEN", "PACVUE_TOKENS", "USER_ID"]
AUTH_STATE_TIMEOUT = 30
BROWSER_PATH_ENV_KEY = "H10_BROWSER_PATH"

# ---------- 登录配置 ----------
BASE_URL = "https://members.helium10.com"
LOGIN_URL = "https://members.helium10.com/user/signin?language=zh-CN"
DASHBOARD_URL = "https://members.helium10.com/dashboard"
LOGIN_TIMEOUT = 60  # 等待跳转到 dashboard 的超时秒数


def load_env() -> dict[str, str]:
    """读取 .env 文件，返回键值对字典"""
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env 文件不存在: {ENV_PATH}")

    values = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("\"'")
    return values


def get_credentials() -> tuple[str, str]:
    """从 .env 获取 Helium10 账号密码"""
    env = load_env()
    email = env.get("Helium_EMAIL") or env.get("HELIUM_EMAIL")
    password = env.get("Helium_PASSWORD") or env.get("HELIUM_PASSWORD")

    if not email or not password:
        missing = []
        if not email:
            missing.append("Helium_EMAIL")
        if not password:
            missing.append("Helium_PASSWORD")
        raise ValueError(f".env 中缺少必要的配置项: {', '.join(missing)}")

    return email, password


def env_value(*keys: str) -> str | None:
    """从 .env 或系统环境变量读取第一个非空值。"""
    env = load_env()
    for key in keys:
        value = os.environ.get(key) or env.get(key)
        if value:
            return value.strip().strip("\"'")
    return None


def browser_path_from_dir(value: str) -> Path | None:
    """从显式配置的浏览器目录中解析 Chrome/Chromium 可执行文件。"""
    path = Path(value).expanduser()
    if not path.is_dir():
        return None

    for relative_path in [
        Path("chrome-win64") / "chrome.exe",
        Path("chrome-win") / "chrome.exe",
        Path("chrome.exe"),
    ]:
        candidate = path / relative_path
        if candidate.is_file():
            return candidate

    return None


def find_system_chrome() -> Path | None:
    """查找系统安装的 Chrome，不扫描 Playwright 浏览器缓存。"""
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    return None


def resolve_browser_path() -> Path:
    """解析 DrissionPage 使用的 Chrome/Chromium 可执行文件路径。"""
    explicit_value = env_value(BROWSER_PATH_ENV_KEY)
    if explicit_value:
        browser_path = browser_path_from_dir(explicit_value)
        if browser_path:
            return browser_path
        print(f"{BROWSER_PATH_ENV_KEY} 配置无效，改用系统 Chrome 或自动下载 Chromium: {explicit_value}")

    system_chrome = find_system_chrome()
    if system_chrome:
        return system_chrome

    raise RuntimeError(
        "未找到可用 Chrome。请安装 Google Chrome，或在 .env 中配置 "
        f"{BROWSER_PATH_ENV_KEY}=Chrome所在目录"
    )


def human_type(page, selector: str, text: str):
    """模拟人类逐字输入，带随机延迟"""
    ele = page.ele(selector)
    ele.click()
    time.sleep(random.uniform(0.1, 0.3))
    for char in text:
        ele.input(char)
        time.sleep(random.uniform(0.05, 0.15))


def click_submit_button(page) -> bool:
    """
    尝试多种方式点击/触发表单提交，返回是否成功。
    策略顺序：按钮点击 → 回车键
    """
    # ---- 策略 1: 按标签+属性查找提交按钮 ----
    # DrissionPage v4 语法: tag:button@type=submit
    for selector in [
        'tag:button@type=submit',
        'tag:input@type=submit',
        '@type=submit',
        'css:button[type="submit"]',
    ]:
        try:
            btn = page.ele(selector, timeout=3)
            if btn:
                btn.click()
                print(f"    [按钮点击] 使用选择器: {selector}")
                return True
        except Exception:
            continue

    # ---- 策略 2: 按文本内容查找按钮 ----
    for text in ["Sign In", "Sign in", "登录", "Login", "Log In"]:
        try:
            btn = page.ele(f'css:button:text("{text}")', timeout=2)
            if btn:
                btn.click()
                print(f"    [按钮点击] 通过文本: {text}")
                return True
        except Exception:
            continue

    # ---- 策略 3: 在密码框按 Enter 提交 ----
    try:
        from DrissionPage.common import Keys

        pw_ele = page.ele('@name=LoginForm[password]', timeout=2)
        pw_ele.input(Keys.ENTER)
        print("    [回车提交] 在密码框按 Enter")
        return True
    except Exception:
        pass

    # ---- 策略 4: 直接提交表单 ----
    try:
        page.run_js("document.querySelector('form').submit()")
        print("    [JS提交] 执行 form.submit()")
        return True
    except Exception:
        pass

    return False


def json_headers(referer: str | None = None) -> dict[str, str]:
    """构造 Helium10 API 请求常用的浏览器 JSON 头。"""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    return headers


def load_cookies(cookies_path: Path = DEFAULT_COOKIES_PATH) -> list[dict]:
    """读取 login.py 保存的 cookies.json。"""
    if not cookies_path.exists():
        raise FileNotFoundError(f"cookies.json 不存在，请先运行 login.py 登录: {cookies_path}")
    cookies = json.loads(cookies_path.read_text(encoding="utf-8-sig"))
    if not isinstance(cookies, list):
        raise RuntimeError(f"cookies.json 格式不正确，应为 Cookie 列表: {cookies_path}")
    names = {cookie.get("name") for cookie in cookies}
    missing = REQUIRED_SESSION_COOKIE_NAMES - names
    if missing:
        raise RuntimeError(
            f"cookies.json 缺少关键登录 Cookie: {', '.join(sorted(missing))}。"
            "请重新运行 DrissionPage_headless.py 生成完整 cookies。"
        )
    return cookies


def parse_json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def load_auth_state(auth_state_path: Path = DEFAULT_AUTH_STATE_PATH) -> dict:
    """读取无头登录脚本从 localStorage 导出的前端认证状态。"""
    if not auth_state_path.exists():
        return {}
    data = json.loads(auth_state_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(f"auth_state.json 格式不正确，应为对象: {auth_state_path}")
    return data


def auth_state_account_id(auth_state: dict) -> str | None:
    value = auth_state.get("CURRENT_ACCOUNT_ID") or auth_state.get("currentAccountId")
    if value in (None, "", "-1", -1):
        return None
    text = str(value).strip()
    if text.lower() in {"null", "undefined", "none", "nan"}:
        return None
    return text


def auth_state_tokens(auth_state: dict, account_id: str) -> tuple[str | None, str | None]:
    bearer_tokens = parse_json_value(auth_state.get("CORE_BEARER_TOKEN")) or {}
    pacvue_tokens = parse_json_value(auth_state.get("PACVUE_TOKENS")) or {}
    if not isinstance(bearer_tokens, dict):
        bearer_tokens = {}
    if not isinstance(pacvue_tokens, dict):
        pacvue_tokens = {}

    token = bearer_tokens.get(str(account_id))
    pacvue_entry = pacvue_tokens.get(str(account_id)) or {}
    pacvue_token = pacvue_entry.get("pacvueToken") if isinstance(pacvue_entry, dict) else None
    return token, pacvue_token


def collect_auth_state(page) -> dict:
    """从浏览器 localStorage 采集 Helium10 前端认证状态。"""
    state = {}
    for key in AUTH_STATE_KEYS:
        try:
            state[key] = page.run_js(f"return window.localStorage.getItem('{key}')")
        except Exception:
            state[key] = None
    state["capturedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return state


def validate_auth_state(auth_state: dict) -> None:
    """确认 localStorage 中存在后续 API 链路需要的 accountId 和 token。"""
    account_id = auth_state_account_id(auth_state)
    if not account_id:
        raise RuntimeError("登录后未从 localStorage 获取到 CURRENT_ACCOUNT_ID")

    token, pacvue_token = auth_state_tokens(auth_state, account_id)
    missing = []
    if not token:
        missing.append("CORE_BEARER_TOKEN")
    if not pacvue_token:
        missing.append("PACVUE_TOKENS")
    if missing:
        raise RuntimeError(f"登录后未从 localStorage 获取到关键认证状态: {', '.join(missing)}")


def wait_for_auth_state(page, *, timeout: int = AUTH_STATE_TIMEOUT) -> dict:
    """等待前端把 accountId 和 token 写入 localStorage。"""
    deadline = time.monotonic() + timeout
    last_state = {}
    while time.monotonic() < deadline:
        last_state = collect_auth_state(page)
        try:
            validate_auth_state(last_state)
            return last_state
        except RuntimeError:
            time.sleep(1)
    validate_auth_state(last_state)
    return last_state


def best_effort_auth_state(page) -> dict:
    """尽量采集认证状态；localStorage 未写完时不阻断 cookies 保存。"""
    auth_state = collect_auth_state(page)
    if not auth_state_account_id(auth_state):
        account_id = extract_account_id(page.url or "")
        if not account_id:
            try:
                account_id = extract_account_id(page.html or "")
            except Exception:
                account_id = None
        if account_id:
            auth_state["CURRENT_ACCOUNT_ID"] = account_id
    return auth_state


def write_auth_state_atomic(auth_state: dict, auth_state_path: Path = DEFAULT_AUTH_STATE_PATH) -> None:
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = auth_state_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(auth_state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(auth_state_path)


def write_cookies_atomic(cookies: list[dict], cookies_path: Path = DEFAULT_COOKIES_PATH) -> None:
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cookies_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(cookies_path)


def apply_cookies(session: requests.Session, cookies: list[dict]) -> None:
    """把 DrissionPage 导出的 cookies 放入 requests.Session。"""
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        session.cookies.set(
            name,
            value,
            domain=cookie.get("domain"),
            path=cookie.get("path") or "/",
        )


def extract_account_id(text: str) -> str | None:
    """从跳转 URL 或页面源码中提取 accountId。"""
    patterns = [
        r"[?&]accountId=([A-Za-z0-9_-]+)",
        r"[?&]account_id=([A-Za-z0-9_-]+)",
        r'"accountId"\s*:\s*"([^"]+)"',
        r'"accountId"\s*:\s*([A-Za-z0-9_-]+)',
        r'"account_id"\s*:\s*"([^"]+)"',
        r'"account_id"\s*:\s*([A-Za-z0-9_-]+)',
        r"'accountId'\s*:\s*'([^']+)'",
        r"accountId\s*[:=]\s*['\"]?([A-Za-z0-9_-]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            account_id = match.group(1).strip()
            if account_id.lower() not in {"null", "undefined", "none", "nan"}:
                return account_id
    return None


def resolve_account_id(session: requests.Session, *, timeout: int = LOGIN_TIMEOUT) -> str:
    """
    用 cookies 访问 dashboard，并从最终 URL 或页面内容里解析 accountId。
    如果跳回登录页，说明 cookies 已失效。
    """
    response = session.get(DASHBOARD_URL, headers=json_headers(DASHBOARD_URL), timeout=timeout)
    final_url = response.url or ""
    if response.status_code in {401, 403} or "/user/signin" in final_url:
        raise RuntimeError("cookies.json 已失效或无权限访问 Dashboard，请重新运行 login.py 获取新 cookies。")

    account_id = extract_account_id(final_url) or extract_account_id(response.text)
    if not account_id:
        raise RuntimeError("已使用 cookies 访问 Dashboard，但未能从网页信息中解析 accountId。")
    return account_id


def chromium_options():
    """创建 login.py 和刷新认证态共用的 Chromium 配置。"""
    from DrissionPage import ChromiumOptions

    co = ChromiumOptions()
    browser_path = resolve_browser_path()
    print(f"使用浏览器: {browser_path}")
    co.set_browser_path(str(browser_path))
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    co.set_user_data_path(str(BROWSER_PROFILE.resolve()))
    co.auto_port()

    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--remote-allow-origins=*")
    return co


def login_session(
    *,
    env_path: Path = DEFAULT_ENV_PATH,
    cookies_path: Path = DEFAULT_COOKIES_PATH,
    auth_state_path: Path = DEFAULT_AUTH_STATE_PATH,
    account_id: str | None = None,
    timeout: int = LOGIN_TIMEOUT,
) -> requests.Session:
    """
    复用已有 cookies.json 创建 requests.Session。
    env_path 仅保留为调用兼容参数；这里不会自动账号密码登录。
    """
    _ = env_path
    session = requests.Session()
    session.headers.update(json_headers(BASE_URL))
    apply_cookies(session, load_cookies(cookies_path))
    auth_state = load_auth_state(auth_state_path)
    resolved_account_id = account_id or auth_state_account_id(auth_state) or resolve_account_id(session, timeout=timeout)
    token, pacvue_token = auth_state_tokens(auth_state, resolved_account_id)
    session.helium10_account_id = resolved_account_id
    session.helium10_token = token
    session.helium10_pacvue_token = pacvue_token
    return session


def login() -> list[dict]:
    """
    使用 DrissionPage ChromiumPage 自动登录 Helium10
    返回 cookies 列表
    """
    email, password = get_credentials()

    # ---------- 配置 Chromium (anti-detect) ----------
    from DrissionPage import ChromiumPage
    co = chromium_options()

    # ---------- 启动浏览器 ----------
    page = ChromiumPage(addr_or_opts=co)

    try:
        # 1. 访问登录页
        print(f"[1/5] 正在访问登录页: {LOGIN_URL}")
        page.get(LOGIN_URL)
        time.sleep(random.uniform(1.5, 3.0))

        # 2. 检查是否已登录（直接跳到 dashboard）
        if "/dashboard" in page.url:
            print("[!] 检测到已登录状态，跳过填表流程")
        else:
            # 等待表单加载
            print("[2/5] 等待登录表单加载...")
            page.wait.ele_displayed('@name=LoginForm[email]', timeout=15)

            # 3. 填写账号密码
            print("[3/5] 填写登录凭据...")
            human_type(page, '@name=LoginForm[email]', email)
            time.sleep(random.uniform(0.3, 0.8))
            human_type(page, '@name=LoginForm[password]', password)
            time.sleep(random.uniform(0.2, 0.5))

            # 4. 提交登录
            print("[4/5] 提交登录表单...")
            submitted = click_submit_button(page)
            if not submitted:
                print("[!] 所有提交方式均失败，但仍继续等待跳转")

            # 5. 等待跳转到 dashboard
            print(f"[5/5] 等待跳转到 Dashboard（最长等待 {LOGIN_TIMEOUT}s）...")
            try:
                page.wait.url_change(DASHBOARD_URL, timeout=LOGIN_TIMEOUT)
                print("[OK] 成功跳转到 Dashboard！")
            except Exception:
                current_url = page.url
                if "/dashboard" in current_url:
                    print(f"[OK] 已在 Dashboard: {current_url}")
                else:
                    print(f"[!] 未检测到 Dashboard 跳转，当前 URL: {current_url}")
                    print(f"[!] 页面标题: {page.title}")
                    if page.ele('iframe@src*=recaptcha'):
                        print("[!] 检测到 reCAPTCHA 验证，可能需要手动处理")
                    else:
                        print("[!] 未知情况，请检查浏览器窗口")

        # 6. 提取认证状态和 cookies
        print("\n正在提取认证状态...")
        try:
            auth_state = wait_for_auth_state(page, timeout=AUTH_STATE_TIMEOUT)
            print(f"已获取 accountId: {auth_state_account_id(auth_state)}")
        except RuntimeError as error:
            auth_state = best_effort_auth_state(page)
            account_id = auth_state_account_id(auth_state)
            if account_id:
                print(f"[!] localStorage 认证状态不完整，将使用 cookies 继续；已解析 accountId: {account_id}")
            else:
                print(f"[!] localStorage 认证状态不完整，将仅保存 cookies，后续请求会尝试从 Dashboard 解析 accountId: {error}")

        print("正在提取 Cookies...")
        cookies = page.cookies(all_domains=True, all_info=True)
        print(f"获取到 {len(cookies)} 个 Cookie")

        write_cookies_atomic(cookies)
        if auth_state_account_id(auth_state):
            write_auth_state_atomic(auth_state)
            print(f"认证状态已保存到: {AUTH_STATE_PATH}")
        else:
            print("未保存认证状态：本次登录未解析到 accountId")
        print(f"Cookies 已保存到: {COOKIES_PATH}")

        return cookies

    finally:
        page.quit()


def main():
    print("=" * 50)
    print("  Helium10 自动登录 (DrissionPage)")
    print("=" * 50)
    try:
        login()
        print("\n[完成] 登录流程已结束")
    except Exception as e:
        print(f"\n[错误] {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
