import logging
import requests
from markdownify import markdownify as md
from .types import ToolDef

logger = logging.getLogger(__name__)

def build_web_fetch_tool() -> ToolDef:
    """构建读取网页内容的工具"""

    def web_fetch_call(url: str, **kwargs) -> str:
        try:
            if not url.startswith("http"):
                url = "https://" + url

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }

            # 使用 stream=True 进行安全截断，防止超大文件把服务器撑爆
            with requests.get(url, headers=headers, stream=True, timeout=15) as r:
                r.raise_for_status()

                # 检查内容类型，避免下载巨大的二进制文件
                content_type = r.headers.get("Content-Type", "")
                if "text/html" not in content_type and "text/plain" not in content_type and "application/json" not in content_type:
                    return f"Warning: URL '{url}' 返回的内容类型为 '{content_type}'，本工具主要支持抓取网页/文本信息。"

                # 限制读取前 5MB 数据，防止被恶意无限大流攻击。
                # 必须设置 decode_content=True，否则在 stream=True 时 r.raw.read() 会返回未解压缩的 gzip 原始二进制数据。
                r.raw.decode_content = True
                raw_content = r.raw.read(5 * 1024 * 1024)

                # 尝试解析文本
                try:
                    html_content = raw_content.decode(r.encoding or 'utf-8')
                except UnicodeDecodeError:
                    html_content = raw_content.decode('utf-8', errors='ignore')

            if "text/html" in content_type:
                from bs4 import BeautifulSoup
                from urllib.parse import urljoin
                
                # 修复相对路径问题：使用 BeautifulSoup 将相对链接转换为绝对链接
                soup = BeautifulSoup(html_content, "html.parser")
                for a_tag in soup.find_all("a", href=True):
                    a_tag["href"] = urljoin(r.url, a_tag["href"])
                
                # 转换为干净的 Markdown 文本
                markdown_text = md(str(soup), escape_asterisks=False, escape_underscores=False, strip=['script', 'style', 'img'])
                
                # 过滤掉过多空行，大幅压缩无用长度
                lines = [line.strip() for line in markdown_text.split('\n')]
                markdown_text = '\n'.join([line for line in lines if line])
            else:
                markdown_text = html_content

            # 防止填爆上下文，如果超过 15000 字符则作截断 (类似于 read_file_tool)
            max_chars = 15000
            if len(markdown_text) > max_chars:
                summary = markdown_text[:max_chars] + f"\n\n...(截断: 省略后 {len(markdown_text)-max_chars} 字符，因网页内容过大暂不显示全部)..."
                return f"网页 '{url}' 获取成功:\n\n{summary}"

            return f"网页 '{url}' 获取成功:\n\n{markdown_text}"

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
