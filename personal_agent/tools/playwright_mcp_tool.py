import asyncio
import threading
import sys
import re as _re
import json
import os
import logging
from typing import List
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from .types import ToolDef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. 快照精简 — 只保留可交互元素，减少 token 消耗
# ---------------------------------------------------------------------------

# 需要保留的交互元素角色（用于操作页面）
_INTERACTIVE_ROLES = frozenset({
    'input', 'button', 'link', 'textbox', 'combobox', 'checkbox',
    'radio', 'select', 'textarea', 'searchbox', 'menuitem', 'option', 'tab',
})

# 需要保留的文本元素角色（用于阅读内容）
_TEXT_ROLES = frozenset({
    'heading', 'text', 'paragraph', 'listitem', 'cell', 'caption',
    'blockquote', 'code', 'time', 'status', 'alert', 'definition',
})


def _compress_snapshot(text: str) -> str:
    """
    精简 browser_snapshot 输出。
    保留两类信息：
      1. 可交互元素（带 [ref=]）— 用于操作
      2. 有实际文本的内容元素（heading/text/paragraph 等）— 用于阅读
    去掉：空容器、纯装饰节点、无文本的布局节点。
    """
    orig_len = len(text)
    if orig_len <= 3000:
        return text

    header_m = _re.search(r'^(.*?)```yaml', text, _re.DOTALL)
    header = header_m.group(1).rstrip() if header_m else text[:200]

    yaml_m = _re.search(r'```yaml\r?\n(.*?)```', text, _re.DOTALL)
    kept: list[str] = []
    if yaml_m:
        for line in yaml_m.group(1).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            role = stripped.lstrip('- ').split()[0].lower()
            # 交互元素：必须有 ref 才保留（无 ref 的交互元素不可操作，无意义）
            if role in _INTERACTIVE_ROLES:
                if '[ref=' in stripped:
                    kept.append('  ' + stripped)
                continue
            # 文本元素：保留有实际内容的行（role 后面有引号包裹的文本）
            if role in _TEXT_ROLES:
                # 检查是否有引号文本内容（如 heading "北京时间"）
                if '"' in stripped:
                    kept.append('  ' + stripped)
                continue
            # 其他角色：如果带文本引号内容也保留（兜底）
            if '"' in stripped and '[ref=' in stripped:
                kept.append('  ' + stripped)

    elem_text = '\n'.join(kept) if kept else '(未检测到有效内容)'
    compressed_len = len(elem_text)
    note = f"⚙️ 快照已精简：{orig_len} → {compressed_len} 字符，保留 {len(kept)} 个有效节点。"
    return f"{header}\n### 页面内容 (精简快照)\n```yaml\n{elem_text}\n```\n{note}"


# ---------------------------------------------------------------------------
# 2. JS-first 操作层
# ---------------------------------------------------------------------------
#
# 设计原则：
#   Playwright MCP 的 target 参数有两种形态：
#     1. CSS 选择器（如 "#kw", ".btn", "input[name=wd]"）
#        → Playwright 原生操作会做 actionability check（可见、可交互等），
#          在有 overlay/弹窗/非标准 UI 的页面上极易 Timeout。
#        → 用 JS 操作可以绕过这些检查，更可靠。
#
#     2. Playwright ref（如 "e36", "searchbox42"）
#        → 这是 Playwright 内部的 accessibility tree 映射，DOM 上没有对应属性，
#          JS 的 querySelector 无法解析。
#        → 只有 Playwright 自己能解析，必须透传给 MCP 原生处理。
#
#   判断规则很简单：包含 CSS 特殊字符（#.[]>~+:）→ CSS 选择器 → 拦截走 JS。
#   否则 → 可能是 ref → 不拦截，透传 MCP。
#
# Playwright MCP 参数 schema（官方文档）：
#   browser_click:         { target, element? }
#   browser_type:          { target, text, element?, submit?, slowly? }
#   browser_fill_form:     { fields: [{target, value, ...}] }
#   browser_select_option: { target, values, element? }
#   browser_evaluate:      { function }

# CSS 选择器特征字符：出现任意一个就确定是 CSS 选择器
_CSS_CHARS = frozenset('#.[]>~+:')


def _has_css_chars(target: str) -> bool:
    """判断 target 是否为 CSS 选择器（而非 Playwright ref）。"""
    return any(c in _CSS_CHARS for c in target)


