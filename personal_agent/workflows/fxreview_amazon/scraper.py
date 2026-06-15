import asyncio
import csv
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path

from .session import (
    open_context,
    is_signed_in,
    _has_continue_shopping,
    _dismiss_continue_shopping,
)

# Per-run logger. scrape() attaches a FileHandler writing scrape.log into the
# run's artifact dir, so any future "why is it empty / slow / blocked" question
# is answered from the log, not guesswork.
log = logging.getLogger("fxreview.scraper")
log.setLevel(logging.DEBUG)
log.propagate = False  # log to the run's file only, not the console


def _attach_log_file(log_path: Path) -> logging.Handler:
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
                                            datefmt="%H:%M:%S"))
    handler.setLevel(logging.DEBUG)
    log.addHandler(handler)
    return handler

REVIEW_FIELDS = [
    "page", "author", "rating", "title",
    "date", "location", "variant", "body",
]


async def _delay(lo: float = 1.0, hi: float = 2.5):
    """Random delay between requests to appear human-like without being excessive."""
    await asyncio.sleep(random.uniform(lo, hi))


async def _text(el, default: str = "") -> str:
    if el is None:
        return default
    return (await el.inner_text()).strip()


async def scrape_summary(page, asin: str) -> dict:
    await page.goto(
        f"https://www.amazon.com/product-reviews/{asin}/",
        wait_until="domcontentloaded",
    )
    await _delay(2, 4)

    r: dict = {}

    el = await page.query_selector("[data-hook='rating-out-of-text']")
    r["overall_rating"] = await _text(el)

    el = await page.query_selector("[data-hook='total-review-count']")
    r["total_ratings"] = await _text(el)

    for row in await page.query_selector_all(".histogram-row-container"):
        text = await _text(row)           # e.g. "5 star\n80%"
        parts = text.split("\n")
        if len(parts) == 2 and "star" in parts[0] and "%" in parts[1]:
            label = parts[0].strip()      # "5 star"
            r[label] = parts[1].strip()   # "80%"

    return r


def _extract_total_reviews(total_ratings_text: str) -> int:
    """Extract review count from text like '12,345 reviews' or '12345'."""
    if not total_ratings_text:
        return 0
    import re
    match = re.search(r'[\d,]+', total_ratings_text)
    if match:
        return int(match.group().replace(',', ''))
    return 0


def _estimate_total_pages(total_reviews: int, reviews_per_page: int = 8) -> int:
    """Estimate total pages based on review count (Amazon shows ~8 per page)."""
    if total_reviews <= 0:
        return 0
    return (total_reviews + reviews_per_page - 1) // reviews_per_page


# Runs inside the browser: extracts review containers in a single CDP
# round-trip, instead of ~15 query_selector/inner_text calls per review from
# Python. Pure DOM reads — invisible to Amazon.
#
# Two kinds of review block, verified live on the review pages:
#   - Main reviews:    <li data-hook="review" id="R...">  (rating: review-star-rating)
#   - "From other countries" reviews:
#       <div id="customer_review_foreign-R..."> with NO data-hook="review",
#       and the star rating uses data-hook="cmps-review-star-rating".
#     Title / date / body / author / variant use the SAME hooks as main reviews.
#
# Main reviews paginate via "Show more" (append at the end) so we extract them
# incrementally from startIndex and report their count. Foreign reviews sit in a
# static section below the main list — their index shifts as main reviews load,
# so they're NOT safe to index incrementally; we extract ALL of them every call
# and let the Python `seen` set dedupe. The id is normalised (the
# "customer_review_foreign-" prefix stripped) so the same review can't slip in
# twice under two different keys.
_EXTRACT_JS = """
(startIndex) => {
    const extractOne = (el) => {
        const q = (sel) => {
            const n = el.querySelector(sel);
            return n ? n.innerText.trim() : "";
        };
        let title = "";
        const titleEl = el.querySelector("[data-hook='review-title']");
        if (titleEl) {
            const spans = Array.from(titleEl.querySelectorAll("span")).reverse();
            for (const s of spans) {
                const t = s.innerText.trim();
                if (t && !t.includes("out of 5 stars")) { title = t; break; }
            }
        }
        return {
            id: (el.id || "").replace(/^customer_review_foreign-/, ""),
            author: q("span.a-profile-name"),
            ratingRaw: q("[data-hook='review-star-rating'] .a-icon-alt")
                    || q("[data-hook='cmps-review-star-rating'] .a-icon-alt"),
            title: title,
            dateRaw: q("[data-hook='review-date']"),
            variant: q("[data-hook='format-strip-linkless']")
                  || q("[data-hook='format-strip']"),
            body: q("[data-hook='review-body'] span[data-hook='review-collapsed']")
               || q("[data-hook='review-body'] span"),
        };
    };

    const main = document.querySelectorAll("[data-hook='review']");
    const results = [];
    for (let i = startIndex; i < main.length; i++) {
        results.push(extractOne(main[i]));
    }
    // 'From other countries' block — extract all each call; Python dedupes.
    const foreign = document.querySelectorAll("[id^='customer_review_foreign']");
    for (const el of foreign) results.push(extractOne(el));

    // count = MAIN only, so the incremental startIndex for main reviews stays correct.
    return { reviews: results, containerCount: main.length };
}
"""


