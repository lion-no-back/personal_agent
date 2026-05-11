import logging
import os
import re
import threading
import time
import json
import concurrent.futures
from typing import Callable, Optional

from openai import APIStatusError, AuthenticationError

from ..agents.base_agent import BaseAgent
from ..context.conversation_context import ConversationContext
from ..core.interaction_logger import InteractionLogger
from ..core.llm import PersonalAgentLLM
from ..core.task_manager import TaskManager
from ..tools import get_all_tools, get_tools_openai_schema

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
#   内置指令表
#   格式：{指令字符串: 描述}
# ──────────────────────────────────────────────────────────────
_BUILTIN_COMMANDS = {
    "/clear": "清空当前会话的历史记录，重新开始对话",
    "/help": "显示所有可用的内置指令",
    "/status": "查看当前会话的状态信息（会话 Key、历史条数等）",
    "/resume": "列出所有历史会话；/resume <序号> 恢复指定会话上下文",
    "/export": "导出当前会话的完整对话记录",
    "/tasks": "查看后台任务处理状态",
}

_HELP_TEXT = (
    "**PersonalAgent 内置指令**\n\n"
    + "\n".join(f"- `{cmd}`：{desc}" for cmd, desc in _BUILTIN_COMMANDS.items())
    + "\n\n> 直接发送消息即可与 AI 对话，无需任何指令。"
)

_LLM_AUTH_USER_MESSAGE = (
    "⚠️ **无法调用 AI 服务**：接口密钥未通过验证。\n\n"
    "请检查环境变量 <text_tag color='neutral'>LLM_API_KEY</text_tag> 与 "
    "<text_tag color='neutral'>LLM_BASE_URL</text_tag> 是否属于同一服务商且填写正确，"
    "修改后需重启服务。"
)


def _is_llm_auth_error(exc: BaseException) -> bool:
    """OpenAI SDK：401 通常为 AuthenticationError（亦为 APIStatusError 子类）。"""
    if isinstance(exc, AuthenticationError):
        return True
    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None) == 401
    return False


