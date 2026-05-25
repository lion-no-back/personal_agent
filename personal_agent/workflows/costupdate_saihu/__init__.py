from personal_agent.workflows.base import WorkflowMeta

WORKFLOW_META = WorkflowMeta(
    id="costupdate_saihu",
    name="赛狐成本每周更新",
    description="更新赛狐平台商品成本价格数据",
    schedule="0 2 * * 1",  # 每周一 UTC 02:00 = 北京时间 10:00
    timeout=1800,
    deliver_to="ou_df92a89ee7a6c632ec7a210a7f25e707",
    misfire_catchup=True,
    max_retries=2,
    retry_delay=300,
    # intent_triggers 不填，此为纯定时 workflow
)
