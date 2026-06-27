"""
钉钉机器人网关

接收：dingtalk-stream SDK（反向 WebSocket 长连接，无需公网 IP）
回复：v2 AI 互动卡片（createAndDeliver + callbackType=STREAM，支持完整 GFM）
     降级：sessionWebhook Markdown（卡片创建失败时）
发文件：OpenAPI 两步（oapi 上传 media → api 发消息）

卡片方案参考：github.com/soimy/openclaw-channel-dingtalk
"""

import atexit
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
from typing import TYPE_CHECKING, Callable, Optional

import httpx

try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
except ImportError as _e:
    raise ImportError(
        "dingtalk-stream 未安装，请执行: pip install dingtalk-stream"
    ) from _e

from ..routing import AccessPolicy, SessionRunLocks
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
    sender_name: str = ""   # 发送者昵称 senderNick，仅用于日志展示
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

def _paragraph_breaks(text: str) -> str:
    """单 \n → \n\n（钉钉 Markdown 单换行等同于空格，需双换行才成段落/换行）。

    这是钉钉 Markdown 自身属性，对所有渠道（webhook / AI 卡片 / 旧模板）通用，
    是排版正确的根本一层。**表格感知 + 代码块感知**：GFM 表格块与围栏代码块
    内部的单换行不扩展，否则会被插空行打散（实测）；仅普通文本行之间扩展。
    幂等：已是 \n\n 的不重复扩展。
    """
    lines = text.split("\n")
    n = len(lines)

    # 标记受保护行：围栏代码块内、GFM 表格行（其内部换行保持单 \n）
    protected = [False] * n
    in_fence = False
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith("```"):
            protected[i] = True
            in_fence = not in_fence
            continue
        if in_fence or s.startswith("|"):
            protected[i] = True

    parts: list[str] = []
    for i, ln in enumerate(lines):
        parts.append(ln)
        if i == n - 1:
            break
        # 任一侧空行（已是段落边界）或两侧同在受保护块内 → 保持单 \n；否则补成 \n\n
        if not ln.strip() or not lines[i + 1].strip() or (protected[i] and protected[i + 1]):
            parts.append("\n")
        else:
            parts.append("\n\n")
    return "".join(parts)


def _dt_code_blocks(text: str) -> str:
    """围栏代码块 → 引用块（> ），钉钉官方唯一支持的类代码视觉效果。"""
    def replacer(m: re.Match) -> str:
        lang = m.group(1).strip()
        code = m.group(2).rstrip("\n")
        header = f"> **[{lang}]**\n" if lang else ""
        body = "\n".join(f"> {line}" for line in code.split("\n"))
        return f"\n\n{header}{body}\n\n"

    return re.sub(r"```(\w*)\n(.*?)```", replacer, text, flags=re.DOTALL)



def _trim_trailing_table(text: str) -> str:
    """
    若文本末尾存在 GFM 表格（无论完整与否），截断到该表格块起始位置前。

    根本原因：钉钉 AI 卡片在 flowStatus=2（streaming）阶段用 content 字段渲染时，
    对 GFM 表格的列宽计算有 bug——列宽按首帧锁定，后续内容撑不开，
    导致每字符单独换行。完整表格同样触发此问题。
    解决策略（参考 soimy/openclaw-channel-dingtalk 的 block-boundary 缓冲）：
    流式帧永远不推送表格，表格仅在 _finalize 的 blockList 里渲染。
    """
    lines = text.split('\n')

    # 找最后一个非空行
    last_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_idx = i
            break

    if last_idx == -1 or not lines[last_idx].strip().startswith('|'):
        return text  # 末尾不是表格行，无需处理

    # 向上找连续的表格行起始（GFM 表格内不允许空行）
    table_start = last_idx
    for i in range(last_idx - 1, -1, -1):
        if lines[i].strip().startswith('|'):
            table_start = i
        else:
            break

    return '\n'.join(lines[:table_start]).rstrip()


def _strip_blockquotes(text: str) -> str:
    """去除 markdown 引用前缀，避免钉钉 streaming 渲染器引用样式粘滞。"""
    return re.sub(r'^> ?', '', text, flags=re.MULTILINE)


