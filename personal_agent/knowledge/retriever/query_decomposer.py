from typing import TYPE_CHECKING

from ._llm import llm_json_array

if TYPE_CHECKING:
    from personal_agent.core.llm_router import LLMRouter


async def decompose_query(query: str, llm: "LLMRouter | None" = None) -> list[str]:
    """Split query into at most 3 single-concept sub-queries via LLM."""
    if llm is None:
        return [query]

    messages = [
        {
            "role": "user",
            "content": (
                "将以下查询分解为最多3条子查询。\n"
                "规则：每条子查询只含1个概念；不使用布尔运算符（AND/OR/NOT）；"
                "保留专有名词和技术术语；直接用原始语言，不翻译；输出 JSON array。\n\n"
                f"查询：{query}"
            ),
        }
    ]
    try:
        parsed = await llm_json_array(llm, messages, 0.3)
        if parsed:
            return [str(q) for q in parsed[:3]]
    except Exception:
        pass
    return [query]
