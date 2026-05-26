"""
钉钉机器人网关

接收：dingtalk-stream SDK（反向 WebSocket 长连接，无需公网 IP）
回复：v2 AI 互动卡片（createAndDeliver + callbackType=STREAM，支持完整 GFM）
     降级：sessionWebhook Markdown（卡片创建失败时）
发文件：OpenAPI 两步（oapi 上传 media → api 发消息）

卡片方案参考：github.com/soimy/openclaw-channel-dingtalk
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

import httpx

try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
except ImportError as _e:
    raise ImportError(
        "dingtalk-stream 未安装，请执行: pip install dingtalk-stream"
    ) from _e

from .base_gateway import BaseGateway
from .dingtalk_media import DingTalkMediaClient

if TYPE_CHECKING:
    from ..agents.base_agent import BaseAgent
    from ..context.conversation_context import ConversationContext
    from ..core.task_manager import TaskManager


# ──────────────────────────────────────────────────────────────
#   消息事件数据模型
# ──────────────────────────────────────────────────────────────

@dataclass
class DingTalkMessageEvent:
    """
    钉钉消息事件的标准化结构，与 FeishuGateway 的 MessageEvent 对齐，
    供 ConversationAgent.process(event) 消费。

    所有 ID 加 "dingtalk_" 前缀：ConversationContext.build_session_key() 据此
    生成 p2p_dingtalk_{staffId} / group_dingtalk_{conversationId}，
    与飞书会话 key 格式互不碰撞，实现完全隔离。
    """
    message_id: str         # 钉钉消息 ID，用于去重
    chat_id: str            # f"dingtalk_{conversationId}"
    chat_type: str          # "p2p" 或 "group"
    content: str            # 消息纯文本
    sender_open_id: str     # f"dingtalk_{senderStaffId}"
    thread_id: Optional[str] = field(default=None)
    attached_files: list = field(default_factory=list)
    images: list = field(default_factory=list)
    is_scheduled: bool = field(default=False)

logger = logging.getLogger(__name__)

# 指数退避重连延迟序列（秒）：2→5→10→30→60
_RECONNECT_DELAYS = [2, 5, 10, 30, 60]


# ──────────────────────────────────────────────────────────────
#   Markdown → 钉钉 markdown 子集转换器
# ──────────────────────────────────────────────────────────────

def _fmt_for_dingtalk(text: str) -> str:
    """
    将标准 Markdown 转换为钉钉机器人官方支持的 markdown 子集。

    钉钉官方支持（来源：open.dingtalk.com/document/group/custom-robot-access）：
        标题 H1-H6、引用 >、加粗 **、斜体 *、链接 []()、图片 ![]()
    不支持：围栏代码块 ``` 、GFM 表格、内联代码 `

    转换策略：
    - 代码块 → > 引用块（官方支持，视觉上与正文区分）
    - 表格   → 首行加粗 + 竖线分隔文本行
    """
    text = _dt_code_blocks(text)
    text = _dt_tables(text)
    return text


def _dt_code_blocks(text: str) -> str:
    """围栏代码块 → 引用块（> ），钉钉官方唯一支持的类代码视觉效果。"""
    def replacer(m: re.Match) -> str:
        lang = m.group(1).strip()
        code = m.group(2).rstrip("\n")
        header = f"> **[{lang}]**\n" if lang else ""
        body = "\n".join(f"> {line}" for line in code.split("\n"))
        return f"\n\n{header}{body}\n\n"

    return re.sub(r"```(\w*)\n(.*?)```", replacer, text, flags=re.DOTALL)


def _stream_safe_text(text: str) -> str:
    """
    返回可安全流式推送的文本：截取到最近换行边界，
    避免推送半截 Markdown（表格行、粗体/斜体标记等）时光标插入 token 内部产生黑色占位乱码。
    无换行时返回原文（单行响应不含多行 Markdown 结构，直接推送即可）。
    """
    idx = text.rfind('\n')
    return text[:idx + 1] if idx >= 0 else text


def _dt_tables(text: str) -> str:
    """GFM 表格 → 加粗表头 + 竖线分隔文本行。"""
    def replacer(m: re.Match) -> str:
        rows: list[str] = []
        is_header = True
        for line in m.group(0).strip().splitlines():
            stripped = line.strip()
            if not stripped or re.fullmatch(r"\|[\s\-|:]+\|", stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if is_header:
                rows.append(" | ".join(f"**{c}**" for c in cells))
                is_header = False
            else:
                rows.append(" | ".join(cells))
        return "\n\n" + "\n".join(rows) + "\n\n"

    table_re = r"(?m)^\|.+\|\s*\n\|[\s\-|:]+\|\s*\n(?:\|.+\|\s*\n?)+"
    return re.sub(table_re, replacer, text)


class DingTalkCardRenderer:
    """
    钉钉 AI 卡片 v2 流式渲染器。

    架构（参考 github.com/DingTalk-Real-AI/dingtalk-openclaw-connector）：
    - 创建：POST /v1.0/card/instances/createAndDeliver，callbackType=STREAM
      使用模板 _CARD_TEMPLATE_ID（content 字段为 markdown-variable-new，支持完整 GFM）
    - 流式：PUT /v1.0/card/streaming，key="content"，isFull=True 全量发送
    - 终态：PUT /v1.0/card/instances，blockList=[{type:0, markdown:text}] + flowStatus="3"

    Token 设计：接收 get_token 可调用，每次 HTTP 请求现取，token 过期自动刷新。
    "tool" 状态静默忽略，下轮 LLM 文本继续累积。
    """

    THROTTLE_S = 0.3

    def __init__(self, out_track_id: str, get_token: Callable[[], str]) -> None:
        self._out_track_id = out_track_id
        self._get_token = get_token
        self._last_push_at: float = time.monotonic()
        self._last_text: str = ""

    def on_update(self, text: str, status: str) -> None:
        try:
            if status == "generating":
                self._on_generating(text)
            elif status == "tool":
                self._on_tool(text)
            elif status == "done":
                self._on_done(text)
            elif status == "error":
                self.fail()
        except Exception as exc:
            logger.error("AI 卡片更新失败 | status=%s error=%s", status, exc)

    def fail(self) -> None:
        """通知卡片进入失败终态，在已推内容后追加错误提示。"""
        try:
            error_text = self._last_text + "\n\n> ⚠️ 处理失败，请重试。"
            self._finalize(error_text)
        except Exception as exc:
            logger.warning("卡片 fail 通知失败: %s", exc)

    def _mk_headers(self) -> dict:
        """每次调用现取 token，token 过期由 _get_api_token() 内部自动刷新。"""
        return {
            "x-acs-dingtalk-access-token": self._get_token(),
            "Content-Type": "application/json",
        }

    def _push(self, text: str, *, finalize: bool = False) -> None:
        """全量推送到 streaming 端点（isFull=True）。finalize=True 正式关闭流式会话。"""
        if text == self._last_text and not finalize:
            return
        payload = {
            "outTrackId": self._out_track_id,
            "guid": str(uuid.uuid4()),
            "key": "content",
            "content": text,
            "isFull": True,
            "isFinalize": finalize,
            "isError": False,
        }
        httpx.put(_CARD_STREAM_URL, headers=self._mk_headers(), json=payload, timeout=10).raise_for_status()
        self._last_text = text

    def _on_tool(self, text: str) -> None:
        """工具执行期间立即推送状态，告知用户正在处理（不走节流）。"""
        self._push(text)
        self._last_push_at = time.monotonic()  # 重置节流，工具完成后首帧 generating 立即可见

    def _on_generating(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_push_at < self.THROTTLE_S:
            return
        # 截取到最近换行边界再推送，避免半截 Markdown 触发黑色占位符
        safe = _stream_safe_text(text)
        if safe:
            self._push(safe)
            self._last_push_at = now

    def _on_done(self, text: str) -> None:
        self._push(text, finalize=True)  # 发送完整内容并正式关闭 streaming 会话
        self._finalize(text)

    def _finalize(self, text: str) -> None:
        """切换卡片到 FINISHED 终态，blockList 使用 markdown-variable-new 渲染完整 GFM。"""
        block_list = json.dumps([{"type": 0, "markdown": text}], ensure_ascii=False)
        payload = {
            "outTrackId": self._out_track_id,
            "cardData": {
                "cardParamMap": {
                    "blockList": block_list,
                    "content": text,
                    "copy_content": text,
                    "flowStatus": "3",  # FINISHED
                }
            },
        }
        httpx.put(_CARD_UPDATE_URL, headers=self._mk_headers(), json=payload, timeout=10).raise_for_status()


_API_BASE = "https://api.dingtalk.com"
_API_TOKEN_URL = f"{_API_BASE}/v1.0/oauth2/accessToken"
_GROUP_SEND_URL = f"{_API_BASE}/v1.0/robot/groupMessages/send"
_SINGLE_SEND_URL = f"{_API_BASE}/v1.0/robot/oToMessages/batchSend"

# v2 AI 卡片 API（createAndDeliver + streaming + instances）
_CARD_CREATE_URL = f"{_API_BASE}/v1.0/card/instances/createAndDeliver"
_CARD_STREAM_URL = f"{_API_BASE}/v1.0/card/streaming"
_CARD_UPDATE_URL = f"{_API_BASE}/v1.0/card/instances"

# 默认使用 openclaw-channel-dingtalk 验证过的社区模板（content=markdown-variable-new, blockList=loopArray）
# 如需自建模板，在 .env 中设置 DINGTALK_CARD_TEMPLATE_ID
_CARD_TEMPLATE_ID = os.getenv(
    "DINGTALK_CARD_TEMPLATE_ID",
    "675cde2f-f526-40cb-b828-f5b2b57b8b77.schema",
)

# api token TTL 官方约 7200s，提前 5min 刷新
_TOKEN_TTL_BUFFER = 300


class DingTalkGateway(BaseGateway):
    """
    钉钉机器人网关 —— 平台接入层。

    职责：
    1. 维护与钉钉服务器的 WebSocket 长连接（dingtalk-stream SDK）
    2. 解析消息，去重，投入线程池派发给 Agent 层
    3. 通过 v2 AI 互动卡片流式回复（createAndDeliver + streaming + instances）
       降级：sessionWebhook Markdown（卡片创建失败时）
    4. send_file()：oapi 上传 media → api 发文件消息

    会话隔离：session_key 加 dingtalk: 前缀，与飞书上下文完全隔离。
    """

    def __init__(
        self,
        agent: "BaseAgent",
        context: "ConversationContext",
        task_manager: Optional["TaskManager"] = None,
        file_stash=None,
    ) -> None:
        client_id = os.getenv("DINGTALK_CLIENT_ID", "")
        client_secret = os.getenv("DINGTALK_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise ValueError(
                "DINGTALK_CLIENT_ID 和 DINGTALK_CLIENT_SECRET 为必填项，"
                "请在 .env 文件中定义。"
            )

        self._client_id = client_id
        self._client_secret = client_secret
        self._agent = agent
        self._context = context
        self._task_manager = task_manager
        self._file_stash = file_stash

        # 用户白名单：DINGTALK_ALLOWED_USERS 逗号分隔的 staffId 列表
        # 未配置时允许所有人（兼容现有部署），但打印警告
        raw_allowlist = os.getenv("DINGTALK_ALLOWED_USERS", "")
        self._allowed_users: frozenset[str] = frozenset(
            uid.strip() for uid in raw_allowlist.split(",") if uid.strip()
        )
        if not self._allowed_users:
            logger.warning(
                "DINGTALK_ALLOWED_USERS 未配置，任何人均可触发机器人。"
                "建议在 .env 中设置白名单。"
            )

        max_workers = int(os.getenv("DINGTALK_MAX_WORKERS", "10"))
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="dingtalk-worker",
        )

        # 会话级串行锁：防止同一会话并发写共享上下文
        self._session_run_locks: dict[str, threading.Lock] = {}
        self._session_run_locks_guard = threading.Lock()

        # api.dingtalk.com token（发文件/主动消息用）
        self._api_token: str = ""
        self._api_token_expires_at: float = 0.0
        self._api_token_lock = threading.Lock()

        # 媒体上传客户端（oapi.dingtalk.com）
        self._media_client = DingTalkMediaClient(client_id, client_secret)

        # Stream SDK 客户端
        self._stream_client = dingtalk_stream.DingTalkStreamClient(
            credential=dingtalk_stream.Credential(client_id, client_secret)
        )
        handler = _MessageHandler(self)
        self._stream_client.register_callback_handler(
            dingtalk_stream.chatbot.ChatbotMessage.TOPIC, handler
        )
        logger.info("DingTalkGateway 初始化完成 | client_id=%s", client_id[:8] + "***")

    # ──────────────────────────────────────────────────────────
    #   BaseGateway 接口实现
    # ──────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "dingtalk"

    def start(self) -> None:
        """
        阻塞当前线程，启动 Stream SDK 长连接。
        连接断开后按指数退避（2→5→10→30→60s）自动重连，连接稳定超过 60s 则重置计数。
        """
        logger.info("🚀 PersonalAgent 钉钉网关已启动，等待消息...")
        print("🚀 PersonalAgent 钉钉网关已启动，等待消息...", flush=True)
        attempt = 0
        while True:
            connect_start = time.monotonic()
            try:
                self._stream_client.start_forever()
                logger.warning("钉钉 start_forever() 意外返回，将自动重连")
            except Exception as exc:
                logger.error("钉钉连接断开: %s", exc)

            # 连接稳定超过 60s，视为正常断线，重置退避计数
            if time.monotonic() - connect_start > 60:
                attempt = 0

            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            logger.info("第 %d 次重连，%ds 后重试...", attempt, delay)
            time.sleep(delay)
            self._recreate_stream_client()

    def send_text(self, session_key: str, text: str) -> None:
        """
        向指定会话发送文本消息。

        session_key 路由规则：
          - dingtalk:user:{staffId}        → 单聊，走 OpenAPI batchSend
          - dingtalk:group:{conversationId} → 群聊，走 OpenAPI groupMessages/send
          - 其他前缀（feishu:* 等）        → 跳过，多网关广播时的非本平台 key
          - 原始 ID（无 : 或非 dingtalk:） → 跳过（飞书原始 ID 传入时不处理）
        """
        if not session_key.startswith("dingtalk:"):
            return
        parts = session_key.split(":", 2)
        if len(parts) != 3:
            logger.warning("send_text: 无效 session_key=%s", session_key)
            return
        _, id_type, raw_id = parts
        try:
            self._send_markdown_via_api(id_type, raw_id, text)
        except Exception as e:
            logger.error("send_text 失败 | session_key=%s error=%s", session_key, e)

    def send_file(self, session_key: str, file_path: str, filename: str) -> None:
        """
        向指定会话发送文件（两步流程）：
          1. oapi 上传媒体 → media_id（现传现用）
          2. api 发文件消息

        session_key 路由同 send_text，非 dingtalk: 前缀直接跳过。
        """
        if not session_key.startswith("dingtalk:"):
            return
        parts = session_key.split(":", 2)
        if len(parts) != 3:
            logger.warning("send_file: 无效 session_key=%s", session_key)
            return
        _, id_type, raw_id = parts
        try:
            media_id = self._media_client.upload(file_path)
            self._send_file_via_api(id_type, raw_id, media_id, filename)
        except Exception as e:
            logger.error("send_file 失败 | session_key=%s error=%s", session_key, e)

    # ──────────────────────────────────────────────────────────
    #   消息派发（由 _MessageHandler 调用）
    # ──────────────────────────────────────────────────────────

    def dispatch(self, incoming: "ChatbotMessage") -> None:
        """
        在 3s 回调窗口内完成：去重 + 白名单检查 + 创建 v2 AI 卡片 + 提交线程池。
        卡片创建（POST createAndDeliver）在回调里同步完成，用户立即看到占位卡片。
        """
        if self._context.is_processed(incoming.message_id):
            logger.debug("重复消息，跳过 | message_id=%s", incoming.message_id)
            return
        self._context.mark_processed(incoming.message_id)

        # 白名单检查：配置了 DINGTALK_ALLOWED_USERS 时，不在列表中的用户静默拒绝
        if self._allowed_users and incoming.sender_staff_id not in self._allowed_users:
            logger.warning(
                "拒绝未授权用户 | staffId=%s nick=%s",
                incoming.sender_staff_id, incoming.sender_nick,
            )
            return

        event = self._build_event(incoming)
        if not event.content:
            return

        renderer = self._try_create_renderer(incoming)

        session_key = self._compute_session_key(event)
        lock = self._get_session_lock(session_key)
        logger.info(
            "收到消息 | session_key=%s sender=%s content_len=%d",
            session_key, incoming.sender_nick, len(event.content),
        )
        self._executor.submit(self._handle, event, incoming, session_key, lock, renderer)

    def _try_create_renderer(self, incoming: "ChatbotMessage") -> "DingTalkCardRenderer | None":
        """
        通过 createAndDeliver API 创建 v2 AI 卡片，返回 DingTalkCardRenderer；
        失败时返回 None（降级为 sessionWebhook Markdown 文本）。

        使用 callbackType=STREAM 开启流式模式，无需在卡片搭建器中启用"AI卡片"开关。
        """
        try:
            token = self._get_api_token()
            # 仅用于 createAndDeliver 这一次请求；后续推送由 renderer 自己现取 token
            create_headers = {
                "x-acs-dingtalk-access-token": token,
                "Content-Type": "application/json",
            }
            out_track_id = uuid.uuid4().hex

            is_group = incoming.conversation_type == "2"
            open_space_id = (
                f"dtv1.card//IM_GROUP.{incoming.conversation_id}"
                if is_group
                else f"dtv1.card//IM_ROBOT.{incoming.conversation_id}"
            )
            deliver_model = {"robotCode": self._client_id, "extension": "{}"}
            payload: dict = {
                "cardTemplateId": _CARD_TEMPLATE_ID,
                "outTrackId": out_track_id,
                "cardData": {
                    "cardParamMap": {
                        "content": "",
                        "flowStatus": "2",  # INPUTING
                    }
                },
                "callbackType": "STREAM",
                "openSpaceId": open_space_id,
                "userIdType": 1,
            }
            if is_group:
                payload["imGroupOpenSpaceModel"] = {"supportForward": True}
                payload["imGroupOpenDeliverModel"] = deliver_model
            else:
                payload["imRobotOpenSpaceModel"] = {"supportForward": True}
                payload["imRobotOpenDeliverModel"] = {**deliver_model, "spaceType": "IM_ROBOT"}

            resp = httpx.post(_CARD_CREATE_URL, headers=create_headers, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            # 钉钉 createAndDeliver 成功时 HTTP 200，body 含 result 字段或空对象
            if result.get("code", 0) != 0:
                logger.warning("v2 卡片创建响应错误码：%s，降级为文本回复", result)
                return None

            logger.debug("v2 卡片已创建 | outTrackId=%s", out_track_id)
            # 传递 token getter（而非静态 headers），确保长响应期间 token 自动刷新
            return DingTalkCardRenderer(out_track_id, self._get_api_token)
        except Exception as e:
            logger.warning("v2 卡片创建失败，降级为文本回复: %s", e)
            return None

    def _handle(
        self,
        event: DingTalkMessageEvent,
        incoming: "ChatbotMessage",
        session_key: str,
        lock: threading.Lock,
        renderer: "DingTalkCardRenderer | None",
    ) -> None:
        with lock:
            self._process(event, incoming, session_key, renderer)

    def _process(
        self,
        event: DingTalkMessageEvent,
        incoming: "ChatbotMessage",
        session_key: str,
        renderer: "DingTalkCardRenderer | None",
    ) -> None:
        """
        调用 Agent，通过 v2 AI 卡片推送结果（renderer 由 dispatch 预先创建）。
        降级路径（renderer=None）：等待 done 后一次 sessionWebhook Markdown 回复。
        """
        if renderer is not None:
            try:
                self._agent.process(event, on_update=renderer.on_update)
            except Exception as e:
                logger.error(
                    "Agent 处理失败 | session_key=%s error=%s", session_key, e, exc_info=True
                )
                renderer.fail()
        else:
            # ── 降级路径：sessionWebhook Markdown（一次发送） ──────────────
            final_reply: list[str] = []

            def on_update(text: str, status: str) -> None:  # type: ignore[misc]
                if status in ("done", "error"):
                    final_reply.append(text)

            try:
                self._agent.process(event, on_update=on_update)
            except Exception as e:
                logger.error(
                    "Agent 处理失败 | session_key=%s error=%s", session_key, e, exc_info=True
                )
                self._reply_markdown(incoming, f"处理出错：{e}")
                return

            reply = final_reply[-1] if final_reply else ""
            if reply:
                self._reply_markdown(incoming, reply)

    # ──────────────────────────────────────────────────────────
    #   发送工具方法
    # ──────────────────────────────────────────────────────────

    def _reply_markdown(self, incoming: "ChatbotMessage", text: str) -> None:
        """通过 sessionWebhook 直接回复，无需 token，群/单聊统一。"""
        if not incoming.session_webhook:
            logger.warning("session_webhook 为空，无法回复")
            return
        # sessionWebhook 有过期时间（通常 1 小时），正常情况处理完毕前不会过期
        # sessionWebhook markdown 与卡片旧版变量同等限制，统一格式化后发送
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "回复",
                "text": _fmt_for_dingtalk(text),
            },
        }
        try:
            resp = httpx.post(
                incoming.session_webhook,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode", 0) != 0:
                logger.error("sessionWebhook 回复失败: %s", result)
        except Exception as e:
            logger.error("sessionWebhook 发送异常: %s", e)

    def _send_markdown_via_api(self, id_type: str, raw_id: str, text: str) -> None:
        """通过 OpenAPI 主动发 Markdown 消息（send_text 后端，需 api token）。"""
        token = self._get_api_token()
        payload = {
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": "消息", "text": _fmt_for_dingtalk(text)}, ensure_ascii=False),
            "robotCode": self._client_id,
        }
        if id_type == "group":
            payload["openConversationId"] = raw_id
            url = _GROUP_SEND_URL
        else:
            payload["userIds"] = [raw_id]
            url = _SINGLE_SEND_URL

        resp = httpx.post(
            url,
            headers={"x-acs-dingtalk-access-token": token},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "processQueryKey" not in data and data.get("code"):
            raise RuntimeError(f"API 发消息失败: {data}")

    def _send_file_via_api(
        self, id_type: str, raw_id: str, media_id: str, filename: str
    ) -> None:
        """通过 OpenAPI 发文件消息（media_id 已上传）。"""
        token = self._get_api_token()
        msg_param = json.dumps({"mediaId": media_id, "fileName": filename, "fileType": "file"}, ensure_ascii=False)
        payload = {
            "msgKey": "sampleFile",
            "msgParam": msg_param,
            "robotCode": self._client_id,
        }
        if id_type == "group":
            payload["openConversationId"] = raw_id
            url = _GROUP_SEND_URL
        else:
            payload["userIds"] = [raw_id]
            url = _SINGLE_SEND_URL

        resp = httpx.post(
            url,
            headers={"x-acs-dingtalk-access-token": token},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "processQueryKey" not in data and data.get("code"):
            raise RuntimeError(f"API 发文件失败: {data}")
        logger.info("文件消息发送成功 | filename=%s id_type=%s", filename, id_type)

    # ──────────────────────────────────────────────────────────
    #   api token 管理（api.dingtalk.com，独立于 oapi token）
    # ──────────────────────────────────────────────────────────

    def _refresh_api_token(self) -> None:
        resp = httpx.post(
            _API_TOKEN_URL,
            json={"appKey": self._client_id, "appSecret": self._client_secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "accessToken" not in data:
            raise RuntimeError(f"获取 api token 失败: {data}")
        self._api_token = data["accessToken"]
        expires_in = int(data.get("expireIn", 7200))
        self._api_token_expires_at = time.monotonic() + expires_in - _TOKEN_TTL_BUFFER
        logger.debug("api token 已刷新，有效期 %ds", expires_in)

    def _get_api_token(self) -> str:
        with self._api_token_lock:
            if time.monotonic() >= self._api_token_expires_at:
                self._refresh_api_token()
            return self._api_token

    # ──────────────────────────────────────────────────────────
    #   辅助方法
    # ──────────────────────────────────────────────────────────

    def _build_event(self, incoming: "ChatbotMessage") -> DingTalkMessageEvent:
        """将钉钉原始消息转换为 DingTalkMessageEvent。
        ID 统一加 dingtalk_ 前缀，确保 ConversationContext 会话 key 与飞书完全隔离。
        """
        chat_type = "group" if incoming.conversation_type == "2" else "p2p"
        content = (incoming.text.content if incoming.text else "").strip()
        return DingTalkMessageEvent(
            message_id=incoming.message_id,
            chat_id=f"dingtalk_{incoming.conversation_id}",
            chat_type=chat_type,
            content=content,
            sender_open_id=f"dingtalk_{incoming.sender_staff_id}",
        )

    def _compute_session_key(self, event: DingTalkMessageEvent) -> str:
        """与 ConversationAgent 内部保持一致，用同一套 build_session_key 计算。"""
        from ..context.conversation_context import ConversationContext
        return ConversationContext.build_session_key(
            chat_type=event.chat_type,
            sender_open_id=event.sender_open_id,
            chat_id=event.chat_id,
            thread_id=event.thread_id,
        )

    def _get_session_lock(self, session_key: str) -> threading.Lock:
        with self._session_run_locks_guard:
            if session_key not in self._session_run_locks:
                self._session_run_locks[session_key] = threading.Lock()
            return self._session_run_locks[session_key]

    def _recreate_stream_client(self) -> None:
        """重连前重建 SDK 客户端（旧客户端状态不可复用）。"""
        self._stream_client = dingtalk_stream.DingTalkStreamClient(
            credential=dingtalk_stream.Credential(self._client_id, self._client_secret)
        )
        handler = _MessageHandler(self)
        self._stream_client.register_callback_handler(
            dingtalk_stream.chatbot.ChatbotMessage.TOPIC, handler
        )


# ──────────────────────────────────────────────────────────────
#   Stream SDK 消息回调
# ──────────────────────────────────────────────────────────────

class _MessageHandler(dingtalk_stream.ChatbotHandler):
    """
    接收钉钉推送的聊天消息，转交给 DingTalkGateway 处理。
    process() 必须在 3s 内返回（钉钉要求），耗时操作由 gateway.dispatch() 投入线程池。
    """

    def __init__(self, gateway: DingTalkGateway) -> None:
        super().__init__()
        self._gw = gateway

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming = ChatbotMessage.from_dict(callback.data)
            self._gw.dispatch(incoming)
        except Exception as e:
            logger.error("消息解析/派发失败: %s", e, exc_info=True)
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"
