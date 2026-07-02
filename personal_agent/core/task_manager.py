"""
后台任务管理器

职责：
- 追踪 FeishuGateway 正在处理的每条消息（任务）
- 提供 /tasks 指令所需的任务状态查询
- 线程安全，支持多 Worker 并发写入
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .telemetry import emit_event, set_trace_context

logger = logging.getLogger(__name__)


class TaskCancelledError(Exception):
    """任务被用户主动取消时抛出，用于在 Agent 和 Gateway 之间传递取消信号。"""


class TaskStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class Task:
    task_id: str
    session_key: str
    description: str
    status: TaskStatus = TaskStatus.RUNNING
    created_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    error: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        """已用时间（秒），运行中任务返回截止当前的耗时。"""
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.created_at


class TaskManager:
    """
    后台任务管理器。

    生命周期：
      gateway._handle_async 开始 → create()
      处理成功              → complete()
      处理异常              → fail()
    """

    def __init__(self, max_done: int = 50) -> None:
        """
        Args:
            max_done: 最多保留的已完成（done/error）任务数量
        """
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()
        self._counter: int = 0
        self._max_done = max_done
        logger.info(f"✅ TaskManager 初始化完成 | max_done={max_done}")

    # ──────────────────────────────────────────────
    #   写操作
    # ──────────────────────────────────────────────

    def create(self, session_key: str, description: str) -> str:
        """
        注册一个新任务，返回任务 ID（格式：T0001）。

        Args:
            session_key:  会话标识，标记任务归属
            description:  任务简述（通常为消息内容前 50 字符）
        """
        with self._lock:
            self._counter += 1
            task_id = f"T{self._counter:04d}"
            self._tasks[task_id] = Task(
                task_id=task_id,
                session_key=session_key,
                description=description,
            )
            set_trace_context(task_id=task_id, session_key=session_key)
            emit_event(
                "task.started",
                source="task_manager",
                status="ok",
                task_id=task_id,
                session_key=session_key,
            )
            logger.debug(f"📌 任务已创建 | {task_id} session={session_key}")
            return task_id

    def complete(self, task_id: str) -> None:
        """将任务标记为成功完成。"""
        with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                task.status = TaskStatus.DONE
                task.finished_at = time.monotonic()
                emit_event(
                    "task.completed",
                    source="task_manager",
                    status="ok",
                    task_id=task_id,
                    duration_ms=int(task.elapsed * 1000),
                )
                logger.debug(f"✅ 任务完成 | {task_id} elapsed={task.elapsed:.1f}s")
            self._trim()

    def fail(self, task_id: str, error: str) -> None:
        """将任务标记为失败，并记录错误信息。"""
        with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                task.status = TaskStatus.ERROR
                task.error = error[:200]  # 截断过长的错误信息
                task.finished_at = time.monotonic()
                emit_event(
                    "task.failed",
                    source="task_manager",
                    status="error",
                    task_id=task_id,
                    duration_ms=int(task.elapsed * 1000),
                    attrs={"error_type": "TaskError"},
                )
                logger.debug(f"❌ 任务失败 | {task_id} error={error[:50]}")
            self._trim()

    def cancel(self, task_id: str) -> None:
        """将任务标记为已取消。"""
        with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.monotonic()
                emit_event(
                    "task.cancelled",
                    source="task_manager",
                    status="cancelled",
                    task_id=task_id,
                    duration_ms=int(task.elapsed * 1000),
                )
                logger.debug("⛔ 任务已取消 | %s", task_id)
            self._trim()

    def get_cancel_event(self, task_id: str) -> Optional[threading.Event]:
        """返回指定任务的取消事件对象，任务不存在时返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return task.cancel_event if task else None

    # ──────────────────────────────────────────────
    #   读操作
    # ──────────────────────────────────────────────

    def list_tasks(self) -> List[Task]:
        """
        返回任务列表，排序规则：
        1. 运行中（RUNNING）排最前
        2. 同状态内按创建时间倒序（最新的在前）
        """
        with self._lock:
            return sorted(
                self._tasks.values(),
                key=lambda t: (
                    0 if t.status == TaskStatus.RUNNING else 1,
                    -t.created_at,
                ),
            )

    def running_count(self) -> int:
        """返回当前运行中的任务数量。"""
        with self._lock:
            return sum(
                1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING
            )

    # ──────────────────────────────────────────────
    #   内部维护
    # ──────────────────────────────────────────────

    def _trim(self) -> None:
        """裁剪多余的已完成任务，保留最新的 max_done 条。"""
        done = [t for t in self._tasks.values() if t.status != TaskStatus.RUNNING]
        if len(done) <= self._max_done:
            return
        # 按创建时间升序，移除最旧的
        done.sort(key=lambda t: t.created_at)
        for task in done[: len(done) - self._max_done]:
            del self._tasks[task.task_id]
            logger.debug(f"🗑️  裁剪旧任务 | {task.task_id}")
