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
    """判断 deliver_to 是否像合法的投递目标，排除空值和占位符。
    飞书：ou_xxx（用户）/ oc_xxx（群）/ p2p_xxx（会话）
    钉钉：dingtalk:group:xxx（群）/ dingtalk:user:xxx（用户）
    """
    if not value:
        return False
    return value.startswith(("ou_", "oc_", "p2p_", "dingtalk:"))


def _wrap_job(
    job_config: CronJobConfig,
    inner: Callable[[], "asyncio.Coroutine"],
    lock: asyncio.Lock,
    persistence: "Optional[JobPersistence]" = None,
    notify_fn: Optional[Callable[[str, str], None]] = None,
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
            max_attempts = job_config.max_retries + 1
            for attempt_num in range(1, max_attempts + 1):
                if attempt_num > 1:
                    logger.info(
                        "[cron:%s] 第 %d/%d 次重试 | delay=%.0fs",
                        job_config.id,
                        attempt_num,
                        max_attempts,
                        job_config.retry_delay,
                    )
                    await asyncio.sleep(job_config.retry_delay)

                _ts = datetime.now().strftime("%H:%M:%S")
                if attempt_num == 1:
                    print(f"\n[{_ts}] ▶ 定时任务启动: {job_config.name} (id={job_config.id})", flush=True)
                else:
                    print(f"\n[{_ts}] ↩ 定时任务重试: {job_config.name} ({attempt_num}/{max_attempts})", flush=True)

                attempt_id: int | None = None
                if persistence is not None:
                    attempt_id = persistence.record_attempt_start(job_config.id, attempt_num)

                t0 = time.monotonic()
                is_final = attempt_num == max_attempts

                try:
                    await asyncio.wait_for(inner(), timeout=job_config.timeout)
                    elapsed = time.monotonic() - t0
                    logger.info(
                        "[cron:%s] 执行完成 | attempt=%d elapsed=%.1fs",
                        job_config.id,
                        attempt_num,
                        elapsed,
                    )
                    if persistence is not None:
                        persistence.record_run(job_config.id)
                        if attempt_id is not None:
                            persistence.record_attempt_end(attempt_id, "success", elapsed)
                    return

                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - t0
                    logger.error(
                        "[cron:%s] 执行超时 | timeout=%.0fs attempt=%d",
                        job_config.id,
                        job_config.timeout,
                        attempt_num,
                    )
                    if persistence is not None and attempt_id is not None:
                        persistence.record_attempt_end(attempt_id, "timeout", elapsed)
                    # 超时不重试：线程仍在运行，重试会并发冲突
                    if notify_fn and _is_valid_receive_id(job_config.deliver_to):
                        try:
                            notify_fn(
                                job_config.deliver_to,
                                f"⏰ {job_config.name} 执行超时（>{job_config.timeout:.0f}s）",
                            )
                        except Exception:
                            logger.exception("[cron:%s] 超时通知投递失败", job_config.id)
                    return

                except Exception:
                    elapsed = time.monotonic() - t0
                    logger.exception(
                        "[cron:%s] 执行异常 | name=%s attempt=%d/%d",
                        job_config.id,
                        job_config.name,
                        attempt_num,
                        max_attempts,
                    )
                    if persistence is not None and attempt_id is not None:
                        persistence.record_attempt_end(attempt_id, "error", elapsed)
                    if is_final:
                        if notify_fn and _is_valid_receive_id(job_config.deliver_to):
                            try:
                                retry_info = (
                                    f"，已重试 {job_config.max_retries} 次"
                                    if job_config.max_retries > 0
                                    else ""
                                )
                                notify_fn(
                                    job_config.deliver_to,
                                    f"❌ {job_config.name} 执行失败{retry_info}",
                                )
                            except Exception:
                                logger.exception("[cron:%s] 失败通知投递失败", job_config.id)

    return wrapped


def _convert_dow(dow: str) -> str:
    """
    将标准 cron 的 day_of_week 值转换为 APScheduler 约定。

    标准 cron: 0/7=Sun  1=Mon  2=Tue  3=Wed  4=Thu  5=Fri  6=Sat
    APScheduler: 0=Mon  1=Tue  2=Wed  3=Thu  4=Fri  5=Sat  6=Sun

    转换公式（数字）: apscheduler_dow = (standard_dow - 1) % 7
    非数字值（*, mon, tue, ... 或范围表达式）直接透传。
    """
    if dow in ("*", "?") or not dow.isdigit():
        return dow
    return str((int(dow) - 1) % 7)


def _parse_cron(schedule: str) -> dict:
    """
    将标准 5 字段 cron 表达式解析为 APScheduler CronTrigger 关键字参数。

    例：'0 9 * * 1' → {'minute': '0', 'hour': '9', 'day': '*', 'month': '*', 'day_of_week': '0'}
    注意：day_of_week 经过标准 cron → APScheduler 约定转换（见 _convert_dow）。
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
        day_of_week=_convert_dow(day_of_week),
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
        send_file_fn: Optional[Callable[[str, str, str], None]] = None,
        send_image_text_fn: Optional[Callable[[str, str, str], None]] = None,
        persistence: "Optional[JobPersistence]" = None,
        default_misfire_grace_time: int = 259200,
    ) -> None:
        """
        Args:
            jobs:                       cron 任务配置列表
            timezone:                   调度时区，约束要求统一 UTC
            agent:                      ConversationAgent 实例（agent_message 类型需要）
            notify_fn:                  notify_fn(session_key, text) 投递文本消息（可选）
            send_file_fn:               send_file_fn(session_key, file_path, filename) 投递文件（可选）
            persistence:                JobPersistence 实例；None = 不持久化、不补跑
            default_misfire_grace_time: 全局默认补跑窗口（秒），可被任务级配置覆盖
        """
        self._jobs = jobs
        self._timezone = timezone
        self._agent = agent
        self._notify_fn = notify_fn
        self._send_file_fn = send_file_fn
        self._send_image_text_fn = send_image_text_fn
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
            wrapped = _wrap_job(job_cfg, inner, lock, self._persistence, self._notify_fn)

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
        检查 job_cfg 是否漏过了触发，若是则立即补跑一次。

        两种路径：
          - last_run 存在：计算 last_run 之后的下一个触发点，在 grace 窗口内则补跑。
          - last_run 为 None（首次运行）：在 grace 窗口内查找最近触发点，找到则补跑。
        """
        assert self._persistence is not None

        try:
            cron_kwargs = _parse_cron(job_cfg.schedule)
        except ValueError:
            return

        trigger = CronTrigger(**cron_kwargs, timezone=self._timezone)
        grace = (
            job_cfg.misfire_grace_time
            if job_cfg.misfire_grace_time is not None
            else self._default_grace
        )

        last_run = self._persistence.get_last_run(job_cfg.id)

        if last_run is None:
            # 首次运行：向前推 grace 秒，看窗口内有没有应触发的点
            probe = now - timedelta(seconds=grace)
            missed_at = trigger.get_next_fire_time(None, probe)
            if missed_at is None or missed_at >= now:
                logger.debug("[cron:%s] 首次运行，grace 窗口内无触发点，跳过", job_cfg.id)
                return
            logger.info(
                "[cron:%s] 首次运行补跑 | missed_at=%s age=%.0fs",
                job_cfg.id,
                missed_at.isoformat(),
                (now - missed_at).total_seconds(),
            )
        else:
            # 常规补跑：last_run 之后有没有漏触发
            probe = last_run + timedelta(microseconds=1)
            missed_at = trigger.get_next_fire_time(last_run, probe)
            if missed_at is None or missed_at >= now:
                logger.debug("[cron:%s] 无需补跑 | next_expected=%s", job_cfg.id, missed_at)
                return
            missed_seconds = (now - missed_at).total_seconds()
            if missed_seconds > grace:
                logger.warning(
                    "[cron:%s] 错过触发超出补跑窗口，放弃补跑 | missed_at=%s age=%.0fs grace=%ds",
                    job_cfg.id,
                    missed_at.isoformat(),
                    missed_seconds,
                    grace,
                )
                return
            logger.info(
                "[cron:%s] 检测到重启补跑 | missed_at=%s age=%.0fs，立即补跑一次",
                job_cfg.id,
                missed_at.isoformat(),
                missed_seconds,
            )

        lock = self._locks[job_cfg.id]
        inner = self._make_inner(job_cfg)
        wrapped = _wrap_job(job_cfg, inner, lock, self._persistence, self._notify_fn)
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
                        "[cron:%s] deliver_to=%r 不是有效的投递目标，跳过投递。"
                        " 飞书格式：ou_xxx / oc_xxx / p2p_xxx；钉钉格式：dingtalk:group:xxx。",
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
        """构造 python_function 类型的 inner 协程函数。

        成功时若返回字符串且 deliver_to 已配置，则通过 notify_fn 投递飞书消息。
        异常向外传播，由 _wrap_job 负责重试和最终失败通知。
        """

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
                result = await fn()
            else:
                # 同步工作流放到独立子进程运行：隔离重原生库（numpy/pandas/torch）的导入与
                # 运行时按需装包，避免与主进程其它线程并发首次导入产生竞态。
                from personal_agent.workflows.dispatch import run_in_subprocess

                # 子进程超时设得略早于外层 asyncio.wait_for(job_config.timeout)，
                # 由子进程先触发并杀掉整棵进程树，再映射成 TimeoutError 走"不重试"分支。
                sub_timeout = max(int(job_cfg.timeout) - 30, 30) if job_cfg.timeout else None
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: run_in_subprocess(
                        module=job_cfg.module,
                        function=job_cfg.function,
                        timeout=sub_timeout,
                    ),
                )
                if result.get("error") == "timeout":
                    raise asyncio.TimeoutError()  # 子进程超时 → _wrap_job 超时分支（不重试）
                if not result.get("ok"):
                    # 真正失败：抛出交给 _wrap_job 的重试/失败通知逻辑
                    raise RuntimeError(
                        result.get("error") or result.get("message") or "工作流子进程执行失败"
                    )

            # 投递目标：以 WORKFLOW_META.deliver_to（__init__.py 静态配置）为主；
            # 仅当它为空时，才回退到按需触发持久化的目标（data/<id>_delivery_target）。
            effective_deliver_to = job_cfg.deliver_to
            if not effective_deliver_to:
                try:
                    from pathlib import Path as _Path
                    _state = _Path(__file__).resolve().parents[2] / "data" / f"{job_cfg.id}_delivery_target"
                    if _state.exists():
                        _saved = _state.read_text(encoding="utf-8").strip()
                        if _saved:
                            effective_deliver_to = _saved
                except Exception:
                    pass

            if effective_deliver_to:
                if isinstance(result, str):
                    if self._notify_fn:
                        try:
                            self._notify_fn(effective_deliver_to, result)
                        except Exception:
                            logger.exception("[cron:%s] 投递消息失败", job_cfg.id)
                elif isinstance(result, dict):
                    msg = result.get("message", "")
                    files = result.get("files", [])
                    if msg and self._notify_fn:
                        try:
                            self._notify_fn(effective_deliver_to, msg)
                        except Exception:
                            logger.exception("[cron:%s] 投递消息失败", job_cfg.id)
                    if files and self._send_file_fn:
                        for f in files:
                            try:
                                self._send_file_fn(
                                    effective_deliver_to, str(f), getattr(f, "name", str(f))
                                )
                            except Exception:
                                logger.exception(
                                    "[cron:%s] 投递文件失败 | file=%s", job_cfg.id, f
                                )

                    # segments：逐条「图片 + 文字」合并投递（与按需触发路径一致）。
                    for seg in result.get("segments", []):
                        img = seg.get("image")
                        txt = seg.get("text", "")
                        try:
                            if img and self._send_image_text_fn:
                                self._send_image_text_fn(effective_deliver_to, str(img), txt)
                            elif txt and self._notify_fn:
                                self._notify_fn(effective_deliver_to, txt)
                        except Exception:
                            logger.exception(
                                "[cron:%s] 投递图文段失败 | image=%s", job_cfg.id, img
                            )

        return inner
