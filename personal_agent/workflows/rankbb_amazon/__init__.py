from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="rankbb_amazon",
    name="亚马逊排行榜播报",
    description="抓取 Amazon 销量榜/新品榜，匹配目标品牌商品并截图，按「图片+文字」播报命中排名；首次需发送【排名配置】安装依赖与浏览器",
    schedule="0 1 * * *",        # 每天 UTC 01:00 = 北京时间 09:00
    timeout=1800,
    deliver_to="dingtalk:group:cid6k73vFK5Njuas9K5idUktg==",  # 群「LXR_亚马逊部」
    misfire_catchup=False,        # 到点没开机就跳过，开机后不补跑
    intent_triggers=["排名配置", "排名播报"],
    persist_delivery=False,       # 已固定 deliver_to，手动触发不再覆盖定时目标
)
