import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..core.task_manager import TaskManager

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
#   消息事件数据模型
# ──────────────────────────────────────────────────────────────


@dataclass
class MessageEvent:
    """
    结构化的飞书消息事件，由 FeishuGateway 解析后传递给 Agent 层。

    字段说明：
    - message_id:      飞书消息唯一 ID（om_xxx），用于去重和作为回复目标
    - chat_id:         会话/群聊 ID（oc_xxx 或 p2p_xxx）
    - chat_type:       "p2p"（私聊）或 "group"（群聊）
    - content:         消息纯文本，已去除 @机器人 等提及占位符
    - sender_open_id:  消息发送者的 open_id（ou_xxx）
    - thread_id:       消息所在话题 ID，群聊话题模式下存在（可选）
    """

    message_id: str
    chat_id: str
    chat_type: str
    content: str
    sender_open_id: str
    thread_id: Optional[str] = field(default=None)


# ──────────────────────────────────────────────────────────────
#   卡片配置常量
# ──────────────────────────────────────────────────────────────

# 卡片标题与颜色主题，对应 3 种稳定状态 + 1 种错误状态
_CARD_CONFIG = {
    "thinking": {"title": "🤔 思考中...", "color": "blue"},
    "generating": {"title": "⚡ 生成中...", "color": "turquoise"},
    "done": {"title": "✅ 完成", "color": "green"},
    "error": {"title": "❌ 出错了", "color": "red"},
}


# ──────────────────────────────────────────────────────────────
#   飞书网关
# ──────────────────────────────────────────────────────────────


