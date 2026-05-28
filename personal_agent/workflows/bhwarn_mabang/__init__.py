from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="bhwarn_mabang",
    name="马帮补货预警",
    description="从马帮ERP导出各站点MSKU库存数据，筛选需要补货的SKU并生成预警Excel报告",
    schedule=None,
    timeout=1800,
    intent_triggers=["补货预警"],
)
