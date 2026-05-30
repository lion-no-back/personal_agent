from typing import TYPE_CHECKING

from personal_agent.knowledge.types import KBSearchResult

from ._llm import llm_json_array

if TYPE_CHECKING:
    from personal_agent.core.llm_router import LLMRouter


async def rerank(
    query: str,
    candidates: list[KBSearchResult],
    llm: "LLMRouter | None" = None,
) -> list[KBSearchResult]:
    """LLM 0/1 filter: keep only relevant chunks; fall back to original order on failure."""
    if not candidates or llm is None:
        return candidates

    lines = "\n".join(
        f"[{i}] {r.chunk.title}: {r.chunk.content[:200]}"
        for i, r in enumerate(candidates)
    )
    prompt = (
        f"查询：{query}\n"
        f"以下片段中，哪些与查询直接相关？\n"
        f"返回 JSON 数字数组，仅含相关编号（0-based），如 [0,2,5]。\n"
        f"{lines}"
    )
    messages = [{"role": "user", "content": prompt}]

    try:
        indices = await llm_json_array(llm, messages, 0.0)
        if indices:
            filtered = [
                candidates[i]
                for i in indices
                if isinstance(i, int) and 0 <= i < len(candidates)
            ]
            if filtered:
                return filtered
    except Exception:
        pass

    return candidates