def _escape_js(s: str) -> str:
    """转义字符串用于嵌入 JS 单引号字面量。"""
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('`', '\\`').replace('\n', '\\n').replace('\r', '\\r')


def _js_click(kwargs: dict) -> str:
    """生成 browser_click 的 JS 替代脚本。"""
    sel = _escape_js(kwargs.get('target', ''))
    return (
        f"() => {{"
        f"  var el = document.querySelector('{sel}');"
        f"  if (!el) return 'ERROR: element not found for: {sel}';"
        f"  el.scrollIntoView({{block:'center'}});"
        f"  el.click();"
        f"  return 'OK: clicked ' + (el.tagName || '') + ' ' + (el.id || el.className || '').substring(0, 50);"
        f"}}"
    )


def _js_type(kwargs: dict) -> str:
    """生成 browser_type 的 JS 替代脚本，兼容 React/Vue。"""
    sel = _escape_js(kwargs.get('target', ''))
    val = _escape_js(kwargs.get('text', ''))
    submit = kwargs.get('submit', False)
    # form.submit() 不触发 submit 事件，SPA 路由会丢失；改用模拟回车
    submit_js = (
        "el.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', bubbles:true}));"
    ) if submit else ""
    return (
        f"() => {{"
        f"  var el = document.querySelector('{sel}');"
        f"  if (!el) return 'ERROR: element not found for: {sel}';"
        f"  el.focus();"
        # 使用 native setter 绕过 React/Vue 对 value 属性的劫持
        f"  var ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')"
        f"    || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');"
        f"  if (ns && ns.set) ns.set.call(el, '{val}');"
        f"  else el.value = '{val}';"
        f"  el.dispatchEvent(new Event('input', {{bubbles:true}}));"
        f"  el.dispatchEvent(new Event('change', {{bubbles:true}}));"
        f"  {submit_js}"
        f"  return 'OK: typed \\'{val}\\' into ' + (el.id || el.name || el.tagName);"
        f"}}"
    )


def _js_fill_form(kwargs: dict) -> str:
    """生成 browser_fill_form 的 JS 替代脚本。"""
    fields = kwargs.get('fields', [])
    if not fields:
        return "() => 'ERROR: no fields provided'"

    fill_stmts = []
    for f in fields:
        sel = _escape_js(f.get('target', ''))
        val = _escape_js(str(f.get('value', '')))
        fill_stmts.append(
            f"(function() {{"
            f"  var el = document.querySelector('{sel}');"
            f"  if (!el) return 'MISS:{sel}';"
            f"  el.focus();"
            f"  var ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')"
            f"    || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');"
            f"  if (ns && ns.set) ns.set.call(el, '{val}');"
            f"  else el.value = '{val}';"
            f"  el.dispatchEvent(new Event('input', {{bubbles:true}}));"
            f"  el.dispatchEvent(new Event('change', {{bubbles:true}}));"
            f"  return 'OK:{sel}';"
            f"}})()"
        )

    joined = ", ".join(fill_stmts)
    return f"() => {{ var results = [{joined}]; return results.join('; '); }}"


def _js_select_option(kwargs: dict) -> str:
    """生成 browser_select_option 的 JS 替代脚本。"""
    sel = _escape_js(kwargs.get('target', ''))
    values = kwargs.get('values', [])
    target_val = _escape_js(values[0] if values else '')
    return (
        f"() => {{"
        f"  var el = document.querySelector('{sel}');"
        f"  if (!el) return 'ERROR: element not found for: {sel}';"
        f"  var tv = '{target_val}';"
        f"  for (var i = 0; i < el.options.length; i++) {{"
        f"    if (el.options[i].value === tv || el.options[i].text === tv) {{"
        f"      el.selectedIndex = i; break;"
        f"    }}"
        f"  }}"
        f"  el.dispatchEvent(new Event('change', {{bubbles:true}}));"
        f"  return 'OK: selected \\'{target_val}\\'';"
        f"}}"
    )


# 拦截器注册表：tool_name → (kwargs) -> JS function string
_JS_INTERCEPTORS = {
    'browser_click': _js_click,
    'browser_type': _js_type,
    'browser_fill_form': _js_fill_form,
    'browser_select_option': _js_select_option,
}


