from __future__ import annotations

from ...guardrails import sanitize_output
from .context import PipelineContext


class DeliveryStage:
    name = "delivery"

    def __init__(self, agent) -> None:
        self.agent = agent

    def run(self, ctx: PipelineContext) -> None:
        if not ctx.delivery_kind:
            return

        if self.agent._interaction_logger:
            self.agent._interaction_logger.log_final(
                ctx.session_key,
                ctx.ui_text_buffer,
            )

        sanitized = sanitize_output(
            ctx.ui_text_buffer.strip(),
            session_key=ctx.session_key,
        )

        if ctx.on_update:
            if ctx.keep_thinking:
                done_text = sanitized
            else:
                done_text = sanitize_output(
                    ctx.final_response.strip(),
                    session_key=ctx.session_key,
                )
            ctx.on_update(done_text, "done")

        if ctx.delivery_kind == "success":
            if self.agent._memory:
                self.agent._memory.extractor.extract_async(
                    ctx.user_id,
                    self.agent._context.get_messages(ctx.session_key),
                )
        elif ctx.delivery_kind == "timeout":
            self.agent._context.add_message(
                ctx.session_key,
                "assistant",
                content="\n\n> ⏰ 抱歉，经过多次尝试未能完成请求，请尝试重组您的问题。",
            )

        ctx.stop(sanitized)