def _parse_review(raw: dict) -> dict:
    r = {
        "author": raw["author"],
        "rating": raw["ratingRaw"].split(" ")[0] if raw["ratingRaw"] else "",
        "title": raw["title"],
        "date": "",
        "location": "",
        "variant": raw["variant"],
        # Remove line breaks and collapse whitespace
        "body": " ".join(raw["body"].split()),
    }
    if raw["dateRaw"]:
        m = re.search(r"Reviewed in (.+?) on (.+)$", raw["dateRaw"])
        if m:
            r["location"] = m.group(1)
            r["date"] = m.group(2)
        else:
            r["date"] = raw["dateRaw"]
    return r


async def extract_new_reviews(page, start_index: int) -> tuple[list[dict], int]:
    """Extract review containers added since start_index.

    Returns (raw reviews, total container count) — the count is the
    start_index for the next call, so each load is processed only once.
    """
    data = await page.evaluate(_EXTRACT_JS, start_index)
    return data["reviews"], data["containerCount"]


async def _filter_match_count(page) -> int:
    """Parse 'NNN matching customer reviews' on a filtered review page. 0 if unknown."""
    el = await page.query_selector("[data-hook='cr-filter-info-review-rating-count']")
    if el is None:
        return 0
    m = re.search(r"([\d,]+)\s+matching", await _text(el))
    return int(m.group(1).replace(",", "")) if m else 0


async def _collect_reviews(
    page,
    writer,
    f,
    seen: set,
    *,
    max_reviews: int,
    target_reviews: int = 0,
    max_pages: int = 0,
) -> int:
    """Extract → dedup → write → click 'Show more' loop on the current page.

    Works on both the unfiltered review list and filterByStar pages (same DOM).
    `seen` is shared across passes so reviews already collected are skipped.
    Returns the number of rows written.
    """
    total_scraped = 0
    load_count = 0
    processed = 0  # review containers already extracted (page is cumulative)

    while True:
        # Extract only the reviews added by the latest load
        extract_start = time.time()
        raw_reviews, processed = await extract_new_reviews(page, processed)
        extract_time = time.time() - extract_start

        # Deduplicate by Amazon review ID (the <li id="R..."> attribute)
        unique_reviews = []
        skipped = 0
        for raw in raw_reviews:
            # Skip if no author (review might be incomplete/hidden)
            if not raw["author"]:
                skipped += 1
                continue
            key = raw["id"] or (raw["author"], raw["ratingRaw"], raw["dateRaw"])
            if key in seen:
                continue
            seen.add(key)
            review = _parse_review(raw)
            review["page"] = load_count + 1
            unique_reviews.append(review)

        for review in unique_reviews:
            writer.writerow(review)
        f.flush()

        total_scraped += len(unique_reviews)
        duplicates = len(raw_reviews) - skipped - len(unique_reviews)
        dup_str = f" (dup: {duplicates})" if duplicates > 0 else ""
        skip_str = f" (skipped: {skipped})" if skipped > 0 else ""
        print(f"Load {load_count+1:>2} | {total_scraped:>5} collected | +{len(unique_reviews)} [extract: {extract_time:.2f}s]{dup_str}{skip_str}")
        log.debug("load=%d containers=%d new=%d dup=%d skipped=%d total=%d extract=%.2fs",
                  load_count + 1, len(raw_reviews), len(unique_reviews),
                  duplicates, skipped, total_scraped, extract_time)

        # Stop if reached max reviews limit
        if total_scraped >= max_reviews:
            print(f"\nReached max reviews limit: {max_reviews} reviews collected.")
            break

        # Stop if collected all available reviews
        if target_reviews and total_scraped >= target_reviews:
            print(f"Reached target: {target_reviews} reviews.")
            break

        if max_pages and load_count >= max_pages:
            print(f"Reached --max-pages {max_pages}.")
            break

        # Check if "Show 10 more reviews" button exists
        # Button is actually an <a> tag with data-hook="show-more-button", not a <button>
        show_more_btn = await page.query_selector('[data-hook="show-more-button"]')

        if not show_more_btn:
            print(f"\nNo more 'Show more' button. All {total_scraped} reviews collected.")
            break

        # Pacing between clicks: this delay IS the request rate Amazon sees —
        # keep it randomized and human-scale. (Everything below is local DOM
        # interaction that sends nothing to Amazon, so its speed doesn't change
        # the observed request rate.)
        await _delay(1.0, 3.0)

        # Click "Show more reviews" button
        print(f"Clicking 'Show more'...", end="  ", flush=True)
        try:
            # Fast JS scroll to bring the button into view. The humanize
            # smooth-scroll cost ~7-8s per load (measured) as the page grew —
            # pure local dead time, no request — so we scroll instantly here and
            # keep the humanized click below for behavioral realism on the
            # actual interaction.
            await show_more_btn.evaluate("el => el.scrollIntoView({block: 'center'})")
            try:
                await show_more_btn.click()
            except Exception:
                await show_more_btn.click(force=True)

            # The AJAX response lands in ~0.5-0.7s; wait until new
            # review containers appear (10s safety timeout)
            await page.wait_for_function(
                "n => document.querySelectorAll('[data-hook=\"review\"]').length > n",
                arg=processed,
                timeout=10_000,
            )
        except Exception as exc:
            print(f"[error clicking button] {exc}")
            break

        load_count += 1

    return total_scraped


