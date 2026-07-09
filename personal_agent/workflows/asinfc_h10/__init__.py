from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="asinfc_h10",
    name="Helium10 ASIN反查",
    description="按【ASIN反查 站点ASIN】触发 Helium10 Cerebro 关键词反查，生成分析总结 PNG 和标注后的 XLSX；首次请发送【反查配置】安装依赖并建立登录态",
    schedule=None,
    timeout=2400,
    intent_triggers=["ASIN反查", "反查配置"],
)
