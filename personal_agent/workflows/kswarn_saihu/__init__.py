import os

from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="kswarn_saihu",
    name="赛狐亏损预警",
    description="每周从赛狐导出产品分析数据，打亏损标签，通过钉钉群推送结果",
    schedule="0 2 * * 2",  # 每周二 UTC 02:00 = 北京时间 10:00
    timeout=3600,
    deliver_to=os.getenv("KSWARN_DELIVER_TO", ""),
    misfire_catchup=True,
    intent_triggers=["赛狐亏损预警"],
    persist_delivery=True,
)