async def _goto_and_settle(page, url: str, content_selector: str | None,
                           tries: int = 3) -> bool:
    """Navigate to `url` (direct URL navigation, not clicking on-page filters)
    and ensure the real page rendered. Returns True if content appeared.

    Handles two intermittent failures without changing the pagination request
    rate Amazon sees: (1) the 'Continue shopping' gate — clicking it bounces to
    the homepage, so we clear it and then re-navigate to `url`; (2) a slow load
    where content lags a fixed delay (five_star once came up empty → 0). Both
    are recovered by re-navigating, up to `tries` times. Every attempt is logged
    with where we actually landed, so an empty result is diagnosable.
    """
    for attempt in range(1, tries + 1):
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        await _delay(1, 2)

        landed = page.url
        title = await page.title()
        interstitial = await _has_continue_shopping(page)
        n_reviews = len(await page.query_selector_all("[data-hook='review']"))
        log.info("nav[%d/%d] (via URL) target=%s -> landed=%s title=%r interstitial=%s reviews_on_page=%d",
                 attempt, tries, url, landed, title, interstitial, n_reviews)

        if interstitial:
            dismissed = await _dismiss_continue_shopping(page)
            log.info("  'Continue shopping' interstitial -> dismissed=%s (lands on homepage); re-navigating to target",
                     dismissed)
            continue

        if content_selector is None or await page.query_selector(content_selector):
            log.info("  content ready (selector=%s)", content_selector)
            return True
        log.warning("  content selector %r NOT found after settle; will re-navigate", content_selector)
        await _delay(1, 2)

    log.error("nav gave up after %d tries for %s; final landed=%s title=%r",
              tries, url, page.url, await page.title())
    return False


async def _save_error_shot(page, out_dir: Path, label: str) -> Path | None:
    """Save a full-page screenshot of the current (failed) page next to
    scrape.log, so a load failure (e.g. 'Page Not Found') is visually
    diagnosable later. Named error_<label>_<HHMMSS>.png so multiple failures
    in one run don't overwrite each other."""
    ts = datetime.now().strftime("%H%M%S")
    path = out_dir / f"error_{label}_{ts}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        log.warning("saved error screenshot -> %s (url=%s)", path.name, page.url)
        return path
    except Exception as exc:
        log.error("failed to save error screenshot for %s: %s", label, exc)
        return None


