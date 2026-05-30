import asyncio
import functools
import os
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from personal_agent.knowledge.store import KBStore

_DEFAULT_MODEL = os.getenv("EMBED_MODEL_ID", "text-embedding-3-small")


@functools.lru_cache(maxsize=1)
def _get_client() -> AsyncOpenAI:
    """Lazy singleton — defers construction until first call so env vars are read at use time."""
    return AsyncOpenAI(
        api_key=os.getenv("EMBED_API_KEY"),
        base_url=os.getenv("EMBED_BASE_URL") or None,
    )


async def _embed_query(query: str, model: str = _DEFAULT_MODEL) -> list[float]:
    resp = await _get_client().embeddings.create(input=[query], model=model)
    return resp.data[0].embedding


async def vector_search(
    store: "KBStore", query: str, n: int = 40
) -> list[tuple[str, float]]:
    """Return (chunk_id, similarity_score) pairs; higher = more similar."""
    embedding = await _embed_query(query)
    raw = store.vector_search(embedding, n)
    return [(chunk_id, 1.0 / (1.0 + dist)) for chunk_id, dist in raw]


async def vector_search_all(
    store: "KBStore", queries: list[str], n: int = 20
) -> list[tuple[str, float]]:
    """Run all sub-query vector searches in parallel, then merge with blending."""
    all_results = await asyncio.gather(*[vector_search(store, q, n=40) for q in queries])

    merged: dict[str, list[float]] = {}
    for results in all_results:
        for chunk_id, score in results:
            merged.setdefault(chunk_id, []).append(score)

    blended: list[tuple[str, float]] = []
    for chunk_id, scores in merged.items():
        top = max(scores)
        b = top + (sum(scores) - top) * 0.3
        blended.append((chunk_id, b))

    blended.sort(key=lambda x: x[1], reverse=True)
    return blended[:n]
