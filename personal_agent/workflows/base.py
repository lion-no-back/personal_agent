"""
Workflow 元数据契约

所有 workflow 包的 __init__.py 需声明一个 WORKFLOW_META 实例。
WorkflowRegistry 通过扫描子包自动发现并注册。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WorkflowMeta:
    id: str
    name: str
    description: str = ""           # 人类可读描述；按需类型用于意图匹配
    # 定时执行（schedule 非空则参与调度）
    schedule: Optional[str] = None  # cron 表达式（5字段 UTC），None = 不参与定时调度
    timeout: int = 1800             # 单次执行超时（秒）
    deliver_to: str = ""            # 飞书 ID（ou_xxx / oc_xxx），空 = 仅写日志
    misfire_catchup: bool = False   # 重启后是否补跑错过的触发
    # 按需执行（预留，定时类型无需填写）
    intent_triggers: list[str] = field(default_factory=list)
