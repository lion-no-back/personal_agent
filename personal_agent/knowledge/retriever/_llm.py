import asyncio
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.core.llm_router import LLMRouter


async def llm_json_array(
    llm: "LLMRouter",
    messages: list[dict],
    temperature: float,
) -> list | None:
    """Call LLM and parse the first JSON array from the response. Returns None on any failure."""
    result = await asyncio.to_thread(llm.think, messages, temperature)
    if not result:
        return None
    m = re.search(r"\[.*?\]", result, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group())
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    return None
