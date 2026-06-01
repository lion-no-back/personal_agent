import asyncio
import json
import logging
import re
from pathlib import Path

from ..types import KBChunk

logger = logging.getLogger(__name__)

_CHAR_LIMIT = 240_000  # ~60K tokens; larger docs are truncated

_STAGE_A_SYSTEM = "你是知识提取专家，负责从文档中提取结构化知识。"

_STAGE_A_USER = """\
以下是文档内容：

{content}
{wiki_context}
请提取文档知识，只输出 JSON，不要有其他文字：
{{
  "entities": [{{"name": "...", "type": "person|concept|event|place|other", "description": "...", "wiki_path": "wiki/entities/name.md"}}],
  "key_facts": ["重要事实1", "重要事实2"],
  "connections_to_existing": ["与现有知识的关联"],
  "contradictions": ["与现有知识的矛盾"],
  "pages_to_create": ["wiki/entities/xxx.md"],
  "pages_to_update": ["wiki/concepts/yyy.md"]
}}\
"""

_STAGE_B_USER = """\
现有页面内容：
{existing}

基于以下分析补充/修正，保留所有现有有价值信息，矛盾时两种说法都保留并标注来源，使用 [[wikilink]] 语法。

分析结果：
{stage_a_json}

只输出 ---PAGE 块，格式如下：
---PAGE: {path}---
（此处填写 Markdown 内容）\
"""


async def run(
    chunks: list[KBChunk],
    existing_wiki_index: str,
    llm,
    wiki_dir: Path | None = None,
) -> tuple[list[KBChunk], dict[str, str]]:
    """Two-phase CoT: Stage A analysis → Stage B wiki generation.

    Returns updated chunks (compiled filled) and wiki_pages {rel_path: content}.
    wiki_pages keys are paths like "wiki/entities/xxx.md" relative to wiki_dir.parent.
    """
    all_content = "\n\n".join(
        f"[Chunk {c.chunk_index}] {c.title}\n{c.content}" for c in chunks
    )
    if len(all_content) > _CHAR_LIMIT:
        logger.warning(
            "Content truncated from %d to %d chars for Stage A CoT",
            len(all_content), _CHAR_LIMIT,
        )
        all_content = all_content[:_CHAR_LIMIT]

    wiki_context = (
        f"\n现有知识库目录：\n{existing_wiki_index}\n" if existing_wiki_index else ""
    )

    stage_a = await _stage_a(all_content, wiki_context, llm)
    wiki_pages = await _stage_b(stage_a, wiki_dir, llm)

    key_facts = stage_a.get("key_facts", [])
    if key_facts:
        compiled = "\n".join(f"- {f}" for f in key_facts)
        for chunk in chunks:
            chunk.compiled = compiled

    return chunks, wiki_pages


async def _stage_a(content: str, wiki_context: str, llm) -> dict:
    messages = [
        {"role": "system", "content": _STAGE_A_SYSTEM},
        {"role": "user", "content": _STAGE_A_USER.format(
            content=content, wiki_context=wiki_context,
        )},
    ]
    response = await asyncio.to_thread(llm.think, messages, 0.3)
    return _parse_json(response or "")


_STAGE_B_CONCURRENCY = 5  # cap concurrent LLM calls to avoid rate limits


async def _stage_b(
    stage_a: dict,
    wiki_dir: Path | None,
    llm,
) -> dict[str, str]:
    # Deduplicate while preserving order
    seen: set[str] = set()
    pages: list[str] = []
    for p in stage_a.get("pages_to_create", []) + stage_a.get("pages_to_update", []):
        if p not in seen:
            seen.add(p)
            pages.append(p)

    if not pages:
        return {}

    stage_a_json = json.dumps(stage_a, ensure_ascii=False, indent=2)
    sem = asyncio.Semaphore(_STAGE_B_CONCURRENCY)

    async def _generate(page_path: str) -> tuple[str, str | None]:
        async with sem:
            existing = _read_existing(page_path, wiki_dir)
            messages = [
                {"role": "user", "content": _STAGE_B_USER.format(
                    existing=existing or "(新页面)",
                    stage_a_json=stage_a_json,
                    path=page_path,
                )},
            ]
            try:
                response = await asyncio.to_thread(llm.think, messages, 0.5)
                return page_path, _extract_page(response, page_path) if response else None
            except Exception as e:
                logger.warning("Stage B failed for %s: %s", page_path, e)
                return page_path, None

    results = await asyncio.gather(*(_generate(p) for p in pages))
    return {path: content for path, content in results if content}


def _read_existing(page_path: str, wiki_dir: Path | None) -> str:
    if wiki_dir is None:
        return ""
    # page_path is relative to wiki_dir.parent (e.g. "wiki/entities/xxx.md")
    full_path = wiki_dir.parent / page_path
    if full_path.exists():
        try:
            return full_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""


def _parse_json(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        logger.warning("Stage A JSON parse failed; returning empty analysis")
        return {}


def _extract_page(response: str, page_path: str) -> str | None:
    pattern = rf"---PAGE:\s*{re.escape(page_path)}\s*---\s*([\s\S]*?)(?=\n---PAGE:|$)"
    match = re.search(pattern, response)
    if match:
        return match.group(1).strip() or None
    # No PAGE block found but response has content — treat whole response as page
    if "---PAGE:" not in response:
        return response.strip() or None
    return None
