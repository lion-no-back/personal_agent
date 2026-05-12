import json
import logging
import os
from pathlib import Path

import requests

from .types import ToolDef

logger = logging.getLogger(__name__)

# 轮换状态持久化文件（项目根目录）
_ROTATION_FILE = Path(__file__).parent.parent.parent / ".search_rotation.json"

_PROVIDERS = ["tavily", "exa", "firecrawl"]


def _read_index() -> int:
    try:
        if _ROTATION_FILE.exists():
            data = json.loads(_ROTATION_FILE.read_text(encoding="utf-8"))
            return int(data.get("current_index", 0)) % len(_PROVIDERS)
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


def _search_tavily(query: str, max_results: int) -> list[dict]:
    api_key = os.getenv("TAVILY_API_KEY", "")
    response = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in data.get("results", [])
    ]


def _search_exa(query: str, max_results: int) -> list[dict]:
    api_key = os.getenv("EXA_API_KEY", "")
    response = requests.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={
            "query": query,
            "numResults": max_results,
            "contents": {"highlights": {"numSentences": 2}},
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    results = []
    for r in data.get("results", []):
        highlights = r.get("highlights", [])
        snippet = highlights[0] if highlights else ""
        results.append(
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": snippet}
        )
    return results


def _search_firecrawl(query: str, max_results: int) -> list[dict]:
    api_key = os.getenv("FIRECRAWL_API_KEY", "")
    response = requests.post(
        "https://api.firecrawl.dev/v1/search",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query, "limit": max_results},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("description", ""),
        }
        for r in data.get("data", [])
    ]


_SEARCH_FUNCS = {
    "tavily": _search_tavily,
    "exa": _search_exa,
    "firecrawl": _search_firecrawl,
}


def build_web_search_tool() -> ToolDef:
    """构建网页搜索工具（Tavily / Exa / Firecrawl 顺序轮换，用量持久化）"""

    def web_search_call(query: str, max_results: int = 5, **kwargs) -> str:
        index = _read_index()

        for attempt in range(len(_PROVIDERS)):
            provider = _PROVIDERS[(index + attempt) % len(_PROVIDERS)]
            try:
                results = _SEARCH_FUNCS[provider](query, max_results)
                # 成功后将索引推进到下一个提供商
                _write_index((index + attempt + 1) % len(_PROVIDERS))

                if not results:
                    return f"搜索 '{query}' 未找到结果（使用 {provider}）"

                lines = [f"搜索 '{query}' 的结果（来源：{provider}）：\n"]
                for i, r in enumerate(results, 1):
                    lines.append(f"{i}. **{r['title']}**")
                    lines.append(f"   URL: {r['url']}")
                    if r["snippet"]:
                        lines.append(f"   摘要: {r['snippet'][:200]}")
                    lines.append("")

                return "\n".join(lines)

            except Exception as e:
                logger.warning(f"搜索提供商 {provider} 失败: {e}，尝试下一个...")
                continue

        return "Error: 所有搜索提供商均失败，请稍后重试"

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
