from __future__ import annotations

from ..runner import AgentRunner, AgentRunSpec, AgentRuntime
from .context import PipelineContext


class AgentLoopStage:
    name = "agent_loop"

    def __init__(self, agent) -> None:
        self.runner = AgentRunner(AgentRuntime.from_agent(agent))

    def run(self, ctx: PipelineContext) -> None:
        run_result = self.runner.run(
            AgentRunSpec(
                event=ctx.event,
                session_key=ctx.session_key,
                memory_block=ctx.memory_block,
                kb_context_block=ctx.kb_context_block,
                response_publisher=ctx.publish_progress,
                stream_updates_sync_with_llm=ctx.stream_updates_sync_with_llm,
                push_target=ctx.push_target,
                cancel_event=ctx.cancel_event,
                keep_thinking=ctx.keep_thinking,
            )
        )
        ctx.final_response = run_result.final_response
        ctx.ui_text_buffer = run_result.ui_text_buffer
        ctx.score_result = run_result.score_result
        ctx.task_type = run_result.task_type
        ctx.delivery_kind = run_result.delivery_kind
        if run_result.stopped:
            ctx.stop(run_result.response)