def _ensure_table_blank_lines(text: str) -> str:
    """
    在 GFM 表格块前插入空行，防止钉钉渲染器将前一行普通文本误识别为表格内容。
    参考 DingTalk-Real-AI/dingtalk-openclaw-connector 的 ensureTableBlankLines()。
    """
    lines = text.split('\n')
    out: list[str] = []
    for line in lines:
        prev = out[-1] if out else ''
        # 当前行是表格行，前一行非空且非表格行 → 插入空行
        if line.strip().startswith('|') and prev.strip() and not prev.strip().startswith('|'):
            out.append('')
        out.append(line)
    return '\n'.join(out)


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


def render_for_dingtalk(text: str, *, target: str, streaming: bool = False) -> str:
    """钉钉出站文本统一归一化边界——所有回复路径（卡片/webhook）都走这里，杜绝漂移。

    分层（可组合、各管一摊）：
      L0 段落换行（通用·表格/代码感知）：_paragraph_breaks —— 真正修排版的一层
      L1 渠道能力：webhook 拍平 GFM（受限子集）；card 保留完整 GFM
      L2 流式守卫：非终态帧裁掉生长中的尾部表格（防 content 字段列宽误锁）；
                  旧模板流式额外拍平表格、去引用前缀

    Args:
        target:    "webhook" | "card_ai"（自建 AI 卡片）| "card_legacy"（旧社区模板）
        streaming: True=非终态流式帧；False=终态/处理中（保留完整 GFM）
    """
    text = _paragraph_breaks(text)  # L0 通用

    if target == "webhook":
        text = _dt_code_blocks(text)  # 代码块 → 引用
        text = _dt_tables(text)       # GFM 表格 → 竖线文本
        text = re.sub(r"\n{3,}", "\n\n", text)  # 收敛多余空行（拍平表格会叠出 3+ 换行）
        return text.rstrip("\n")

    # card_ai / card_legacy：终态保留完整 GFM（钉钉卡片组件可原生渲染）
    if streaming:  # L2 流式守卫
        if target == "card_legacy":
            text = _dt_tables(text)        # 旧模板流式帧拍平表格，防列宽误锁
        text = _trim_trailing_table(text)  # 裁掉正在生长的尾部表格
        if target == "card_legacy":
            text = _strip_blockquotes(text)
    text = _ensure_table_blank_lines(text)  # L1：表前补空行，保证 GFM 表格被识别
    return text.rstrip("\n")


# ──────────────────────────────────────────────────────────────
#   钉钉卡片 API 全局限流（跨会话共享，防 403 QpsLimit）
# ──────────────────────────────────────────────────────────────

# 钉钉卡片接口总 QPS 约 40（按机器人 appKey 计，所有会话累加，与会话数无关）。
# 保守取 20，给 createAndDeliver 等非流式调用留出余量。
_CARD_MAX_QPS = float(os.getenv("DINGTALK_CARD_MAX_QPS", "20"))
# 撞 QpsLimit 后的退避时长（秒）
_CARD_QPS_BACKOFF_S = 2.0


class _CardRateLimiter:
    """进程级全局令牌桶：所有钉钉卡片 streaming/更新请求共享同一速率上限。

    背景：钉钉卡片 API 的 QPS 按机器人 appKey 累加，与会话数无关。
    DingTalkCardRenderer 每个会话各自的 THROTTLE_S 只能约束「单会话」速度，
    多个会话并发流式推送时总量会叠加超过钉钉上限，触发 403 QpsLimit。
    本限流器把所有会话的卡片请求汇到一处统一限速：无论几个会话同时在线，
    合计速度都不超过 _CARD_MAX_QPS。

    线程安全：取令牌期间持锁（含必要的 sleep），天然把并发请求串行成
    「每 1/QPS 秒放行一个」，避免多线程各自判断「有令牌」把令牌桶击穿。
    """

    def __init__(self, max_qps: float, backoff_s: float) -> None:
        self._max_qps = max_qps
        self._backoff_s = backoff_s
        self._tokens = max_qps
        self._last_refill = time.monotonic()
        self._backoff_until = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """阻塞直到取得一个令牌（必要时先等退避结束，再等令牌补充）。"""
        with self._lock:
            now = time.monotonic()
            if now < self._backoff_until:
                time.sleep(self._backoff_until - now)
                now = time.monotonic()
            self._refill(now)
            if self._tokens < 1:
                time.sleep((1 - self._tokens) / self._max_qps)
                self._refill(time.monotonic())
            self._tokens -= 1

    def trigger_backoff(self) -> None:
        """撞 QpsLimit 后调用：暂停所有卡片请求一段时间并清空令牌。"""
        with self._lock:
            self._backoff_until = time.monotonic() + self._backoff_s
            self._tokens = 0
            self._last_refill = self._backoff_until

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._max_qps, self._tokens + elapsed * self._max_qps)
            self._last_refill = now


