import threading


class SessionRunLocks:
    """同一 session_key 的请求串行执行，防止并发写共享上下文导致消息交错
    （进而引发 OpenAI 400 invalid_request_error）。

    替代两个网关内联的 _session_run_locks + guard 模式。注意：这是「请求串行锁」，
    与 ConversationContext 内部保护历史读写的 _session_locks 是两套不同的锁。
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, session_key: str) -> threading.Lock:
        """获取（或懒创建）指定会话的串行锁。"""
        with self._guard:
            if session_key not in self._locks:
                self._locks[session_key] = threading.Lock()
            return self._locks[session_key]
