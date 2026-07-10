from __future__ import annotations

from .context import PipelineContext


class CommandStage:
    name = "command"

    def __init__(self, agent) -> None:
        self.agent = agent

    def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        command_response = self.agent._handle_command(
            event.content,
            ctx.session_key,
            event.sender_open_id,
        )
        if command_response is None:
            return

        if ctx.on_update:
            ctx.on_update(command_response, "done")
        ctx.stop(command_response)
