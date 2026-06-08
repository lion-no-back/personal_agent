"""
飞书机器人首次配置向导

当 FEISHU_APP_ID 或 FEISHU_APP_SECRET 未配置时，由 main.py 自动触发。
引导用户在飞书开放平台创建应用、输入凭证、验证有效性，并自动写入 .env。
"""

import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

# ──────────────────────────────────────────────────────────────
#   ANSI 颜色常量（Windows 10+ 和主流终端均支持）
# ──────────────────────────────────────────────────────────────
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_WHITE = "\033[97m"

# ──────────────────────────────────────────────────────────────
#   常量
# ──────────────────────────────────────────────────────────────
_CONSOLE_URL = "https://open.feishu.cn/app"
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_BOT_GUIDE_URL = (
    "https://open.feishu.cn/document/home/develop-a-bot-in-5-minutes/create-an-app"
)
_MAX_RETRIES = 3


# ──────────────────────────────────────────────────────────────
#   内部工具函数
# ──────────────────────────────────────────────────────────────


def _enable_ansi_on_windows() -> None:
    """在 Windows 上启用 ANSI 转义序列支持（需 Windows 10 1511+）。"""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # 开启 ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def _c(text: str, *codes: str) -> str:
    """将多个 ANSI 代码应用到文本，末尾自动 RESET。"""
    return "".join(codes) + text + _RESET


def _banner() -> None:
    width = 62
    border = _c("═" * width, _BOLD, _CYAN)
    print()
    print(f"  {border}")
    print(_c(f"  {'🤖  PersonalAgent 飞书机器人配置向导':^{width}}", _BOLD, _CYAN))
    print(f"  {border}")
    print()


def _step(n: int, total: int, title: str) -> None:
    tag = _c(f" 步骤 {n}/{total} ", _BOLD, _CYAN)
    print(f"\n  {tag}  {_c(title, _BOLD, _WHITE)}")
    print(f"  {'─' * 56}")


def _ok(msg: str) -> None:
    print(f"  {_c('✅', _GREEN)}  {msg}")


def _warn(msg: str) -> None:
    print(f"  {_c('⚠', _YELLOW)}  {msg}")


def _err(msg: str) -> None:
    print(f"  {_c('❌', _RED)}  {msg}")


def _info(msg: str) -> None:
    print(f"  {_c('ℹ', _CYAN)}  {msg}")


def _print_qr_code(url: str) -> None:
    """
    尝试在终端打印 URL 的 QR 码。
    依赖 qrcode 包（可选）；若未安装则仅打印链接。
    """
    try:
        import qrcode  # type: ignore[import]
        import qrcode.constants as _qr_const  # type: ignore[import]

        qr = qrcode.QRCode(
            version=1,
            error_correction=_qr_const.ERROR_CORRECT_L,
            box_size=1,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)

        print()
        # print_ascii 不依赖 Pillow，invert=True 在深色背景终端更清晰
        qr.print_ascii(invert=True)
        print()
    except ImportError:
        # qrcode 未安装时静默跳过，只显示链接
        print()
        _info(f"提示：安装 qrcode 包（{_c('uv add qrcode', _CYAN)}）可在此处显示二维码")
        print()
    except Exception:
        print()


def _validate_credentials(app_id: str, app_secret: str) -> Tuple[bool, str]:
    """
    调用飞书「获取 tenant_access_token」接口验证凭证。

    Returns:
        (True, "验证成功")          —— 凭证有效
        (False, "<错误描述>")       —— 凭证无效或网络异常
    """
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        _TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            result: dict = json.loads(resp.read().decode("utf-8"))
        code = result.get("code", -1)
        if code == 0:
            return True, "验证成功"
        # 常见错误码友好化
        msg = result.get("msg", "未知错误")
        hints = {
            10003: "App Secret 错误，请重新确认",
            10014: "App ID 不存在，请检查是否复制完整",
            10019: "应用未发布，请先在开放平台发布应用",
        }
        return False, hints.get(code, f"{msg}（code={code}）")
    except urllib.error.HTTPError as e:
        return False, f"HTTP 错误 {e.code}：{e.reason}"
    except urllib.error.URLError as e:
        return False, f"网络连接失败（{e.reason}），请检查网络后重试"
    except Exception as e:
        return False, f"请求异常：{e}"


