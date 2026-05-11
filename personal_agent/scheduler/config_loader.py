"""
调度器配置加载器

从 YAML 文件加载 cron 任务与心跳检测配置，支持 ${ENV_VAR} 占位符展开。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


def _expand_env(value: str) -> str:
    """将 ${VAR} 替换为对应的环境变量值；变量不存在时保留原始占位符。"""
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def _expand_recursive(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_recursive(item) for item in obj]
    return obj


@dataclass
class CronJobConfig:
    id: str
    name: str
    schedule: str
    type: str                      # "agent_message" | "python_function"
    timeout: float = 120.0         # 超时秒数
    # agent_message 专用
    session_key: str = "cron_default"
    prompt: str = ""
    deliver_to: str = ""           # 飞书 chat_id；空=仅日志
    # python_function 专用
    module: str = ""
    function: str = "run"


@dataclass
class CheckConfig:
    id: str
    name: str
    url: str
    method: str = "GET"
    timeout: float = 10.0          # 单次请求超时
    interval: float = 60.0         # 检测间隔
    failure_threshold: int = 3     # 连续失败 N 次才判定 Down
    headers: dict = field(default_factory=dict)


@dataclass
class SchedulerConfig:
    timezone: str = "UTC"
    alert_to: str = ""                  # 心跳告警投递目标（飞书 open_id 或 chat_id）
    cron_jobs: list[CronJobConfig] = field(default_factory=list)
    heartbeat_checks: list[CheckConfig] = field(default_factory=list)


def load_scheduler_config(config_path: Path) -> SchedulerConfig:
    """
    加载并解析 scheduler.yaml。

    文件不存在时返回默认空配置（不抛出异常）。
    """
    if not config_path.exists():
        return SchedulerConfig()

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    raw = _expand_recursive(raw)

    timezone = raw.get("timezone", "UTC")
    alert_to = raw.get("alert_to", "")

    cron_jobs: list[CronJobConfig] = []
    for i, job_raw in enumerate((raw.get("cron") or {}).get("jobs") or []):
        missing = [f for f in ("id", "schedule", "type") if not job_raw.get(f)]
        if missing:
            logger.error(
                "跳过 cron 任务 #%d：缺少必填字段 %s", i, missing
            )
            continue
        cron_jobs.append(
            CronJobConfig(
                id=job_raw["id"],
                name=job_raw.get("name", job_raw["id"]),
                schedule=job_raw["schedule"],
                type=job_raw["type"],
                timeout=float(job_raw.get("timeout", 120)),
                session_key=job_raw.get("session_key", "cron_default"),
                prompt=job_raw.get("prompt", ""),
                deliver_to=job_raw.get("deliver_to", ""),
                module=job_raw.get("module", ""),
                function=job_raw.get("function", "run"),
            )
        )

    heartbeat_checks: list[CheckConfig] = []
    for i, check_raw in enumerate((raw.get("heartbeat") or {}).get("checks") or []):
        missing = [f for f in ("id", "url") if not check_raw.get(f)]
        if missing:
            logger.error(
                "跳过 heartbeat 检测 #%d：缺少必填字段 %s", i, missing
            )
            continue
        heartbeat_checks.append(
            CheckConfig(
                id=check_raw["id"],
                name=check_raw.get("name", check_raw["id"]),
                url=check_raw["url"],
                method=check_raw.get("method", "GET").upper(),
                timeout=float(check_raw.get("timeout", 10)),
                interval=float(check_raw.get("interval", 60)),
                failure_threshold=int(check_raw.get("failure_threshold", 3)),
                headers=check_raw.get("headers") or {},
            )
        )

    return SchedulerConfig(
        timezone=timezone,
        alert_to=alert_to,
        cron_jobs=cron_jobs,
        heartbeat_checks=heartbeat_checks,
    )