# 全局唯一实例：跨所有 DingTalkCardRenderer / 会话共享
_CARD_RATE_LIMITER = _CardRateLimiter(_CARD_MAX_QPS, _CARD_QPS_BACKOFF_S)


def _is_qps_limit_error(exc: httpx.HTTPStatusError) -> bool:
    """识别钉钉卡片接口的 403 QpsLimit 限流错误（瞬时、可重试）。

    与「网络/鉴权等真失败」区分开：只有这类错误才静默退避重试，
    其余错误照常抛出交由上层降级，避免把可恢复错误展示成「处理失败」。
    """
    resp = exc.response
    if resp.status_code != 403:
        return False
    try:
        code = str(resp.json().get("code", ""))
    except Exception:
        code = ""
    return "QpsLimit" in code or "QpsLimit" in (resp.text or "")


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

    THROTTLE_S = 0.05  # 旧社区模板：flusher 已在 card_update_interval 节流，renderer 只需防暴打
    # 自建 AI 卡片模板：流式帧必须按 ~0.3s 节流（对齐 QwenPaw _STREAM_DELTA_MIN_INTERVAL_S=0.3 /
    # soimy throttleMs=300）。高频帧（0.05s）会冲垮 AI流式富文本 的打字机 → 表格列宽误锁拉长 +
    # 内容重复重渲染。这是流式渲染干净的关键。
    AI_STREAM_THROTTLE_S = 0.3

    def __init__(
        self,
        out_track_id: str,
        get_token: Callable[[], str],
        on_register: Optional[Callable[[str], None]] = None,
        on_unregister: Optional[Callable[[str], None]] = None,
        ai_card_mode: bool = False,
    ) -> None:
        self._out_track_id = out_track_id
        self._get_token = get_token
        self._last_push_at: float = time.monotonic()
        self._last_text: str = ""
        # 处理中卡片登记回调（供网关停机扫尾用）；默认 no-op
        self._on_register = on_register or (lambda otid: None)
        self._on_unregister = on_unregister or (lambda otid: None)
        # ai_card_mode=True：自建 AI 卡片模板（正文绑 content，状态靠流式 isFinalize/isError 驱动）。
        # False：旧社区模板（blockList + flowStatus）。由 DINGTALK_AI_CARD_TEMPLATE_ID 是否配置决定。
        self._ai_card_mode = ai_card_mode
        # 终态守卫：卡片一旦 finalize/error，任何迟到的非终态帧（generating/tool/processing）
        # 都必须丢弃，否则会把已完成的卡片重新翻回「输出中」并永久卡住（card-flusher 线程
        # 可能在 done 之后才挤出迟到帧）。锁保证「判断终态 + 推送」原子化，杜绝中途竞态。
        self._finalized = False
        self._render_lock = threading.Lock()

    _TERMINAL_STATUSES = ("done", "error")

    def on_update(self, text: str, status: str) -> None:
        with self._render_lock:
            if self._finalized and status not in self._TERMINAL_STATUSES:
                logger.info("卡片已终态，丢弃迟到的 %s 帧（防止翻回输出中）| otid=%s",
                            status, self._out_track_id)
                return
            try:
                self._dispatch(text, status)
                if status in self._TERMINAL_STATUSES:
                    self._finalized = True
            except Exception as exc:
                logger.error("AI 卡片更新失败 | status=%s error=%s", status, exc)

    def _dispatch(self, text: str, status: str) -> None:
        """按状态分发（持 _render_lock 调用；异常由 on_update 统一兜底）。"""
        if status == "generating":
            self._on_generating(text)
        elif status == "tool":
            self._on_tool(text)
        elif status == "processing":
            self.set_processing(text)
        elif status == "done":
            self._on_done(text)
        elif status == "error":
            self._finalize_error()

    def _finalize_error(self) -> None:
        """失败终态（持 _render_lock 调用）：在已推内容后追加错误提示。"""
        error_text = self._last_text + "\n\n> ⚠️ 处理失败，请重试。"
        self._finalize_terminal(error_text, success=False)

    def fail(self) -> None:
        """外部直接调用的失败入口：与 on_update 共用终态守卫，幂等、加锁。"""
        with self._render_lock:
            if self._finalized:
                return
            try:
                self._finalize_error()
                self._finalized = True
            except Exception as exc:
                logger.warning("卡片 fail 通知失败: %s", exc)

    def _mk_headers(self) -> dict:
        """每次调用现取 token，token 过期由 _get_api_token() 内部自动刷新。"""
        return {
            "x-acs-dingtalk-access-token": self._get_token(),
            "Content-Type": "application/json",
        }

    def _put_card(self, url: str, payload: dict) -> None:
        """经全局限流后 PUT 卡片接口；撞 QpsLimit 时退避重试一次。

        仅对瞬时可恢复的 QpsLimit 做静默重试；其余错误照常抛出，
        交由上层（on_update 的 except / 降级路径）处理，不掩盖真失败。
        """
        _CARD_RATE_LIMITER.acquire()
        try:
            httpx.put(url, headers=self._mk_headers(), json=payload, timeout=10).raise_for_status()
            return
        except httpx.HTTPStatusError as exc:
            if not _is_qps_limit_error(exc):
                raise
        # QpsLimit：退避后重试一次。流式帧带 guid，刷新以避免被去重丢弃。
        logger.warning("钉钉卡片撞 QpsLimit，退避 %.1fs 后重试 | url=%s", _CARD_QPS_BACKOFF_S, url)
        _CARD_RATE_LIMITER.trigger_backoff()
        if "guid" in payload:
            payload["guid"] = str(uuid.uuid4())
        _CARD_RATE_LIMITER.acquire()
        httpx.put(url, headers=self._mk_headers(), json=payload, timeout=10).raise_for_status()

    def _push(self, text: str, *, finalize: bool = False, is_error: bool = False) -> None:
        """全量推送到 streaming 端点（isFull=True）。

        归一化统一收口到 render_for_dingtalk：
        - L0 表格感知段落换行对所有帧生效（修结构化短消息挤一坨）。
        - 流式帧（非终态）裁生长中的尾部表格、靠 0.3s 节流防打字机被冲垮（见 _on_generating）。
        - 终态帧保留完整 GFM。前缀单调由 L0/L2 的确定性逐帧变换保证。
        """
        terminal = finalize or is_error
        target = "card_ai" if self._ai_card_mode else "card_legacy"
        content = render_for_dingtalk(text, target=target, streaming=not terminal)
        if content == self._last_text and not terminal:
            return
        payload = {
            "outTrackId": self._out_track_id,
            "guid": str(uuid.uuid4()),
            "key": "content",
            "content": content,
            "isFull": True,
            "isFinalize": finalize,
            "isError": is_error,
        }
        self._put_card(_CARD_STREAM_URL, payload)
        self._last_text = content

    def _set_processing_content(self, text: str) -> None:
        """ai_card_mode：instances 更新 content + flowStatus=1（处理中），**不发流式帧**，
        从而保持「处理中」状态（钉钉一旦收到流式帧就会切到「输出中」）。
        前提：模板的「处理中」状态布局里有一个绑 content 的文本/富文本组件来显示该文字。"""
        normalized = render_for_dingtalk(text, target="card_ai", streaming=False)
        payload = {
            "outTrackId": self._out_track_id,
            "cardData": {"cardParamMap": {"content": normalized, "flowStatus": _FLOW_PROCESSING}},
        }
        self._put_card(_CARD_UPDATE_URL, payload)

    def _on_tool(self, text: str) -> None:
        """工具执行期间显示状态。新模板停在「处理中」（不抢跑到输出中）；旧模板走流式。"""
        if self._ai_card_mode:
            self._set_processing_content(text)
        else:
            self._push(text)
        self._last_push_at = time.monotonic()  # 重置节流，工具完成后首帧 generating 立即可见

    def _on_generating(self, text: str) -> None:
        now = time.monotonic()
        throttle = self.AI_STREAM_THROTTLE_S if self._ai_card_mode else self.THROTTLE_S
        if now - self._last_push_at < throttle:
            return
        clean = text.removesuffix(" ▌")
        if clean:
            self._push(clean)
            self._last_push_at = now

    def _on_done(self, text: str) -> None:
        self._finalize_terminal(text, success=True)

    def _finalize_terminal(self, text: str, *, success: bool) -> None:
        """切换卡片到终态并注销待办登记。

        新模板(ai_card_mode)：发流式 isFinalize/isError 帧 → 完成/失败状态布局。
        旧模板：instances API 置 FINISHED/FAILED（_finalize_with_status 内部已注销）。
        """
        if self._ai_card_mode:
            self._push(text, finalize=success, is_error=not success)
            self._on_unregister(self._out_track_id)
        else:
            self._finalize_with_status(text, _FLOW_FINISHED if success else _FLOW_FAILED)

    def set_processing(self, text: str) -> None:
        """非终态：把卡片置为「处理中」并显示提示文字，保持卡片可被后续 finalize。

        新模板(ai_card_mode)：instances 更新 content + flowStatus=1，停在「处理中」状态
        （工作流执行期间没有真实输出，不该进"输出中"）。
        旧模板：用 flowStatus=2(INPUTING) instances 更新（PROCESSING 内置转圈会无视文字）。
        """
        if self._ai_card_mode:
            self._set_processing_content(text)  # 处理中状态显示「正在执行【X】…」
            self._on_register(self._out_track_id)
            return
        # INPUTING（非终态，可显示文字 + 边框动画）
        self._last_text = self._put_blocklist(text, _FLOW_INPUTING)
        self._on_register(self._out_track_id)

    def _put_blocklist(self, text: str, flow_status: str) -> str:
        """旧模板：instances API 用 blockList(markdown-variable-new) 渲染完整 GFM 并置 flowStatus。

        表格空行同样补上，确保渲染正确。返回规范化后的文本，供调用方更新 _last_text。
        """
        normalized = render_for_dingtalk(text, target="card_legacy", streaming=False)
        block_list = json.dumps([{"type": 0, "markdown": normalized}], ensure_ascii=False)
        payload = {
            "outTrackId": self._out_track_id,
            "cardData": {
                "cardParamMap": {
                    "blockList": block_list,
                    "content": "",
                    "copy_content": normalized,
                    "flowStatus": flow_status,
                }
            },
        }
        self._put_card(_CARD_UPDATE_URL, payload)
        return normalized

    def _finalize_with_status(self, text: str, flow_status: str) -> None:
        """instances API 置终态，blockList 使用 markdown-variable-new 渲染完整 GFM。"""
        self._put_blocklist(text, flow_status)
        self._on_unregister(self._out_track_id)


