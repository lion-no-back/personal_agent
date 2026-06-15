"""
亚马逊评论采集与 VoC 分析 workflow 入口（配置 / 运行两段，详见 SPEC.md）。

由 ConversationAgent 按需触发，单包内按消息内容分流：
  - 含「亚马逊配置」 → 配置段：确保 Chromium + 有头自动登录建立 browser_profile 信任态
  - 否则抽 ASIN     → 运行段：headless 采集 + DeepSeek VoC 分析

均返回 {"message": str, "files": [Path]}，调用方负责把文件发回原会话。
"""

import asyncio
import csv
import importlib.util
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ASIN：10 位字母数字（消息里的中文触发词不参与匹配）
_ASIN_RE = re.compile(r"[A-Za-z0-9]{10}")
_CONFIG_TRIGGER = "亚马逊配置"
_LLM_KEYS = ("LLM_REASONING_API_KEY", "LLM_REASONING_BASE_URL", "LLM_REASONING_MODEL_ID")

# 文案（SPEC §3 / §4）
_MSG_NO_ASIN = "⚠️ 未能识别 ASIN。请按【评论采集+10位ASIN】发送，例如：评论采集B0CWKH83WY"
_MSG_NEED_CONFIG = "⚠️ 尚未完成亚马逊配置，请先发送【亚马逊配置】"
_MSG_RELOGIN_FAILED = "无头登录失效，请再次发送【亚马逊配置】"


# ────────────────────────── 工具函数 ──────────────────────────

def _extract_asin(content: str) -> str | None:
    m = _ASIN_RE.search(content or "")
    return m.group(0).upper() if m else None


def _count_reviews(csv_file: Path) -> int:
    """统计 reviews.csv 的评论条数（不含表头）。"""
    if not csv_file.exists():
        return 0
    try:
        with open(csv_file, "r", encoding="utf-8-sig") as f:
            return max(sum(1 for _ in csv.reader(f)) - 1, 0)
    except Exception:
        return 0


def _require_credentials() -> tuple[str, str]:
    email = os.environ.get("AMAZON_EMAIL")
    password = os.environ.get("AMAZON_PASSWORD")
    if not email or not password:
        raise RuntimeError("缺少亚马逊账号凭证，请在 .env 配置 AMAZON_EMAIL / AMAZON_PASSWORD")
    return email, password


def _chromium_ready() -> bool:
    """检查 Chromium 是否就绪（纯路径检查，绝不触发下载）。"""
    from . import session  # noqa: F401 — 导入即设置 CLOAKBROWSER_CACHE_DIR / BINARY_PATH
    from cloakbrowser import config as cb

    override = cb.get_local_binary_override()
    if override and Path(override).exists():
        return True
    try:
        if cb.get_binary_path().exists():
            return True
        return cb.get_binary_path(cb.get_effective_version()).exists()
    except Exception:
        return False


# ────────────────────────── 配置段 ──────────────────────────

def _run_config() -> dict:
    """确保 Chromium + 有头自动登录建立信任态。"""
    if importlib.util.find_spec("cloakbrowser") is None:
        raise RuntimeError("缺少依赖 cloakbrowser，请先安装后重试")
    email, password = _require_credentials()

    from . import session  # noqa: F401 — 设置 CLOAKBROWSER_* 环境
    from cloakbrowser.download import ensure_binary

    # ① 确保 Chromium（存在跳过下载，不在才下）
    try:
        logger.info("[fxreview_amazon] 配置：确保 Chromium...")
        ensure_binary()
    except Exception as exc:
        logger.exception("[fxreview_amazon] Chromium 准备失败")
        return {"message": f"❌ 亚马逊配置失败：Chromium 下载/准备失败：{exc}", "files": []}

    # ② 有头自动登录建立信任态
    message = asyncio.run(_config_login_async(email, password))
    return {"message": message, "files": []}


async def _config_login_async(email: str, password: str) -> str:
    from .login import run_login

    try:
        logger.info("[fxreview_amazon] 配置：有头登录建立信任态...")
        ok = await run_login(email, password, headless=False)
    except Exception as exc:
        logger.exception("[fxreview_amazon] 有头登录异常")
        return f"❌ 亚马逊配置失败：登录异常：{exc}"

    if ok:
        return "✅ 亚马逊配置完成，现在可发送【评论采集+ASIN】"
    return "❌ 亚马逊配置失败：有头登录未建立信任态（如有验证码请在弹出窗口清理后重试）"


# ────────────────────────── 运行段 ──────────────────────────

def _run_collect(asin: str) -> dict:
    """headless 采集 + VoC 分析。前置：已完成亚马逊配置。"""
    email, password = _require_credentials()

    missing_llm = [k for k in _LLM_KEYS if not os.environ.get(k)]
    if missing_llm:
        raise RuntimeError(f"缺少 DeepSeek 分析配置，请在 .env 配置：{', '.join(missing_llm)}")
    if importlib.util.find_spec("cloakbrowser") is None:
        raise RuntimeError("缺少依赖 cloakbrowser，请先安装后重试")

    # Chromium 未就绪即视为未配置——绝不在运行段静默下载 500MB
    if not _chromium_ready():
        return {"message": _MSG_NEED_CONFIG, "files": []}

    start = datetime.now()
    outcome = asyncio.run(_collect_async(email, password, asin))
    if outcome["status"] != "ok":
        return {"message": outcome["message"], "files": []}

    elapsed = int((datetime.now() - start).total_seconds())
    artifact_dir = outcome["artifact_dir"]
    reviews_csv = Path(artifact_dir) / "reviews.csv"
    n_reviews = _count_reviews(reviews_csv)
    files = [p for p in (Path(outcome["analysis_file"]), reviews_csv) if p.exists()]

    message = (
        f"✅ 亚马逊评论采集与 VoC 分析完成\n"
        f"ASIN: {asin}\n"
        f"采集评论: {n_reviews} 条\n"
        f"耗时: {elapsed}s"
    )
    return {"message": message, "files": files}


async def _collect_async(email: str, password: str, asin: str) -> dict:
    """登录态判定 → 采集 → 分析。只走 headless，绝不弹有头。"""
    from .session import verify_signed_in
    from .login import _login_once
    from .scraper import scrape
    from .analyzer import analyze_reviews, save_analysis

    if not await verify_signed_in(headless=True):
        # 无头静默重登（headless-only，绕开 run_login 的自动有头升级）
        ok, blocker = await _login_once(email, password, headless=True)
        if not ok:
            if blocker == "UNTRUSTED":
                # 从未受信 = 从未配置
                return {"status": "need_config", "message": _MSG_NEED_CONFIG}
            # 曾受信但重登失败（信任态失效 / 改密 / 挑战）
            return {"status": "relogin_failed", "message": _MSG_RELOGIN_FAILED}

    artifact_dir, _timings, summary = await scrape(asin=asin, max_pages=0, headless=True)
    analysis_text = await analyze_reviews(asin, artifact_dir, summary)
    analysis_file = save_analysis(analysis_text, artifact_dir, asin)
    return {"status": "ok", "artifact_dir": artifact_dir, "analysis_file": analysis_file}


# ────────────────────────── 入口分流 ──────────────────────────

def run(content: str = "") -> dict:
    """按消息内容分流：含「亚马逊配置」走配置段，否则抽 ASIN 走运行段。"""
    if _CONFIG_TRIGGER in content:
        return _run_config()

    asin = _extract_asin(content)
    if not asin:
        return {"message": _MSG_NO_ASIN, "files": []}
    return _run_collect(asin)
