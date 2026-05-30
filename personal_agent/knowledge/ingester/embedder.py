import asyncio
import hashlib
import os
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from ..types import KBChunk

if TYPE_CHECKING:
    from ..store import KBStore

_BATCH_SIZE = 200
_MAX_CONCURRENT = 5
_DEFAULT_MODEL = os.getenv("EMBED_MODEL_ID", "text-embedding-3-small")


def _cache_key(content: str, model: str) -> str:
    return hashlib.sha256((content + model).encode()).hexdigest()


async def embed_chunks(
    chunks: list[KBChunk],
    model: str = _DEFAULT_MODEL,
    store: "KBStore | None" = None,
) -> list[KBChunk]:
    """Fill chunk.embedding via OpenAI-compatible embedding API.

    Checks kb_embed_cache first (cache_key = sha256(content + model)).
    Only calls the API for cache misses. Batches up to 200 texts per request.
    """
    if not chunks:
        return chunks

    client = AsyncOpenAI(
        api_key=os.getenv("EMBED_API_KEY"),
        base_url=os.getenv("EMBED_BASE_URL") or None,
    )

    to_embed: list[tuple[int, KBChunk]] = []
    for i, chunk in enumerate(chunks):
        if store is not None:
            cached = store.get_embed_cache(_cache_key(chunk.content, model))
            if cached is not None:
                chunk.embedding = cached
                continue
        to_embed.append((i, chunk))

    if not to_embed:
        return chunks

    batches = [
        to_embed[i : i + _BATCH_SIZE]
        for i in range(0, len(to_embed), _BATCH_SIZE)
    ]
    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _call_batch(batch: list[tuple[int, KBChunk]]) -> None:
        async with sem:
            texts = [c.content for _, c in batch]
            resp = await client.embeddings.create(input=texts, model=model)
            for (_, chunk), emb_obj in zip(batch, resp.data):
                chunk.embedding = emb_obj.embedding
                if store is not None:
                    store.set_embed_cache(
                        _cache_key(chunk.content, model), emb_obj.embedding
                    )

    await asyncio.gather(*[_call_batch(b) for b in batches])
    return chunks
