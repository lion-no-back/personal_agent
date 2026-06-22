"""
亚马逊排行榜播报 workflow 入口（配置 / 运行两段，仿 fxreview_amazon）。

由 ConversationAgent 按需触发，单包内按消息内容分流：
  - 含「排名配置」 → 配置段：装 scrapling[all] + 下载 Chromium 到本工作流 chromium/ 目录
  - 否则           → 运行段：前置需已配置，否则提示先配置；抓榜 + 详情页解析 + 品牌匹配 +
                     截图，按命中商品组装「图片(在前) + 文字(在后)」的 segments

运行段绝不下载依赖/浏览器（重活全部前置到配置段），未配置时直接提示先发【排名配置】。
品牌 / 类目范围写死在 brand_rank_checker 中。

均返回 {"message": str, "segments": [{"image": Path|None, "text": str}]}。
"""

import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

from . import config  # 设置 PLAYWRIGHT_BROWSERS_PATH 指向本工作流 chromium/ 目录

logger = logging.getLogger(__name__)

# scrapling[all] 装好后会带来这两个包；缺任一即视为未安装。
# 裸装 scrapling 也能 import scrapling，但 import scrapling.fetchers 需要 extra 带来的 curl_cffi。
_CHECK_PACKAGES = ["scrapling", "curl_cffi"]
_INSTALL_PACKAGES = ["scrapling[all]>=0.4.9"]

_CONFIG_TRIGGER = "排名配置"
_MSG_NEED_CONFIG = "⚠️ 尚未完成排名配置，请先发送【排名配置】"


def _browser_ready() -> bool:
    """本工作流 chromium/ 目录里是否已有 Playwright 下载的 Chromium 可执行文件。"""
    base = config.BROWSERS_DIR
    if not base.exists():
        return False
    return any(base.glob("**/chrome.exe")) or any(base.glob("**/headless_shell.exe"))


def _install_browser() -> None:
    """下载 Chromium 到本工作流 chromium/ 目录（PLAYWRIGHT_BROWSERS_PATH 已由 config 指向此处）。

    scrapling 的 DynamicFetcher 底层用标准 playwright，`scrapling install` 本质即
    `python -m playwright install chromium`（见 scrapling/cli.py），这里直接调用，
    仅下载 Chromium（不含 StealthyFetcher 的 Firefox），落到本地目录、不进机器级缓存。
    """
    logger.info("[rankbb_amazon] 下载 Chromium 到 %s（首次较慢）...", config.BROWSERS_DIR)
    # 关键：必须捕获子进程输出（capture_output）。agent 以守护进程运行、无真实控制台，
    # 若让 playwright 的 Node 驱动继承守护进程的 stdout/stderr 句柄，Node CLI 会在非控制台
    # 环境下访问无效句柄而崩溃（Windows 0xC0000005 访问违例，启动即挂）。给它独立的管道
    # 句柄即可避免。fxreview 用 cloakbrowser 进程内 httpx 下载、不起 Node，故无此问题。
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        stdin=subprocess.DEVNULL,  # 守护进程下 stdin 句柄也可能无效，一并隔离
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-1500:]
        logger.error("[rankbb_amazon] Chromium 下载失败 rc=%s\n%s", proc.returncode, tail)
        raise RuntimeError("Chromium 下载失败，请手动执行：playwright install chromium")
    logger.info("[rankbb_amazon] Chromium 就绪")


def _deps_ready() -> bool:
    """运行段前置检查：scrapling/curl_cffi 已装 且 本地 chromium 已就位。"""
    pkgs_ok = all(importlib.util.find_spec(p) is not None for p in _CHECK_PACKAGES)
    return pkgs_ok and _browser_ready()


def _ensure_deps() -> None:
    """配置段调用：装 scrapling[all]，并确保本工作流目录内有 Chromium（缺则下载）。"""
    missing = [p for p in _CHECK_PACKAGES if importlib.util.find_spec(p) is None]
    if missing:
        logger.info("[rankbb_amazon] 缺少依赖 %s，正在安装...", missing)
        try:
            from personal_agent.workflows.deplock import uv_pip_install
            uv_pip_install(_INSTALL_PACKAGES)  # 跨进程串行装包 + 刷新导入缓存
            logger.info("[rankbb_amazon] 依赖安装完成")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"依赖安装失败，请手动执行：uv pip install {' '.join(_INSTALL_PACKAGES)}"
            ) from exc

    if not _browser_ready():
        _install_browser()


# ────────────────────────── 配置段 ──────────────────────────

def _run_config() -> dict:
    """配置段：装 scrapling[all] + 下载 Chromium（仅安装，不做浏览器启动自检）。"""
    try:
        _ensure_deps()
    except RuntimeError as exc:
        logger.exception("[rankbb_amazon] 排名配置失败")
        return {"message": f"❌ 排名配置失败：{exc}", "segments": []}
    return {"message": "✅ 排名配置完成，现在可发送【排名播报】", "segments": []}


# ────────────────────────── 运行段 ──────────────────────────

def _run_broadcast() -> dict:
    """运行段：前置需已配置；抓榜 + 解析 + 截图，组装图文 segments。绝不下载。"""
    if not _deps_ready():
        return {"message": _MSG_NEED_CONFIG, "segments": []}

    from . import brand_rank_checker as checker

    results_sorted, shots, stats_by_site = checker.run_pipeline()

    if not results_sorted:
        return {"message": "（本次抓取范围内无目标品牌上榜）", "segments": []}

    # 按站穿插投递：每站 喜报头部(纯文字段) → 该站命中商品图文段。
    # 头部用 image=None 的文字段（投递层会发成独立文字气泡），故 message 留空；
    # 某站无命中则整块（头部+商品）都不发。
    segments = []
    for site in checker.SITES:
        site_results = [r for r in results_sorted if r["site_order"] == site.order]
        if not site_results:
            continue
        header = checker.build_site_header(site, stats_by_site.get(site.order) or {})
        if header:
            segments.append({"image": None, "text": header})
        for r in site_results:
            shot = shots.get(r["asin"])
            if not shot:
                logger.warning("[rankbb_amazon] 命中商品无截图，仅发文字 asin=%s", r.get("asin"))
            segments.append({
                "image": Path(shot) if shot else None,
                "text": checker.segment_text(r),
            })

    return {"message": "", "segments": segments}


# ────────────────────────── 入口分流 ──────────────────────────

def run(content: str = "") -> dict:
    """按消息内容分流：含「排名配置」走配置段，否则走运行段。"""
    if _CONFIG_TRIGGER in content:
        return _run_config()
    return _run_broadcast()
