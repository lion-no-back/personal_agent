"""Helium10 ASIN reverse lookup workflow entrypoint.

由 ConversationAgent 按需触发：
  - 含「反查配置」：安装 DrissionPage / Pillow，检查 Chrome，并登录生成认证态
  - 含「ASIN反查」：解析 1 个 ASIN 和站点，执行 Cerebro 导出、LLM 分析、XLSX 标注

运行段不静默安装依赖；如果 cookies/auth_state 失效，底层会自动重新登录。
返回时 PNG 走 segments.image 作为图片消息发送，XLSX 走 files 作为文件发送。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_ASIN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{10})(?![A-Za-z0-9])")
_CONFIG_TRIGGER = "反查配置"
_LOOKUP_TRIGGER = "ASIN反查"

_CHECK_PACKAGES = {
    "DrissionPage": "DrissionPage>=4.1.0",
    "PIL": "Pillow>=10.0.0",
}

_MSG_NEED_CONFIG = "⚠️ 尚未完成反查配置，请先发送【反查配置】"
_MSG_BAD_FORMAT = "⚠️ 未能识别站点或 ASIN。请按【ASIN反查 美国B0FDKNWJS5】发送，一次只支持 1 个 ASIN"

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACT_ROOT = _PROJECT_ROOT / "artifact"


def _deps_ready() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in _CHECK_PACKAGES)


def _ensure_deps() -> None:
    missing = [
        package
        for module_name, package in _CHECK_PACKAGES.items()
        if importlib.util.find_spec(module_name) is None
    ]
    if not missing:
        return

    logger.info("[asinfc_h10] 缺少依赖 %s，正在安装...", missing)
    try:
        from personal_agent.workflows.deplock import uv_pip_install

        uv_pip_install(missing)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"依赖安装失败，请手动执行：uv pip install {' '.join(missing)}") from exc
    importlib.invalidate_caches()


def _parse_request(content: str) -> tuple[str, str] | None:
    matches = _ASIN_RE.findall(content or "")
    if len(matches) != 1:
        return None

    raw_asin = matches[0]
    asin = raw_asin.upper()
    site = content.replace(_LOOKUP_TRIGGER, "").replace(raw_asin, "")
    site = re.sub(r"[\s:：,，;；]+", "", site).strip()
    if not site:
        return None

    from .cerebro_keywords import resolve_marketplace

    resolve_marketplace(site)
    return asin, site


def _run_config() -> dict:
    try:
        _ensure_deps()
        from . import login

        login.resolve_browser_path()
        login.login()
    except Exception as exc:
        logger.exception("[asinfc_h10] 反查配置失败")
        return {"message": f"❌ 反查配置失败：{exc}", "files": []}

    return {"message": "✅ 反查配置完成，现在可发送【ASIN反查 美国B0FDKNWJS5】", "files": []}


def _run_lookup(content: str) -> dict:
    if not _deps_ready():
        return {"message": _MSG_NEED_CONFIG, "files": []}

    try:
        parsed = _parse_request(content)
    except ValueError as exc:
        return {"message": f"{_MSG_BAD_FORMAT}\n{exc}", "files": []}
    if parsed is None:
        return {"message": _MSG_BAD_FORMAT, "files": []}

    asin, site = parsed
    started_at = datetime.now()
    output_dir = _ARTIFACT_ROOT / f"keywordfc_{date.today():%Y%m%d}"

    try:
        from . import cerebro_keywords, csvprocess, lanalyze

        csv_path = cerebro_keywords.run(
            asin=asin,
            site=site,
            account_id=None,
            env_path=cerebro_keywords.DEFAULT_ENV_PATH,
            cookies_path=cerebro_keywords.DEFAULT_COOKIES_PATH,
            auth_state_path=cerebro_keywords.DEFAULT_AUTH_STATE_PATH,
            output_dir=output_dir,
            timeout=60,
        )

        env = lanalyze.load_env(lanalyze.DEFAULT_ENV_PATH)
        markdown_text = lanalyze.strip_markdown_fences(
            lanalyze.call_llm(
                api_key=lanalyze.env_value(env, "LLM_WORKFLOW_API_KEY"),
                base_url=lanalyze.env_value(env, "LLM_WORKFLOW_BASE_URL"),
                model=lanalyze.env_value(env, "LLM_WORKFLOW_MODEL_ID"),
                csv_text=lanalyze.read_csv_text(csv_path),
                timeout=180,
            )
        )
        markdown_path, screenshot_paths = lanalyze.write_outputs(markdown_text, csv_path.parent)

        xlsx_path = csvprocess.default_output_path(csv_path)
        csvprocess.process_csv(csv_path, markdown_path, xlsx_path)
    except Exception as exc:
        logger.exception("[asinfc_h10] ASIN反查失败 | asin=%s site=%s", asin, site)
        return {"message": f"❌ ASIN反查失败：{exc}", "files": []}

    elapsed = int((datetime.now() - started_at).total_seconds())
    files = [xlsx_path] if xlsx_path.exists() else []
    segments = [
        {
            "image": screenshot_path,
            "text": "",
        }
        for screenshot_path in screenshot_paths
        if screenshot_path.exists()
    ]
    png_note = "" if screenshot_paths else "\n提示：未生成分析总结 PNG，仅回传 XLSX。"
    message = (
        "✅ ASIN反查完成\n"
        f"站点: {site}\n"
        f"ASIN: {asin}\n"
        f"耗时: {elapsed}s"
        f"{png_note}"
    )
    return {"message": message, "files": files, "segments": segments}


def run(content: str = "") -> dict:
    if _CONFIG_TRIGGER in content:
        return _run_config()
    return _run_lookup(content)
