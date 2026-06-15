# -*- coding: utf-8 -*-
"""
Amazon 评论采集与 VoC 分析主控脚本
执行流程：验证会话 → 采集 → 分析，任一步骤失败即终止
运行方式：python fenxireview/fxreview_controller.py
"""

import sys
import os
import asyncio
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from .session import verify_signed_in
from .login import run_login
from .scraper import scrape
from .analyzer import analyze_reviews, save_analysis


def _fmt_duration(seconds: float) -> str:
    """45.3s → '45.3s';  195s → '3m 15s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s}s"


async def _verify_session_online() -> bool:
    """用持久 profile 健壮验证会话是否仍有效（清过渡页 + 重试，避免误报未登录）"""
    return await verify_signed_in(headless=True)


async def _ensure_valid_session(email: str, password: str) -> None:
    """验证持久 profile 的会话，有效则跳过登录，否则静默重登（必要时自动转有头）"""
    print("[*] Verifying session with Amazon...", end=" ", flush=True)
    valid = await _verify_session_online()
    if valid:
        print("[OK]")
        return

    print("[NO VALID SESSION] - logging in...")
    ok = await run_login(email, password)
    if not ok:
        raise SystemExit("Login failed — try: python -m fenxireview.login --headed")


async def main_async(email: str, password: str, asin: str):
    """Main async workflow"""

    # ── Step 1: 验证会话 ──────────────────────────────────────────────
    print("=" * 60)
    print("Step 1 / 3  验证 Amazon 会话")
    print("=" * 60)

    await _ensure_valid_session(email, password)

    # ── Step 2: 采集评论 ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Step 2 / 3  采集产品评论")
    print("=" * 60)

    stage_t0 = time.perf_counter()
    try:
        artifact_dir, timings, summary = await scrape(asin=asin, max_pages=0, headless=True)
    except Exception as e:
        print(f"\n[ERR] 采集失败: {e}")
        sys.exit(1)
    stage_elapsed = time.perf_counter() - stage_t0

    print(
        f"\n[耗时] 评论采集 {_fmt_duration(timings['collect'])}"
        f" | 阶段总计 {_fmt_duration(stage_elapsed)}"
    )

    # ── Step 3: 分析评论 ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Step 3 / 3  VoC 分析")
    print("=" * 60)

    stage_t0 = time.perf_counter()
    try:
        print(f"正在分析 {asin} 的评论数据（好评 + 差评）...", flush=True)
        analysis_text = await analyze_reviews(asin, artifact_dir, summary)
        analysis_file = save_analysis(analysis_text, artifact_dir, asin)
        print(f"[DONE] VoC 分析完成 → {analysis_file}")
    except Exception as e:
        print(f"\n[ERR] 分析失败: {e}")
        sys.exit(1)
    stage_elapsed = time.perf_counter() - stage_t0

    print(f"\n[耗时] VoC 分析阶段 {_fmt_duration(stage_elapsed)}")

    print("\n[DONE] 全部完成")


def main():
    asin = os.environ.get("AMAZON_ASIN", "B0CWKH83WY")
    email = os.environ.get("AMAZON_EMAIL") or input("Amazon email: ").strip()
    password = os.environ.get("AMAZON_PASSWORD") or input("Amazon password: ").strip()

    asyncio.run(main_async(
        email=email,
        password=password,
        asin=asin,
    ))


if __name__ == "__main__":
    main()
