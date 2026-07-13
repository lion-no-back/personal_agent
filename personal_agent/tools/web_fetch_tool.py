import re
import logging
from urllib.parse import urlparse
import requests
from .types import ToolDef

logger = logging.getLogger(__name__)

# URL 路由表：匹配 netloc 前缀 → provider 链
_ROUTE_TABLE: dict[str, list[str]] = {
    "mp.weixin.qq.com": ["defuddle"],
}
_DEFAULT_CHAIN = ["jina", "defuddle"]

_MAX_CHARS = 15000


def _route(url: str) -> list[str]:
    netloc = urlparse(url).netloc
    for pattern, chain in _ROUTE_TABLE.items():
        if netloc == pattern or netloc.endswith("." + pattern):
            return chain
    return _DEFAULT_CHAIN


def _fetch_jina(url: str) -> str | None:
    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain", "X-Return-Format": "markdown"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text or None
    except Exception as e:
        logger.warning("Jina 失败 (%s): %s", url, e)
        return None


def _fetch_defuddle(url: str) -> str | None:
    try:
        resp = requests.get(
            f"https://defuddle.md/{url}",
            headers={"Accept": "text/plain"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text or None
    except Exception as e:
        logger.warning("Defuddle 失败 (%s): %s", url, e)
        return None


_PROVIDERS = {
    "jina": _fetch_jina,
    "defuddle": _fetch_defuddle,
}


def _strip_frontmatter(text: str) -> str:
    """剥离 defuddle 返回的 YAML frontmatter（--- ... --- 头部块）"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _normalize(content: str, from_defuddle: bool) -> str:
    if from_defuddle:
        content = _strip_frontmatter(content)
    if len(content) > _MAX_CHARS:
        content = content[:_MAX_CHARS] + f"\n\n...(截断: 省略后 {len(content) - _MAX_CHARS} 字符)"
    return content


def build_web_fetch_tool() -> ToolDef:

    def web_fetch_call(url: str, **kwargs) -> str:
        if not url.startswith("http"):
            url = "https://" + url

        chain = _route(url)
        for provider_name in chain:
            raw = _PROVIDERS[provider_name](url)
            if raw:
                content = _normalize(raw, from_defuddle=(provider_name == "defuddle"))
                return f"网页 '{url}' 获取成功:\n\n{content}"

        return f"Error: 所有提取方式均失败，无法获取网页内容 '{url}'"

    return ToolDef(
        name="web_fetch",
        description=(
            "抓取指定 URL 的网页内容，并提取为纯净的 Markdown 文本返回。"
            "当用户发送网页链接并明确期望你提取网页内容、总结文章或搜索信息时调用此工具。"
            "如果用户意图不清楚，必须先调用 clarify 澄清。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的网页 URL，例如 https://example.com"
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        call=web_fetch_call,
    )
