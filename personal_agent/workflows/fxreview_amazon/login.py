"""
Amazon login on a persistent CloakBrowser profile.

Steady state is silent and headless: a trusted profile that signs in (or is
already signed in) with no popup. Credentials are only entered when the profile
has no live session; a challenge (OTP/CAPTCHA) escalates to a one-time headed
bootstrap rather than looping — see session.py for the rationale.

Usage:
    python -m fenxireview.login            # auto: headless if trusted, else headed
    python -m fenxireview.login --headed   # force a headed bootstrap
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from .session import (
    open_context,
    is_signed_in,
    has_amazon_trust,
    _detect_challenge,
)


async def _fill_credentials(page, email: str, password: str) -> None:
    """Walk Amazon's two-step sign-in form (email → continue → password → submit)."""
    # gp/sign-in.html is the proven entry that reliably renders the email form
    # (it redirects through the openid flow that exposes #ap_email).
    await page.goto("https://www.amazon.com/gp/sign-in.html", wait_until="load")
    await asyncio.sleep(2)

    # Interstitial "Continue shopping" page (shown intermittently)
    try:
        btn = page.locator(
            "button, input[type='button'], input[type='submit']"
        ).filter(has_text="Continue shopping").first
        await btn.wait_for(timeout=3_000)
        await btn.click()
        await page.wait_for_load_state("load", timeout=10_000)
        await asyncio.sleep(1)
    except Exception:
        pass

    # Step 1: email
    print("[1/4] Email...", end=" ", flush=True)
    email_sel = "#ap_email, #ap_email_login, input[name='email']"
    await page.wait_for_selector(email_sel, timeout=20_000)
    await asyncio.sleep(0.4)
    await page.locator(email_sel).first.click()
    await page.locator(email_sel).first.type(email, delay=85)
    await asyncio.sleep(0.6)
    print("OK")

    print("[2/4] Continue...", end=" ", flush=True)
    continue_sel = "#continue, input[id='continue'], button[id='continue'], input[type='submit']"
    await page.wait_for_selector(continue_sel, timeout=10_000)
    await page.locator(continue_sel).first.click()
    print("OK")

    # Step 2: password
    print("[3/4] Password...", end=" ", flush=True)
    await page.wait_for_selector("#ap_password", timeout=15_000)
    await asyncio.sleep(0.4)
    await page.locator("#ap_password").click()
    await page.locator("#ap_password").type(password, delay=90)
    await asyncio.sleep(0.5)
    print("OK")

    print("[4/4] Sign in...", end=" ", flush=True)
    await page.wait_for_selector("#signInSubmit", timeout=10_000)
    await page.locator("#signInSubmit").click()
    await page.wait_for_load_state("load", timeout=20_000)
    await asyncio.sleep(1.5)
    print("OK")


async def _has_x_main(context) -> bool:
    """x-main is only set once Amazon has fully authenticated the session — it
    is the definitive 'logged in' signal (clicking the sign-in button is not)."""
    return any(c.get("name") == "x-main" for c in await context.cookies())


async def _await_x_main(context, timeout: float = 180.0) -> bool:
    """Poll for the x-main cookie until it appears or `timeout` elapses.

    Polling (not a one-shot check) is what makes a hands-off headed login work:
    with no challenge, x-main appears within a few seconds → success; if Amazon
    does throw an OTP/CAPTCHA, you clear it by hand in the visible window and the
    poll still catches x-main the moment you're through — no manual Enter, and no
    OTP-code handling in the program.
    """
    deadline = time.monotonic() + timeout
    while True:
        if await _has_x_main(context):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(2)


async def _login_once(email: str, password: str, headless: bool) -> tuple[bool, str | None]:
    """One login attempt in the persistent profile.

    Returns (signed_in, challenge_label). challenge_label is set when Amazon
    blocked us with OTP/CAPTCHA — the caller decides whether to escalate.
    In headed mode a human can clear the challenge inline.
    """
    context = await open_context(headless=headless)
    try:
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://www.amazon.com", wait_until="domcontentloaded", timeout=25_000)
        await asyncio.sleep(1.5)

        # Already trusted? Done — fully silent path. Session lives in the profile.
        if await is_signed_in(page):
            print("Already signed in (trusted profile).")
            return True, None

        # Need credentials. A brand-new (untrusted) profile entering credentials
        # headless is the classic "new device" pattern that triggers a ban —
        # so don't submit them headless; signal for a one-time headed bootstrap.
        if headless and not await has_amazon_trust(context):
            return False, "UNTRUSTED"

        await _fill_credentials(page, email, password)

        if not headless:
            # Headed login: success = the x-main cookie appears. Poll for it so
            # that, if Amazon happens to ask for OTP/CAPTCHA, you can clear it by
            # hand in the visible window and we still detect success — no manual
            # Enter, no OTP-code logic. The persistent profile saves the cookies
            # automatically; context.close() (finally) closes the window.
            print("Signing in — if Amazon asks for a code, solve it in the window; "
                  "I'll continue automatically once you're in...")
            if await _await_x_main(context, timeout=180):
                print("Login successful! Session saved to the profile.")
                return True, None
            shot = (Path(__file__).resolve().parents[3] / "artifact"
                    / f"debug_login_{int(time.time())}.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=True)
            print(f"Login failed (no x-main within timeout). URL={page.url}  screenshot={shot}")
            return False, None

        # Headless re-login (trusted profile): no human to clear a challenge, so
        # bail on one and let run_login escalate to a one-time headed login.
        challenge = await _detect_challenge(page)
        if challenge:
            return False, challenge
        if await _await_x_main(context, timeout=20):
            print("Login successful! Session saved to the profile.")
            return True, None
        return False, None
    finally:
        await context.close()


async def run_login(email: str, password: str, headless: bool | None = None) -> bool:
    """Ensure the persistent profile holds a live Amazon session.

    headless=None (default): try silent/headless first. If the profile is
    untrusted (brand-new device) or Amazon throws a challenge, escalate once
    to a headed bootstrap so a human can establish trust / clear OTP-CAPTCHA.
    Whether to go headed is driven by the profile's actual trust state, not by
    directory existence — the verify step creates the dir before we get here.
    """
    if headless is None:
        headless = True

    signed_in, blocker = await _login_once(email, password, headless=headless)
    if signed_in:
        return True

    # blocker is "UNTRUSTED" (new device) or an OTP/CAPTCHA label — both mean
    # "this can't be done silently", so do one visible headed login.
    if blocker and headless:
        reason = ("new device — establishing trust"
                  if blocker == "UNTRUSTED"
                  else f"{blocker} challenge — Amazon wants to verify this device")
        print(f"\n[{blocker}] {reason}. Switching to a one-time headed login...")
        signed_in, _ = await _login_once(email, password, headless=False)
        return signed_in

    return signed_in


def main():
    parser = argparse.ArgumentParser(description="Amazon login (persistent profile)")
    parser.add_argument("--headed", action="store_true",
                        help="Force a visible headed login (first-time bootstrap)")
    args = parser.parse_args()

    email = os.environ.get("AMAZON_EMAIL") or input("Amazon email: ").strip()
    password = os.environ.get("AMAZON_PASSWORD") or input("Amazon password: ").strip()

    headless = False if args.headed else None
    ok = asyncio.run(run_login(email, password, headless=headless))
    if not ok:
        raise SystemExit("Login failed — try: python -m fenxireview.login --headed")


if __name__ == "__main__":
    main()
