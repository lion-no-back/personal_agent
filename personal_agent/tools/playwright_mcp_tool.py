import asyncio
import threading
import sys
from typing import List
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from .types import ToolDef

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
                        
                    return f"❌ 工具内部报错: {err_msg}。{hint}"

                # 2. 正常返回结果
                if res.content and hasattr(res.content[0], 'text'):
                    return res.content[0].text
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