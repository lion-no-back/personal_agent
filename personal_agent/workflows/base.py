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
    deliver_to: str = ""            # 投递目标：飞书(ou_/oc_/p2p_) 或 钉钉(dingtalk:group:/dingtalk:user:)，空 = 仅写日志
    misfire_catchup: bool = False   # 重启后是否补跑错过的触发
    max_retries: int = 0            # 失败后最多重试几次（仅 Exception，超时不重试）
    retry_delay: float = 60.0       # 每次重试前等待秒数
    # 按需执行（预留，定时类型无需填写）
    intent_triggers: list[str] = field(default_factory=list)
    # 按需触发成功后，将投递目标（群/用户 key）持久化，供下次定时运行使用
    persist_delivery: bool = False
