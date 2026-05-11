"""
心跳引擎

约束保证：
- asyncio + httpx 全异步非阻塞
- 每个 check 独立任务（Task），互不影响
- 每个 check 独立超时（httpx timeout）
- 连续失败 failure_threshold 次才判定 Down（防抖动）
- 状态变更（Up↔Down）才触发 alert_callback（避免刷屏）
- check 内部异常被捕获，不影响引擎本身或其他 check
"""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Callable, Optional

import httpx

from .config_loader import CheckConfig

logger = logging.getLogger(__name__)


class ServiceStatus(enum.Enum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DOWN = "DOWN"


# alert_callback(service_id, service_name, is_up, message)
AlertCallback = Callable[[str, str, bool, str], None]


class HeartbeatEngine:
    """
    周期性 HTTP 健康检测引擎。

    每个 CheckConfig 对应一个独立的 asyncio Task，并行执行，彼此隔离。
    状态机：UNKNOWN → UP / DOWN，仅在状态切换时触发 alert_callback。
    """

    def __init__(
        self,
        checks: list[CheckConfig],
        alert_callback: Optional[AlertCallback] = None,
    ) -> None:
        """
        Args:
            checks:         心跳检测配置列表
            alert_callback: 状态变更回调 alert_callback(id, name, is_up, message)
                            为 None 时仅记录日志
        """
        self._checks = checks
        self._alert_callback = alert_callback
        self._status: dict[str, ServiceStatus] = {}
        self._failure_count: dict[str, int] = {}
        self._tasks: list[asyncio.Task] = []
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False

    async def start(self) -> None:
        """在调用方的 asyncio 事件循环中启动所有检测任务。"""
        if not self._checks:
            logger.info("[HeartbeatEngine] 无检测配置，跳过启动")
            return

        self._running = True
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            verify=True,
        )
        self._tasks = [
            asyncio.create_task(
                self._check_loop(check),
                name=f"heartbeat-{check.id}",
            )
            for check in self._checks
        ]
        logger.info(
            "[HeartbeatEngine] 已启动 | checks=%d",
            len(self._checks),
        )

    async def stop(self) -> None:
        """优雅停止：取消所有检测任务并关闭 HTTP 客户端。"""
        self._running = False

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        if self._client:
            await self._client.aclose()
            self._client = None

        logger.info("[HeartbeatEngine] 已停止")

    # ──────────────────────────────────────────────────────────
    #   内部实现
    # ──────────────────────────────────────────────────────────

    async def _check_loop(self, check: CheckConfig) -> None:
        """单个检测项的独立循环，异常不向外传播。"""
        while self._running:
            try:
                await self._do_check(check)
            except asyncio.CancelledError:
                raise  # 允许 stop() 正常取消
            except Exception as e:
                # 防御：_do_check 内未被捕获的异常兜底处理
                self._on_failure(check, f"检测器内部异常: {e}")

            try:
                await asyncio.sleep(check.interval)
            except asyncio.CancelledError:
                raise

    async def _do_check(self, check: CheckConfig) -> None:
        """发起一次 HTTP 检测，更新成功/失败状态。"""
        if self._client is None:
            return

        try:
            resp = await self._client.request(
                method=check.method,
                url=check.url,
                headers=check.headers,
                timeout=check.timeout,
            )
            # 5xx 视为服务不可用；4xx 视为可达（服务在跑，只是鉴权等问题）
            if resp.status_code >= 500:
                self._on_failure(check, f"HTTP {resp.status_code}")
            else:
                self._on_success(check)

        except httpx.TimeoutException:
            self._on_failure(check, f"请求超时（>{check.timeout}s）")
        except httpx.ConnectError as e:
            self._on_failure(check, f"连接失败: {e}")
        except httpx.RequestError as e:
            self._on_failure(check, f"请求异常: {e}")

    def _on_success(self, check: CheckConfig) -> None:
        self._failure_count[check.id] = 0
        prev = self._status.get(check.id, ServiceStatus.UNKNOWN)

        if prev == ServiceStatus.DOWN:
            self._status[check.id] = ServiceStatus.UP
            logger.info("[HeartbeatEngine] 💚 %s 恢复正常", check.name)
            self._fire_alert(check, is_up=True, message="服务恢复正常")
        elif prev == ServiceStatus.UNKNOWN:
            self._status[check.id] = ServiceStatus.UP
            logger.info("[HeartbeatEngine] ✅ %s 首次检测正常", check.name)
        else:
            logger.debug("[HeartbeatEngine] %s 状态正常", check.name)

    def _on_failure(self, check: CheckConfig, error: str) -> None:
        count = self._failure_count.get(check.id, 0) + 1
        self._failure_count[check.id] = count
        prev = self._status.get(check.id, ServiceStatus.UNKNOWN)

        logger.warning(
            "[HeartbeatEngine] ⚠️ %s 检测失败 (%d/%d): %s",
            check.name,
            count,
            check.failure_threshold,
            error,
        )

        if count >= check.failure_threshold and prev != ServiceStatus.DOWN:
            self._status[check.id] = ServiceStatus.DOWN
            logger.error(
                "[HeartbeatEngine] 🔴 %s 判定为 Down | 连续失败 %d 次",
                check.name,
                count,
            )
            self._fire_alert(check, is_up=False, message=error)

    def _fire_alert(self, check: CheckConfig, is_up: bool, message: str) -> None:
        if self._alert_callback is None:
            return
        try:
            self._alert_callback(check.id, check.name, is_up, message)
        except Exception:
            logger.exception("[HeartbeatEngine] alert_callback 执行异常 | id=%s", check.id)