_API_BASE = "https://api.dingtalk.com"
_API_TOKEN_URL = f"{_API_BASE}/v1.0/oauth2/accessToken"
_GROUP_SEND_URL = f"{_API_BASE}/v1.0/robot/groupMessages/send"
_SINGLE_SEND_URL = f"{_API_BASE}/v1.0/robot/oToMessages/batchSend"

# v2 AI 卡片 API（createAndDeliver + streaming + instances）
_CARD_CREATE_URL = f"{_API_BASE}/v1.0/card/instances/createAndDeliver"
_CARD_STREAM_URL = f"{_API_BASE}/v1.0/card/streaming"
_CARD_UPDATE_URL = f"{_API_BASE}/v1.0/card/instances"

# AI 卡片 flowStatus 五态（对齐 soimy AICardStatus；"1"/"5" 为本次新增使用）
_FLOW_PROCESSING = "1"   # 处理中（工作流 / 工具执行）
_FLOW_INPUTING = "2"     # 输入中（建卡 / 文本生成）
_FLOW_FINISHED = "3"     # 完成
_FLOW_FAILED = "5"       # 失败

# 消息表情回复 API：收到调用时在用户消息上贴一个 👌（OK）表情，替代发独立贴纸消息
# emotionType=1 为普通表情（无背景气泡）；OK 对应钉钉标准表情 emotion_023
_EMOTION_REPLY_URL = f"{_API_BASE}/v1.0/robot/emotion/reply"
_ACK_EMOTION_ID = "emotion_023"
_ACK_EMOTION_NAME = "OK"

