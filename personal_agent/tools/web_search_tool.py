import json
import logging
import os
from pathlib import Path

import requests
from ddgs import DDGS

from .types import ToolDef

logger = logging.getLogger(__name__)

_ROTATION_FILE = Path(__file__).parent.parent.parent / ".search_rotation.json"


# ── 付费提供商检测 ────────────────────────────────────────────────

def _get_paid_providers() -> list[str]:
    """返回已配置 API Key 的付费提供商列表（按 Tavily→Exa→Firecrawl 顺序）。"""
    providers = []
    if os.getenv("TAVILY_API_KEY"):
        providers.append("tavily")
    if os.getenv("EXA_API_KEY"):
        providers.append("exa")
    if os.getenv("FIRECRAWL_API_KEY"):
        providers.append("firecrawl")
    return providers


# ── 轮换状态持久化 ────────────────────────────────────────────────

def _read_index(total: int) -> int:
    try:
        if _ROTATION_FILE.exists():
            data = json.loads(_ROTATION_FILE.read_text(encoding="utf-8"))
            return int(data.get("current_index", 0)) % total
    except Exception:
        pass
    return 0


def _write_index(index: int) -> None:
    try:
        _ROTATION_FILE.write_text(
            json.dumps({"current_index": index}), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"无法写入轮换状态文件: {e}")


# ── 付费提供商实现 ────────────────────────────────────────────────

def _search_tavily(query: str, max_results: int) -> list[dict]:
    response = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": os.getenv("TAVILY_API_KEY", ""), "query": query, "max_results": max_results},
        timeout=15,
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in response.json().get("results", [])
    ]


def _search_exa(query: str, max_results: int) -> list[dict]:
    response = requests.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": os.getenv("EXA_API_KEY", ""), "Content-Type": "application/json"},
        json={"query": query, "numResults": max_results, "contents": {"highlights": {"numSentences": 2}}},
        timeout=15,
    )
    response.raise_for_status()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("highlights") or [""])[0],
        }
        for r in response.json().get("results", [])
    ]


def _search_firecrawl(query: str, max_results: int) -> list[dict]:
    response = requests.post(
        "https://api.firecrawl.dev/v1/search",
        headers={"Authorization": f"Bearer {os.getenv('FIRECRAWL_API_KEY', '')}", "Content-Type": "application/json"},
        json={"query": query, "limit": max_results},
        timeout=15,
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in response.json().get("data", [])
    ]


_PAID_SEARCH_FUNCS = {
    "tavily": _search_tavily,
    "exa": _search_exa,
    "firecrawl": _search_firecrawl,
}


# ── 免费兜底：DuckDuckGo ──────────────────────────────────────────

def _search_ddgs(query: str, max_results: int) -> list[dict]:
    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))
    return [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
        for r in raw
    ]


# ── 统一格式化 ────────────────────────────────────────────────────

def _format_results(query: str, results: list[dict], source: str) -> str:
    lines = [f"搜索 '{query}' 的结果（来源：{source}）：\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r.get('title', '')}**")
        lines.append(f"   URL: {r.get('url', '')}")
        if r.get("snippet"):
            lines.append(f"   摘要: {r.get('snippet', '')[:200]}")
        lines.append("")
    return "\n".join(lines)


# ── 工具入口 ──────────────────────────────────────────────────────

def build_web_search_tool() -> ToolDef:
    """
    构建网页搜索工具。

    策略：
    - 有配置付费 API Key（Tavily / Exa / Firecrawl）→ 付费提供商轮换，用量均摊
    - 全无配置或付费全部失败 → 回退到 DuckDuckGo（免费，无需 Key）
    """

    def web_search_call(query: str, max_results: int = 5, **kwargs) -> str:
        max_results = min(max(max_results, 1), 10)
        paid = _get_paid_providers()

        # ── 有付费 API：走轮换方案 ──────────────────────────────
        if paid:
            index = _read_index(len(paid))
            for attempt in range(len(paid)):
                provider = paid[(index + attempt) % len(paid)]
                try:
                    results = _PAID_SEARCH_FUNCS[provider](query, max_results)
                    _write_index((index + attempt + 1) % len(paid))
                    if not results:
                        return f"搜索 '{query}' 未找到结果（使用 {provider}）"
                    return _format_results(query, results, provider)
                except Exception as e:
                    logger.warning(f"搜索提供商 {provider} 失败: {e}，尝试下一个...")
            logger.warning("所有付费搜索提供商均失败，回退到 DuckDuckGo")

        # ── 无付费 API 或付费全部失败：DuckDuckGo 兜底 ──────────
        try:
            results = _search_ddgs(query, max_results)
            if not results:
                return f"搜索 '{query}' 未找到结果"
            return _format_results(query, results, "DuckDuckGo")
        except Exception as e:
            logger.warning(f"DuckDuckGo 搜索失败: {e}")
            return f"Error: 所有搜索方式均失败 — {e}"

    return ToolDef(
        name="web_search",
        description=(
            "在互联网上搜索信息，返回相关网页的标题、URL 和摘要列表。"
            "当需要查找最新资讯、不知道具体 URL 时使用此工具；"
            "如需阅读某个结果的全文，再配合 web_fetch 工具使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题，例如 '2024年最新 LLM 框架对比'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数量，默认 5，最多 10",
                    "default": 5,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        call=web_search_call,
    )