class FeishuGateway:
    """
    飞书机器人网关 —— 平台接入层。

    职责：
    1. 维护与飞书服务器的 WebSocket 长连接，接收消息推送事件
    2. 解析事件，进行 @提及过滤、消息去重、内容清洗
    3. 将有效消息异步分发给 Agent 层处理（ThreadPool，不阻塞事件回调）
    4. 管理飞书交互卡片的生命周期：创建"思考中"卡片 → 流式更新 → 完成/错误

    两个飞书客户端：
    - _api_client:  lark.Client        主动调用飞书 REST API（发消息、更新卡片）
    - _ws_client:   lark.ws.Client     被动接收飞书事件推送（WebSocket 长连接）

    依赖注入（解耦设计）：
    - agent:    任何实现了 BaseAgent.process() 的 Agent 实例
    - context:  ConversationContext 实例（用于消息去重）
    """

    def __init__(self, agent, context, task_manager: Optional["TaskManager"] = None):
        """
        Args:
            agent:        已初始化的 Agent 实例（实现 BaseAgent 接口）
            context:      已初始化的 ConversationContext 实例
            task_manager: 后台任务管理器（可选），用于追踪消息处理状态
        """
        self._app_id: str = os.getenv("FEISHU_APP_ID") or ""
        self._app_secret: str = os.getenv("FEISHU_APP_SECRET") or ""
        self._max_workers: int = int(os.getenv("FEISHU_MAX_WORKERS", "10"))

        if not self._app_id or not self._app_secret:
            raise ValueError(
                "FEISHU_APP_ID 和 FEISHU_APP_SECRET 为必填项，请在 .env 文件中定义。"
            )

        self._agent = agent
        self._context = context
        self._task_manager = task_manager

        # 线程池：承载 LLM 推理等耗时操作，确保事件回调在 3s 内返回
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="feishu-worker",
        )

        # ── API Client（主动调用）────────────────────────────
        self._api_client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

        # ── 事件分发器（注册消息接收处理函数）───────────────────
        # 长连接模式下 builder 参数必须传空字符串
        _handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

        # ── WebSocket Client（被动接收）──────────────────────
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=_handler,
            log_level=lark.LogLevel.WARNING,
        )

        logger.info(f"✅ FeishuGateway 初始化完成 | app_id={self._app_id[:8]}...")

    # ──────────────────────────────────────────────────────────
    #   公共接口
    # ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        启动 WebSocket 长连接，阻塞当前线程持续监听飞书消息推送。
        通常作为 main.py 的最后一行调用。
        """
        logger.info("🚀 PersonalAgent 飞书网关已启动，等待消息...")
        print("🚀 PersonalAgent 飞书网关已启动，等待消息...", flush=True)
        self._ws_client.start()

    def update_card(
        self,
        message_id: str,
        content: str,
        status: str,
    ) -> None:
        """
        更新已发送的飞书交互卡片内容。
        由 Agent 层通过 on_update 回调间接调用。

        Args:
            message_id: 需要更新的卡片消息 ID（由 _send_thinking_card 返回）
            content:    卡片正文（Markdown 格式）
            status:     卡片状态："generating" | "done" | "error"
        """
        card = self._build_card(status=status, content=content)
        card_str = json.dumps(card, ensure_ascii=False)

        try:
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder().content(card_str).build()
                )
                .build()
            )

            response = self._api_client.im.v1.message.patch(request)  # type: ignore[union-attr]

            if not response.success():
                logger.warning(
                    f"更新卡片失败 | message_id={message_id} "
                    f"code={response.code} msg={response.msg}"
                )

        except Exception as e:
            logger.error(f"更新卡片异常 | message_id={message_id} error={e}")

    # ──────────────────────────────────────────────────────────
    #   事件回调（必须在 3 秒内返回）
    # ──────────────────────────────────────────────────────────

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """
        飞书消息接收事件回调。

        ⚠️  此方法必须在 3 秒内返回，否则飞书会触发重推机制。
        实际的 LLM 推理等耗时操作一律提交到线程池异步执行。
        """
        # ── 解析和过滤事件 ────────────────────────────────────
        event = self._parse_event(data)
        if event is None:
            return

        # ── 消息去重（防飞书重推） ────────────────────────────
        if self._context.is_processed(event.message_id):
            logger.debug(f"消息已处理，跳过 | message_id={event.message_id}")
            return

        # 标记为已处理（在提交线程池之前，防止并发重入）
        self._context.mark_processed(event.message_id)

        logger.info(
            f"📨 收到新消息 | message_id={event.message_id} "
            f"chat_type={event.chat_type} "
            f"content={event.content[:30]!r}"
        )

        # ── 提交到线程池，立即返回 ───────────────────────────
        self._executor.submit(self._handle_async, event)

    # ──────────────────────────────────────────────────────────
    #   异步处理（在线程池中执行）
    # ──────────────────────────────────────────────────────────

    def _handle_async(self, event: MessageEvent) -> None:
        """
        在工作线程中完成完整的消息处理流程：
        发送初始卡片 → 调用 Agent → 流式更新卡片 → 完成/错误

        同时向 TaskManager 注册任务，并在结束时标记完成或失败。

        Args:
            event: 已解析的消息事件
        """
        reply_message_id: Optional[str] = None

        # ── 注册后台任务 ──────────────────────────────────────
        task_id: Optional[str] = None
        if self._task_manager is not None:
            from ..context.conversation_context import ConversationContext

            session_key = ConversationContext.build_session_key(
                chat_type=event.chat_type,
                sender_open_id=event.sender_open_id,
                chat_id=event.chat_id,
                thread_id=event.thread_id,
            )
            task_id = self._task_manager.create(
                session_key=session_key,
                description=event.content[:50],
            )

        try:
            # ── Step 1: 发送"思考中"初始卡片 ─────────────────
            reply_message_id = self._send_thinking_card(event)
            if not reply_message_id:
                logger.error(
                    f"发送初始卡片失败，终止处理 | message_id={event.message_id}"
                )
                if task_id and self._task_manager:
                    self._task_manager.fail(task_id, "发送初始卡片失败")
                return

            # ── Step 2: 构造卡片更新回调 ──────────────────────
            # 节流由 Agent 层的 card-flusher 线程统一负责，
            # Gateway 层只需无条件转发给 update_card。

            def on_update(content: str, status: str) -> None:
                """转发卡片更新到飞书 REST API（节流已在 Agent 层处理）。"""
                if reply_message_id:
                    self.update_card(reply_message_id, content, status)

            # ── Step 3: 调用 Agent 处理（LLM 推理在此发生）──
            self._agent.process(event, on_update=on_update)

            # ── Step 4: 标记任务完成 ──────────────────────────
            if task_id and self._task_manager:
                self._task_manager.complete(task_id)

        except Exception as e:
            logger.error(
                f"消息处理异常 | message_id={event.message_id} error={e}",
                exc_info=True,
            )
            # 将错误信息展示在卡片上，让用户看到（而非静默失败）
            if reply_message_id:
                self.update_card(
                    reply_message_id,
                    f"处理消息时发生内部错误，请稍后重试。\n\n> `{e}`",
                    status="error",
                )
            # 标记任务失败
            if task_id and self._task_manager:
                self._task_manager.fail(task_id, str(e))

    # ──────────────────────────────────────────────────────────
    #   卡片发送
    # ──────────────────────────────────────────────────────────

    def _send_thinking_card(self, event: MessageEvent) -> Optional[str]:
        """
        以"回复"方式向用户发送初始"思考中"状态的交互卡片。
        使用 ReplyMessageRequest 确保卡片出现在用户消息的同一话题线程中。

        Args:
            event: 消息事件（使用其 message_id 作为回复目标）

        Returns:
            新创建的卡片消息 ID，失败时返回 None
        """
        card = self._build_card(status="thinking", content="")
        card_str = json.dumps(card, ensure_ascii=False)

        try:
            request = (
                ReplyMessageRequest.builder()
                .message_id(event.message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(card_str)
                    .msg_type("interactive")
                    .build()
                )
                .build()
            )

            response = self._api_client.im.v1.message.reply(request)  # type: ignore[union-attr]

            if not response.success():
                logger.error(
                    f"发送初始卡片失败 | "
                    f"code={response.code} msg={response.msg} "
                    f"log_id={response.get_log_id()}"
                )
                return None

            resp_data = response.data
            if resp_data is None:
                logger.error("发送初始卡片失败 | response.data 为 None")
                return None

            card_message_id: str = resp_data.message_id or ""
            logger.info(f"✉️  初始卡片已发送 | card_message_id={card_message_id}")
            return card_message_id or None

        except Exception as e:
            logger.error(f"发送初始卡片异常 | error={e}", exc_info=True)
            return None

    # ──────────────────────────────────────────────────────────
    #   事件解析与过滤
    # ──────────────────────────────────────────────────────────

    def _parse_event(self, data: P2ImMessageReceiveV1) -> Optional[MessageEvent]:
        """
        解析并过滤飞书消息事件，返回标准化的 MessageEvent。

        过滤规则：
        1. 只处理 text 类型消息（忽略图片、文件等）
        2. 私聊（P2P）：直接处理所有文本消息
        3. 群聊（Group）：仅当消息中包含 @机器人 时处理
        4. 消息文本为空时忽略

        Args:
            data: 飞书 SDK 原始事件对象

        Returns:
            解析后的 MessageEvent，不满足处理条件时返回 None
        """
        try:
            # ── lark SDK 的 event / message / sender 均为 Optional，先做空值守卫 ──
            if data.event is None:
                return None

            msg = data.event.message
            sender = data.event.sender

            if msg is None or sender is None:
                return None

            # ── 仅处理文本消息 ────────────────────────────────
            msg_type: str = getattr(msg, "message_type", "") or ""
            if msg_type != "text":
                logger.debug(f"非文本消息，跳过 | type={msg_type}")
                return None

            # ── 解析消息内容 JSON ─────────────────────────────
            # 飞书文本消息 content 格式：{"text": "消息内容"}
            raw_content: str = getattr(msg, "content", "") or ""
            try:
                content_dict = json.loads(raw_content)
                raw_text: str = content_dict.get("text", "").strip()
            except (json.JSONDecodeError, AttributeError):
                logger.warning(f"消息内容解析失败 | content={raw_content!r}")
                return None

            if not raw_text:
                return None

            chat_type: str = getattr(msg, "chat_type", "") or ""

            # ── 群聊：@提及过滤 ───────────────────────────────
            if chat_type == "group":
                mentions = getattr(msg, "mentions", None) or []

                # 没有任何 @提及，忽略（飞书权限为 group_at_msg 时只收到 @消息）
                if not mentions:
                    return None

                # 检查是否有机器人被提及（is_bot 为 True 即为机器人）
                bot_mentioned = any(getattr(m, "is_bot", False) for m in mentions)
                if not bot_mentioned:
                    logger.debug("群消息中未 @机器人，跳过")
                    return None

                # 清除文本中的 @占位符，保留纯业务内容
                clean_text = self._strip_mentions(raw_text, mentions)
                if not clean_text:
                    # @机器人 但没有附加内容，返回帮助提示
                    clean_text = "/help"

            else:
                # 私聊：直接使用原始文本
                clean_text = raw_text

            # ── 提取各字段（均使用 getattr 防御 None 属性）──────
            message_id: str = getattr(msg, "message_id", "") or ""
            chat_id: str = getattr(msg, "chat_id", "") or ""
            thread_id: Optional[str] = getattr(msg, "thread_id", None) or None

            sender_id_obj = getattr(sender, "sender_id", None)
            sender_open_id: str = (
                (getattr(sender_id_obj, "open_id", None) or "")
                if sender_id_obj is not None
                else ""
            )

            if not message_id or not chat_id or not chat_type:
                logger.warning("消息关键字段缺失，跳过")
                return None

            return MessageEvent(
                message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                content=clean_text,
                sender_open_id=sender_open_id,
                thread_id=thread_id,
            )

        except Exception as e:
            logger.error(f"解析消息事件失败 | error={e}", exc_info=True)
            return None

    def _strip_mentions(self, text: str, mentions) -> str:
        """
        从文本中移除 @提及占位符（@_user_N 格式），返回干净的业务内容。

        飞书文本消息中，@提及以 "@_user_1"、"@_user_2" 等形式嵌入文本，
        对应的真实用户信息在 mentions 数组中。

        Args:
            text:     原始消息文本，含 @占位符
            mentions: 飞书事件中的 mentions 列表

        Returns:
            去除 @占位符后的纯净文本
        """
        cleaned = text

        # 优先按 mention.key 精确替换（如 "@_user_1"）
        for mention in mentions:
            key = getattr(mention, "key", None)
            if key:
                cleaned = cleaned.replace(key, "")

        # 兜底：正则清除残余的 @xxx 格式（处理 key 为 None 的情况）
        cleaned = re.sub(r"@\S+", "", cleaned)

        # 规范化多余空白
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        return cleaned

    # ──────────────────────────────────────────────────────────
    #   卡片 JSON 构建
    # ──────────────────────────────────────────────────────────

    def _build_card(self, status: str, content: str) -> dict:
        """
        构建飞书交互卡片的 JSON 结构。

        卡片格式：标准飞书卡片（兼容所有客户端版本）
        - config.update_multi=true：更新对所有可见用户生效
        - header: 带颜色主题的标题栏，反映当前状态
        - elements: 正文区域，使用 lark_md 格式支持 Markdown

        Args:
            status:  卡片状态（"thinking" | "generating" | "done" | "error"）
            content: 卡片正文内容（Markdown 格式），thinking 状态可为空

        Returns:
            卡片 JSON 字典，需调用方序列化为字符串
        """
        cfg = _CARD_CONFIG.get(status, _CARD_CONFIG["done"])

        # 正文元素
        body_content = content if content else "..."
        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": body_content,
                },
            }
        ]

        return {
            "config": {
                "wide_screen_mode": True,
                # update_multi=true: 卡片更新对所有看到此卡片的用户生效
                "update_multi": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": cfg["title"],
                },
                "template": cfg["color"],
            },
            "elements": elements,
        }
