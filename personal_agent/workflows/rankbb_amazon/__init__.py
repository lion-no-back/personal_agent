from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="rankbb_amazon",
    name="亚马逊排行榜播报",
    description="抓取 Amazon 销量榜/新品榜，匹配目标品牌商品并截图，按「图片+文字」播报命中排名；首次需发送【排名配置】安装依赖与浏览器",
    schedule=None,
    timeout=1800,
    intent_triggers=["排名配置", "排名播报"],
)
