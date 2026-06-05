from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="rmorder_mabang",
    name="马帮移除订单报告",
    description="从马帮ERP下载各站点FBA移除订单报告，合并后填入MSKU采购价，生成merged.csv",
    schedule=None,
    timeout=3600,
    intent_triggers=["移除订单", "订单移除"],
)
