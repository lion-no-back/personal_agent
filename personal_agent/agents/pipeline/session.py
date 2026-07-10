from __future__ import annotations

from .context import PipelineContext


class SessionStage:
    name = "session"

    def __init__(self, agent) -> None:
        self.agent = agent

    def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        ctx.session_key = self.agent._build_session_key(event)

        if self.agent._interaction_logger:
            self.agent._interaction_logger.log_session_start(
                ctx.session_key,
                event.content,
                getattr(event, "sender_name", None),
            )