async def scrape(
    asin: str,
    max_pages: int = 0,
    headless: bool = True,
) -> Path:
    max_reviews = 600  # 固定最多采集 600 条评论（五个星级累计）
    # Create artifact directory with timestamp
    now = datetime.now()
    date_str = now.strftime("%Y.%m.%d")
    time_str = now.strftime("%H%M%S")
    artifact_dir = Path("artifact") / date_str / f"{asin}_{time_str}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    output_file = artifact_dir / "reviews.csv"
    log_handler = _attach_log_file(artifact_dir / "scrape.log")
    log.info("=== scrape start: asin=%s max_pages=%s headless=%s cap=%d ===",
             asin, max_pages, headless, max_reviews)

    # Reuse the shared persistent profile — same trusted device as login,
    # session already on disk, no cookie injection needed.
    context = await open_context(headless=headless)
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        # The profile must hold a live session — review pages redirect to the
        # sign-in page when logged out, which would otherwise be scraped as junk.
        # Clear the 'Continue shopping' interstitial first; it hides the account
        # nav and would falsely read as "not signed in".
        await _goto_and_settle(page, "https://www.amazon.com",
                               "#nav-link-accountList-nav-line-1")
        signed_in = await is_signed_in(page)
        log.info("session check: signed_in=%s (url=%s)", signed_in, page.url)
        if not signed_in:
            raise RuntimeError(
                "Profile not signed in — run: python -m fenxireview.login"
            )

        print("Scraping summary...")
        summary = await scrape_summary(page, asin)
        total_reviews = _extract_total_reviews(summary.get('total_ratings', ''))
        print(
            f"Summary: {summary.get('overall_rating')} | {summary.get('total_ratings')}\n"
            f"  5★ {summary.get('5 star')}  4★ {summary.get('4 star')}  "
            f"3★ {summary.get('3 star')}  2★ {summary.get('2 star')}  "
            f"1★ {summary.get('1 star')}\n"
            f"Total rated: {total_reviews} (cap {max_reviews} collected)\n"
        )

        await page.goto(
            f"https://www.amazon.com/product-reviews/{asin}/",
            wait_until="domcontentloaded",
        )
        await _delay(1, 2)

        # CAPTCHA check at start
        is_captcha = (
            await page.query_selector("#captchacharacters") is not None
            or "captcha" in page.url.lower()
            or "robot" in (await page.title()).lower()
        )
        if is_captcha:
            # A challenge here means the trusted profile lost standing. Don't
            # hammer it headless — stop and let the user re-bootstrap trust
            # with a one-time headed login.
            raise RuntimeError(
                "CAPTCHA during scrape — profile trust lost. "
                "Re-run: python -m fenxireview.login --headed"
            )

        # ── Collect across all five star filters into one reviews.csv ──
        # The unfiltered "all reviews" view exposes only a limited slice (mostly
        # 4-5★). Walking 1★…5★ filters instead surfaces both ends — good and bad.
        # Each filter view also stops after a bounded number of "Show more"
        # loads: on the one product we monitored, the five_star AJAX returned no
        # nextPageToken and dropped the button at review #100, and a real human
        # hit the same wall — so a big pool can't be fully retrieved. (Whether
        # that limit is exactly ~100 may vary by product; the loop doesn't rely
        # on the number — it just collects until the button is gone.)
        # A recency-sorted second window was tried for big pools but mostly
        # re-exposed the same reviews (heavy overlap, ~13 net new for a 742 pool)
        # at full click cost, so it was removed.
        # A shared `seen` set dedupes across stars; max_reviews is a cumulative
        # budget.
        seen: set = set()
        total_scraped = 0

        collect_t0 = time.perf_counter()
        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
            writer.writeheader()

            for star in ("one_star", "two_star", "three_star", "four_star", "five_star"):
                if total_scraped >= max_reviews:
                    break

                base_url = (
                    f"https://www.amazon.com/product-reviews/{asin}/"
                    f"?filterByStar={star}"
                )
                log.info("--- star=%s : navigating via URL %s ---", star, base_url)
                # Robust load: clears the interstitial and waits for the review
                # list to actually render (a fixed delay alone raced slow loads —
                # five_star once came up empty → 0 collected).
                ok = await _goto_and_settle(page, base_url, "[data-hook='review']")
                if not ok:
                    # Page/reviews failed to load — capture the failed page next
                    # to the log for diagnosis.
                    await _save_error_shot(page, artifact_dir, star)

                matching = await _filter_match_count(page)
                star_t0 = time.perf_counter()
                print(f"\n[{star}] {matching or '?'} matching reviews")
                log.info("star=%s ready=%s match_count=%s", star, ok, matching)
                got = await _collect_reviews(
                    page, writer, f, seen,
                    max_reviews=max_reviews - total_scraped,
                    max_pages=max_pages,
                )
                total_scraped += got
                log.info("star=%s collected=%d total=%d elapsed=%.1fs",
                         star, got, total_scraped, time.perf_counter() - star_t0)

        collect_elapsed = time.perf_counter() - collect_t0
        log.info("=== scrape done: %d reviews in %.1fs -> %s ===",
                 total_scraped, collect_elapsed, output_file)
        print(f"\nDone. {total_scraped} reviews saved → {output_file}")

    except Exception:
        log.exception("scrape failed with exception")
        raise
    finally:
        await context.close()
        log.removeHandler(log_handler)
        log_handler.close()

    return artifact_dir, {"collect": collect_elapsed}, summary
