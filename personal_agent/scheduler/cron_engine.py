"""
Cron 引擎

基于 APScheduler AsyncIOScheduler 实现，全部配置从 YAML 加载。

约束保证：
- 任务必须 async，统一通过 _wrap_job 装饰
- 每个任务独立超时（asyncio.wait_for）
- 防重入：per-job asyncio.Lock，上轮未完成则跳过
- 统一异常捕获包装在 _wrap_job 最外层
- 时区统一 UTC（由调用方通过 timezone 参数传入）
- 进程退出时 graceful shutdown（scheduler.shutdown(wait=True)）
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config_loader import CronJobConfig

if TYPE_CHECKING:
    from .persistence import JobPersistence

logger = logging.getLogger(__name__)


def _is_valid_receive_id(value: str) -> bool:
    """判断 deliver_to 是否像一个合法的飞书 ID（ou_ / oc_ / p2p_），排除占位符。"""
    if not value:
        return False
    return value.startswith(("ou_", "oc_", "p2p_"))


def _wrap_job(
    job_config: CronJobConfig,
    inner: Callable[[], "asyncio.Coroutine"],
    lock: asyncio.Lock,
    persistence: "Optional[JobPersistence]" = None,
) -> Callable[[], "asyncio.Coroutine"]:
    """
    统一执行器包装：
      1. 防重入 — lock 已占用则跳过本次，不排队
      2. 独立超时 — asyncio.wait_for
      3. 统一异常捕获 — 任何 Exception 均被捕获并记录，不向外传播
      4. 持久化 — 成功执行后更新 last_run 记录（仅在 persistence 不为 None 时）
    """

    async def wrapped() -> None:
        if lock.locked():
            logger.warning(
                "[cron:%s] 上轮执行尚未完成，跳过本次 | name=%s",
                job_config.id,
                job_config.name,
            )
            return

        async with lock:
            logger.info(
                "[cron:%s] 开始执行 | name=%s schedule=%r",
                job_config.id,
                job_config.name,
                job_config.schedule,
            )
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(inner(), timeout=job_config.timeout)
                logger.info(
                    "[cron:%s] 执行完成 | elapsed=%.1fs",
                    job_config.id,
                    time.monotonic() - t0,
                )
                if persistence is not None:
                    persistence.record_run(job_config.id)
            except asyncio.TimeoutError:
                logger.error(
                    "[cron:%s] 执行超时 | timeout=%.0fs",
                    job_config.id,
                    job_config.timeout,
                )
            except Exception:
                logger.exception(
                    "[cron:%s] 执行异常 | name=%s",
                    job_config.id,
                    job_config.name,
                )

    return wrapped


def _parse_cron(schedule: str) -> dict:
    """
    将标准 5 字段 cron 表达式解析为 APScheduler CronTrigger 关键字参数。

    例：'0 9 * * *' → {'minute': '0', 'hour': '9', 'day': '*', 'month': '*', 'day_of_week': '*'}
    """
    parts = schedule.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"cron 表达式需要 5 个字段（分 时 日 月 周），实际: {schedule!r}"
        )
    minute, hour, day, month, day_of_week = parts
    return dict(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
    )


class CronEngine:
    """
    基于 APScheduler AsyncIOScheduler 的定时任务引擎。

    支持两种任务类型：
    - agent_message: 向 ConversationAgent 发送 prompt，可选投递到飞书
    - python_function: 导入指定模块函数并执行（async/sync 均支持）
    """

    def __init__(
        self,
        jobs: list[CronJobConfig],
        timezone: str = "UTC",
        agent=None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
        persistence: "Optional[JobPersistence]" = None,
        default_misfire_grace_time: int = 259200,
    ) -> None:
        """
        Args:
            jobs:                       cron 任务配置列表
            timezone:                   调度时区，约束要求统一 UTC
            agent:                      ConversationAgent 实例（agent_message 类型需要）
            notify_fn:                  notify_fn(chat_id, text) 投递结果到飞书（可选）
            persistence:                JobPersistence 实例；None = 不持久化、不补跑
            default_misfire_grace_time: 全局默认补跑窗口（秒），可被任务级配置覆盖
        """
        self._jobs = jobs
        self._timezone = timezone
        self._agent = agent
        self._notify_fn = notify_fn
        self._persistence = persistence
        self._default_grace = default_misfire_grace_time
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        """在调用方的 asyncio 事件循环中启动调度器。"""
        if not self._jobs:
            logger.info("[CronEngine] 无定时任务配置，跳过启动")
            return

        self._scheduler = AsyncIOScheduler(timezone=self._timezone)

        for job_cfg in self._jobs:
            lock = asyncio.Lock()
            self._locks[job_cfg.id] = lock

            inner = self._make_inner(job_cfg)
            wrapped = _wrap_job(job_cfg, inner, lock, self._persistence)

            try:
                cron_kwargs = _parse_cron(job_cfg.schedule)
            except ValueError as e:
                logger.error("[CronEngine] 跳过无效任务 | id=%s error=%s", job_cfg.id, e)
                continue

            self._scheduler.add_job(
                wrapped,
                trigger="cron",
                id=job_cfg.id,
                name=job_cfg.name,
                **cron_kwargs,
                timezone=self._timezone,
                misfire_grace_time=60,  # 进程内短暂延迟容忍窗口（秒）
                coalesce=True,          # 多个积压触发只执行一次
                max_instances=1,        # 单任务最多 1 个并发实例
                replace_existing=True,  # 开发期重启时用新配置覆盖旧记录
            )
            logger.info(
                "[CronEngine] 注册任务 | id=%s name=%s schedule=%r",
                job_cfg.id,
                job_cfg.name,
                job_cfg.schedule,
            )

        self._scheduler.start()
        logger.info(
            "[CronEngine] 调度器已启动 | tasks=%d timezone=%s",
            len(self._jobs),
            self._timezone,
        )

        if self._persistence is not None:
            await self._run_catchup_checks()

    async def stop(self) -> None:
        """优雅停止：等待正在运行的任务结束后再退出。

        scheduler.shutdown(wait=True) 是同步阻塞调用，必须放到线程池里执行，
        否则会在 asyncio 事件循环里等待同一循环里的 job future，造成死锁。
        """
        if self._scheduler and self._scheduler.running:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._scheduler.shutdown)
            logger.info("[CronEngine] 调度器已停止")

    # ──────────────────────────────────────────────────────────
    #   补跑检测
    # ──────────────────────────────────────────────────────────

    async def _run_catchup_checks(self) -> None:
        """遍历所有 misfire_catchup=True 的任务，检测并触发补跑。"""
        now = datetime.now(timezone.utc)
        for job_cfg in self._jobs:
            if not job_cfg.misfire_catchup:
                continue
            try:
                self._check_catchup_for_job(job_cfg, now)
            except Exception:
                logger.exception(
                    "[CronEngine] 补跑检测异常 | id=%s", job_cfg.id
                )

    def _check_catchup_for_job(self, job_cfg: CronJobConfig, now: datetime) -> None:
        """
        检查 job_cfg 是否在上次执行后漏过了至少一次触发。
        若是，且漏过时间在补跑窗口内，则立即创建一次补跑任务。

        逻辑：
          1. 从 persistence 读取 last_run；若无记录则跳过（无历史基准）。
          2. 用 CronTrigger 计算 last_run 之后的下一个预期触发时间 next_expected。
          3. 若 next_expected < now，说明至少漏过一次触发。
          4. 检查漏过时间是否在 misfire_grace_time 内；超出则仅记录警告，不补跑。
          5. 在 grace 内则 asyncio.create_task 立即执行一次（coalesce 语义：只补一次）。
        """
        assert self._persistence is not None

        last_run = self._persistence.get_last_run(job_cfg.id)
        if last_run is None:
            logger.debug(
                "[cron:%s] 无历史执行记录，跳过补跑检测", job_cfg.id
            )
            return

        try:
            cron_kwargs = _parse_cron(job_cfg.schedule)
        except ValueError:
            return

        trigger = CronTrigger(**cron_kwargs, timezone=self._timezone)

        # get_next_fire_time(previous, now) 返回 >= now 的下一个触发时间。
        # 传入 now=last_run + 1μs 让它返回"last_run 之后的第一个触发时间"。
        probe = last_run + timedelta(microseconds=1)
        next_expected = trigger.get_next_fire_time(last_run, probe)

        if next_expected is None or next_expected >= now:
            logger.debug(
                "[cron:%s] 无需补跑 | next_expected=%s",
                job_cfg.id,
                next_expected,
            )
            return

        missed_seconds = (now - next_expected).total_seconds()
        grace = (
            job_cfg.misfire_grace_time
            if job_cfg.misfire_grace_time is not None
            else self._default_grace
        )

        if missed_seconds > grace:
            logger.warning(
                "[cron:%s] 错过触发超出补跑窗口，放弃补跑 | missed_at=%s age=%.0fs grace=%ds",
                job_cfg.id,
                next_expected.isoformat(),
                missed_seconds,
                grace,
            )
            return

        logger.info(
            "[cron:%s] 检测到重启补跑 | missed_at=%s age=%.0fs，立即补跑一次",
            job_cfg.id,
            next_expected.isoformat(),
            missed_seconds,
        )

        lock = self._locks[job_cfg.id]
        inner = self._make_inner(job_cfg)
        wrapped = _wrap_job(job_cfg, inner, lock, self._persistence)
        asyncio.create_task(wrapped(), name=f"catchup-{job_cfg.id}")

    # ──────────────────────────────────────────────────────────
    #   内部任务工厂
    # ──────────────────────────────────────────────────────────

    def _make_inner(self, job_cfg: CronJobConfig) -> Callable[[], "asyncio.Coroutine"]:
        if job_cfg.type == "agent_message":
            return self._make_agent_message(job_cfg)
        if job_cfg.type == "python_function":
            return self._make_python_function(job_cfg)
        raise ValueError(f"未知任务类型: {job_cfg.type!r}")

    def _make_agent_message(self, job_cfg: CronJobConfig):
        """构造 agent_message 类型的 inner 协程函数。"""

        async def inner() -> None:
            if self._agent is None:
                logger.warning("[cron:%s] agent 未注入，跳过", job_cfg.id)
                return

            from personal_agent.gateway.feishu_gateway import MessageEvent

            # ConversationContext 对 p2p 会话的 key 规则：p2p_{sender_open_id}
            # 用 job_cfg.session_key 作为 sender_open_id，保证不同任务会话互相隔离
            cron_session_key = f"p2p_{job_cfg.session_key}"

            # ① 每次执行前清空上下文，防止历史跨轮累积（token 膨胀 + 语义污染）
            context = getattr(self._agent, "_context", None)
            if context is not None:
                context.clear(cron_session_key)

            # ② 静默该会话的终端日志（仍写文件），避免 cron 日志淹没主流输出
            ilogger = getattr(self._agent, "_interaction_logger", None)
            if ilogger is not None:
                ilogger.suppress_console(cron_session_key)

            try:
                fake_event = MessageEvent(
                    message_id=f"cron_{job_cfg.id}_{uuid.uuid4().hex[:8]}",
                    chat_id=f"cron_{job_cfg.session_key}",
                    chat_type="p2p",
                    content=job_cfg.prompt,
                    sender_open_id=job_cfg.session_key,
                    is_scheduled=True,
                )

                loop = asyncio.get_running_loop()
                result: str = await loop.run_in_executor(
                    None, self._agent.process, fake_event
                )
            finally:
                # ③ 无论成功/异常都恢复，避免永久静默该 session_key
                if ilogger is not None:
                    ilogger.restore_console(cron_session_key)

            logger.info(
                "[cron:%s] Agent 回复完成 | chars=%d deliver_to=%r",
                job_cfg.id,
                len(result),
                job_cfg.deliver_to or "(none)",
            )

            if job_cfg.deliver_to and self._notify_fn:
                if not _is_valid_receive_id(job_cfg.deliver_to):
                    logger.error(
                        "[cron:%s] deliver_to=%r 不是有效的飞书 ID，跳过投递。"
                        " 格式应为 ou_xxx（用户 open_id）或 oc_xxx / p2p_xxx（chat_id）。"
                        " 向机器人发一条消息后，从日志 'session=p2p_<open_id>' 中提取 open_id。",
                        job_cfg.id,
                        job_cfg.deliver_to,
                    )
                else:
                    try:
                        self._notify_fn(job_cfg.deliver_to, result)
                    except Exception:
                        logger.exception("[cron:%s] 投递消息失败", job_cfg.id)

        return inner

    def _make_python_function(self, job_cfg: CronJobConfig):
        """构造 python_function 类型的 inner 协程函数。"""

        async def inner() -> None:
            try:
                mod = importlib.import_module(job_cfg.module)
            except ModuleNotFoundError:
                logger.error("[cron:%s] 模块不存在: %s", job_cfg.id, job_cfg.module)
                return

            fn = getattr(mod, job_cfg.function, None)
            if fn is None:
                logger.error(
                    "[cron:%s] 函数 %s.%s 不存在",
                    job_cfg.id,
                    job_cfg.module,
                    job_cfg.function,
                )
                return

            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, fn)

        return inner
