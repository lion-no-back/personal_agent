"""
WorkflowRegistry — 自动发现所有 workflow 子包

扫描 personal_agent/workflows/ 下的每个子包，
查找其 __init__.py 中声明的 WORKFLOW_META 实例，
对外提供两个接口：
  - get_scheduled_jobs()     → 转换为 CronJobConfig 列表，供调度器注册
  - get_ondemand_workflows() → 返回含 intent_triggers 的 WorkflowMeta 列表，供意图匹配（预留）
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

from .base import WorkflowMeta

if TYPE_CHECKING:
    from personal_agent.scheduler.config_loader import CronJobConfig

logger = logging.getLogger(__name__)

_WORKFLOWS_DIR = Path(__file__).parent


class WorkflowRegistry:
    def discover(self) -> list[WorkflowMeta]:
        """扫描所有子包，返回已注册的 WorkflowMeta 列表。"""
        results: list[WorkflowMeta] = []
        for _finder, name, is_pkg in pkgutil.iter_modules([str(_WORKFLOWS_DIR)]):
            if not is_pkg:
                continue
            module_name = f"personal_agent.workflows.{name}"
            try:
                mod = importlib.import_module(module_name)
                meta = getattr(mod, "WORKFLOW_META", None)
                if isinstance(meta, WorkflowMeta):
                    results.append(meta)
                    logger.debug("[WorkflowRegistry] 发现 workflow: %s", meta.id)
            except Exception:
                logger.exception("[WorkflowRegistry] 加载 workflow 失败: %s", module_name)
        return results

    def get_scheduled_jobs(self) -> list[CronJobConfig]:
        """返回所有定时 workflow 对应的 CronJobConfig，供 CronEngine 注册。"""
        from personal_agent.scheduler.config_loader import CronJobConfig

        jobs: list[CronJobConfig] = []
        for meta in self.discover():
            if meta.schedule is None:
                continue
            jobs.append(
                CronJobConfig(
                    id=meta.id,
                    name=meta.name,
                    schedule=meta.schedule,
                    type="python_function",
                    module=f"personal_agent.workflows.{meta.id}.runner",
                    function="run",
                    timeout=float(meta.timeout),
                    deliver_to=meta.deliver_to,
                    misfire_catchup=meta.misfire_catchup,
                    max_retries=meta.max_retries,
                    retry_delay=meta.retry_delay,
                )
            )
        logger.info("[WorkflowRegistry] 发现 %d 个定时 workflow", len(jobs))
        return jobs

    def get_ondemand_workflows(self) -> list[WorkflowMeta]:
        """返回所有含 intent_triggers 的按需 workflow，供意图匹配使用（预留）。"""
        return [m for m in self.discover() if m.intent_triggers]
