import logging
import requests
from .types import ToolDef

logger = logging.getLogger(__name__)

def build_web_fetch_tool() -> ToolDef:
    """构建读取网页内容的工具（使用 Jina Reader，返回干净 Markdown）"""

    def web_fetch_call(url: str, **kwargs) -> str:
        try:
            if not url.startswith("http"):
                url = "https://" + url

            jina_url = f"https://r.jina.ai/{url}"
            headers = {
                "Accept": "text/plain",
                "X-Return-Format": "markdown",
            }

            response = requests.get(jina_url, headers=headers, timeout=30)
            response.raise_for_status()

            content = response.text

            max_chars = 15000
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n...(截断: 省略后 {len(content) - max_chars} 字符，因网页内容过大暂不显示全部)..."

            return f"网页 '{url}' 获取成功:\n\n{content}"

        except requests.exceptions.RequestException as e:
            return f"Error: 网络请求异常 '{str(e)}'"
        except Exception as e:
            return f"Error: 读取网页时发生异常 '{str(e)}'"

    return ToolDef(
        name="web_fetch",
        description="抓取指定 URL 的网页内容，并提取为纯净的 Markdown 文本返回。当用户发送网页链接并期望你提取网页内容、总结文章或搜索信息时，务必调用此工具。",
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
