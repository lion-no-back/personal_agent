from __future__ import annotations

import logging

import personal_agent.knowledge as _kb_mod

from .context import PipelineContext

logger = logging.getLogger(__name__)


class ContextEnrichmentStage:
    name = "context_enrichment"

    def __init__(self, agent) -> None:
        self.agent = agent

    def run(self, ctx: PipelineContext) -> None:
        event = ctx.event

        self.agent._context.add_message(ctx.session_key, "user", event.content)

        ctx.user_id = event.sender_open_id
        if self.agent._memory:
            ctx.memory_block = self.agent._memory.retriever.build_memory_block(
                ctx.user_id
            )

        if _kb_mod.kb is None or getattr(event, "is_scheduled", False):
            return

        try:
            kb_hits = _kb_mod.kb.quick_search_sync(event.content, top_k=3)
            if not kb_hits:
                return
            lines = []
            for result in kb_hits:
                body = (result.chunk.compiled or result.chunk.content)[:300]
                lines.append(f"- 【{result.chunk.title}】{body}")
            ctx.kb_context_block = "【知识库相关内容，优先参考】\n" + "\n".join(
                lines
            )
        except Exception:
            logger.debug("KB 预检索失败，跳过", exc_info=True)