def _should_intercept(name: str, kwargs: dict) -> bool:
    """
    判断是否应该拦截当前工具调用走 JS 路径。
    只有当 target 是 CSS 选择器时才拦截；Playwright ref 必须透传给 MCP。
    """
    if name not in _JS_INTERCEPTORS:
        return False

    if name == 'browser_fill_form':
        # fill_form 的 target 在 fields 数组内，只要所有 field 的 target 都是 CSS 选择器才拦截
        fields = kwargs.get('fields', [])
        return bool(fields) and all(_has_css_chars(f.get('target', '')) for f in fields)

    # 其他工具直接检查顶层 target
    return _has_css_chars(kwargs.get('target', ''))


# ---------------------------------------------------------------------------
# 3. MCP 桥接层（懒加载 + 闲置自动销毁版）
# ---------------------------------------------------------------------------

# 闲置超时：20 分钟无调用后自动关闭 Node.js 进程
_IDLE_TIMEOUT = 1200
# Schema 缓存文件（与本文件同目录，首次拉取后持久化，后续启动秒级读取）
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "playwright_schema.json")

# 模块级状态，生命周期与 Python 进程一致
_state: dict = {
    "loop": None,        # 当前 MCP 事件循环
    "session": None,     # 当前 MCP ClientSession
    "is_running": False, # Node.js 进程是否在线
    "idle_timer": None,  # 闲置倒计时句柄
    "stop_future": None, # asyncio.Future，取消即触发优雅关闭
}
_state_lock = threading.Lock()


# --- 内部生命周期管理 ---

def _cancel_idle_timer() -> None:
    """取消当前倒计时（调用方须持有 _state_lock）。"""
    t = _state["idle_timer"]
    if t is not None:
        t.cancel()
        _state["idle_timer"] = None


def _reset_idle_timer() -> None:
    """重置 20 分钟倒计时（调用方须持有 _state_lock）。"""
    _cancel_idle_timer()
    t = threading.Timer(_IDLE_TIMEOUT, _shutdown_mcp)
    t.daemon = True
    t.start()
    _state["idle_timer"] = t


def _shutdown_mcp() -> None:
    """闲置超时回调：优雅关闭 Node.js 进程并重置状态。"""
    with _state_lock:
        if not _state["is_running"]:
            return
        logger.info("🛑 [懒加载] Playwright MCP 闲置 20 分钟，正在自动销毁...")
        _cancel_idle_timer()
        stop_future = _state["stop_future"]
        loop = _state["loop"]
        if stop_future is not None and loop is not None and loop.is_running():
            # 在事件循环线程内取消 Future，触发 async with 上下文退出，进而终止 Node.js
            loop.call_soon_threadsafe(stop_future.cancel)
        _state.update({"is_running": False, "session": None, "stop_future": None, "loop": None})
        logger.info("✅ [懒加载] Playwright MCP 进程已销毁，内存已释放")


def _start_mcp_server() -> None:
    """
    启动 MCP 服务。阻塞直到服务就绪（最多 60 秒）。
    调用方须持有 _state_lock。
    """
    loop = asyncio.new_event_loop()
    _state["loop"] = loop
    ready = threading.Event()

    async def _run() -> None:
        cmd = "npx.cmd" if sys.platform == "win32" else "npx"
        p = StdioServerParameters(command=cmd, args=["-y", "@playwright/mcp@latest"])
        async with stdio_client(p) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                _state["session"] = s
                _state["mcp_tools"] = (await s.list_tools()).tools
                stop = loop.create_future()
                _state["stop_future"] = stop
                _state["is_running"] = True
                ready.set()
                try:
                    await stop  # 保持长连接，直到 _shutdown_mcp() 取消此 Future
                except asyncio.CancelledError:
                    pass  # 正常关闭路径

    threading.Thread(
        target=lambda: loop.run_until_complete(_run()),
        daemon=True,
        name="playwright-mcp-loop",
    ).start()

    if not ready.wait(timeout=60):
        _state["is_running"] = False
        raise RuntimeError("启动 Playwright MCP 服务超时（60 秒）")


