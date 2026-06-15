# -*- coding: utf-8 -*-
"""
Shared browser session built on a persistent CloakBrowser profile.

Why a persistent profile:
    Amazon grants a device "trust" (device-bound cookies, localStorage, an
    encoded device token) the first time you sign in. The old code threw that
    trust away every run — it used an ephemeral context and only saved a few
    cookies to a JSON file. So every login looked like a brand-new device,
    and repeated new-device logins from one IP read as bot/credential-stuffing
    behaviour → OTP/CAPTCHA challenges → eventually a temporary IP ban.

    Keeping one on-disk profile (browser_profile/) means the trust survives
    across restarts — it is the single source of truth for the session, holding
    cookies natively in its own store. Steady state is a returning, trusted
    device that signs in silently in headless mode with no challenge.

Login model:
    - Profile already trusted  → headless, silent.
    - Brand-new / untrusted    → one-time HEADED bootstrap so a human can clear
                                 any OTP/CAPTCHA, after which trust is saved.
    - Challenge during headless → fail fast and escalate to a headed bootstrap.
                                 Never retry-loop (that is what caused the ban).
"""

import asyncio
import os
import random
from pathlib import Path

# Setup CloakBrowser binary path before importing cloakbrowser.
# Keep the patched-Chromium download/cache inside this workflow dir instead of
# the user home (~/.cloakbrowser/), so deleting chromium/ makes the next launch
# re-download into fxreview_amazon/chromium/ rather than scattering to ~.
_chromium_dir = Path(__file__).resolve().parent / "chromium"
os.environ.setdefault("CLOAKBROWSER_CACHE_DIR", str(_chromium_dir))
# If a local binary is already present, use it directly (skips any download).
_chromium = _chromium_dir / "chrome.exe"
if _chromium.exists():
    os.environ.setdefault("CLOAKBROWSER_BINARY_PATH", str(_chromium))

from cloakbrowser import launch_persistent_context_async


async def _delay(lo: float = 1.0, hi: float = 2.0):
    await asyncio.sleep(random.uniform(lo, hi))

PROFILE_DIR = Path(__file__).resolve().parent / "browser_profile"

# amazon.com is the US storefront; advertise a US locale/timezone so the
# session is internally consistent (the raw machine locale was zh-CN only).
_LOCALE = "en_US"
_TIMEZONE = "America/Los_Angeles"


async def open_context(headless: bool = True):
    """Open the shared persistent browser context. The caller must close it."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return await launch_persistent_context_async(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        locale=_LOCALE,
        timezone=_TIMEZONE,
        humanize=True,
        human_config={
            "idle_between_actions": True,
            "idle_between_duration": [0.3, 0.8],
        },
    )


async def is_signed_in(page) -> bool:
    """Read the nav account label. 'Hello, <name>' → signed in."""
    el = await page.query_selector("#nav-link-accountList-nav-line-1")
    if el is None:
        return False
    text = (await el.inner_text()).strip().lower()
    return bool(text) and "sign in" not in text


# Cookies Amazon only sets for an authenticated session. Their presence means
# this device has signed in before (trust persists past session expiry). The
# anonymous cookies every visitor gets (session-id, ubid-main, ak_bmsc, ...) are
# deliberately NOT here — otherwise merely visiting amazon.com would look
# "trusted" and a brand-new profile would skip the headed bootstrap.
_AUTH_COOKIES = {"x-main", "at-main", "sess-at-main", "sst-main"}


async def has_amazon_trust(context) -> bool:
    """Has this device ever held an authenticated Amazon session?

    Drives headless-vs-headed for a credential login: a known (previously
    signed-in) device can re-login silently headless; a brand-new device must
    do a one-time headed bootstrap. Judged by auth-only cookies, not by any
    Amazon cookie — anonymous tracking cookies appear from a plain visit.
    """
    return any(c.get("name") in _AUTH_COOKIES for c in await context.cookies())


async def _has_continue_shopping(page) -> bool:
    """Fast check for the intermittent 'Continue shopping' interstitial Amazon
    serves instead of the real page (no account nav, no review list)."""
    btn = page.locator(
        "button, input[type='submit'], input[type='button'], a"
    ).filter(has_text="Continue shopping").first
    try:
        return await btn.count() > 0
    except Exception:
        return False


async def _dismiss_continue_shopping(page) -> bool:
    """Click through the 'Continue shopping' gate. NOTE: it lands on the
    homepage, not the page we asked for — callers must re-navigate to their
    target afterwards. (Just clicking an interstitial as a human would; no
    impact on request rate / account safety.)"""
    if not await _has_continue_shopping(page):
        return False
    try:
        btn = page.locator(
            "button, input[type='submit'], input[type='button'], a"
        ).filter(has_text="Continue shopping").first
        await btn.click(timeout=4_000)
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await _delay(1, 2)
        return True
    except Exception:
        return False


async def verify_signed_in(headless: bool = True, tries: int = 3) -> bool:
    """Open the persistent profile and robustly check whether amazon.com shows a
    signed-in session.

    Why robust: the old check did a single goto + fixed sleep + one is_signed_in
    read. After an IP change Amazon often serves the 'Continue shopping'
    interstitial (no account nav) or renders the nav slowly, so that single read
    misfired → a VALID session was reported as logged-out → an unnecessary, and
    crash-prone, login was triggered. Here we clear the interstitial and retry,
    and only conclude "logged out" when the account nav actually rendered and
    still says "sign in".
    """
    context = await open_context(headless=headless)
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        for _ in range(tries):
            await page.goto("https://www.amazon.com",
                            wait_until="domcontentloaded", timeout=25_000)
            await _delay(1.5, 2.5)

            if await _has_continue_shopping(page):
                await _dismiss_continue_shopping(page)
                continue  # gate cleared (lands on homepage) — re-navigate & recheck

            # Give the account nav a chance to render before judging.
            try:
                await page.wait_for_selector("#nav-link-accountList-nav-line-1",
                                             timeout=6_000)
            except Exception:
                pass

            if await is_signed_in(page):
                return True
            # Nav rendered but shows "sign in" → genuinely logged out, no retry.
            if await page.query_selector("#nav-link-accountList-nav-line-1"):
                return False
            await _delay(1, 2)  # nav still not present — retry navigation
        return False
    finally:
        await context.close()


async def _detect_challenge(page) -> str | None:
    """Return a short label if Amazon is showing a challenge, else None."""
    url = page.url.lower()
    if "/ax/claim" in url:
        return "OTP"
    if "captcha" in url:
        return "CAPTCHA"
    if await page.query_selector("#captchacharacters") is not None:
        return "CAPTCHA"
    title = (await page.title()).lower()
    if "robot" in title:
        return "CAPTCHA"
    return None
