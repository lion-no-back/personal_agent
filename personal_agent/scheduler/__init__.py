"""
调度器守护进程

在独立 daemon asyncio 事件循环中运行 CronEngine 和 HeartbeatEngine，
与现有 threading 架构完全兼容（同 Playwright MCP 桥接模式）。

用法（main.py）：

    from personal_agent.scheduler import SchedulerDaemon

    scheduler = SchedulerDaemon(config_path=_PROJECT_ROOT / "config" / "scheduler.yaml")
    scheduler.start(
        agent=agent,
        notify_fn=gateway.send_text,
        alert_callback=None,   # 仅日志；传入回调即可接入飞书告警
    )
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from .config_loader import SchedulerConfig, load_scheduler_config
from .cron_engine import CronEngine
from .heartbeat_engine import AlertCallback, HeartbeatEngine
from .persistence import JobPersistence

logger = logging.getLogger(__name__)


class SchedulerDaemon:
    """
    调度器守护进程。

    - 创建独立 asyncio 事件循环，运行在 daemon 线程中
    - 启动 CronEngine（定时任务）和 HeartbeatEngine（心跳检测）
    - atexit 自动注册 graceful shutdown
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = config_path
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cron: Optional[CronEngine] = None
        self._heartbeat: Optional[HeartbeatEngine] = None
        self._stopped = False
        # config_path 的父目录作为 db_path 的解析基准
        self._project_root: Path = (
            config_path.parent.parent if config_path else Path(".")
        )

    def start(
        self,
        *,
        agent=None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
        alert_callback: Optional[AlertCallback] = None,
    ) -> None:
        """
        非阻塞启动：创建 daemon 线程并在其中运行 asyncio 事件循环。

        Args:
            agent:          ConversationAgent 实例（agent_message 类型任务需要）
            notify_fn:      notify_fn(chat_id, text) 将 cron 结果投递到飞书
            alert_callback: 心跳状态变更回调 callback(id, name, is_up, message)
        """
        config = (
            load_scheduler_config(self._config_path)
            if self._config_path
            else SchedulerConfig()
        )

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="scheduler-loop",
        )
        self._thread.start()

        # 若 YAML 配置了 alert_to 且外部未传入回调，则自动构建飞书告警回调
        if config.alert_to and notify_fn and alert_callback is None:
            _alert_to = config.alert_to
            _notify = notify_fn
            def alert_callback(
                service_id: str, service_name: str, is_up: bool, message: str
            ) -> None:
                status = "💚 恢复正常" if is_up else "🔴 服务异常"
                _notify(_alert_to, f"{status} | {service_name}\n{message}")

        persistence: Optional[JobPersistence] = None
        if config.persist.enabled and config.cron_jobs:
            db_path = self._project_root / config.persist.db_path
            persistence = JobPersistence(db_path)
            logger.info(
                "[SchedulerDaemon] 任务持久化已启用 | db=%s grace=%ds",
                db_path,
                config.persist.default_misfire_grace_time,
            )

        self._cron = CronEngine(
            jobs=config.cron_jobs,
            timezone=config.timezone,
            agent=agent,
            notify_fn=notify_fn,
            persistence=persistence,
            default_misfire_grace_time=config.persist.default_misfire_grace_time,
        )
        self._heartbeat = HeartbeatEngine(
            checks=config.heartbeat_checks,
            alert_callback=alert_callback,
        )

        # 在调度器循环中初始化两个引擎（需要在目标循环中创建 asyncio.Lock / Task）
        future = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
        future.result(timeout=30)

        atexit.register(self.stop)
        logger.info("[SchedulerDaemon] 已启动 | cron_jobs=%d heartbeat_checks=%d",
                    len(config.cron_jobs), len(config.heartbeat_checks))

    def stop(self) -> None:
        """优雅停止：等待正在运行的任务结束，再关闭事件循环。幂等，可多次调用。"""
        if self._stopped:
            return
        self._stopped = True

        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            return

        future = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.error("[SchedulerDaemon] 停止时出错: %s", e)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

        logger.info("[SchedulerDaemon] 已停止")

    # ──────────────────────────────────────────────────────────
    #   内部实现
    # ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_start(self) -> None:
        await self._cron.start()
        await self._heartbeat.start()

    async def _async_stop(self) -> None:
        if self._heartbeat:
            await self._heartbeat.stop()
        if self._cron:
            await self._cron.stop()
