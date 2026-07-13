from __future__ import annotations

from .context import PipelineContext


class WorkflowStage:
    name = "workflow"

    def __init__(self, agent) -> None:
        self.agent = agent

    def run(self, ctx: PipelineContext) -> None:
        ondemand_ack = self.agent._check_ondemand_workflow(
            ctx.event,
            ctx.session_key,
            ctx.publish_progress,
        )
        if ondemand_ack is not None:
            ctx.stop(ondemand_ack)