# 默认使用 openclaw-channel-dingtalk 验证过的社区模板（content=markdown-variable-new, blockList=loopArray）
# 如需自建模板，在 .env 中设置 DINGTALK_CARD_TEMPLATE_ID
_CARD_TEMPLATE_ID = os.getenv(
    "DINGTALK_CARD_TEMPLATE_ID",
    "675cde2f-f526-40cb-b828-f5b2b57b8b77.schema",
)

# 自建 AI 卡片模板（正文绑 content，四态彩色横幅，状态靠流式 isFinalize/isError 驱动）。
# 配置后启用"新模式"；留空则用上面的社区模板，效果完全不变（自动回退）。
_AI_CARD_TEMPLATE_ID = os.getenv("DINGTALK_AI_CARD_TEMPLATE_ID", "")

# api token TTL 官方约 7200s，提前 5min 刷新
_TOKEN_TTL_BUFFER = 300

# 活跃卡片落盘登记表：进程崩溃 / kill -9 / 断电后，重启时把残留在「处理中/输出中」
# 的卡片强制 finalize，避免永久转圈。参考 QwenPaw AICardPendingStore + _recover_active_cards。
# 路径与项目其它状态文件一致（<项目根>/data/）。
_ACTIVE_CARDS_FILE = Path(__file__).resolve().parents[2] / "data" / "dingtalk-active-cards.json"
_CARD_RECOVERY_TEXT = "⚠️ 服务已重启，上次的处理已中断，请重新发送。"