class ConversationAgent(BaseAgent):
    """
    对话 Agent —— 核心业务逻辑层。

    职责：
    1. 识别并响应内置指令（/clear、/help 等）
    2. 将用户消息追加到对应会话的上下文
    3. 组装含历史记录的 messages 列表并传给 LLM
    4. 驱动流式 LLM 调用，通过 on_update 回调实时推送卡片更新
    5. 将 AI 回复写回上下文，供后续轮次使用

    解耦设计：
    - 不直接引用任何飞书 SDK 类，平台细节由 Gateway 层处理
    - on_update 回调由 Gateway 层注入，Agent 只负责何时调用
    - card_update_interval 控制回调频率（旧版 PATCH 卡片；飞书 CardKit 流式模式应使用
      stream_updates_sync_with_llm=True，由客户端按 print_step 做打字机）
    """

    def __init__(
        self,
        llm: PersonalAgentLLM,
        context: ConversationContext,
        card_update_interval: Optional[float] = None,
        task_manager: Optional[TaskManager] = None,
        interaction_logger: Optional[InteractionLogger] = None,
        skill_bodies: Optional[dict] = None,
    ):
        """
        Args:
            llm:                   已初始化的 LLM 客户端
            context:               会话上下文管理器
            card_update_interval:  打字机刷新间隔（秒），默认读取环境变量
                                   CARD_UPDATE_INTERVAL，再回退到 0.3 秒
            stream_chars_per_tick: 每帧揭露的字符数，默认读取环境变量
                                   STREAM_CHARS_PER_TICK，再回退到 10 个字符
            task_manager:          后台任务管理器，为 None 时 /tasks 指令提示未启用
        """
        self._llm = llm
        self._context = context
        self._task_manager = task_manager
        self._interaction_logger = interaction_logger
        self._skill_bodies: dict = skill_bodies or {}
        self._card_update_interval = card_update_interval or float(
            os.getenv("CARD_UPDATE_INTERVAL", "0.3")
        )
        self._stream_chars_per_tick = int(os.getenv("STREAM_CHARS_PER_TICK", "10"))

        # 工具集预加载缓冲
        self._tool_list = get_all_tools()
        self._tools_schema = get_tools_openai_schema()
        self._tool_map = {t.name: t for t in self._tool_list}

        logger.info(
            f"✅ ConversationAgent 初始化完成 | "
            f"card_update_interval={self._card_update_interval}s | "
            f"stream_chars_per_tick={self._stream_chars_per_tick} | "
            f"task_manager={'启用' if task_manager else '未启用'} | "
            f"已加载 {len(self._tool_list)} 个外挂工具 | "
            f"已注册 {len(self._skill_bodies)} 个技能文档（按需注入）"
        )

    # ──────────────────────────────────────────────────────────
    #   BaseAgent 实现
    # ──────────────────────────────────────────────────────────

    def process(
        self,
        event,
        on_update: Optional[Callable[[str, str], None]] = None,
        *,
        stream_updates_sync_with_llm: bool = False,
        push_fn: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        处理一条消息事件的完整链路：
        指令识别 → 上下文读写 → LLM 流式推理 → 卡片更新 → 历史保存

        Args:
            event:     MessageEvent 实例（由 FeishuGateway 解析）
            on_update: 卡片更新回调 on_update(content, status)
                       - status: "generating" | "done" | "error"

        Returns:
            AI 生成的完整回复文本；指令场景直接返回指令响应文本
        """
        # ── 1. 计算会话 Key ──────────────────────────────────
        session_key = ConversationContext.build_session_key(
            chat_type=event.chat_type,
            sender_open_id=event.sender_open_id,
            chat_id=event.chat_id,
            thread_id=event.thread_id,
        )

        logger.info(
            f"📨 处理消息 | message_id={event.message_id} "
            f"session={session_key} content={event.content[:50]!r}"
        )

        if self._interaction_logger:
            self._interaction_logger.log_session_start(session_key, event.content)

        # ── 2. 内置指令拦截 ──────────────────────────────────
        command_response = self._handle_command(event.content, session_key)
        if command_response is not None:
            # 指令直接返回，不走 LLM，也不写入上下文历史
            if on_update:
                on_update(command_response, "done")
            return command_response

        # ── 3. 写入用户消息到上下文 ──────────────────────────
        self._context.add_message(session_key, "user", event.content)

        final_response = ""
        ui_text_buffer = ""  # UI历史累计区，确保多轮迭代输出不会覆写先前的文字
        
        turns_base = int(os.getenv("AGENT_MAX_TURNS_BASE", "5"))
        turns_per_tool = int(os.getenv("AGENT_TURNS_PER_TOOL", "2"))
        turns_hard = int(os.getenv("AGENT_MAX_TURNS_HARD", "30"))
        remaining = turns_base
        i = 0
        while remaining > 0 and i < turns_hard:
            # ── 4. 组装完整 messages（含历史） ───────────────────
            messages = self._context.get_messages(session_key)
            
            # ── 4.5. 瞬态注入文件上下文 ─────
            file_ctx = getattr(event, "file_context", None)
            if file_ctx and messages and messages[-1]["role"] == "user":
                last_msg = dict(messages[-1])
                last_msg["content"] = f"{file_ctx}\n\n[用户请求]\n{last_msg['content']}"
                messages[-1] = last_msg

            # ── 4.6. 瞬态注入技能完整文档（仅首轮、非定时任务时触发一次）─────
            # i==0：工具调用后续轮不重复识别；is_scheduled：定时任务 prompt 已知，跳过
            skill_body = (
                self._resolve_skill_body(event.content)
                if i == 0 and not getattr(event, "is_scheduled", False)
                else ""
            )
            if skill_body and messages and messages[-1]["role"] == "user":
                last_msg = dict(messages[-1])
                last_msg["content"] = f"【当前技能完整文档】\n{skill_body}\n\n{last_msg['content']}"
                messages[-1] = last_msg

            i += 1
            remaining -= 1
            logger.debug(f"LLM 输入 | session={session_key} 轮次={i} 剩余={remaining} messages_count={len(messages)}")

            # 定义携带 buffer 的专属卡片刷入函数
            def custom_on_update(content: str, status: str) -> None:
                if on_update:
                    combined = ui_text_buffer + content
                    on_update(combined.strip(), status)

            # ── 5. 流式 LLM 调用 + 卡片更新 ─────────────────────
            if self._interaction_logger:
                self._interaction_logger.log_llm_call(session_key, i, messages)
            full_response, tool_calls = self._stream_and_update(
                messages, custom_on_update, stream_updates_sync_with_llm,
                tools=self._tools_schema
            )
            if self._interaction_logger:
                self._interaction_logger.log_llm_response(session_key, i, full_response, tool_calls)

            # ── 6. 写入 AI 回复到上下文 ──────────────────────────
            if full_response or tool_calls:
                self._context.add_message(
                    session_key, 
                    "assistant", 
                    content=full_response, 
                    tool_calls=tool_calls if tool_calls else None
                )
            else:
                logger.warning(f"⚠️  LLM 返回空响应 | session={session_key}")
                return ""
                
            final_response = full_response or ""

            if tool_calls:
                # 产生工具调用了，在此轮思考末尾做一点格式美化和保留
                additional_ui_text = (final_response + "\n\n") if final_response else ""
                
                # 并发执行的子例程
                def execute_single_tool(tc: dict) -> dict:
                    c_func_name = tc.get("function", {}).get("name", "")
                    c_func_args_str = tc.get("function", {}).get("arguments", "{}")
                    c_tc_id = tc.get("id", "")
                    log_args: dict = {}

                    c_tool = self._tool_map.get(c_func_name)
                    if not c_tool:
                        res = f"Error: 找不到工具 '{c_func_name}'"
                    else:
                        try:
                            kwargs_dict = json.loads(c_func_args_str)
                            log_args = kwargs_dict.copy()
                            # 注入上下文供支持后台通知的工具（如 exec）使用
                            if push_fn is not None:
                                kwargs_dict["_tool_ctx"] = {"push_fn": push_fn}
                            res = c_tool.call(**kwargs_dict)
                        except Exception as e:
                            res = f"Error: 执行工具 '{c_func_name}' 失败: {str(e)}"
                    return {"name": c_func_name, "id": c_tc_id, "result": res, "args": log_args}

                # 拼接并发提示
                func_names = [tc.get("function", {}).get("name", "") for tc in tool_calls]
                status_text = f"{additional_ui_text}> 🛠️ 正在执行工具：`{', '.join(func_names)}`..."
                custom_on_update(status_text, "generating")
                
                results_collected = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tool_calls), 5)) as executor:
                    futures = [executor.submit(execute_single_tool, tc) for tc in tool_calls]
                    for fut in concurrent.futures.as_completed(futures):
                        results_collected.append(fut.result())

                for res in results_collected:
                    self._context.add_message(session_key, "tool", content=res["result"], tool_call_id=res["id"], name=res["name"])
                    logger.info(f"🛠️ 工具执行完成 | {res['name']} -> {res['result'][:30]}...")
                    if self._interaction_logger:
                        self._interaction_logger.log_tool_result(session_key, res["name"], res.get("args", {}), res["result"])

                # 每个工具调用追加预算，但受硬上限约束
                bonus = turns_per_tool * len(tool_calls)
                remaining = min(remaining + bonus, turns_hard - i)
                logger.debug(f"工具调用追加预算 +{bonus} | 剩余={remaining} | i={i} hard={turns_hard}")

                ui_text_buffer += additional_ui_text
                continue  # 进入下一轮思考
                
            else:
                logger.info(f"✅ 回复完成 | session={session_key} response_len={len(final_response)}")
                ui_text_buffer += final_response
                if self._interaction_logger:
                    self._interaction_logger.log_final(session_key, ui_text_buffer)
                if on_update:
                    on_update(ui_text_buffer.strip(), "done")
                return ui_text_buffer

        timeout_msg = "\n\n> ⏰ 抱歉，经过多次尝试未能完成请求，请尝试重组您的问题。"
        ui_text_buffer += final_response + timeout_msg
        if self._interaction_logger:
            self._interaction_logger.log_final(session_key, ui_text_buffer)
        if on_update:
            on_update(ui_text_buffer.strip(), "done")
        self._context.add_message(session_key, "assistant", content=timeout_msg)
        return ui_text_buffer

    # ──────────────────────────────────────────────────────────
    #   技能正文按需匹配
    # ──────────────────────────────────────────────────────────

    def _resolve_skill_body(self, user_text: str) -> str:
        """
        通过 LLM 语义判断用户消息是否匹配某个技能。

        发起一次独立的非流式 LLM 调用（temperature=0），仅请求返回技能名或 none。
        命中则返回该技能完整 body，供瞬态注入当次消息；未命中返回空字符串。
        调用异常时静默降级，不影响主流程。
        """
        if not self._skill_bodies:
            return ""

        skill_list = "\n".join(
            f"- {name}: {data['description']}"
            for name, data in self._skill_bodies.items()
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是意图识别助手，判断用户消息是否明确匹配以下技能之一。\n"
                    "规则：只回复技能名称（必须与列表完全一致），或回复 none。"
                    "不得输出任何解释或多余内容。\n\n"
                    f"可用技能：\n{skill_list}"
                ),
            },
            {"role": "user", "content": user_text},
        ]
        try:
            result = (self._llm.think(messages, temperature=0) or "").strip()
            for name, data in self._skill_bodies.items():
                if name.lower() in result.lower():
                    return data.get("body", "")
        except Exception:
            pass
        return ""

    # ──────────────────────────────────────────────────────────
    #   内置指令处理
    # ──────────────────────────────────────────────────────────

    def _handle_command(self, text: str, session_key: str) -> Optional[str]:
        """
        识别并执行内置指令，支持带参数的指令（如 /resume 2）。

        Args:
            text:        用户消息文本（已去除 @提及）
            session_key: 当前会话 Key

        Returns:
            指令响应文本；如果不是内置指令则返回 None
        """
        # 分离指令名和参数："/resume 2" → cmd="/resume", args="2"
        parts = text.strip().split(None, 1)
        if not parts:
            return None
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        # ── /clear ────────────────────────────────────────────
        if cmd == "/clear":
            self._context.clear(session_key)
            return (
                "✅ **会话历史已清空**\n\n我们可以重新开始对话了，有什么我可以帮你的？"
            )

        # ── /help ─────────────────────────────────────────────
        if cmd == "/help":
            return _HELP_TEXT

        # ── /status ───────────────────────────────────────────
        if cmd == "/status":
            history_len = self._context.get_history_length(session_key)
            max_history = self._context._max_history
            running = (
                self._task_manager.running_count() if self._task_manager else "N/A"
            )
            return (
                f"**📊 当前会话状态**\n\n"
                f"- 会话 Key：`{session_key}`\n"
                f"- 历史消息：{history_len} / {max_history} 条\n"
                f"- 卡片更新间隔：{self._card_update_interval}s\n"
                f"- 运行中任务：{running}\n"
            )

        # ── /resume ───────────────────────────────────────────
        if cmd == "/resume":
            return self._cmd_resume(session_key, args)

        # ── /export ───────────────────────────────────────────
        if cmd == "/export":
            return self._cmd_export(session_key)

        # ── /tasks ────────────────────────────────────────────
        if cmd == "/tasks":
            return self._cmd_tasks()

        return None  # 不是内置指令，正常走 LLM

    # ──────────────────────────────────────────────────────────
    #   /resume 实现
    # ──────────────────────────────────────────────────────────

    def _cmd_resume(self, session_key: str, args: str) -> str:
        """
        /resume        → 列出所有历史会话（带序号、消息数、预览）
        /resume <n>    → 将第 n 个会话的历史复制到当前会话
        /resume <key>  → 按 session_key 名称恢复
        """
        all_sessions = self._context.list_sessions()

        if not all_sessions:
            return "📭 当前没有任何历史会话记录。"

        # 无参数：列出所有会话
        if not args:
            lines = [f"**📋 历史会话列表**（共 {len(all_sessions)} 个）\n"]
            for i, sk in enumerate(all_sessions, 1):
                count = self._context.get_history_length(sk)
                preview = self._context.get_session_preview(sk)
                marker = " ← **当前**" if sk == session_key else ""
                lines.append(f"**{i}.** `{sk}`{marker}")
                lines.append(f"   📝 {count} 条消息 | {preview}")
            lines.append("\n> 发送 `/resume <序号>` 恢复指定会话的上下文到当前会话")
            return "\n".join(lines)

        # 有参数：先尝试解析为序号
        try:
            idx = int(args) - 1
            if not (0 <= idx < len(all_sessions)):
                return (
                    f"⚠️ 序号 `{args}` 超出范围，共有 {len(all_sessions)} 个会话。\n"
                    f"发送 `/resume` 查看完整列表。"
                )
            source_key = all_sessions[idx]
        except ValueError:
            # 尝试作为 session_key 名称匹配
            if args not in all_sessions:
                return f"⚠️ 未找到会话 `{args}`。\n发送 `/resume` 查看所有可用会话。"
            source_key = args

        if source_key == session_key:
            return "⚠️ 选中的就是当前会话，无需恢复。"

        count = self._context.copy_session(source_key, session_key)
        return (
            f"✅ **会话已恢复**\n\n"
            f"已从 `{source_key}` 导入 **{count}** 条历史消息到当前会话。\n"
            f"现在可以继续之前的对话了。"
        )

    # ──────────────────────────────────────────────────────────
    #   /export 实现
    # ──────────────────────────────────────────────────────────

    def _cmd_export(self, session_key: str) -> str:
        """
        将当前会话的历史消息格式化为 Markdown 文本并返回。
        超过 3800 字符时截断并提示。
        """
        messages = self._context.get_messages(session_key)
        # 过滤掉系统提示词，只导出对话历史
        history = [m for m in messages if m["role"] != "system"]

        if not history:
            return "📭 当前会话没有任何对话记录。"

        role_label = {"user": "👤 用户", "assistant": "🤖 Assistant"}

        lines = [
            "**📄 会话导出**",
            f"会话 Key：`{session_key}`  |  共 {len(history)} 条消息",
            "",
            "---",
        ]

        for msg in history:
            label = role_label.get(msg["role"], msg["role"])
            content = msg["content"].strip()
            lines.append(f"\n**{label}**\n\n{content}\n\n---")

        result = "\n".join(lines)

        # 飞书卡片 lark_md 内容上限约 4000 字符，保守取 3800
        MAX_LEN = 3800
        if len(result) > MAX_LEN:
            result = result[:MAX_LEN] + (
                f"\n\n> ⚠️ 内容已截断（总计 {len(result)} 字符，"
                f"仅显示前 {MAX_LEN} 字符）"
            )

        return result

    # ──────────────────────────────────────────────────────────
    #   /tasks 实现
    # ──────────────────────────────────────────────────────────

    def _cmd_tasks(self) -> str:
        """
        列出所有后台任务（运行中优先，再按创建时间倒序）。
        """
        if self._task_manager is None:
            return "⚠️ 任务管理器未启用，请检查 main.py 的初始化配置。"

        tasks = self._task_manager.list_tasks()

        if not tasks:
            return "📋 **后台任务**\n\n当前没有任何任务记录。"

        _STATUS_ICON = {
            "running": "⏳",
            "done": "✅",
            "error": "❌",
        }

        running = sum(1 for t in tasks if t.status == "running")
        lines = [f"**📋 后台任务**（{running} 运行中 / 共 {len(tasks)} 条）\n"]

        for task in tasks:
            icon = _STATUS_ICON.get(task.status, "❓")
            elapsed = task.elapsed
            if elapsed < 60:
                elapsed_str = f"{elapsed:.1f}s"
            else:
                elapsed_str = f"{elapsed / 60:.1f}min"

            desc = task.description
            if len(desc) > 30:
                desc = desc[:30] + "..."

            session_short = task.session_key.split("_", 1)[-1][:12]
            error_info = f"\n   ⚠️ {task.error[:60]}" if task.error else ""

            lines.append(
                f"{icon} `{task.task_id}` [{session_short}] {desc} | {elapsed_str}{error_info}"
            )

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────
    #   流式 LLM 调用 + 节流卡片更新
    # ──────────────────────────────────────────────────────────

    def _stream_and_update(
        self,
        messages: list,
        on_update: Optional[Callable[[str, str], None]],
        stream_updates_sync_with_llm: bool = False,
        tools: Optional[list] = None,
    ) -> tuple[str, Optional[list]]:
        """
        调用 LLM 流式接口，通过后台 flusher 线程解耦 token 消费与卡片刷新。

        架构说明：
        - 主流程（当前工作线程）：从 think_stream() 消费 token，写入共享 buffer，
          完全不阻塞于任何 HTTP 调用。
        - card-flusher 线程：每隔 card_update_interval 秒读取 buffer，
          调用 on_update 触发平台更新；与主流程完全解耦。
        - stream_updates_sync_with_llm=True 时：不启用 flusher，在 LLM 流式循环内
          每次收到累计文本即 on_update(..., "generating")，结束时 on_update(..., "done")，
          供飞书 CardKit 流式接口推送全文并由客户端逐字渲染。
        - 两者通过 threading.Event 同步生命周期：主流程结束后 set() 通知
          flusher 做最终一次刷新并退出。

        节流逻辑（均在 flusher 中完成，主流程零感知）：
        - 每隔 card_update_interval 秒触发一次 on_update("generating")（带光标 ▌）
        - 流式结束时必定触发一次 on_update("done")（无光标，最终完整内容）
        - 若发生异常，触发 on_update("error")；API 密钥无效（401）时不再向上抛出
        - 其他异常仍向上抛出，由 Gateway 层统一处理

        Args:
            messages:  完整的 messages 列表（含系统提示 + 历史 + 当前用户消息）
            on_update: 卡片更新回调，可为 None（测试 / 纯文本模式）

        Returns:
            完整的 LLM 响应文本；认证失败时为空字符串

        Raises:
            Exception: 非认证类 LLM 调用异常
        """
        # ── 共享状态（CPython GIL 保证简单赋值的原子性） ────────
        _buf: dict = {"text": "", "error": None, "has_tools": False}
        _stop = threading.Event()
        _has_content = threading.Event()  # 首个 token 到达时 set，唤醒 flusher 推首帧
        _flusher_thread: Optional[threading.Thread] = None

        if on_update and stream_updates_sync_with_llm:
            try:
                full_response = ""
                final_tool_calls = None
                for _delta, accumulated, tcs in self._llm.think_stream(messages, tools=tools):
                    full_response = accumulated
                    final_tool_calls = tcs
                    if accumulated:
                        on_update(accumulated, "generating")
                # When flow finishes, if no tools, it's safe to mark 'done'. If there are tools, we still mark generating until final loop.
                if final_tool_calls:
                    pass  # Keep it generating because tools will be called.
                else:
                    on_update(full_response or "", "done")
                return full_response, final_tool_calls
            except Exception as e:
                if _is_llm_auth_error(e):
                    logger.error(
                        "❌ LLM API 认证失败（请检查 LLM_API_KEY / LLM_BASE_URL）：%s",
                        e,
                        exc_info=False,
                    )
                    on_update(_LLM_AUTH_USER_MESSAGE, "error")
                    return "", None
                error_msg = (
                    f"抱歉，AI 调用过程中出现了错误，请稍后重试。\n\n> 错误信息：`{e}`"
                )
                on_update(error_msg, "error")
                logger.error(f"❌ LLM 流式调用失败：{e}", exc_info=True)
                raise

        if on_update:

            def _flusher() -> None:
                """
                打字机刷新线程：以固定节奏逐步揭露 buffer 中的文本。

                核心设计：
                - 用 time.sleep(interval) 替代 _stop.wait(timeout=interval)。
                  _stop.wait 一旦被 stop 信号唤醒就立即返回，导致快速 LLM
                  完成时中间帧全部被跳过（一次性出现全文）；time.sleep 则
                  严格等满 interval，LLM 完成后仍按节奏继续揭字，直到追上。
                - display_idx 是"已揭露到第几个字符"的游标，每帧前进
                  stream_chars_per_tick 步，与 buffer 实际写入进度解耦。
                - LLM 完成（_stop set）且剩余字符过多时自动加速，保证
                  最多再经过 5 帧就能揭完并发出 "done"，不会长时间占用线程。
                """
                display_idx = 0

                # 等待首个 token（认证失败时由 finally 负责 set 以解除阻塞）
                _has_content.wait()

                while True:
                    full_text = _buf["text"]
                    err = _buf["error"]

                    # 错误路径：立即推送错误信息并退出
                    if err is not None:
                        on_update(err, "error")
                        return

                    llm_done = _stop.is_set()
                    avail = len(full_text)

                    if avail > display_idx:
                        step = self._stream_chars_per_tick
                        if llm_done:
                            # LLM 已结束：加速步长，剩余字符 ≤5 帧内揭完
                            remaining = avail - display_idx
                            step = max(step, (remaining + 4) // 5)

                        display_idx = min(display_idx + step, avail)
                        visible = full_text[:display_idx]
                        caught_up = display_idx >= avail

                        if llm_done and caught_up:
                            final_status = "generating" if _buf.get("has_tools") else "done"
                            on_update(visible, final_status)
                            return
                        else:
                            on_update(visible + " ▌", "generating")

                    elif llm_done:
                        # display_idx 已追上 buffer 且 LLM 已结束
                        final_status = "generating" if _buf.get("has_tools") else "done"
                        on_update(full_text, final_status)
                        return
                    # else: LLM 仍在生成，buffer 暂无新内容，等下一帧

                    time.sleep(self._card_update_interval)

            _flusher_thread = threading.Thread(
                target=_flusher, daemon=True, name="card-flusher"
            )
            _flusher_thread.start()

        try:
            full_response = ""
            final_tool_calls = None
            for _delta, accumulated, tcs in self._llm.think_stream(messages, tools=tools):
                full_response = accumulated
                final_tool_calls = tcs
                if tcs:
                    _buf["has_tools"] = True
                if accumulated:
                    # 非阻塞写入共享 buffer，flusher 会自行读取
                    _buf["text"] = accumulated
                # 无论是否有文本，首次调用时唤醒 flusher 推送首帧
                _has_content.set()

            # 由于有可能是纯 tools 的结果（accumulated 为空），如果没设文本但也结束了
            _buf["text"] = full_response
            return full_response, final_tool_calls

        except Exception as e:
            if _is_llm_auth_error(e):
                logger.error(
                    "❌ LLM API 认证失败（请检查 LLM_API_KEY / LLM_BASE_URL）：%s",
                    e,
                    exc_info=False,
                )
                _buf["error"] = _LLM_AUTH_USER_MESSAGE
                return "", None

            error_msg = (
                f"抱歉，AI 调用过程中出现了错误，请稍后重试。\n\n> 错误信息：`{e}`"
            )
            _buf["error"] = error_msg
            logger.error(f"❌ LLM 流式调用失败：{e}", exc_info=True)
            raise

        finally:
            # 无论正常结束还是异常，都通知 flusher 执行最终刷新并等待其退出。
            # _has_content.set() 确保 flusher 不会永久阻塞在阶段 1 的 wait()
            # （例如 LLM 在产生任何 token 之前就抛出认证异常的情况）。
            if _flusher_thread is not None:
                _has_content.set()
                _stop.set()
                _flusher_thread.join(timeout=30)
