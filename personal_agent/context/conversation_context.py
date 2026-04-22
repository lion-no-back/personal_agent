import logging
import threading
from collections import deque
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ConversationContext:
    """
    多会话上下文管理器。

    核心职责：
    1. 以 session_key 为维度维护多路对话历史（线程安全）
    2. 滑动窗口裁剪，防止历史消息超出 Token 限制
    3. 注入系统提示词（始终保留在消息列表首位）
    4. 消息去重（基于 message_id，防止飞书重推导致重复处理）

    session_key 策略（由 FeishuGateway 计算后传入）：
    - 私聊  (P2P)    → "p2p_{sender_open_id}"
    - 群聊 + 话题    → "thread_{thread_id}"
    - 群聊（无话题） → "group_{chat_id}"
    """

    # 默认系统提示词，可通过构造函数覆盖
    _DEFAULT_SYSTEM_PROMPT = (
        "你是一个专业、友好的个人 AI 助理，名叫 PersonalAgent。\n"
        "请用简洁、准确的语言回答问题，必要时使用 Markdown 格式增强可读性。\n"
        "如果用户要求你处理文件，但当前上下文中没有提供任何[附件信息]，请务必直接告知用户“请提供需要处理的文件”，绝对不要凭空捏造虚构的文件名或文件内容。\n"
        "如果你不确定，请直接说明，不要编造内容。"
    )

    def __init__(
        self,
        max_history: int = 20,
        system_prompt: Optional[str] = None,
    ):
        """
        Args:
            max_history:   每个会话最多保留的历史消息条数（不含系统提示），默认 20 条（即 10 轮对话）
            system_prompt: 系统提示词，为 None 时使用默认值
        """
        self._max_history = max_history
        self._system_prompt = system_prompt or self._DEFAULT_SYSTEM_PROMPT

        # 存储各会话的消息历史：session_key → deque[{"role": ..., "content": ...}]
        self._sessions: Dict[str, deque] = {}

        # 每个会话独立的读写锁，避免并发写冲突
        self._session_locks: Dict[str, threading.Lock] = {}

        # 保护 _sessions 和 _session_locks 字典本身的结构修改
        self._global_lock = threading.Lock()

        # 已处理的 message_id 集合，用于去重（防止飞书重推）
        self._processed_ids: set = set()
        self._processed_lock = threading.Lock()

        logger.info(
            f"✅ ConversationContext 初始化完成 | max_history={self._max_history}"
        )

    # ──────────────────────────────────────────────
    #   内部辅助：懒初始化会话锁和队列
    # ──────────────────────────────────────────────

    def _get_session_lock(self, session_key: str) -> threading.Lock:
        """获取（或懒创建）指定会话的锁。"""
        with self._global_lock:
            if session_key not in self._session_locks:
                self._session_locks[session_key] = threading.Lock()
            return self._session_locks[session_key]

    def _get_session(self, session_key: str) -> deque:
        """获取（或懒创建）指定会话的消息队列。"""
        with self._global_lock:
            if session_key not in self._sessions:
                self._sessions[session_key] = deque()
            return self._sessions[session_key]

    # ──────────────────────────────────────────────
    #   消息历史管理
    # ──────────────────────────────────────────────

    def add_message(self, session_key: str, role: str, content: str) -> None:
        """
        向指定会话追加一条消息。

        Args:
            session_key: 会话唯一标识
            role:        消息角色，"user" 或 "assistant"
            content:     消息内容
        """
        if not content or not content.strip():
            logger.debug(f"跳过空消息 | session={session_key} role={role}")
            return

        lock = self._get_session_lock(session_key)
        session = self._get_session(session_key)

        with lock:
            session.append({"role": role, "content": content.strip()})

            # 滑动窗口：超出限制时从队头删除最旧的消息
            while len(session) > self._max_history:
                removed = session.popleft()
                logger.debug(
                    f"滑动窗口裁剪 | session={session_key} "
                    f"已移除: [{removed['role']}] {removed['content'][:30]}..."
                )

        logger.debug(
            f"消息已追加 | session={session_key} role={role} history_len={len(session)}"
        )

    def get_messages(self, session_key: str) -> List[Dict[str, str]]:
        """
        获取指定会话的完整消息列表，用于传给 LLM。

        返回格式：[system_prompt] + history[-max_history:]
        系统提示词始终位于首位，不占用历史窗口配额。

        Args:
            session_key: 会话唯一标识

        Returns:
            消息列表，格式为 [{"role": "...", "content": "..."}]
        """
        lock = self._get_session_lock(session_key)
        session = self._get_session(session_key)

        with lock:
            messages = [{"role": "system", "content": self._system_prompt}]
            messages.extend(list(session))
            return messages

    def clear(self, session_key: str) -> None:
        """
        清空指定会话的历史消息（保留系统提示词）。
        通常由 /clear 指令触发。

        Args:
            session_key: 会话唯一标识
        """
        lock = self._get_session_lock(session_key)
        session = self._get_session(session_key)

        with lock:
            session.clear()

        logger.info(f"🗑️  会话历史已清空 | session={session_key}")

    def get_history_length(self, session_key: str) -> int:
        """返回指定会话当前的历史消息条数（不含系统提示词）。"""
        session = self._get_session(session_key)
        return len(session)

    def list_sessions(self) -> List[str]:
        """返回所有活跃会话的 session_key 列表。"""
        with self._global_lock:
            return list(self._sessions.keys())

    # ──────────────────────────────────────────────
    #   消息去重（防飞书重推）
    # ──────────────────────────────────────────────

    def mark_processed(self, message_id: str) -> None:
        """
        将 message_id 标记为已处理。
        应在开始处理一条消息前调用，防止并发或重推时重复处理。

        Args:
            message_id: 飞书消息唯一 ID（om_xxx 格式）
        """
        with self._processed_lock:
            self._processed_ids.add(message_id)

    def is_processed(self, message_id: str) -> bool:
        """
        检查 message_id 是否已被处理过。

        Args:
            message_id: 飞书消息唯一 ID

        Returns:
            True 表示已处理，应跳过；False 表示首次收到，应处理
        """
        with self._processed_lock:
            return message_id in self._processed_ids

    # ──────────────────────────────────────────────
    #   会话复制与预览
    # ──────────────────────────────────────────────

    def copy_session(self, source_key: str, target_key: str) -> int:
        """
        将 source_key 会话的历史消息复制到 target_key 会话中。
        target_key 原有历史将被清空后替换。
        通常由 /resume 指令触发。

        Args:
            source_key: 源会话 Key
            target_key: 目标会话 Key（当前会话）

        Returns:
            实际复制的消息条数（受 max_history 限制）
        """
        # ── 读取源会话快照（持锁期间拷贝，避免迭代时被修改） ──
        source_lock = self._get_session_lock(source_key)
        source_session = self._get_session(source_key)

        with source_lock:
            snapshot = list(source_session)

        if not snapshot:
            return 0

        # ── 写入目标会话 ──────────────────────────────────────
        target_lock = self._get_session_lock(target_key)
        target_session = self._get_session(target_key)

        with target_lock:
            target_session.clear()
            for msg in snapshot:
                target_session.append(dict(msg))  # 浅拷贝，防止共享引用
            # 遵守滑动窗口上限
            while len(target_session) > self._max_history:
                target_session.popleft()
            copied = len(target_session)

        logger.info(f"📋 会话已复制 | {source_key} → {target_key} | 共 {copied} 条")
        return copied

    def get_session_preview(self, session_key: str) -> str:
        """
        返回指定会话最后一条消息的文本预览（最多 40 字符）。
        用于 /resume 列表展示。

        Args:
            session_key: 会话唯一标识

        Returns:
            预览字符串，空会话返回 "(空)"
        """
        session = self._get_session(session_key)
        if not session:
            return "(空)"
        last_msg = list(session)[-1]
        content: str = last_msg.get("content", "")
        preview = content.replace("\n", " ").strip()
        if len(preview) > 40:
            return preview[:40] + "..."
        return preview if preview else "(空)"

    # ──────────────────────────────────────────────
    #   session_key 计算策略（静态方法）
    # ──────────────────────────────────────────────

    @staticmethod
    def build_session_key(
        chat_type: str,
        sender_open_id: str,
        chat_id: str,
        thread_id: Optional[str] = None,
    ) -> str:
        """
        根据消息上下文计算 session_key，实现多维度会话隔离。

        策略：
        - P2P  私聊         → "p2p_{sender_open_id}"   每个用户独立上下文
        - 群聊 + 话题        → "thread_{thread_id}"     话题内共享上下文，话题间隔离
        - 群聊（无话题）     → "group_{chat_id}"        整群共享上下文

        Args:
            chat_type:       "p2p" 或 "group"
            sender_open_id:  消息发送者的 open_id
            chat_id:         会话/群聊 ID
            thread_id:       话题 ID（仅群聊话题消息有此字段）

        Returns:
            唯一的 session_key 字符串
        """
        if chat_type == "p2p":
            return f"p2p_{sender_open_id}"
        elif thread_id:
            return f"thread_{thread_id}"
        else:
            return f"group_{chat_id}"