class _PendingCardStore:
    """活跃卡片的线程安全磁盘登记表，用于跨进程重启恢复。

    与内存态 `_pending_cards` 互补：
    - 内存表持有 token getter，供本进程 atexit 优雅退出时兜底 finalize；
    - 本表落盘 out_track_id，供新进程启动时 finalize 残留卡片，覆盖 atexit 兜不住的
      kill -9 / 崩溃 / 断电场景。

    多个 dingtalk-worker 线程并发读改写同一文件，故全程持锁；写入用 tmp + replace 原子落盘。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cards: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            cards = data.get("pending_cards", []) if isinstance(data, dict) else []
            self._cards = {
                c["out_track_id"]: c
                for c in cards
                if isinstance(c, dict) and c.get("out_track_id")
            }
        except FileNotFoundError:
            self._cards = {}
        except Exception:
            logger.warning("钉钉活跃卡片登记表读取失败，按空处理", exc_info=True)
            self._cards = {}

    def _flush_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": int(time.time() * 1000),
                "pending_cards": list(self._cards.values()),
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception:
            logger.warning("钉钉活跃卡片登记表写入失败", exc_info=True)

    def add(self, out_track_id: str, ai_card_mode: bool) -> None:
        with self._lock:
            if out_track_id in self._cards:
                return
            self._cards[out_track_id] = {
                "out_track_id": out_track_id,
                "ai_card_mode": ai_card_mode,
                "created_at": int(time.time() * 1000),
            }
            self._flush_locked()

    def remove(self, out_track_id: str) -> None:
        with self._lock:
            if self._cards.pop(out_track_id, None) is not None:
                self._flush_locked()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._cards.values())


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
        self._access = AccessPolicy.from_env("dingtalk")
        if self._access.is_open:
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
        self._run_locks = SessionRunLocks()

        # 未完成卡片的双层兜底登记（建卡即登记，finalize 时注销）：
        # 1) 内存表 _pending_cards：out_track_id -> get_token 闭包，供 atexit 优雅退出兜底；
        # 2) 磁盘表 _card_store：落盘 out_track_id，供下次启动 _recover_pending_cards() 兜底，
        #    覆盖 atexit 兜不住的 kill -9 / 崩溃 / 断电场景，避免卡片永久卡在「处理中/输出中」。
        self._pending_cards: dict[str, Callable[[], str]] = {}
        self._card_store = _PendingCardStore(_ACTIVE_CARDS_FILE)
        # 自建 AI 卡片模式开关：配了 DINGTALK_AI_CARD_TEMPLATE_ID 才启用，否则回退社区模板。
        self._ai_card_mode = bool(_AI_CARD_TEMPLATE_ID)
        atexit.register(self._finalize_pending_cards)

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
        self._recover_pending_cards()
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

    def send_image_text(self, session_key: str, image_path: str, text: str) -> None:
        """
        向指定会话发送「图片 + 文字」合并的一条消息（图在前、文字在后）。

        先把本地图上传 media 拿 media_id（/media/upload 返回原值，含 @ 前缀），
        再用 sampleMarkdown 发 ![](media_id) + 文字一条消息。钉钉 markdown 支持把
        media_id 直接当作图片地址（注意：不能用 down.dingtalk.com 下载 URL，那个
        在客户端不可访问会裂图；而 media_id 与 sampleImageMsg 用的是同一份、可渲染）。

        session_key 路由同 send_text，非 dingtalk: 前缀直接跳过。
        """
        if not session_key.startswith("dingtalk:"):
            return
        parts = session_key.split(":", 2)
        if len(parts) != 3:
            logger.warning("send_image_text: 无效 session_key=%s", session_key)
            return
        _, id_type, raw_id = parts
        try:
            media_id = self._media_client.upload(image_path)
            markdown = f"![]({media_id})\n\n{text}" if text else f"![]({media_id})"
            self._send_markdown_via_api(id_type, raw_id, markdown)
        except Exception as e:
            logger.error("send_image_text 失败 | session_key=%s error=%s", session_key, e)

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
        if not self._access.is_allowed(incoming.sender_staff_id):
            logger.warning(
                "拒绝未授权用户 | staffId=%s nick=%s",
                incoming.sender_staff_id, incoming.sender_nick,
            )
            return

        event = self._build_event(incoming)
        if not event.content:
            return

        # 收到调用：在用户消息上贴一个 👌 表情回复（替代发送独立的「收到」贴纸消息）
        threading.Thread(target=self._react, args=(incoming,), daemon=True).start()

        renderer = self._try_create_renderer(incoming)

        session_key = self._compute_session_key(event)
        lock = self._run_locks.get(session_key)
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
                else f"dtv1.card//IM_ROBOT.{incoming.sender_staff_id}"
            )
            deliver_model = {"robotCode": self._client_id, "extension": "{}"}
            payload: dict = {
                "cardTemplateId": _AI_CARD_TEMPLATE_ID or _CARD_TEMPLATE_ID,
                "outTrackId": out_track_id,
                "cardData": {
                    "cardParamMap": {
                        "content": "🤔 思考中…",
                        # 新模板从「处理中」起步（建卡即输出中是错的，会导致 输出中→处理中→输出中）；
                        # 旧社区模板沿用 INPUTING。
                        "flowStatus": _FLOW_PROCESSING if _AI_CARD_TEMPLATE_ID else _FLOW_INPUTING,
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
                payload["imRobotOpenDeliverModel"] = {
                    **deliver_model,
                    "spaceType": "IM_ROBOT",
                    "userId": incoming.sender_staff_id,
                }

            resp = httpx.post(_CARD_CREATE_URL, headers=create_headers, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            # 钉钉 createAndDeliver 成功时 HTTP 200，body 含 result 字段或空对象
            if result.get("code", 0) != 0:
                logger.warning("v2 卡片创建响应错误码：%s，降级为文本回复", result)
                return None

            logger.debug("v2 卡片已创建 | outTrackId=%s", out_track_id)
            # 建卡即登记（覆盖普通流式回复，不止 set_processing 的工作流卡片）：
            # 任何已建但未 finalize 的卡片，进程异常退出后都能被重启恢复。
            self._register_pending_card(out_track_id)
            # 传递 token getter（而非静态 headers），确保长响应期间 token 自动刷新
            return DingTalkCardRenderer(
                out_track_id,
                self._get_api_token,
                on_register=self._register_pending_card,
                on_unregister=self._unregister_pending_card,
                ai_card_mode=self._ai_card_mode,
            )
        except Exception as e:
            logger.warning("v2 卡片创建失败，降级为文本回复: %s", e)
            return None

    def stop(self) -> None:
        """优雅停止：把所有未完成卡片实时 finalize 成中断提示。

        由 main.py 的退出信号处理器调用（os._exit 前）。复用 atexit 同款收尾逻辑：
        成功 finalize 即从磁盘表销账，失败则保留登记留给下次启动恢复。同步、幂等、不抛出。
        """
        self._finalize_pending_cards()

    def _register_pending_card(self, out_track_id: str) -> None:
        """登记一张未完成卡片（内存表 + 磁盘表）。幂等。"""
        self._pending_cards[out_track_id] = self._get_api_token
        self._card_store.add(out_track_id, self._ai_card_mode)

    def _unregister_pending_card(self, out_track_id: str) -> None:
        """卡片进入终态后注销登记（内存表 + 磁盘表）。幂等。"""
        self._pending_cards.pop(out_track_id, None)
        self._card_store.remove(out_track_id)

    def _finalize_pending_cards(self) -> None:
        """进程优雅退出（atexit）时把所有未完成卡片 finalize 成中断提示。尽力而为，逐卡兜异常。

        成功 finalize 才从磁盘表销账；失败则保留登记，留给下次启动 _recover_pending_cards() 兜底。
        """
        for out_track_id, get_token in list(self._pending_cards.items()):
            try:
                r = DingTalkCardRenderer(out_track_id, get_token, ai_card_mode=self._ai_card_mode)
                if self._ai_card_mode:
                    r._push("⚠️ 处理中断，请重试。", is_error=True)
                else:
                    r._finalize_with_status("⚠️ 处理中断，请重试。", _FLOW_FAILED)
                self._card_store.remove(out_track_id)
            except Exception:
                pass
        self._pending_cards.clear()

    def _recover_pending_cards(self) -> None:
        """启动时把上次进程残留的未完成卡片强制 finalize，避免永久卡在「处理中/输出中」。

        覆盖 atexit 兜不住的 kill -9 / 崩溃 / 断电场景。参考 QwenPaw _recover_active_cards：
        逐卡 finalize 后即销账（一次性，失败也不再保留——卡片多半已过期，无限重试无意义）。
        """
        records = self._card_store.snapshot()
        if not records:
            return
        logger.info("发现 %d 张残留钉钉卡片，启动恢复 finalize...", len(records))
        for rec in records:
            out_track_id = rec.get("out_track_id")
            if not out_track_id:
                continue
            try:
                r = DingTalkCardRenderer(
                    out_track_id,
                    self._get_api_token,
                    ai_card_mode=bool(rec.get("ai_card_mode")),
                )
                if r._ai_card_mode:
                    r._push(_CARD_RECOVERY_TEXT, is_error=True)
                else:
                    r._finalize_with_status(_CARD_RECOVERY_TEXT, _FLOW_FAILED)
                logger.info("已恢复 finalize 残留卡片 | outTrackId=%s", out_track_id)
            except Exception:
                logger.warning(
                    "残留卡片恢复 finalize 失败 | outTrackId=%s", out_track_id, exc_info=True
                )
            finally:
                self._card_store.remove(out_track_id)

    def _handle(
        self,
        event: DingTalkMessageEvent,
        incoming: "ChatbotMessage",
        session_key: str,
        lock: threading.Lock,
        renderer: "DingTalkCardRenderer | None",
    ) -> None:
        # 绑定当前用户到 contextvar（每请求作用域，供用量记账归属到发送人）
        from ..core.usage_tracker import set_current_user, reset_current_user
        _user_token = set_current_user(
            f"dingtalk:{incoming.sender_staff_id}", incoming.sender_nick or ""
        )
        try:
            with lock:
                self._process(event, incoming, session_key, renderer)
        finally:
            reset_current_user(_user_token)

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
                self._agent.process(event, on_update=renderer.on_update, keep_thinking=False)
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
                "text": render_for_dingtalk(text, target="webhook"),
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

    def _react(self, incoming: "ChatbotMessage") -> None:
        """
        收到调用时在触发消息上贴一个 👌（OK）表情回复，告知用户请求已在处理中。
        替代旧的「[收到]」贴纸消息：表情贴在用户原消息上，不占独立气泡，更轻量。
        emotionType=1 普通表情（无背景）。失败仅告警，不影响主流程。
        """
        payload = {
            "robotCode": self._client_id,
            "openMsgId": incoming.message_id,
            "openConversationId": incoming.conversation_id,
            "emotionType": 1,
            "emotionId": _ACK_EMOTION_ID,
            "emotionName": _ACK_EMOTION_NAME,
        }
        try:
            resp = httpx.post(
                _EMOTION_REPLY_URL,
                headers={"x-acs-dingtalk-access-token": self._get_api_token()},
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning("表情回复失败 | error=%s", e)

    def _send_markdown_via_api(self, id_type: str, raw_id: str, text: str) -> None:
        """通过 OpenAPI 主动发 Markdown 消息（send_text 后端，需 api token）。"""
        token = self._get_api_token()
        payload = {
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": "消息", "text": render_for_dingtalk(text, target="webhook")}, ensure_ascii=False),
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
        file_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else "file"
        msg_param = json.dumps({"mediaId": media_id, "fileName": filename, "fileType": file_type}, ensure_ascii=False)
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
            sender_name=incoming.sender_nick or "异常",
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
