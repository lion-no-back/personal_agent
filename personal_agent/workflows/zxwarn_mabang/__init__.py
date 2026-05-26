from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="zxwarn_mabang",
    name="马帮滞销预警",
    description="从马帮ERP导出各站点MSKU库存数据，筛选滞销SKU并生成预警Excel报告",
    schedule=None,
    timeout=1800,
    intent_triggers=["滞销预警"],
)
