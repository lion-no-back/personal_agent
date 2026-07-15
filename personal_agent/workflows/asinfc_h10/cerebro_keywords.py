from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import requests

try:
    from .login import (
        BASE_URL,
        DEFAULT_AUTH_STATE_PATH,
        DEFAULT_COOKIES_PATH,
        DEFAULT_ENV_PATH,
        PROJECT_ROOT,
        json_headers,
        load_auth_state,
        login,
        login_session,
        write_auth_state_atomic,
    )
except ImportError:
    from login import (
        BASE_URL,
        DEFAULT_AUTH_STATE_PATH,
        DEFAULT_COOKIES_PATH,
        DEFAULT_ENV_PATH,
        PROJECT_ROOT,
        json_headers,
        load_auth_state,
        login,
        login_session,
        write_auth_state_atomic,
    )


H10_API_BASE_URL = "https://h10api.pacvue.com"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "artifact" / f"keywordfc_{date.today():%Y%m%d}"


class AuthenticationExpiredError(RuntimeError):
    """当前 cookies 无法换取 Helium 10 API token。"""


def parse_auth_json(value: Any) -> dict:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def save_site_tokens(
    *,
    auth_state_path: Path,
    account_id: str,
    token: str,
    pacvue_token: str,
) -> None:
    auth_state = load_auth_state(auth_state_path)
    bearer_tokens = parse_auth_json(auth_state.get("CORE_BEARER_TOKEN"))
    pacvue_tokens = parse_auth_json(auth_state.get("PACVUE_TOKENS"))

    account_key = str(account_id)
    bearer_tokens[account_key] = token
    pacvue_entry = pacvue_tokens.get(account_key)
    if not isinstance(pacvue_entry, dict):
        pacvue_entry = {}
    pacvue_entry["pacvueToken"] = pacvue_token
    pacvue_tokens[account_key] = pacvue_entry

    auth_state["CURRENT_ACCOUNT_ID"] = account_key
    auth_state["CORE_BEARER_TOKEN"] = json.dumps(bearer_tokens, ensure_ascii=False)
    auth_state["PACVUE_TOKENS"] = json.dumps(pacvue_tokens, ensure_ascii=False)
    auth_state["capturedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_auth_state_atomic(auth_state, auth_state_path)


@dataclass(frozen=True)
class Marketplace:
    domain_suffix: str
    marketplace: str
    marketplace_id: int | None = None


MARKETPLACES = {
    "com": Marketplace("com", "ATVPDKIKX0DER", 1),
    "ca": Marketplace("ca", "A2EUQ1WTGCTBG2"),
    "com.mx": Marketplace("com.mx", "A1AM78C64UM0Y8"),
    "com.br": Marketplace("com.br", "A2Q3Y263D00KWC"),
    "de": Marketplace("de", "A1PA6795UKMFR9"),
    "es": Marketplace("es", "A1RKKUPIHCS9HS"),
    "it": Marketplace("it", "APJ6JRA9NG5V4"),
    "fr": Marketplace("fr", "A13V1IB3VIYZZH"),
}


SITE_ALIASES = {
    "美国": "com",
    "加拿大": "ca",
    "墨西哥": "com.mx",
    "巴西": "com.br",
    "德国": "de",
    "西班牙": "es",
    "意大利": "it",
    "法国": "fr",
}


PREFERRED_CSV_FIELDS = [
    "phrase",
    "impressionExact30",
    "iq",
    "clickShareRate",
    "conversionShareRate",
    "cpc",
    "lowCpc",
    "highCpc",
    "organicPosition",
    "sponsoredPosition",
    "searchVolumeTrend",
    "resultsNumber",
    "matchType",
    "sponsoredAsins",
    "amzSuggestedPosition",
    "newCprExact",
    "newCprBroad",
    "monthlySales",
    "searchFrequencyRank",
]

MANUAL_EXPORT_FIELDS = [
    "Keyword Phrase",
    "ABA Total Click Share",
    "ABA Total Conv. Share",
    "Cerebro IQ Score",
    "Search Volume",
    "Search Volume Trend",
    "H10 PPC Sugg. Bid",
    "H10 PPC Sugg. Min Bid",
    "H10 PPC Sugg. Max Bid",
    "Sponsored ASINs",
    "Competing Products",
    "CPR",
    "Title Density",
    "Amazon Recommended",
    "Sponsored",
    "Organic",
    "Amazon Rec. Rank",
    "Sponsored Rank",
    "Organic Rank",
]

MANUAL_EXPORT_FIELD_LABELS = {
    "Keyword Phrase": "关键词词组",
    "ABA Total Click Share": "ABA总计 点击份额",
    "ABA Total Conv. Share": "ABA总计 转化份额",
    "Cerebro IQ Score": "Cerebro IQ得分",
    "Search Volume": "搜索量",
    "Search Volume Trend": "搜索量趋势",
    "H10 PPC Sugg. Bid": "H10 PPC建议竞价",
    "H10 PPC Sugg. Min Bid": "H10 PPC建议最低竞价",
    "H10 PPC Sugg. Max Bid": "H10 PPC建议最高竞价",
    "Sponsored ASINs": "广告推广ASIN",
    "Competing Products": "竞品数",
    "CPR": "CPR",
    "Title Density": "标题密度",
    "Amazon Recommended": "亚马逊推荐",
    "Sponsored": "广告推广",
    "Organic": "自然",
    "Amazon Rec. Rank": "亚马逊推荐广告排名",
    "Sponsored Rank": "广告排名",
    "Organic Rank": "自然排名",
}

MANUAL_EXPORT_MARKET_PREFIXES = {
    "com": "US_AMAZON",
    "ca": "CA_AMAZON",
    "com.mx": "MX_AMAZON",
    "com.br": "BR_AMAZON",
    "de": "DE_AMAZON",
    "es": "ES_AMAZON",
    "it": "IT_AMAZON",
    "fr": "FR_AMAZON",
}


def normalize_site_suffix(site: str) -> str:
    normalized = site.strip().lower()
    if normalized.endswith("站"):
        normalized = normalized[:-1].strip()
    normalized = SITE_ALIASES.get(normalized, normalized)
    for prefix in ("https://www.amazon.", "http://www.amazon.", "www.amazon.", "amazon."):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.removeprefix(".").rstrip("/")


def resolve_marketplace(site: str) -> Marketplace:
    suffix = normalize_site_suffix(site)
    try:
        return MARKETPLACES[suffix]
    except KeyError as exc:
        choices = ", ".join(sorted(MARKETPLACES))
        raise ValueError(f"Unsupported Amazon site suffix: {site}. Supported values: {choices}") from exc


def output_csv_path(asin: str, site: str, output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    suffix = normalize_site_suffix(site)
    safe_asin = asin.strip().upper()
    market_prefix = MANUAL_EXPORT_MARKET_PREFIXES.get(suffix, f"{suffix.upper().replace('.', '_')}_AMAZON")
    return output_dir / f"{market_prefix}_cerebro_{safe_asin}_{date.today().isoformat()}.csv"


def api_headers(token: str, pacvue_token: str, referer: str) -> dict[str, str]:
    return {
        **json_headers(referer),
        "Origin": BASE_URL,
        "Authorization": f"Bearer {token}",
        "x-pacvue-token": f"Bearer {pacvue_token}",
    }


def response_excerpt(response: requests.Response, limit: int = 300) -> str:
    text = response.text.strip()
    if not text:
        return "<empty response body>"
    return text[:limit]


def response_content_type(response: requests.Response) -> str:
    return response.headers.get("content-type", "<missing content-type>")


def looks_like_html_response(response: requests.Response) -> bool:
    content_type = response_content_type(response).lower()
    body = response.text.lstrip().lower()
    return "text/html" in content_type or body.startswith("<!doctype html") or body.startswith("<html")


def parse_json_response(response: requests.Response, context: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as error:
        raise RuntimeError(
            f"{context} returned non-JSON response. "
            f"Status: {response.status_code}. Content-Type: {response_content_type(response)}. "
            f"Body: {response_excerpt(response)}"
        ) from error
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{context} returned JSON but the top-level value is not an object. "
            f"Status: {response.status_code}. Body: {response_excerpt(response)}"
        )
    return data


def get_site_tokens(
    session: requests.Session,
    *,
    account_id: str,
    referer: str,
    timeout: int,
    auth_state_path: Path | None = None,
) -> tuple[str, str]:
    response = session.get(
        f"{BASE_URL}/api/v1/site/token",
        params={"accountId": account_id},
        headers=json_headers(referer),
        timeout=timeout,
    )
    if response.status_code != 200:
        if response.status_code in {401, 403}:
            raise AuthenticationExpiredError(
                "/api/v1/site/token 返回 401/403，cookies.json 已被服务端判定失效。"
            )
        raise RuntimeError(f"Could not fetch Helium 10 API tokens. Status: {response.status_code}. Body: {response.text[:200]}")

    if looks_like_html_response(response):
        raise AuthenticationExpiredError(
            "/api/v1/site/token returned HTML instead of JSON; cookies/auth_state are expired "
            "or the browser session has not entered the API-authenticated state."
        )

    data = parse_json_response(response, "/api/v1/site/token").get("data") or {}
    token = data.get("token")
    pacvue_token = data.get("pacvueToken")
    if not token or not pacvue_token:
        raise RuntimeError("Token response did not include token and pacvueToken.")
    session.helium10_token = token
    session.helium10_pacvue_token = pacvue_token
    if auth_state_path:
        save_site_tokens(
            auth_state_path=auth_state_path,
            account_id=account_id,
            token=token,
            pacvue_token=pacvue_token,
        )
    return token, pacvue_token


def create_cerebro_search(
    session: requests.Session,
    *,
    account_id: str,
    asin: str,
    marketplace: Marketplace,
    token: str,
    pacvue_token: str,
    timeout: int,
) -> int:
    referer = f"{BASE_URL}/cerebro-new?accountId={account_id}"
    response = session.post(
        f"{H10_API_BASE_URL}/rta/cerebro/v1/amazon/search/single",
        params={"accountId": account_id},
        json={
            "marketplace": marketplace.marketplace,
            "productId": asin.strip().upper(),
            "adminSearch": False,
            "exactProduct": False,
        },
        headers=api_headers(token, pacvue_token, referer),
        timeout=timeout,
    )
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Could not create Cerebro search. Status: {response.status_code}. Body: {response.text[:300]}")

    search_id = (parse_json_response(response, "create Cerebro search").get("data") or {}).get("id")
    if not search_id:
        raise RuntimeError("Cerebro search response did not include a search id.")
    return int(search_id)


def wait_for_sponsored_data(
    session: requests.Session,
    *,
    account_id: str,
    search_id: int,
    token: str,
    pacvue_token: str,
    timeout: int,
    poll_timeout: int = 120,
    poll_interval: int = 5,
) -> None:
    referer = f"{BASE_URL}/cerebro-new/find-by-product/amazon/view/{search_id}?accountId={account_id}"
    task_url = f"{H10_API_BASE_URL}/rta/cerebro/v1/amazon/search/{search_id}/sponsored-data"
    response = session.post(
        task_url,
        params={"accountId": account_id},
        headers=api_headers(token, pacvue_token, referer),
        timeout=timeout,
    )
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Could not create sponsored data task. Status: {response.status_code}. Body: {response.text[:300]}")

    deadline = time.monotonic() + poll_timeout
    last_status = None
    while time.monotonic() < deadline:
        status_response = session.get(
            task_url,
            params={"accountId": account_id},
            headers=api_headers(token, pacvue_token, referer),
            timeout=timeout,
        )
        if status_response.status_code != 200:
            raise RuntimeError(
                f"Could not fetch sponsored data task status. Status: {status_response.status_code}. "
                f"Body: {status_response.text[:300]}"
            )
        data = parse_json_response(status_response, "fetch sponsored data task status").get("data") or {}
        last_status = data.get("sponsoredStatus")
        if last_status == 101:
            return
        time.sleep(poll_interval)

    raise RuntimeError(f"Sponsored data task did not finish within {poll_timeout}s. Last status: {last_status}")


def fetch_exported_keywords(
    session: requests.Session,
    *,
    account_id: str,
    search_id: int,
    token: str,
    pacvue_token: str,
    timeout: int,
) -> list[dict[str, Any]]:
    referer = f"{BASE_URL}/cerebro-new/find-by-product/amazon/view/{search_id}?accountId={account_id}"
    response = session.get(
        f"{H10_API_BASE_URL}/rta/cerebro/v1/amazon/search/single/{search_id}/exported-data",
        params={
            "accountId": account_id,
            "include-all": 0,
            "include-any": 1,
            "sort": "default",
        },
        headers=api_headers(token, pacvue_token, referer),
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Could not fetch exported keyword data. Status: {response.status_code}. Body: {response.text[:300]}")

    data = parse_json_response(response, "fetch exported keyword data").get("data")
    if not isinstance(data, list):
        raise RuntimeError("Exported keyword response did not include a data list.")
    return data


def csv_fields(rows: list[dict[str, Any]]) -> list[str]:
    seen = set()
    fields: list[str] = []
    for field in PREFERRED_CSV_FIELDS:
        if any(field in row for row in rows):
            fields.append(field)
            seen.add(field)
    for row in rows:
        for field in row:
            if field not in seen:
                fields.append(field)
                seen.add(field)
    return fields


def js_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def normalize_js_number(value: float | int) -> int | float:
    number = float(value)
    if math.isfinite(number) and number.is_integer():
        return int(number)
    return number


def js_round(value: Any, maximum_fraction_digits: int = 0) -> int | float | None:
    number = js_number(value)
    if number is None:
        return None
    scale = 10**maximum_fraction_digits
    return normalize_js_number(math.floor(number * scale + 0.5) / scale)


def js_truthy_number(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, str):
        return True
    number = js_number(value)
    return bool(number) and not math.isnan(number)


def format_number_for_export(value: Any, maximum_fraction_digits: int | None = None) -> int | float | str:
    number = js_number(value)
    if number is None:
        return "-"
    if maximum_fraction_digits is not None:
        rounded = js_round(number, maximum_fraction_digits)
        return "-" if rounded is None else rounded
    return normalize_js_number(number)


def price_from_api(value: Any, currency: str = "USD") -> int | float | str:
    number = js_number(value)
    if number is None:
        return ""
    if currency == "JPY":
        return normalize_js_number(number)
    return normalize_js_number(number / 100)


def match_types(row: dict[str, Any]) -> set[str]:
    raw = row.get("matchType")
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").split("|")
    return {str(value).strip().lower() for value in values if str(value).strip()}


def export_if_truthy_number(value: Any, maximum_fraction_digits: int = 0) -> int | float | str:
    if js_truthy_number(value):
        return format_number_for_export(value, maximum_fraction_digits)
    return "-"


def export_if_present_number(value: Any, maximum_fraction_digits: int = 0) -> int | float | str:
    if value is not None:
        return format_number_for_export(value, maximum_fraction_digits)
    return "-"


def export_non_negative_number(value: Any, maximum_fraction_digits: int = 0) -> int | float | str:
    number = js_number(value)
    if number is not None and not math.isnan(number) and number >= 0:
        return format_number_for_export(number, maximum_fraction_digits)
    return "-"


def manual_export_row(row: dict[str, Any]) -> dict[str, Any]:
    matches = match_types(row)
    return {
        "Keyword Phrase": row.get("phrase") or "",
        "ABA Total Click Share": js_round(row.get("clickShareRate"), 1) if js_truthy_number(row.get("clickShareRate")) else "",
        "ABA Total Conv. Share": js_round(row.get("conversionShareRate"), 1)
        if js_truthy_number(row.get("conversionShareRate"))
        else "",
        "Cerebro IQ Score": export_if_truthy_number(row.get("iq")),
        "Search Volume": export_non_negative_number(row.get("impressionExact30")),
        "Search Volume Trend": export_if_truthy_number(row.get("searchVolumeTrend")),
        "H10 PPC Sugg. Bid": price_from_api(row.get("cpc")) if row.get("cpc") is not None else "",
        "H10 PPC Sugg. Min Bid": price_from_api(row.get("lowCpc")) if row.get("lowCpc") is not None else "",
        "H10 PPC Sugg. Max Bid": price_from_api(row.get("highCpc")) if row.get("highCpc") is not None else "",
        "Sponsored ASINs": format_number_for_export(row.get("sponsoredAsins"), 0),
        "Competing Products": export_non_negative_number(row.get("resultsNumber")),
        "CPR": format_number_for_export(row.get("newCprExact"), 0),
        "Title Density": format_number_for_export(row.get("exactTitleMatchProductsCount"), 0),
        "Amazon Recommended": 1 if "ppc" in matches else 0,
        "Sponsored": 1 if "sponsored" in matches or "lexical" in matches else 0,
        "Organic": 1 if "organic" in matches or "contextual" in matches else 0,
        "Amazon Rec. Rank": export_if_present_number(row.get("amzSuggestedPosition")),
        "Sponsored Rank": export_if_truthy_number(row.get("sponsoredPosition")),
        "Organic Rank": export_if_truthy_number(row.get("organicPosition")),
    }


def write_keywords_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow([MANUAL_EXPORT_FIELD_LABELS[field] for field in MANUAL_EXPORT_FIELDS])
        for row in rows:
            export_row = manual_export_row(row)
            writer.writerow([export_row[field] for field in MANUAL_EXPORT_FIELDS])


def run(
    *,
    asin: str,
    site: str,
    account_id: str | None,
    env_path: Path,
    cookies_path: Path,
    output_dir: Path,
    timeout: int,
    auth_state_path: Path = DEFAULT_AUTH_STATE_PATH,
) -> Path:
    marketplace = resolve_marketplace(site)
    try:
        session = login_session(
            env_path=env_path,
            cookies_path=cookies_path,
            auth_state_path=auth_state_path,
            account_id=account_id,
            timeout=timeout,
        )
    except (FileNotFoundError, RuntimeError) as error:
        print(f"cookies.json/auth_state.json 不可用，正在运行 login.py 自动登录: {error}")
        login()
        session = login_session(
            env_path=env_path,
            cookies_path=cookies_path,
            auth_state_path=auth_state_path,
            account_id=account_id,
            timeout=timeout,
        )
    account_id = session.helium10_account_id
    referer = f"{BASE_URL}/dashboard?accountId={account_id}"
    try:
        token, pacvue_token = get_site_tokens(
            session,
            account_id=account_id,
            referer=referer,
            timeout=timeout,
            auth_state_path=auth_state_path,
        )
    except AuthenticationExpiredError:
        print("/api/v1/site/token 返回 401/403，cookies.json 已被服务端判定失效，正在运行 login.py 重新登录...")
        login()
        session = login_session(
            env_path=env_path,
            cookies_path=cookies_path,
            auth_state_path=auth_state_path,
            account_id=account_id,
            timeout=timeout,
        )
        account_id = session.helium10_account_id
        referer = f"{BASE_URL}/dashboard?accountId={account_id}"
        token, pacvue_token = get_site_tokens(
            session,
            account_id=account_id,
            referer=referer,
            timeout=timeout,
            auth_state_path=auth_state_path,
        )
    search_id = create_cerebro_search(
        session,
        account_id=account_id,
        asin=asin,
        marketplace=marketplace,
        token=token,
        pacvue_token=pacvue_token,
        timeout=timeout,
    )
    wait_for_sponsored_data(
        session,
        account_id=account_id,
        search_id=search_id,
        token=token,
        pacvue_token=pacvue_token,
        timeout=timeout,
    )
    rows = fetch_exported_keywords(
        session,
        account_id=account_id,
        search_id=search_id,
        token=token,
        pacvue_token=pacvue_token,
        timeout=timeout,
    )
    csv_path = output_csv_path(asin, marketplace.domain_suffix, output_dir)
    write_keywords_csv(rows, csv_path)
    return csv_path


def main() -> int:
    asin = "B0FG2W77VY"
    site = "德国"
    try:
        csv_path = run(
            asin=asin,
            site=site,
            account_id=None,
            env_path=DEFAULT_ENV_PATH,
            cookies_path=DEFAULT_COOKIES_PATH,
            auth_state_path=DEFAULT_AUTH_STATE_PATH,
            output_dir=DEFAULT_OUTPUT_DIR,
            timeout=60,
        )
    except (RuntimeError, ValueError, requests.RequestException) as error:
        print(f"Error: {error}")
        return 1

    print(f"Saved CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
