import asyncio
import threading
import sys
import re as _re
from typing import List
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from .types import ToolDef

# Roles that carry actionable ref IDs the model actually needs
_INTERACTIVE_ROLES = frozenset({
    'input', 'button', 'link', 'textbox', 'combobox', 'checkbox',
    'radio', 'select', 'textarea', 'searchbox', 'menuitem', 'option', 'tab',
})

# Per-session snapshot dedup state
_snap_state: dict = {'url': '', 'count': 0}


def _compress_snapshot(text: str) -> str:
    """
    Replace a large browser_snapshot result with only its interactive elements.
    Tracks same-URL repeated calls and escalates guidance accordingly.
    """
    orig_len = len(text)
    if orig_len <= 3000:
        return text

    # Extract current URL for dedup tracking
    url_m = _re.search(r'- Page URL: (.+)', text)
    current_url = url_m.group(1).strip() if url_m else ''
    if current_url != _snap_state['url']:
        _snap_state['url'] = current_url
        _snap_state['count'] = 1
    else:
        _snap_state['count'] += 1
    call_no = _snap_state['count']

    # Extract page header block (everything before the yaml block)
    header_m = _re.search(r'^(.*?)```yaml', text, _re.DOTALL)
    header = header_m.group(1).rstrip() if header_m else text[:200]

    # Extract only interactive-element lines from the YAML block
    yaml_m = _re.search(r'```yaml\n(.*?)```', text, _re.DOTALL)
    interactive: list[str] = []
    if yaml_m:
        for line in yaml_m.group(1).splitlines():
            stripped = line.strip()
            if not stripped or '[ref=' not in stripped:
                continue
            # First token after "- " is the ARIA role
            role = stripped.lstrip('- ').split()[0].lower()
            if role in _INTERACTIVE_ROLES:
                interactive.append('  ' + stripped)

    elem_text = '\n'.join(interactive) if interactive else '(未检测到可交互元素)'

    if call_no == 1:
        note = f"⚙️ 快照已精简：{orig_len} 字符 → 提取 {len(interactive)} 个交互元素。若需提取正文内容请用 browser_evaluate + JS。"
    else:
        note = (
            f"🔁 [同页第 {call_no} 次快照，ref 可能已更新]\n"
            "💡 ref 失效时请直接用 browser_evaluate 执行 JS，例如：\n"
            "   `document.querySelector('input[name=wd]').value='关键词'; "
            "document.querySelector('#su').click()`"
        )

    return f"{header}\n### 可交互元素 (精简快照)\n```yaml\n{elem_text}\n```\n{note}"


def build_playwright_tools() -> List[ToolDef]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    async def _run():
        # 修复 1：兼容 Windows 的 npx 命令后缀
        cmd = "npx.cmd" if sys.platform == "win32" else "npx"
        
        # 修复 2：使用官方正确的包名 @playwright/mcp@latest
        p = StdioServerParameters(command=cmd, args=["-y", "@playwright/mcp@latest"])
        
        async with stdio_client(p) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                state['session'] = s
                # 获取该 MCP Server 提供的所有可用工具
                state['mcp_tools'] = (await s.list_tools()).tools
                ready.set()
                # 挂起协程，保持长连接后台存活
                await asyncio.Future()

    # 在后台守护线程中运行 asyncio 事件循环
    threading.Thread(target=lambda: loop.run_until_complete(_run()), daemon=True).start()
    
    # 等待进程启动和 MCP 握手完成
    if not ready.wait(timeout=60):  # 第一次下载包可能较慢，适当放宽超时时间到 60 秒
        raise RuntimeError("启动 Playwright MCP 服务超时")

    # 利用闭包封装具体的工具调用逻辑
    def make_call(name: str, schema: dict):
        def _call(**kwargs):
            try:
                valid_keys = schema.get("properties", {}).keys()
                clean_kwargs = {k: v for k, v in kwargs.items() if k in valid_keys}

                f = asyncio.run_coroutine_threadsafe(
                    state['session'].call_tool(name, arguments=clean_kwargs), loop
                )
                res = f.result(timeout=60)
                
                # 1. 处理 MCP 协议层面的工具内部错误
                if getattr(res, 'isError', False):
                    err_msg = res.content[0].text if res.content else "未知错误"
                    
                    # 【新增优雅逻辑】：局部异常引导
                    # 如果是因为 Playwright 严格校验导致的超时，直接在报错中教大模型怎么绕过，不浪费下一轮思考
                    hint = ""
                    if "Timeout" in err_msg and ("not visible" in err_msg or "not stable" in err_msg):
                        hint = "\n💡 提示：该元素可能被前端遮挡或存在动态渲染。请勿继续尝试原生 click/fill，请立即改用 `browser_evaluate` 执行 JS 强制操作（例如：document.querySelector('...').value='...'）。"
                    elif "does not match any elements" in err_msg:
                        hint = (
                            "\n💡 ref 已失效（页面更新后 ref ID 会变化）。"
                            "请立即改用 CSS 选择器通过 browser_evaluate 操作，"
                            "例如：`document.querySelector('input[name=wd]').value='...'`，"
                            "不要再调用 browser_snapshot 重新扫描。"
                        )

                    return f"❌ 工具内部报错: {err_msg}。{hint}"

                # 2. 正常返回结果
                if res.content and hasattr(res.content[0], 'text'):
                    result_text = res.content[0].text
                    if name == "browser_snapshot":
                        result_text = _compress_snapshot(result_text)
                    return result_text
                return str(res.content)

            except Exception as e:
                return f"❌ 执行异常: {str(e)}。请反思参数或换用其他工具。"
                
        return _call

    # 动态批量构造系统的 ToolDef，这里传入 t.inputSchema
    return [
        ToolDef(
            name=t.name,
            description=t.description or "",
            parameters=t.inputSchema,
            call=make_call(t.name, t.inputSchema)  # <--- 修改了这里：传入 schema 用于过滤
        ) for t in state['mcp_tools']
    ]