import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.cardkit.v1.model.content_card_element_request import (
    ContentCardElementRequest,
)
from lark_oapi.api.cardkit.v1.model.content_card_element_request_body import (
    ContentCardElementRequestBody,
)
from lark_oapi.api.cardkit.v1.model.create_card_request import CreateCardRequest
from lark_oapi.api.cardkit.v1.model.card import Card
from lark_oapi.api.cardkit.v1.model.create_card_request_body import (
    CreateCardRequestBody,
)
from lark_oapi.api.cardkit.v1.model.settings_card_request import SettingsCardRequest
from lark_oapi.api.cardkit.v1.model.settings_card_request_body import (
    SettingsCardRequestBody,
)
from lark_oapi.api.cardkit.v1.model.update_card_request import UpdateCardRequest
from lark_oapi.api.cardkit.v1.model.update_card_request_body import (
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    P2ImMessageMessageReadV1,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..bus import InboundMessage, MessageBus
from ..core.task_manager import TaskManager
from ..guardrails import check_input
from ..routing.clarification_gateway import (
    cancel_pending_clarification,
    has_pending_clarification,
    resolve_text_response_for_session,
)
from ..routing.control_messages import is_control_message
from ..routing import AccessPolicy, SessionRunLocks
from .base_gateway import BaseGateway

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
    parent_id: Optional[str] = field(default=None)
    attached_files: list = field(default_factory=list)
    images: list = field(default_factory=list)  # base64 编码的图片字符串列表
    is_scheduled: bool = field(default=False)


# ──────────────────────────────────────────────────────────────
#   卡片配置常量
# ──────────────────────────────────────────────────────────────

# 卡片标题与颜色主题：创建=思考中，首次输出前切到生成中，收尾=完成/出错/取消
_CARD_CONFIG = {
    "thinking": {"title": "🤔 思考中...", "color": "blue"},
    "generating": {"title": "⚡ 生成中...", "color": "turquoise"},
    "done": {"title": "✅ 完成", "color": "green"},
    "error": {"title": "❌ 出错了", "color": "red"},
    "cancelled": {"title": "⛔ 已取消", "color": "grey"},
}

# 流式正文的 markdown 组件 element_id（需与创建卡片实体时的 JSON 一致）
_STREAM_BODY_ELEMENT_ID = "body_md"
# 创建后、首包正文前的占位（与飞书卡片初始展示一致，便于首包 content 做增量）
_STREAM_PLACEHOLDER_BODY = "..."


@dataclass
class _StreamingCardSession:
    """一次回复对应的 CardKit 卡片实体与流式序号。"""

    card_id: str
    task_id: str = ""
    sender_open_id: str = ""
    sequence: int = 0
    last_pushed_markdown: str = ""
    header_is_generating: bool = False


def _strip_stream_cursor(text: str) -> str:
    """去掉 Agent 旧版打字机光标，避免破坏飞书「新内容为旧内容前缀」的流式增量规则。"""
    if text.endswith(" ▌"):
        return text[:-2]
    if text.endswith("▌"):
        return text[:-1]
    return text


def _infer_feishu_file_type(filename: str) -> str:
    """将本地文件扩展名映射到飞书 IM 文件上传的 file_type。"""
    ext = Path(filename).suffix.lower()
    if ext in {".mp3", ".wav", ".m4a", ".aac", ".amr", ".opus"}:
        return "opus"
    if ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        return "mp4"
    if ext == ".pdf":
        return "pdf"
    if ext in {".doc", ".docx"}:
        return "doc"
    if ext in {".xls", ".xlsx", ".csv"}:
        return "xls"
    if ext in {".ppt", ".pptx"}:
        return "ppt"
    return "stream"


class _CardStreamAsyncSink:
    """
    将 CardKit 流式更新放到独立线程：LLM 的 on_update(generating) 立即返回，不再被
    每次 HTTP 阻塞；后台顺序推送并合并突发增量，整体墙钟时间显著缩短。
    """

    def __init__(self, gateway: "FeishuGateway", session: _StreamingCardSession) -> None:
        self._gw = gateway
        self._sess = session
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._latest: str = ""
        self._end: Optional[str] = None
        self._end_text: str = ""
        self._finished = threading.Event()
        self._t = threading.Thread(
            target=self._loop, daemon=True, name="feishu-card-flush"
        )
        self._t.start()

    def feed(self, content: str, status: str) -> None:
        text = _strip_stream_cursor(content)
        with self._cv:
            if status in ("generating", "tool", "processing"):
                if text:
                    self._latest = text
            elif status == "done":
                self._end = "done"
                self._end_text = text
            else:
                self._end = "error"
                if text:
                    self._end_text = text
            self._cv.notify_all()
        if status in ("done", "error"):
            self._finished.wait(timeout=180)

    def cancel(self) -> None:
        """用户点击取消按钮时调用，将卡片更新为已取消状态。非阻塞返回。"""
        with self._cv:
            if self._end is None:
                self._end = "cancelled"
                self._cv.notify_all()

    def _loop(self) -> None:
        try:
            while True:
                with self._lock:
                    end = self._end
                    et = self._end_text
                    latest = self._latest
                if end is not None:
                    if end == "cancelled":
                        body_out = self._sess.last_pushed_markdown
                        self._gw._card_stream_finalize_card(
                            self._sess, "cancelled", body_out, last_content_delta_chars=0
                        )
                        return
                    final = et or latest
                    prev = self._sess.last_pushed_markdown
                    delta_chars = max(0, len(final) - len(prev)) if final else 0
                    if final and final != prev:
                        self._gw._card_stream_push_markdown(self._sess, final)
                    theme = "error" if end == "error" else "done"
                    body_out = (et or latest) or self._sess.last_pushed_markdown
                    self._gw._card_stream_finalize_card(
                        self._sess,
                        theme,
                        body_out,
                        last_content_delta_chars=delta_chars,
                    )
                    return
                if latest and latest != self._sess.last_pushed_markdown:
                    self._gw._card_stream_push_markdown(self._sess, latest)
                    continue
                with self._cv:
                    self._cv.wait(timeout=0.05)
        finally:
            self._finished.set()


# ──────────────────────────────────────────────────────────────
#   飞书网关
# ──────────────────────────────────────────────────────────────


class FeishuGateway(BaseGateway):
    """
    飞书机器人网关 —— 平台接入层。

    职责：
    1. 维护与飞书服务器的 WebSocket 长连接，接收消息推送事件
    2. 解析事件，进行 @提及过滤、消息去重、内容清洗
    3. 将有效消息异步分发给 Agent 层处理（ThreadPool，不阻塞事件回调）
    4. 管理飞书卡片生命周期：CardKit 创建 JSON 2.0 流式卡片实体 → 回复消息挂载 card_id
       → 默认由后台线程异步刷 CardKit（LLM 不被 HTTP 阻塞；客户端仍按 streaming_config 逐字渲染）
       → 关闭 streaming_mode

    两个飞书客户端：
    - _api_client:  lark.Client        主动调用飞书 REST API（发消息、更新卡片）
    - _ws_client:   lark.ws.Client     被动接收飞书事件推送（WebSocket 长连接）

    依赖注入（解耦设计）：
    - agent:    任何实现了 BaseAgent.process_inbound() 的 Agent 实例
    - context:  ConversationContext 实例（用于消息去重）
    """

    def __init__(self, agent, context, task_manager: Optional["TaskManager"] = None, file_stash=None):
        """
        ...
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
        self._file_stash = file_stash
        self._message_bus: MessageBus | None = None

        # 线程池：承载 LLM 推理等耗时操作，确保事件回调在 3s 内返回
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="feishu-worker",
        )

        # 会话级串行锁（按 session_key 串行，rationale 见 SessionRunLocks）
        self._run_locks = SessionRunLocks()

        # 用户白名单：FEISHU_ALLOWED_USERS 逗号分隔的 open_id 列表。
        # 未配置时放行所有（飞书默认靠群聊 @机器人 控制，故不打告警）。
        self._access = AccessPolicy.from_env("feishu")

        # 活跃任务的 sink 注册表，用于接收卡片取消信号（task_id → sink）
        self._active_sinks: dict[str, "_CardStreamAsyncSink"] = {}
        self._active_sinks_lock = threading.Lock()

        # ── API Client（主动调用）────────────────────────────
        self._api_client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

        # ── 获取机器人自身 open_id（用于群聊 @提及过滤）──────────
        self._bot_open_id: str = self._fetch_bot_open_id()

        # ── 事件分发器（注册消息接收处理函数）───────────────────
        # 长连接模式下 builder 参数必须传空字符串
        _handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            # 已读回执：开放平台/应用若订阅了 im.message.message_read_v1，长连接会推送；
            # 未注册对应 processor 时 SDK 报 processor not found（与业务无关但必须接住）。
            .register_p2_im_message_message_read_v1(self._on_im_message_read_v1)
            # 卡片取消按钮回调：需在飞书开发者后台订阅 card.action.trigger 事件
            .register_p2_card_action_trigger(self._on_card_action_trigger)
            .build()
        )

        # ── 未注册事件统一兜底 ───────────────────────────────────
        # lark-oapi 的分发器是纯 map 匹配、无兜底：凡是飞书后台订阅了、但这里没
        # register 的事件类型（如 im.message.reaction.created 表情回应），都会被
        # do_without_validation 抛 "processor not found"，进而被 WS 客户端打成
        # ERROR 日志并向飞书回 HTTP 500 —— 飞书据此判定投递失败而重试，形成噪音+重试风暴。
        # 这里在分发入口加一道兜底：识别出"未注册的事件类型"则安静忽略并回成功（200），
        # 不靠脆弱的异常文案匹配，只放过真正没注册的事件；已注册事件的真实错误照常上抛。
        self._install_unregistered_event_guard(_handler)

        # ── WebSocket Client（被动接收）──────────────────────
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=_handler,
            log_level=lark.LogLevel.WARNING,
        )

        logger.info(f"✅ FeishuGateway 初始化完成 | app_id={self._app_id[:8]}... bot_open_id={self._bot_open_id or '未获取'}")

    # ──────────────────────────────────────────────────────────
    #   机器人身份获取
    # ──────────────────────────────────────────────────────────

    def _fetch_bot_open_id(self) -> str:
        """
        调用飞书 bot/v3/info API 获取机器人自身的 open_id。
        用于群聊 @提及过滤：只有 @机器人 才触发处理。
        """
        import httpx

        try:
            # 先获取 tenant_access_token
            token_resp = httpx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            token_data = token_resp.json()
            token = token_data.get("tenant_access_token", "")
            if not token:
                logger.warning("获取 tenant_access_token 失败: %s", token_data)
                return ""

            # 调用 bot/v3/info 获取机器人信息
            bot_resp = httpx.get(
                "https://open.feishu.cn/open-apis/bot/v3/info/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            bot_data = bot_resp.json()
            bot_info = bot_data.get("bot", {})
            open_id = bot_info.get("open_id", "")
            if open_id:
                logger.info("✅ 获取机器人 open_id 成功 | open_id=%s", open_id)
            else:
                logger.warning("bot/v3/info 返回中无 open_id: %s", bot_data)
            return open_id
        except Exception as e:
            logger.error("获取机器人 open_id 失败: %s", e, exc_info=True)
            return ""

    # ──────────────────────────────────────────────────────────
    #   公共接口
    # ──────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "feishu"

    def set_message_bus(self, message_bus: MessageBus) -> None:
        self._message_bus = message_bus

    def _publish_agent_message(self, event: MessageEvent, **runtime):
        runtime_for_bus = dict(runtime)
        progress_sink = runtime_for_bus.pop("on_update", None)
        progress_target = runtime_for_bus.get("progress_target") or event.message_id
        runtime_for_bus["progress_target"] = progress_target
        runtime_for_bus["publish_progress"] = callable(progress_sink)
        metadata = {
            "_runtime": runtime_for_bus,
            "file_context": getattr(event, "file_context", None),
        }
        inbound = InboundMessage(
            source="feishu",
            message_id=event.message_id,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            content=event.content,
            sender_id=event.sender_open_id,
            thread_id=event.thread_id,
            parent_id=event.parent_id,
            attached_files=list(event.attached_files),
            images=list(event.images),
            is_scheduled=event.is_scheduled,
            metadata=metadata,
        )
        if self._message_bus is None:
            return self._agent.process_inbound(inbound, **runtime)
        unregister_progress = None
        if callable(progress_sink):
            unregister_progress = self._message_bus.register_progress_sink(
                progress_target,
                progress_sink,
            )
        try:
            responses = self._message_bus.publish_inbound(inbound)
        except Exception:
            if unregister_progress is not None:
                unregister_progress()
            raise
        for response in responses:
            if isinstance(response, str):
                return response
        return ""

    def start(self) -> None:
        """阻塞当前线程，维持飞书 WebSocket 长连接。"""
        logger.info("🚀 PersonalAgent 飞书网关已启动，等待消息...")
        print("🚀 PersonalAgent 飞书网关已启动，等待消息...", flush=True)
        self._ws_client.start()

    def stop(self) -> None:
        """优雅停止：把所有进行中的流式卡片实时收尾，避免冻结在「⚡ 生成中…」。

        由 main.py 退出信号处理器在 os._exit 前调用。复用 sink 现成的 cancel()：
        它把卡片 finalize 成「已取消」终态并关闭 streaming_mode。cancel() 非阻塞
        （真正的 finalize HTTP 在 sink 的 daemon 线程里跑），故先全部 cancel，再逐个
        等 _finished 事件，确保收尾请求在进程退出前真正发出。同步、幂等、不抛出。
        """
        with self._active_sinks_lock:
            sinks = list(self._active_sinks.values())
        if not sinks:
            return
        logger.info("飞书优雅退出：收尾 %d 张进行中的流式卡片...", len(sinks))
        for sink in sinks:
            try:
                sink.cancel()
            except Exception:
                logger.warning("飞书卡片 cancel 失败", exc_info=True)
        for sink in sinks:
            try:
                sink._finished.wait(timeout=5)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    #   CardKit 流式卡片（JSON 2.0 + 流式更新文本 OpenAPI）
    #   参考：https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview
    # ──────────────────────────────────────────────────────────

    def _read_streaming_render_params(self) -> tuple[int, int, str]:
        """
        客户端打字机参数：频率(ms)、步长(字符)、策略(fast|delay)。
        print_step=1 为逐字上屏；print_frequency_ms 越小动画越快（受客户端实现限制）。
        读取 os.environ（由 main.py 从项目根 .env 注入）。未设置时本仓库回退默认 10ms；
        飞书流式文档示例中 print_frequency_ms 默认为 70ms。
        """
        freq = int(os.getenv("FEISHU_STREAM_PRINT_FREQ_MS", "10"))
        step = int(os.getenv("FEISHU_STREAM_PRINT_STEP", "1"))
        strategy = os.getenv("FEISHU_STREAM_PRINT_STRATEGY", "fast")
        freq = max(1, freq)
        step = max(1, step)
        return freq, step, strategy

    def _sleep_before_stream_finalize(
        self,
        body_markdown: str,
        *,
        last_content_delta_chars: Optional[int] = None,
    ) -> None:
        """
        最后一次流式 content 推送后，客户端仍按 print_frequency_ms / print_step 做打字机。
        若立刻全量 PUT 收尾，会打断动画，尾部常「整块蹦出」。此处留出扫尾时间。

        last_content_delta_chars:
            相对「上一次已成功推送的正文」本次多出来的字符数（通常等于最后一次 content
            里客户端要动画播放的增量）。异步刷盘时 LLM 多段会合并成一次 HTTP，增量可能很大，
            必须用增量估时；若省略则回退为按全文长度的保守估时。
        设 FEISHU_STREAM_FINISH_GRACE_MS=0 可关闭等待。
        """
        raw = os.getenv("FEISHU_STREAM_FINISH_GRACE_MS", "900").strip().lower()
        if raw in ("0", "false", "no", "off"):
            return
        base_ms = int(os.getenv("FEISHU_STREAM_FINISH_GRACE_MS", "900"))
        freq = max(1, int(os.getenv("FEISHU_STREAM_PRINT_FREQ_MS", "10")))
        step = max(1, int(os.getenv("FEISHU_STREAM_PRINT_STEP", "1")))
        cap_ms = int(os.getenv("FEISHU_STREAM_FINISH_GRACE_MAX_MS", "12000"))
        if last_content_delta_chars is not None and last_content_delta_chars > 0:
            steps = (last_content_delta_chars + step - 1) // step
            tail_ms = steps * freq
        else:
            # 无增量信息时：按全文保守估时（系数避免收尾过久）
            tail_ms = (len(body_markdown) * freq) // 3
        total_ms = min(cap_ms, max(0, base_ms) + tail_ms)
        if total_ms > 0:
            time.sleep(total_ms / 1000.0)

    def _stream_async_flush_enabled(self) -> bool:
        v = os.getenv("FEISHU_STREAM_ASYNC_FLUSH", "1").strip().lower()
        return v not in ("0", "false", "no", "off")

    def _build_streaming_card_v2(
        self,
        status: str,
        body_markdown: str,
        *,
        task_id: str = "",
        sender_open_id: str = "",
    ) -> dict:
        cfg = _CARD_CONFIG.get(status, _CARD_CONFIG["done"])
        freq, step, strategy = self._read_streaming_render_params()
        body_elements: list = [
            {
                "tag": "markdown",
                "content": body_markdown,
                "element_id": _STREAM_BODY_ELEMENT_ID,
            }
        ]
        # 仅在流式进行中的状态显示取消按钮
        if task_id and status in ("thinking", "generating"):
            body_elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⛔ 取消"},
                "type": "danger",
                "size": "small",
                "behaviors": [{
                    "type": "callback",
                    "value": {
                        "task_id": task_id,
                        "sender_open_id": sender_open_id,
                    },
                }],
            })
        return {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "streaming_mode": True,
                "summary": {"content": ""},
                "streaming_config": {
                    "print_frequency_ms": {
                        "default": freq,
                        "android": freq,
                        "ios": freq,
                        "pc": freq,
                    },
                    "print_step": {
                        "default": step,
                        "android": step,
                        "ios": step,
                        "pc": step,
                    },
                    "print_strategy": strategy,
                },
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": cfg["title"],
                },
                "template": cfg["color"],
            },
            "body": {
                "elements": body_elements,
            },
        }

    def _card_stream_put_streaming_snapshot(
        self,
        session: _StreamingCardSession,
        status_key: str,
        body_markdown: str,
    ) -> bool:
        """
        流式过程中全量 PUT（仍为 streaming_mode=true）：用于在首次正文推送前把标题从「思考中」切到「生成中」。
        partial_update 无法改 header，故与收尾一样走 card.update。
        """
        if self._api_client.cardkit is None:
            return False
        inner = json.dumps(
            self._build_streaming_card_v2(
                status_key,
                body_markdown,
                task_id=session.task_id,
                sender_open_id=session.sender_open_id,
            ),
            ensure_ascii=False,
        )
        session.sequence += 1
        seq = session.sequence
        try:
            card_obj = Card.builder().type("card_json").data(inner).build()
            req = (
                UpdateCardRequest.builder()
                .card_id(session.card_id)
                .request_body(
                    UpdateCardRequestBody.builder()
                    .card(card_obj)
                    .uuid(str(uuid.uuid4()))
                    .sequence(seq)
                    .build()
                )
                .build()
            )
            resp = self._api_client.cardkit.v1.card.update(req)
            if not resp.success():
                logger.warning(
                    "流式中途更新卡片快照失败 | card_id=%s seq=%s code=%s msg=%s",
                    session.card_id,
                    seq,
                    resp.code,
                    resp.msg,
                )
                return False
            return True
        except Exception as e:
            logger.error(
                "流式中途更新卡片快照异常 | card_id=%s error=%s",
                session.card_id,
                e,
                exc_info=True,
            )
            return False

    def _card_stream_ensure_generating_header(self, session: _StreamingCardSession) -> None:
        if session.header_is_generating:
            return
        if not self._card_stream_put_streaming_snapshot(
            session, "generating", _STREAM_PLACEHOLDER_BODY
        ):
            return
        session.header_is_generating = True
        session.last_pushed_markdown = _STREAM_PLACEHOLDER_BODY

    def _cardkit_create_entity(
        self,
        status: str,
        body_markdown: str,
        *,
        task_id: str = "",
        sender_open_id: str = "",
    ) -> Optional[str]:
        if self._api_client.cardkit is None:
            logger.error("CardKit 服务不可用")
            return None
        payload = self._build_streaming_card_v2(
            status, body_markdown, task_id=task_id, sender_open_id=sender_open_id
        )
        inner = json.dumps(payload, ensure_ascii=False)
        try:
            req = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(inner)
                    .build()
                )
                .build()
            )
            resp = self._api_client.cardkit.v1.card.create(req)
            if not resp.success():
                logger.error(
                    "创建卡片实体失败 | code=%s msg=%s log_id=%s",
                    resp.code,
                    resp.msg,
                    resp.get_log_id(),
                )
                return None
            if resp.data is None or not resp.data.card_id:
                logger.error("创建卡片实体失败 | response.data 无 card_id")
                return None
            return resp.data.card_id
        except Exception as e:
            logger.error("创建卡片实体异常 | error=%s", e, exc_info=True)
            return None

    def _card_stream_push_markdown(
        self, session: _StreamingCardSession, markdown: str
    ) -> None:
        if self._api_client.cardkit is None:
            return
        if markdown and not session.header_is_generating:
            self._card_stream_ensure_generating_header(session)
        session.sequence += 1
        seq = session.sequence
        op_uuid = str(uuid.uuid4())
        try:
            req = (
                ContentCardElementRequest.builder()
                .card_id(session.card_id)
                .element_id(_STREAM_BODY_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .uuid(op_uuid)
                    .content(markdown)
                    .sequence(seq)
                    .build()
                )
                .build()
            )
            resp = self._api_client.cardkit.v1.card_element.content(req)
            if not resp.success():
                logger.warning(
                    "流式更新正文失败 | card_id=%s seq=%s code=%s msg=%s",
                    session.card_id,
                    seq,
                    resp.code,
                    resp.msg,
                )
            else:
                session.last_pushed_markdown = markdown
        except Exception as e:
            logger.error(
                "流式更新正文异常 | card_id=%s error=%s",
                session.card_id,
                e,
                exc_info=True,
            )

    def _card_stream_build_final_card_dict(
        self, theme_key: str, body_markdown: str
    ) -> dict:
        """非流式收尾用完整 JSON：新标题 + 正文快照 + streaming_mode=false。"""
        cfg = _CARD_CONFIG.get(theme_key, _CARD_CONFIG["done"])
        return {
            "schema": "2.0",
            "config": {"update_multi": True, "streaming_mode": False},
            "header": {
                "title": {"tag": "plain_text", "content": cfg["title"]},
                "template": cfg["color"],
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": body_markdown,
                        "element_id": _STREAM_BODY_ELEMENT_ID,
                    }
                ]
            },
        }

    def _card_stream_finalize_card(
        self,
        session: _StreamingCardSession,
        theme_key: str,
        body_markdown: str,
        *,
        last_content_delta_chars: Optional[int] = None,
    ) -> None:
        """
        全量更新卡片实体（PUT）：同时更新标题栏、正文与关闭流式。
        partial_update_setting 仅支持 config/card_link，不能改 header，故必须用本接口。
        文档：https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/cardkit-v1/card/update
        """
        if self._api_client.cardkit is None:
            return
        self._sleep_before_stream_finalize(
            body_markdown, last_content_delta_chars=last_content_delta_chars
        )
        inner = json.dumps(
            self._card_stream_build_final_card_dict(theme_key, body_markdown),
            ensure_ascii=False,
        )
        session.sequence += 1
        seq = session.sequence
        try:
            card_obj = Card.builder().type("card_json").data(inner).build()
            req = (
                UpdateCardRequest.builder()
                .card_id(session.card_id)
                .request_body(
                    UpdateCardRequestBody.builder()
                    .card(card_obj)
                    .uuid(str(uuid.uuid4()))
                    .sequence(seq)
                    .build()
                )
                .build()
            )
            resp = self._api_client.cardkit.v1.card.update(req)
            if not resp.success():
                logger.warning(
                    "全量收尾卡片失败 | card_id=%s seq=%s code=%s msg=%s log_id=%s",
                    session.card_id,
                    seq,
                    resp.code,
                    resp.msg,
                    resp.get_log_id(),
                )
                self._card_stream_disable_mode(session)
            else:
                session.last_pushed_markdown = body_markdown
        except Exception as e:
            logger.error(
                "全量收尾卡片异常 | card_id=%s error=%s",
                session.card_id,
                e,
                exc_info=True,
            )
            self._card_stream_disable_mode(session)

    def _card_stream_disable_mode(self, session: _StreamingCardSession) -> None:
        """结束流式模式，恢复会话列表摘要与转发能力。参见流式文档步骤四。"""
        if self._api_client.cardkit is None:
            return
        settings_obj = {"config": {"streaming_mode": False}}
        settings_str = json.dumps(settings_obj, ensure_ascii=False)
        session.sequence += 1
        seq = session.sequence
        try:
            req = (
                SettingsCardRequest.builder()
                .card_id(session.card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_str)
                    .uuid(str(uuid.uuid4()))
                    .sequence(seq)
                    .build()
                )
                .build()
            )
            resp = self._api_client.cardkit.v1.card.settings(req)
            if not resp.success():
                logger.warning(
                    "关闭卡片流式模式失败 | card_id=%s code=%s msg=%s",
                    session.card_id,
                    resp.code,
                    resp.msg,
                )
        except Exception as e:
            logger.error(
                "关闭卡片流式模式异常 | card_id=%s error=%s",
                session.card_id,
                e,
                exc_info=True,
            )

    def _on_streaming_card_update(
        self,
        session: _StreamingCardSession,
        content: str,
        status: str,
    ) -> None:
        text = _strip_stream_cursor(content)
        if status in ("generating", "tool", "processing"):
            if text and text != session.last_pushed_markdown:
                self._card_stream_push_markdown(session, text)
            return
        if status == "done":
            prev = session.last_pushed_markdown
            if text != prev:
                self._card_stream_push_markdown(session, text)
            delta = max(0, len(text) - len(prev))
            self._card_stream_finalize_card(
                session, "done", text, last_content_delta_chars=delta
            )
            return
        if status == "error":
            prev = session.last_pushed_markdown
            if text and text != prev:
                self._card_stream_push_markdown(session, text)
            body_out = text or session.last_pushed_markdown
            delta = max(0, len(body_out) - len(prev))
            self._card_stream_finalize_card(
                session,
                "error",
                body_out,
                last_content_delta_chars=delta,
            )

    def _send_streaming_card_session(
        self,
        event: MessageEvent,
        *,
        task_id: str = "",
        sender_open_id: str = "",
    ) -> Optional[_StreamingCardSession]:
        """
        创建流式卡片实体，并以回复消息方式发送 card_id（保持在原话题线程内）。
        """
        card_id = self._cardkit_create_entity(
            "thinking",
            _STREAM_PLACEHOLDER_BODY,
            task_id=task_id,
            sender_open_id=sender_open_id,
        )
        if not card_id:
            return None
        reply_content = json.dumps(
            {"type": "card", "data": {"card_id": card_id}},
            ensure_ascii=False,
        )
        try:
            request = (
                ReplyMessageRequest.builder()
                .message_id(event.message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(reply_content)
                    .msg_type("interactive")
                    .build()
                )
                .build()
            )
            response = self._api_client.im.v1.message.reply(request)  # type: ignore[union-attr]
            if not response.success():
                logger.error(
                    "发送流式卡片消息失败 | code=%s msg=%s log_id=%s",
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return None
            logger.info("✉️ 流式卡片已发送 | card_id=%s", card_id)
            return _StreamingCardSession(
                card_id=card_id, task_id=task_id, sender_open_id=sender_open_id
            )
        except Exception as e:
            logger.error("发送流式卡片消息异常 | error=%s", e, exc_info=True)
            return None

    # 永久性错误码：重试无意义，直接放弃
    _PERMANENT_ERROR_CODES = frozenset({230001, 230002, 10014, 99991400})

    def _send_text_message(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str = "chat_id",
        _retries: int = 1,
        raise_on_failure: bool = False,
    ) -> bool:
        """发送普通文本消息，失败时最多重试 _retries 次（间隔 2s）。永久错误码不重试。"""
        import time
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = (CreateMessageRequest.builder()
               .receive_id_type(receive_id_type)
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(receive_id)
                             .msg_type("text")
                             .content(json.dumps({"text": text}, ensure_ascii=False))
                             .build())
               .build())
        last_error = ""
        for attempt in range(1 + _retries):
            try:
                resp = self._api_client.im.v1.message.create(req)
            except Exception as e:
                last_error = str(e)
                logger.error(
                    "发送文本消息异常 | receive_id=%s attempt=%d error=%s",
                    receive_id, attempt + 1, e,
                )
            else:
                if resp.success():
                    return True
                last_error = f"code={resp.code} msg={resp.msg}"
                logger.error(
                    "发送文本消息失败 | receive_id=%s type=%s attempt=%d code=%s msg=%s",
                    receive_id, receive_id_type, attempt + 1, resp.code, resp.msg,
                )
                if resp.code in self._PERMANENT_ERROR_CODES:
                    if raise_on_failure:
                        raise RuntimeError(f"发送文本消息失败: {last_error}")
                    return False  # 永久错误，不重试
            if attempt < _retries:
                time.sleep(2)
        if raise_on_failure:
            raise RuntimeError(
                f"发送文本消息失败: receive_id={receive_id} type={receive_id_type} error={last_error}"
            )
        return False

    def send_text(self, session_key: str, text: str) -> None:
        """发送普通文本消息（供外部调用，如定时任务结果投递）。

        session_key 支持两种格式：
          - BaseGateway 标准格式：feishu:user:ou_xxx / feishu:group:oc_xxx
          - 原始 ID（向后兼容）：ou_xxx / oc_xxx
        非 feishu: 前缀的 session_key 直接跳过（多网关广播时其他平台的 key）。
        """
        if session_key.startswith("dingtalk:") or (
            ":" in session_key and not session_key.startswith("feishu:")
        ):
            return
        # 剥离 feishu:{id_type}: 前缀，还原原始 ID
        raw_id = session_key.split(":")[-1] if session_key.startswith("feishu:") else session_key
        receive_id_type = "open_id" if raw_id.startswith("ou_") else "chat_id"
        self._send_text_message(raw_id, text, receive_id_type=receive_id_type, raise_on_failure=True)

    def send_file(self, session_key: str, file_path: str, filename: str) -> None:
        """
        向指定飞书会话发送文件。

        飞书 IM 文件消息需要两步：
          1. 上传本地文件到 /im/v1/files，拿到 file_key
          2. 通过 /im/v1/messages 发送 msg_type=file 的消息
        """
        if session_key.startswith("dingtalk:") or (
            ":" in session_key and not session_key.startswith("feishu:")
        ):
            return

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        raw_id = session_key.split(":")[-1] if session_key.startswith("feishu:") else session_key
        receive_id_type = "open_id" if raw_id.startswith("ou_") else "chat_id"
        display_name = filename or path.name

        try:
            from lark_oapi.api.im.v1 import (
                CreateFileRequest,
                CreateFileRequestBody,
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            with open(path, "rb") as file_obj:
                upload_req = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(_infer_feishu_file_type(display_name))
                        .file_name(display_name)
                        .file(file_obj)
                        .build()
                    )
                    .build()
                )
                upload_resp = self._api_client.im.v1.file.create(upload_req)

            if not upload_resp.success():
                raise RuntimeError(
                    "上传飞书文件失败 | "
                    f"code={upload_resp.code} msg={upload_resp.msg} "
                    f"log_id={upload_resp.get_log_id()}"
                )

            file_key = getattr(upload_resp.data, "file_key", "") if upload_resp.data else ""
            if not file_key:
                raise RuntimeError("上传飞书文件失败：响应缺少 file_key")

            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(raw_id)
                    .msg_type("file")
                    .content(json.dumps({"file_key": file_key}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = self._api_client.im.v1.message.create(req)
            if not resp.success():
                raise RuntimeError(
                    "发送飞书文件消息失败 | "
                    f"code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
                )
            logger.info(
                "飞书文件消息发送成功 | filename=%s receive_id=%s type=%s",
                display_name,
                raw_id,
                receive_id_type,
            )
        except Exception as exc:
            logger.error(
                "send_file 失败 | session_key=%s file=%s error=%s",
                session_key,
                file_path,
                exc,
            )
            raise

    def send_image_text(self, session_key: str, image_path: str, text: str) -> None:
        if session_key.startswith("dingtalk:") or (
            ":" in session_key and not session_key.startswith("feishu:")
        ):
            return

        raw_id = session_key.split(":")[-1] if session_key.startswith("feishu:") else session_key
        receive_id_type = "open_id" if raw_id.startswith("ou_") else "chat_id"

        try:
            from lark_oapi.api.im.v1 import (
                CreateImageRequest,
                CreateImageRequestBody,
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            with open(image_path, "rb") as image_file:
                upload_req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(image_file)
                        .build()
                    )
                    .build()
                )
                upload_resp = self._api_client.im.v1.image.create(upload_req)
            if not upload_resp.success():
                raise RuntimeError(
                    "上传飞书图片失败 | "
                    f"code={upload_resp.code} msg={upload_resp.msg} "
                    f"log_id={upload_resp.get_log_id()}"
                )

            image_key = getattr(upload_resp.data, "image_key", "") if upload_resp.data else ""
            if not image_key:
                raise RuntimeError("上传飞书图片失败：响应缺少 image_key")

            rows = [[{"tag": "img", "image_key": image_key}]]
            for line in (text or "").splitlines():
                rows.append([{"tag": "text", "text": line}])
            content = {"zh_cn": {"content": rows}}

            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(raw_id)
                    .msg_type("post")
                    .content(json.dumps(content, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = self._api_client.im.v1.message.create(req)
            if not resp.success():
                raise RuntimeError(
                    "发送飞书图文消息失败 | "
                    f"code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
                )
        except Exception as exc:
            logger.error(
                "send_image_text 失败 | session_key=%s image=%s error=%s",
                session_key,
                image_path,
                exc,
            )
            raise

    def send_stash_remind_card(self, chat_id: str, file_name: str) -> None:
        """发送超时卡片提醒"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        card_content = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {
                    "content": "⏰ 文件暂存提醒",
                    "tag": "plain_text"
                }
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"你之前发送的 `{file_name}` 还没有说明 **处理意图**。"
                },
                {
                    "tag": "markdown",
                    "content": "请在 **2 分钟**内 发送文字说明 **要做什么**（如整理、分析、翻译等）。否则会话中的 **文件暂存记录** 将 **自动清除**。"
                }
            ]
        }
        req = (CreateMessageRequest.builder()
               .receive_id_type("chat_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(chat_id)
                             .msg_type("interactive")
                             .content(json.dumps(card_content, ensure_ascii=False))
                             .build())
               .build())
        self._api_client.im.v1.message.create(req)

    # ──────────────────────────────────────────────────────────
    #   事件回调（必须在 3 秒内返回）
    # ──────────────────────────────────────────────────────────


    @staticmethod
    def _install_unregistered_event_guard(handler) -> None:
        """给事件分发器加一道兜底：未注册的事件类型安静忽略并回成功，避免回 500 触发飞书重试。

        实现：包裹 handler.do_without_validation（WS 长连接收到事件时调用的入口）。
        先按 SDK 同样的方式解析出事件 key（p2.<event_type> / p1.<type>），若该 key 既不在
        事件处理器表、也不在回调处理器表中，则判定为"订阅了但未注册"，记 DEBUG 后返回 None
        （WS 客户端据此回 200）。其余情况一律走原逻辑，已注册事件的真实异常照常上抛。
        若解析或内省失败则回退到原逻辑，绝不改变既有行为。
        """
        registered = set(getattr(handler, "_processorMap", {})) | set(
            getattr(handler, "_callback_processor_map", {})
        )
        orig_do = handler.do_without_validation

        def _do(payload: bytes):
            key = None
            try:
                ctx = json.loads(payload.decode("utf-8"))
                if ctx.get("schema"):  # p2 事件
                    key = "p2." + (ctx.get("header") or {}).get("event_type", "")
                elif ctx.get("uuid"):  # p1 事件
                    key = "p1." + (ctx.get("event") or {}).get("type", "")
            except Exception:
                key = None
            if key and key not in registered:
                logger.debug("忽略未注册的飞书事件: %s", key)
                return None
            return orig_do(payload)

        handler.do_without_validation = _do

    def _on_im_message_read_v1(self, data: P2ImMessageMessageReadV1) -> None:
        """消息已读事件：用户侧已读会话中的消息时飞书会推送，本机器人无需处理，仅消费事件以完成 SDK 分发。"""
        if logger.isEnabledFor(logging.DEBUG) and data.event is not None:
            mids = data.event.message_id_list
            reader = data.event.reader
            logger.debug(
                "im.message.message_read_v1 | reader=%s message_ids=%s",
                getattr(reader, "reader_id", reader) if reader else None,
                mids[:5] if mids else None,
            )

    def _on_card_action_trigger(self, data):
        """
        卡片按钮点击回调（card.action.trigger）。
        收到取消按钮点击后，立即设置取消信号，并异步更新卡片为已取消状态。
        必须在 3 秒内返回，所以只做信号设置，卡片更新交给 sink 后台线程。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
            CallBackToast,
        )
        try:
            event = data.event if data else None
            if not event or not event.action or not event.action.value:
                return None

            value = event.action.value
            task_id: str = value.get("task_id", "")
            expected_sender: str = value.get("sender_open_id", "")

            clicker_open_id = ""
            if event.operator:
                clicker_open_id = event.operator.open_id or ""

            # 仅允许发消息的人取消
            if expected_sender and clicker_open_id != expected_sender:
                resp = P2CardActionTriggerResponse()
                resp.toast = CallBackToast()
                resp.toast.type = "warning"
                resp.toast.content = "只有发消息的人才能取消"
                return resp

            if not task_id:
                return None

            logger.info("⛔ 收到取消请求 | task_id=%s clicker=%s", task_id, clicker_open_id)

            # 设置取消事件（通知 agent 循环退出）
            if self._task_manager:
                cancel_event = self._task_manager.get_cancel_event(task_id)
                if cancel_event:
                    cancel_event.set()

            # 异步触发 sink 更新卡片为已取消（不阻塞回调）
            with self._active_sinks_lock:
                sink = self._active_sinks.get(task_id)
            if sink:
                t = threading.Thread(target=sink.cancel, daemon=True, name="cancel-sink")
                t.start()

            resp = P2CardActionTriggerResponse()
            resp.toast = CallBackToast()
            resp.toast.type = "info"
            resp.toast.content = "已取消，请重新发送"
            return resp

        except Exception as e:
            logger.error("处理卡片取消回调异常 | error=%s", e, exc_info=True)
            return None

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """
        飞书消息接收事件回调。

        ⚠️  此方法必须在 3 秒内返回，否则飞书会触发重推机制。
        实际的 LLM 推理等耗时操作一律提交到线程池异步执行。
        """
        # ── 解析和过滤事件 ────────────────────────────────────
        event = self._parse_event(data)
        if event is None or isinstance(event, str):
            return

        # ── 消息去重（防飞书重推） ────────────────────────────
        if self._context.is_processed(event.message_id):
            logger.debug(f"消息已处理，跳过 | message_id={event.message_id}")
            return

        # 标记为已处理（在提交线程池之前，防止并发重入）
        self._context.mark_processed(event.message_id)

        # ── 准入检查：配置了 FEISHU_ALLOWED_USERS 时，非白名单用户静默拒绝 ──
        if not self._access.is_allowed(event.sender_open_id):
            logger.warning(
                "拒绝未授权用户 | open_id=%s message_id=%s",
                event.sender_open_id, event.message_id,
            )
            return

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
        # ── 计算 session_key（加锁和注册任务均需要）──────────
        from ..context.conversation_context import ConversationContext
        session_key = ConversationContext.build_session_key(
            chat_type=event.chat_type,
            sender_open_id=event.sender_open_id,
            chat_id=event.chat_id,
            thread_id=event.thread_id,
        )

        if has_pending_clarification(session_key):
            if is_control_message(event.content):
                cancel_pending_clarification(
                    session_key,
                    reason="new_control_message",
                )
            elif resolve_text_response_for_session(session_key, event.content):
                logger.info(
                    "飞书消息作为澄清回复处理 | session=%s message_id=%s",
                    session_key,
                    event.message_id,
                )
                return

        # ── 绑定当前用户到 contextvar（每请求作用域，供用量记账归属到发送人）──
        from ..core.usage_tracker import set_current_user, reset_current_user
        _user_token = set_current_user(
            f"feishu:{event.sender_open_id}", getattr(event, "sender_name", "") or ""
        )

        # ── 会话串行锁：同 session 并发请求排队，防止上下文消息交错 ──
        try:
            with self._run_locks.get(session_key):
                self._handle_async_body(event, session_key)
        finally:
            reset_current_user(_user_token)

    def _handle_async_body(self, event: "MessageEvent", session_key: str) -> None:
        """_handle_async 的实际执行体，在会话串行锁保护下运行。"""
        from ..core.task_manager import TaskCancelledError

        # ── L1 Input Guard（LLM 调用前拦截）─────────────────────
        _guard = check_input(event.content, session_key=session_key)
        if not _guard.passed:
            self._send_text_message(event.chat_id, _guard.user_message)
            return

        stream_session: Optional[_StreamingCardSession] = None
        stream_sink: Optional[_CardStreamAsyncSink] = None

        # ── 注册后台任务 ──────────────────────────────────────
        task_id: Optional[str] = None
        cancel_event = None
        if self._task_manager is not None:
            task_id = self._task_manager.create(
                session_key=session_key,
                description=event.content[:50],
            )
            cancel_event = self._task_manager.get_cancel_event(task_id)

        # 保存上下文快照长度，取消时用于回滚
        snapshot_len = self._context.get_history_length(session_key)

        try:
            # ── Step 0: 文件挂载/合并处理 ───────────────────────
            if getattr(self, "_file_stash", None):
                # 1. 优先尝试合并单聊遗留的文件暂存
                if event.chat_type == "p2p" and session_key:
                    stashed_files = self._file_stash.pop_all(session_key)
                    if stashed_files:
                        event.attached_files.extend(stashed_files)
                
                # 2. 对于群聊处理 parent_id 获取原生附件
                if event.chat_type == "group" and getattr(event, "parent_id", None):
                    from lark_oapi.api.im.v1 import GetMessageRequest
                    req = GetMessageRequest.builder().message_id(event.parent_id).build()
                    resp = self._api_client.im.v1.message.get(req)
                    if resp.success() and resp.data and resp.data.items:
                        parent_msg = resp.data.items[0]
                        if parent_msg.msg_type == "file":
                            try:
                                parent_content = json.loads(parent_msg.body.content)
                                file_key = parent_content.get("file_key")
                                file_name = parent_content.get("file_name")
                                if file_key and file_name:
                                    if not self._file_stash.is_allowed(file_name):
                                        self._send_text_message(event.chat_id, "不支持此文件类型")
                                    else:
                                        dl_file = self._file_stash.download_direct(
                                            file_key, file_name, event.parent_id, self._api_client
                                        )
                                        if dl_file:
                                            event.attached_files.append(dl_file)
                            except Exception as e:
                                logger.error(f"解析群聊父消息文件失败: {e}", exc_info=True)
                
                if event.attached_files:
                    event.file_context = self._file_stash.build_file_context(event.attached_files)

            # ── Step 1: CardKit 创建流式卡片实体并回复 card_id ──
            stream_session = self._send_streaming_card_session(
                event,
                task_id=task_id or "",
                sender_open_id=event.sender_open_id,
            )
            if not stream_session:
                logger.error(
                    f"发送流式卡片失败，终止处理 | message_id={event.message_id}"
                )
                if task_id and self._task_manager:
                    self._task_manager.fail(task_id, "发送流式卡片失败")
                return

            # ── Step 2: CardKit 刷新：默认异步，避免 LLM 被每次 HTTP 阻塞 ──
            if self._stream_async_flush_enabled():
                stream_sink = _CardStreamAsyncSink(self, stream_session)
                # 注册 sink 以便取消回调可找到它
                if task_id:
                    with self._active_sinks_lock:
                        self._active_sinks[task_id] = stream_sink

                def on_update(content: str, status: str) -> None:
                    stream_sink.feed(content, status)
            else:

                def on_update(content: str, status: str) -> None:
                    self._on_streaming_card_update(stream_session, content, status)

            # ── Step 3: 调用 Agent（CardKit 流式下不受 im PATCH 5QPS 限制）──
            # push_target 用于 exec 等工具在后台任务完成后通过 MessageBus 主动推送文本消息
            self._publish_agent_message(
                event,
                on_update=on_update,
                stream_updates_sync_with_llm=True,
                push_target=event.chat_id,
                cancel_event=cancel_event,
            )

            # ── Step 4: 标记任务完成 ──────────────────────────
            if task_id and self._task_manager:
                self._task_manager.complete(task_id)

        except TaskCancelledError:
            logger.info("⛔ 任务已取消，回滚上下文 | task_id=%s session=%s", task_id, session_key)
            self._context.rollback_to_length(session_key, snapshot_len)
            if task_id and self._task_manager:
                self._task_manager.cancel(task_id)

        except Exception as e:
            logger.error(
                f"消息处理异常 | message_id={event.message_id} error={e}",
                exc_info=True,
            )
            if stream_sink is not None:
                stream_sink.feed(
                    f"处理消息时发生内部错误，请稍后重试。\n\n> `{e}`",
                    "error",
                )
            elif stream_session:
                self._on_streaming_card_update(
                    stream_session,
                    f"处理消息时发生内部错误，请稍后重试。\n\n> `{e}`",
                    "error",
                )
            if task_id and self._task_manager:
                self._task_manager.fail(task_id, str(e))

        finally:
            # 无论结果如何，清除 sink 注册
            if task_id:
                with self._active_sinks_lock:
                    self._active_sinks.pop(task_id, None)

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

            # ── 媒体或文本类型的分流处理 ──────────────────────
            msg_type: str = getattr(msg, "message_type", "") or ""
            chat_type: str = getattr(msg, "chat_type", "") or ""
            
            if msg_type == "file":
                if chat_type == "group":
                    logger.debug("群聊文件消息，静默忽略待用户回复追溯。")
                    return None
                    
                raw_content = getattr(msg, "content", "") or ""
                try:
                    content_dict = json.loads(raw_content)
                    file_key = content_dict.get("file_key", "")
                    file_name = content_dict.get("file_name", "")
                except json.JSONDecodeError:
                    return None
                    
                if getattr(self, "_file_stash", None):
                    message_id: str = getattr(msg, "message_id", "") or ""
                    chat_id: str = getattr(msg, "chat_id", "") or ""

                    from ..context.conversation_context import ConversationContext
                    if self._context.is_processed(message_id):
                        logger.debug(f"文件消息重复投递，已跳过 | message_id={message_id}")
                        return "file_stashed_discarded"
                    self._context.mark_processed(message_id)

                    if not self._file_stash.is_allowed(file_name):
                        self._send_text_message(chat_id, "不支持此文件类型")
                        return "file_stashed_discarded"
                        
                    downloaded = self._file_stash.download_direct(file_key, file_name, message_id, self._api_client)
                    if downloaded:
                        sender_id_obj = getattr(sender, "sender_id", None)
                        sender_open_id = (getattr(sender_id_obj, "open_id", None) or "") if sender_id_obj else ""
                        thread_id = getattr(msg, "thread_id", None) or None
                        session_key = ConversationContext.build_session_key(chat_type, sender_open_id, chat_id, thread_id)
                        self._file_stash.stash_file(session_key, chat_id, downloaded)
                return "file_stashed"

            elif msg_type == "image":
                # ── 图片消息：下载后附带到事件，交由视觉模型处理 ──
                message_id_img: str = getattr(msg, "message_id", "") or ""
                chat_id_img: str = getattr(msg, "chat_id", "") or ""
                thread_id_img: Optional[str] = getattr(msg, "thread_id", None) or None
                sender_id_obj_img = getattr(sender, "sender_id", None)
                sender_open_id_img: str = (
                    (getattr(sender_id_obj_img, "open_id", None) or "")
                    if sender_id_obj_img is not None else ""
                )
                raw_content_img: str = getattr(msg, "content", "") or ""
                try:
                    image_key = json.loads(raw_content_img).get("image_key", "")
                except Exception:
                    image_key = ""
                if not image_key:
                    logger.warning("图片消息缺少 image_key，跳过")
                    return None
                b64 = self._download_image_b64(message_id_img, image_key)
                if not b64:
                    return None
                return MessageEvent(
                    message_id=message_id_img,
                    chat_id=chat_id_img,
                    chat_type=chat_type,
                    content="",
                    sender_open_id=sender_open_id_img,
                    thread_id=thread_id_img,
                    images=[b64],
                )

            elif msg_type != "text":
                logger.debug(f"非文本/文件消息，跳过 | type={msg_type}")
                return None

            # ── 解析文本消息内容 JSON ─────────────────────────────
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

            # ── 群聊：@提及过滤 ───────────────────────────────
            if chat_type == "group":
                mentions = getattr(msg, "mentions", None) or []

                # 没有任何 @提及，忽略（飞书权限为 group_at_msg 时只收到 @消息）
                if not mentions:
                    return None

                # 检查 mentions 中是否包含对机器人自身的 @提及
                # mention.id 对应被 @ 对象的 open_id（类型由 id_type 指定）
                bot_mentioned = False
                if self._bot_open_id:
                    for mention in mentions:
                        mention_id = getattr(mention, "id", None) or ""
                        mention_open_id = (
                            getattr(mention_id, "open_id", None)
                            if hasattr(mention_id, "open_id")
                            else str(mention_id)
                        )
                        if mention_open_id == self._bot_open_id:
                            bot_mentioned = True
                            break
                else:
                    # bot_open_id 获取失败时回退到旧逻辑（有 @就处理），防止机器人完全沉默
                    logger.warning("bot_open_id 未获取，回退为所有 @提及均处理")
                    bot_mentioned = True

                if not bot_mentioned:
                    logger.debug(
                        "群聊消息未 @机器人，跳过 | mentions=%s",
                        [getattr(m, "id", None) for m in mentions],
                    )
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
            parent_id: Optional[str] = getattr(msg, "parent_id", None) or None

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
                parent_id=parent_id,
            )

        except Exception as e:
            logger.error(f"解析消息事件失败 | error={e}", exc_info=True)
            return None

    def _download_image_b64(self, message_id: str, image_key: str) -> Optional[str]:
        """从飞书下载图片并返回 base64 字符串，失败返回 None。"""
        import base64
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            resp = self._api_client.im.v1.message_resource.get(req)
            if not resp.success():
                logger.warning("下载图片失败 | code=%s msg=%s", resp.code, resp.msg)
                return None
            raw: bytes = resp.data.file.read()
            return base64.b64encode(raw).decode("utf-8") if raw else None
        except Exception as e:
            logger.error("下载图片异常 | image_key=%s error=%s", image_key, e)
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
