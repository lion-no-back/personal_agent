from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="fxreview_amazon",
    name="亚马逊评论采集与VoC分析",
    description="采集指定 ASIN 的亚马逊评论（逐星级去重），用 DeepSeek 生成 VoC 分析报告（卖点/痛点/买家关注点）；首次需发送【评论配置】建立登录态",
    schedule=None,
    timeout=1800,
    intent_triggers=["评论采集", "评论配置"],
)