def _write_env(app_id: str, app_secret: str, env_path: Path) -> None:
    """
    将 FEISHU_APP_ID 和 FEISHU_APP_SECRET 写入 .env 文件。
    若字段已存在则替换，否则追加到末尾。
    """
    lines: list[str] = []
    id_written = False
    secret_written = False

    if env_path.exists():
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                key = line.split("=", 1)[0].strip()
                if key == "FEISHU_APP_ID":
                    lines.append(f"FEISHU_APP_ID={app_id}\n")
                    id_written = True
                elif key == "FEISHU_APP_SECRET":
                    lines.append(f"FEISHU_APP_SECRET={app_secret}\n")
                    secret_written = True
                else:
                    lines.append(line)
    else:
        # .env 不存在时，写入一个带注释的模板头
        lines.append("# PersonalAgent 配置文件（由配置向导自动生成）\n")
        lines.append("# 请勿将此文件提交到版本控制系统\n\n")

    if not id_written:
        lines.append(f"FEISHU_APP_ID={app_id}\n")
    if not secret_written:
        lines.append(f"FEISHU_APP_SECRET={app_secret}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _update_os_environ(app_id: str, app_secret: str) -> None:
    """将凭证同步写入当前进程的环境变量，使调用方无需重启即可读取。"""
    os.environ["FEISHU_APP_ID"] = app_id
    os.environ["FEISHU_APP_SECRET"] = app_secret


# ──────────────────────────────────────────────────────────────
#   公开接口
# ──────────────────────────────────────────────────────────────


def needs_setup() -> bool:
    """
    检查当前进程环境变量中是否缺少飞书凭证。
    在 load_dotenv() 之后调用。
    """
    return not (os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET"))


def run_feishu_setup_wizard(env_path: Optional[Path] = None) -> bool:
    """
    运行交互式飞书机器人配置向导。

    向导流程：
      1. 引导用户在飞书开放平台创建应用（附 QR 码）
      2. 交互输入 App ID / App Secret
      3. 调用飞书 API 验证凭证有效性（最多重试 3 次）
      4. 验证通过后写入 .env，并同步到当前进程环境变量

    Args:
        env_path: .env 文件路径。默认为项目根目录（本文件上三级）的 .env。

    Returns:
        True  —— 配置完成，调用方可继续初始化飞书网关
        False —— 用户取消或多次验证失败
    """
    _enable_ansi_on_windows()

    if env_path is None:
        # personal_agent/core/setup_wizard.py → 上三级即项目根
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"

    _banner()

    print(
        f"  检测到 {_c('FEISHU_APP_ID', _YELLOW)} 或 {_c('FEISHU_APP_SECRET', _YELLOW)} 尚未配置。"
    )
    print(f"  首次运行需要关联一个飞书机器人，整个过程约 {_c('2 分钟', _GREEN)}。")
    print()

    answer = input(f"  是否立即启动配置向导？[{_c('Y', _GREEN)}/n]  ").strip().lower()
    if answer in ("n", "no", "q", "quit"):
        print()
        _warn("已跳过配置向导。")
        _info(f"请手动在 {_c(str(env_path), _CYAN)} 中填写以下字段后重新启动：")
        print("\n    FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx")
        print("    FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        return False

    # ── 步骤 1：引导创建飞书应用 ────────────────────────────────
    _step(1, 2, "在飞书开放平台创建企业自建应用")

    print()
    print("  用手机扫描下方二维码，或在浏览器中打开：")
    print(f"  {_c(_CONSOLE_URL, _CYAN, _BOLD)}")
    _print_qr_code(_CONSOLE_URL)

    print(f"  {_c('操作步骤：', _BOLD)}")
    steps = [
        "登录飞书开放平台（open.feishu.cn）",
        "点击「创建企业自建应用」，填写应用名称（如 PersonalAgent）",
        "进入应用 → 左侧「添加应用能力」→ 开启「机器人」",
        "进入「凭证与基础信息」→ 复制 App ID 和 App Secret",
        "进入「版本管理与发布」→ 创建并发布版本（审核即时通过）",
    ]
    for i, s in enumerate(steps, 1):
        print(f"    {_c(str(i) + '.', _CYAN, _BOLD)}  {s}")

    print()
    _info(f"详细图文教程：{_c(_BOT_GUIDE_URL, _CYAN)}")
    print()
    input(f"  {_c('完成上述操作后，按 Enter 继续...', _DIM)}")

    # ── 步骤 2：输入并验证凭证 ──────────────────────────────────
    _step(2, 2, "输入并验证应用凭证")

    app_id = ""
    app_secret = ""

    for attempt in range(1, _MAX_RETRIES + 1):
        print()

        # 输入 App ID
        raw_id = input(f"  请输入 {_c('App ID', _BOLD)}（以 cli_ 开头）：  ").strip()
        if not raw_id:
            _err("App ID 不能为空")
            continue
        if not raw_id.startswith("cli_"):
            _warn("App ID 通常以 cli_ 开头，请确认是否粘贴正确")

        # 输入 App Secret（不回显）
        try:
            raw_secret = getpass.getpass(
                f"  请输入 {_c('App Secret', _BOLD)}（输入时不显示）：  "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _warn("输入已取消")
            return False

        if not raw_secret:
            _err("App Secret 不能为空")
            continue

        # 验证凭证
        print()
        print(f"  {_c('正在验证凭证...', _DIM)}", end="", flush=True)
        ok, msg = _validate_credentials(raw_id, raw_secret)

        if ok:
            # 清除"正在验证"行并打印成功
            print(f"\r  {_c('✅ 凭证验证通过！', _GREEN, _BOLD)}{'  ' * 20}")
            app_id = raw_id
            app_secret = raw_secret
            break
        else:
            print(f"\r  {_c('❌ 验证失败：' + msg, _RED)}{'  ' * 10}")

            if attempt < _MAX_RETRIES:
                retry = (
                    input(
                        f"\n  是否重新输入？（剩余 {_MAX_RETRIES - attempt} 次）[{_c('Y', _GREEN)}/n]  "
                    )
                    .strip()
                    .lower()
                )
                if retry in ("n", "no"):
                    print()
                    _warn("已取消配置向导，请检查凭证后重新运行。")
                    return False
            else:
                print()
                _err(f"已连续失败 {_MAX_RETRIES} 次，退出配置向导。")
                _info("请确认 App ID / App Secret 无误，且应用已发布后重新运行。")
                return False

    # ── 写入 .env 并同步环境变量 ─────────────────────────────────
    try:
        _write_env(app_id, app_secret, env_path)
    except OSError as e:
        print()
        _err(f"写入 .env 失败：{e}")
        _info("凭证验证已通过，本次启动将使用内存中的凭证；")
        _info(f"请手动将以下内容添加到 {env_path}：")
        print(f"\n    FEISHU_APP_ID={app_id}")
        print(f"    FEISHU_APP_SECRET={app_secret}\n")

    _update_os_environ(app_id, app_secret)

    # ── 完成 ─────────────────────────────────────────────────────
    print()
    border = _c("═" * 62, _BOLD, _GREEN)
    print(f"  {border}")
    _ok(f"配置已保存至  {_c(str(env_path), _CYAN)}")
    _ok("飞书机器人配置完成，PersonalAgent 即将启动...")
    print(f"  {border}")
    print()
    return True
