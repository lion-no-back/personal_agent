import logging
import os
import re
import threading
import time
import json
from typing import Callable, Optional

from ..agents.base_agent import BaseAgent
from ..context.conversation_context import ConversationContext
from ..core.interaction_logger import InteractionLogger
from ..core.llm_router import LLMRouter
from ..core.request_scorer import RequestScorer
from ..core.task_manager import TaskManager
from ..memory import MemoryModule
from ..tools import get_all_tools, get_tools_openai_schema
from ..tools.exec_tool import CommandResult
from .conversation_runtime import is_llm_auth_error as _is_llm_auth_error
from .pipeline import MessagePipeline, PipelineContext

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
    "/remember": "主动记住某件事：/remember <内容>（默认写入经验记忆）",
    "/memories": "查看当前用户的记忆内容",
    "/用量": "查看本人本月至今与上月的 token 用量和费用（别名 /usage）",
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
        llm: LLMRouter,
        context: ConversationContext,
        card_update_interval: Optional[float] = None,
        task_manager: Optional[TaskManager] = None,
        interaction_logger: Optional[InteractionLogger] = None,
        skill_bodies: Optional[dict] = None,
        scorer: Optional[RequestScorer] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
        send_file_fn: Optional[Callable[[str, str, str], None]] = None,
        send_image_text_fn: Optional[Callable[[str, str, str], None]] = None,
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
        self._scorer = scorer
        self._memory: Optional[MemoryModule] = None
        self._notify_fn = notify_fn
        self._send_file_fn = send_file_fn
        self._send_image_text_fn = send_image_text_fn
        self._ondemand_workflows: Optional[list] = None  # lazy-loaded
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

    def set_memory(self, memory: MemoryModule) -> None:
        """注入记忆模块（main.py 初始化完成后调用）。"""
        self._memory = memory
        logger.info("✅ MemoryModule 已接入 ConversationAgent")

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
        cancel_event: Optional[threading.Event] = None,
        keep_thinking: bool = True,
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
        ctx = PipelineContext(
            event=event,
            on_update=on_update,
            stream_updates_sync_with_llm=stream_updates_sync_with_llm,
            push_fn=push_fn,
            cancel_event=cancel_event,
            keep_thinking=keep_thinking,
        )
        return MessagePipeline(self).run(ctx)

    # ──────────────────────────────────────────────────────────
    #   按需 Workflow 意图触发
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_session_key(event) -> str:
        """生成 Agent 感知的会话 key，避免多机器人/多 Agent 共享历史。"""
        base_key = ConversationContext.build_session_key(
            chat_type=event.chat_type,
            sender_open_id=event.sender_open_id,
            chat_id=event.chat_id,
            thread_id=event.thread_id,
        )
        account_id = getattr(event, "account_id", "")
        agent_id = getattr(event, "agent_id", "")
        if account_id and agent_id:
            return f"agent_{agent_id}_dingtalk_{account_id}_{base_key}"
        if agent_id:
            return f"agent_{agent_id}_{base_key}"
        return base_key

    @staticmethod
    def _derive_delivery_key(event) -> str:
        """从任意平台的 event 推导 gateway-compatible 投递 key。"""
        chat_id = getattr(event, "chat_id", "")
        sender_id = getattr(event, "sender_open_id", "")
        chat_type = getattr(event, "chat_type", "p2p")
        account_id = getattr(event, "account_id", "")

        if chat_id.startswith("dingtalk_"):
            if chat_type == "group":
                raw_id = chat_id[len("dingtalk_"):]
                return (
                    f"dingtalk:{account_id}:group:{raw_id}"
                    if account_id
                    else f"dingtalk:group:{raw_id}"
                )
            else:
                raw_id = sender_id[len("dingtalk_"):]
                return (
                    f"dingtalk:{account_id}:user:{raw_id}"
                    if account_id
                    else f"dingtalk:user:{raw_id}"
                )
        else:
            # 飞书：raw ID 兼容 send_text
            return chat_id if chat_type == "group" else sender_id

    def _get_ondemand_workflows(self) -> list:
        """懒加载按需 workflow 列表（首次调用时扫描注册表）。"""
        if self._ondemand_workflows is None:
            try:
                from ..workflows import WorkflowRegistry
                self._ondemand_workflows = WorkflowRegistry().get_ondemand_workflows()
                logger.info("已加载 %d 个按需 workflow", len(self._ondemand_workflows))
            except Exception:
                logger.exception("按需 workflow 加载失败")
                self._ondemand_workflows = []
        return self._ondemand_workflows

    def _check_ondemand_workflow(self, event, session_key: str, on_update) -> Optional[str]:
        """
        检查消息是否命中某个按需 workflow 的 intent_triggers。
        命中则启动后台线程执行，立即返回 ACK 字符串；未命中返回 None。
        """
        content = event.content

        if "工作流使用说明" in content:
            workflows = self._get_ondemand_workflows()
            if not workflows:
                return "当前没有可用的按需工作流。"
            lines = [
                "| 工作流 | 触发词 |",
                "|:---|:---|",
            ]
            for m in workflows:
                triggers = " / ".join(f"**{t}**" for t in m.intent_triggers)
                lines.append(f"| {m.name} | {triggers} |")
            help_text = "\n".join(lines)
            if on_update:
                on_update(help_text, "done")
            return help_text

        for meta in self._get_ondemand_workflows():
            if not any(trigger in content for trigger in meta.intent_triggers):
                continue

            delivery_key = self._derive_delivery_key(event)
            ack = f"⏳ 正在执行【{meta.name}】，请稍候..."
            if on_update:
                on_update(ack, "processing")

            # 捕获触发者（当前 worker 线程的 contextvar），供工作流 usage 回传后归账
            from ..core.usage_tracker import current_user
            usage_user = current_user.get()
            self._run_ondemand_workflow_async(
                meta, delivery_key, content, on_update=on_update, usage_user=usage_user
            )
            return ack

        return None

    def _run_ondemand_workflow_async(self, meta, delivery_key: str, event_content: str = "", on_update=None, usage_user=None) -> None:
        """在后台 daemon 线程中执行按需 workflow，完成后通过 notify_fn/send_file_fn 投递结果。

        工作流在独立子进程里运行（见 workflows/dispatch.py）：主进程不 import 其重原生库
        （numpy/pandas/torch 等），从根上避免多线程并发首次导入的竞态。
        """
        from personal_agent.workflows.dispatch import run_in_subprocess, INSTALL_GRACE_SECONDS

        def _run():
            base_timeout = getattr(meta, "timeout", None)
            result = run_in_subprocess(
                module=f"personal_agent.workflows.{meta.id}.runner",
                function="run",
                content=event_content,
                # 按需入口无外层超时，硬总额 = 执行超时 + 装包宽限（首次现装大依赖用）
                timeout=(base_timeout + INSTALL_GRACE_SECONDS) if base_timeout else None,
            )
            # 工作流子进程回传的 LLM usage，按触发者记账（source=workflow）
            if usage_user is not None:
                try:
                    from ..core.usage_tracker import get_tracker
                    _uid, _name = usage_user
                    _tracker = get_tracker()
                    for u in result.get("usage", []) or []:
                        _tracker.record(
                            user_id=_uid, display_name=_name, source="workflow",
                            model=u.get("model", ""),
                            in_tokens=u.get("in_tokens", 0),
                            out_tokens=u.get("out_tokens", 0),
                            cache_read_tokens=u.get("cache_read_tokens", 0),
                            slug=os.getenv("LLM_REASONING_OPENROUTER_SLUG") or os.getenv("LLM_OPENROUTER_SLUG"),
                        )
                except Exception as _e:
                    logger.warning("[ondemand:%s] 工作流 usage 记账失败 | %s", meta.id, _e)
            success = result.get("ok", False)
            files = result.get("files", [])
            segments = result.get("segments", [])
            if success:
                message = result.get("message") or "✅ 完成"
            elif result.get("error") == "timeout":
                message = result.get("message") or f"❌ 【{meta.name}】执行超时"
            else:
                err = result.get("error") or "未知错误"
                message = result.get("message") or f"❌ 【{meta.name}】执行失败：{err}"

            if message:
                # 优先把结果 finalize 到同一张「处理中」卡片；失败/无回调回退独立文字消息
                delivered = False
                if on_update is not None:
                    try:
                        # 卡片成功终态自带「✅完成」徽标，去掉正文重复的成功 emoji 前缀。
                        # 只剥成功标记（✅✔️☑️）；失败的 ❌/⚠️ 必须保留——否则卡片仍显示
                        # 「完成」徽标、正文又没了失败标记，用户将无法分辨成功还是失败。
                        # 文字降级路径（notify_fn）无徽标，仍用带前缀的原 message。
                        card_message = re.sub(r"^[\s✅✔️☑️]+", "", message)
                        on_update(card_message, "done")
                        delivered = True
                    except Exception:
                        logger.exception("[ondemand:%s] 卡片 finalize 失败，回退文字投递", meta.id)
                if not delivered and self._notify_fn:
                    try:
                        self._notify_fn(delivery_key, message)
                    except Exception:
                        logger.exception("[ondemand:%s] 文字结果投递失败", meta.id)

            if self._send_file_fn and files:
                for fp in files:
                    try:
                        self._send_file_fn(delivery_key, str(fp), fp.name)
                    except Exception:
                        logger.exception("[ondemand:%s] 文件发送失败: %s", meta.id, fp)

            # segments：逐条「图片 + 文字」合并投递（图在前、文字在后，一条消息）。
            # 有图走 send_image_text，无图回退纯文本，确保截图缺失时文字信息不丢。
            if segments:
                for seg in segments:
                    img = seg.get("image")
                    txt = seg.get("text", "")
                    try:
                        if img and self._send_image_text_fn:
                            self._send_image_text_fn(delivery_key, str(img), txt)
                        elif txt and self._notify_fn:
                            self._notify_fn(delivery_key, txt)
                    except Exception:
                        logger.exception("[ondemand:%s] 图文段投递失败: %s", meta.id, img)

            if success and delivery_key and getattr(meta, "persist_delivery", False):
                try:
                    from pathlib import Path as _Path
                    _data_dir = _Path(__file__).resolve().parents[2] / "data"
                    _data_dir.mkdir(exist_ok=True)
                    (_data_dir / f"{meta.id}_delivery_target").write_text(
                        delivery_key, encoding="utf-8"
                    )
                    logger.info("[ondemand:%s] 已持久化投递目标: %s", meta.id, delivery_key)
                except Exception:
                    logger.exception("[ondemand:%s] 持久化投递目标失败", meta.id)

        t = threading.Thread(target=_run, name=f"ondemand-{meta.id}", daemon=True)
        t.start()

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
            result = (self._llm.think(messages, temperature=0, task_type="fast") or "").strip()
            for name, data in self._skill_bodies.items():
                if name.lower() in result.lower():
                    return data.get("body", "")
        except Exception:
            pass
        return ""

    # ──────────────────────────────────────────────────────────
    #   内置指令处理
    # ──────────────────────────────────────────────────────────

    def _handle_command(self, text: str, session_key: str, sender_open_id: Optional[str] = None) -> Optional[str]:
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

        # ── /remember ─────────────────────────────────────────
        if cmd == "/remember":
            return self._cmd_remember(sender_open_id, args)

        # ── /memories ─────────────────────────────────────────
        if cmd == "/memories":
            return self._cmd_memories(sender_open_id)

        # ── /用量 /usage ──────────────────────────────────────
        if cmd in ("/用量", "/usage"):
            return self._cmd_usage()

        return None  # 不是内置指令，正常走 LLM

    def _cmd_usage(self) -> str:
        """返回当前发送人的本月至今 + 上月完整用量与费用。"""
        from ..core.usage_tracker import current_user, get_tracker
        cu = current_user.get()
        if cu is None:
            return "📊 暂无法识别当前用户的用量。"
        user_id, name = cu
        try:
            return get_tracker().format_report(user_id, name)
        except Exception as e:
            logger.warning("用量查询失败 | user=%s err=%s", user_id, e)
            return "📊 用量查询失败，请稍后再试。"

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
    #   /remember 实现
    # ──────────────────────────────────────────────────────────

    def _cmd_remember(self, user_id: Optional[str], content: str) -> str:
        if not content:
            return (
                "⚠️ 请告诉我要记住什么内容。\n\n"
                "用法：`/remember <内容>`\n"
                "例如：`/remember exec 马帮脚本必须先 cd 到 scripts/ 目录`"
            )
        if not self._memory:
            return "⚠️ 记忆模块未启用，无法保存。"
        if not user_id:
            return "⚠️ 无法识别用户身份，无法保存记忆。"

        self._memory.extractor.save_direct(user_id, content)
        return f"✅ **已记住**\n\n> {content}\n\n（已写入经验记忆，下次对话时自动带入）"

    # ──────────────────────────────────────────────────────────
    #   /memories 实现
    # ──────────────────────────────────────────────────────────

    def _cmd_memories(self, user_id: Optional[str]) -> str:
        if not self._memory:
            return "⚠️ 记忆模块未启用。"
        if not user_id:
            return "⚠️ 无法识别用户身份，无法读取记忆。"

        block = self._memory.retriever.build_memory_block(user_id)
        if not block:
            return "📭 当前没有任何记忆内容，继续对话后会自动积累。"

        content = re.sub(r"</?user_memory>\n?", "", block).strip()
        MAX_LEN = 3500
        if len(content) > MAX_LEN:
            content = content[:MAX_LEN] + f"\n\n> ⚠️ 内容过长，已截断（共 {len(content)} 字符）"
        return f"**🧠 当前记忆内容**\n\n{content}"

    # ──────────────────────────────────────────────────────────
    #   后台命令结果格式化（agent 层统一负责，exec_tool 只产出数据）
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _format_command_result(result: CommandResult) -> str:
        cmd_short = result.command.strip()[:40] + ("..." if len(result.command.strip()) > 40 else "")
        if result.rc == 0:
            last_line = next(
                (l.strip() for l in reversed(result.stdout.splitlines()) if l.strip()), ""
            )
            detail = (last_line[:50] + ("..." if len(last_line) > 50 else "")) if last_line else ""
            return f"🔔 ✅ `{cmd_short}` 执行成功" + (f"：{detail}" if detail else "")
        raw_err = result.stderr.strip() or result.stdout.strip()
        snippet = raw_err[:50] + ("..." if len(raw_err) > 50 else "")
        return f"🔔 ❌ `{cmd_short}` 执行失败" + (f"，{snippet}" if snippet else "")

    @staticmethod
    def _make_exec_push_fn(push_fn: "Callable") -> "Callable":
        """包装 push_fn：CommandResult 在 agent 层格式化为字符串，字符串直接透传。"""
        def _push(result) -> None:
            if isinstance(result, CommandResult):
                try:
                    msg = ConversationAgent._format_command_result(result)
                except Exception as e:
                    logger.warning("命令结果格式化失败: %s", e)
                    msg = f"🔔 后台命令完成（rc={result.rc}）"
                push_fn(msg)
            else:
                push_fn(result)
        return _push

    # ──────────────────────────────────────────────────────────
    #   路由提示（边界区时附加在回复末尾）
    # ──────────────────────────────────────────────────────────

    def _routing_suggestion(self, score_result, is_scheduled: bool = False) -> str:
        """评分处于边界区且 reasoning 已配置时，在回复末尾追加切换提示。"""
        if is_scheduled or score_result is None:
            return ""
        if not score_result.is_borderline or score_result.task_type != "default":
            return ""
        model_name = self._llm.get_model_name("reasoning")
        if not model_name:
            return ""
        return (
            f"\n\n---\n"
            f"> 💡 如果回答不够深入，回复「换推理模型」可切换至 **{model_name}** 重新分析。"
        )

    # ──────────────────────────────────────────────────────────
    #   流式 LLM 调用 + 节流卡片更新
    # ──────────────────────────────────────────────────────────

    def _stream_and_update(
        self,
        messages: list,
        on_update: Optional[Callable[[str, str], None]],
        stream_updates_sync_with_llm: bool = False,
        tools: Optional[list] = None,
        task_type: str = "default",
        cancel_event: Optional[threading.Event] = None,
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
                gen = self._llm.think_stream(messages, tools=tools, task_type=task_type)
                for _delta, accumulated, tcs in gen:
                    if cancel_event and cancel_event.is_set():
                        gen.close()
                        return full_response, None
                    full_response = accumulated
                    final_tool_calls = tcs
                    if accumulated:
                        on_update(accumulated, "generating")
                # When flow finishes, if no tools, it's safe to mark 'done'. If there are tools, we still mark generating until final loop.
                if not final_tool_calls:
                    # Send final generating frame; process() sends the authoritative "done".
                    on_update(full_response or "", "generating")
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

                    # ── 写完即停手 ──────────────────────────────────
                    # LLM 一结束就立刻把完整全文推一帧后退出，不再逐帧"补动画"。
                    # 这样打字线程在 done 之后的存活窗口≈0，join 立即返回，
                    # 上层 process() 紧随其后发的 "done" 收尾天然串行在最后一帧之后，
                    # 不会再出现"收尾后迟到 generating 帧把卡片翻回输出中"。
                    # 此处仍发 "generating"（而非 "done"）：权威的 "done" 由 process()
                    # 追加路由提示后统一发送，避免重复 _on_done 导致内容重复。
                    if llm_done:
                        on_update(full_text, "generating")
                        return

                    # LLM 仍在生成：按节奏逐步揭字，做打字机动画
                    if avail > display_idx:
                        display_idx = min(display_idx + self._stream_chars_per_tick, avail)
                        on_update(full_text[:display_idx] + " ▌", "generating")

                    time.sleep(self._card_update_interval)

            _flusher_thread = threading.Thread(
                target=_flusher, daemon=True, name="card-flusher"
            )
            _flusher_thread.start()

        try:
            full_response = ""
            final_tool_calls = None
            for _delta, accumulated, tcs in self._llm.think_stream(messages, tools=tools, task_type=task_type):
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
