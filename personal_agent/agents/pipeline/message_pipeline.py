from __future__ import annotations

from .agent_loop import AgentLoopStage
from .command import CommandStage
from .context import PipelineContext
from .delivery import DeliveryStage
from .enrichment import ContextEnrichmentStage
from .session import SessionStage
from .workflow import WorkflowStage


class MessagePipeline:
    """Lightweight in-process message pipeline for ConversationAgent."""

    def __init__(self, agent) -> None:
        self.stages = [
            SessionStage(agent),
            CommandStage(agent),
            WorkflowStage(agent),
            ContextEnrichmentStage(agent),
            AgentLoopStage(agent),
            DeliveryStage(agent),
        ]

    def run(self, ctx: PipelineContext) -> str:
        for stage in self.stages:
            if ctx.stopped:
                break
            stage.run(ctx)
        return ctx.response