def _ensure_running() -> None:
    """
    确保 MCP 服务在线；若已销毁则重新唤醒。
    每次工具调用前都调用此函数，同时重置 20 分钟倒计时。
    """
    with _state_lock:
        if not _state["is_running"]:
            logger.info("🔌 [懒加载触发] 正在按需唤醒 Playwright MCP 服务...")
            _start_mcp_server()
            logger.info("✅ [懒加载] Playwright MCP 服务已就绪")
        _reset_idle_timer()


def _load_or_fetch_schema() -> list:
    """
    读取工具 Schema：
    - 已有缓存 → 直接读取文件，不启动 Node.js（毫秒级）
    - 无缓存   → 启动服务拉取 Schema，写入缓存后立即关闭进程
    """
    if os.path.exists(_CACHE_FILE):
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        logger.debug(f"📋 [懒加载] 从缓存读取 {len(cached)} 个 Playwright 工具 Schema")
        return cached

    logger.info("🚀 [首次初始化] 正在拉取 Playwright MCP Schema 并生成缓存...")
    with _state_lock:
        _start_mcp_server()
        tools_raw = _state.get("mcp_tools") or []

    tools_data = []
    for t in tools_raw:
        schema = t.inputSchema
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump()
        elif not isinstance(schema, dict):
            schema = dict(schema)
        tools_data.append({"name": t.name, "description": t.description or "", "inputSchema": schema})

    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(tools_data, f, ensure_ascii=False, indent=2)
    logger.info(f"✅ [首次初始化] Schema 已缓存（{len(tools_data)} 个工具）→ {_CACHE_FILE}")

    # Schema 获取完毕，立即关闭进程；真正的工具调用触发时再重新唤醒
    _shutdown_mcp()
    return tools_data


# --- 底层 MCP 调用 ---

def _call_mcp(name: str, arguments: dict) -> str:
    """底层 MCP 调用，返回结果文本。"""
    f = asyncio.run_coroutine_threadsafe(
        _state["session"].call_tool(name, arguments=arguments), _state["loop"]
    )
    res = f.result(timeout=60)
    if getattr(res, "isError", False):
        err_msg = res.content[0].text if res.content else "未知错误"
        return f"❌ 工具内部报错: {err_msg}"
    if res.content and hasattr(res.content[0], "text"):
        return res.content[0].text
    return str(res.content)


# --- 公开工厂函数 ---

def build_playwright_tools() -> List[ToolDef]:
    cached_tools = _load_or_fetch_schema()

    def make_call(name: str, schema: dict):
        def _call(**kwargs):
            try:
                # 按需唤醒 Node.js（若已销毁则重启），并重置闲置倒计时
                _ensure_running()

                valid_keys = schema.get("properties", {}).keys()
                clean_kwargs = {k: v for k, v in kwargs.items() if k in valid_keys}

                # 修正 LLM 常见的 ref 格式错误：
                # 快照显示 [ref=e36]，LLM 常传 "ref=e36"，但 Playwright 期望 "e36"
                t = clean_kwargs.get("target", "")
                if isinstance(t, str) and t.startswith("ref="):
                    clean_kwargs["target"] = t[4:]
                if name == "browser_fill_form":
                    for field in clean_kwargs.get("fields", []):
                        ft = field.get("target", "")
                        if isinstance(ft, str) and ft.startswith("ref="):
                            field["target"] = ft[4:]

                # CSS 选择器 → 拦截走 JS（绕过 Playwright actionability check）
                # Playwright ref → 透传 MCP（只有 Playwright 能解析 ref）
                if _should_intercept(name, clean_kwargs):
                    interceptor = _JS_INTERCEPTORS[name]
                    js_fn = interceptor(clean_kwargs)
                    result = _call_mcp("browser_evaluate", {"function": js_fn})
                    if "ERROR" not in result:
                        return result
                    # JS 执行失败 → 回退到 MCP 原生调用
                    return _call_mcp(name, clean_kwargs)

                # 非拦截工具 / ref target → 透传 MCP
                result_text = _call_mcp(name, clean_kwargs)
                if name == "browser_snapshot":
                    result_text = _compress_snapshot(result_text)
                return result_text

            except Exception as e:
                return f"❌ 执行异常: {e}"

        return _call

    return [
        ToolDef(
            name=t["name"],
            description=t["description"] or "",
            parameters=t["inputSchema"],
            call=make_call(t["name"], t["inputSchema"]),
        )
        for t in cached_tools
    ]