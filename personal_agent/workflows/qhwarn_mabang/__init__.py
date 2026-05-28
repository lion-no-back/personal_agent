from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="qhwarn_mabang",
    name="马帮清货预警",
    description="从马帮ERP导出各站点MSKU库存数据，筛选需要清货的SKU并生成预警Excel报告",
    schedule=None,
    timeout=1800,
    intent_triggers=["清货预警"],
)
