import asyncio
import hashlib
import os
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from ..types import KBChunk

if TYPE_CHECKING:
    from ..store import KBStore

_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "10"))
_MAX_CONCURRENT = 5
_DIMENSION_CACHE: dict[tuple[str, str], int] = {}


def _cache_key(content: str, model: str, base_url: str | None, dimension: int) -> str:
    parts = (content, model, base_url or "", str(dimension))
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


async def _detect_dimension(client: AsyncOpenAI, model: str, base_url: str | None) -> int:
    key = (model, base_url or "")
    cached = _DIMENSION_CACHE.get(key)
    if cached is not None:
        return cached

    resp = await client.embeddings.create(
        input=["embedding dimension probe"],
        model=model,
    )
    dimension = len(resp.data[0].embedding)
    if dimension <= 0:
        raise RuntimeError(f"Embedding API returned an invalid dimension for model {model!r}")
    _DIMENSION_CACHE[key] = dimension
    return dimension


async def embed_chunks(
    chunks: list[KBChunk],
    model: str | None = None,
    store: "KBStore | None" = None,
) -> list[KBChunk]:
    if model is None:
        model = os.getenv("EMBED_MODEL_ID", "text-embedding-3-small")
    """Fill chunk.embedding via OpenAI-compatible embedding API.

    Checks kb_embed_cache first (cache_key = sha256(content + model)).
    Only calls the API for cache misses. Batches up to 200 texts per request.
    """
    if not chunks:
        return chunks

    base_url = os.getenv("EMBED_BASE_URL") or None
    client = AsyncOpenAI(
        api_key=os.getenv("EMBED_API_KEY"),
        base_url=base_url,
    )
    dimension = await _detect_dimension(client, model, base_url)
    if store is not None:
        store.ensure_embedding_config(dimension, model, base_url)

    to_embed: list[tuple[int, KBChunk]] = []
    for i, chunk in enumerate(chunks):
        if store is not None:
            cached = store.get_embed_cache(
                _cache_key(chunk.content, model, base_url, dimension)
            )
            if cached is not None:
                if len(cached) != dimension:
                    raise RuntimeError(
                        f"Cached embedding dimension mismatch: got {len(cached)}, expected {dimension}"
                    )
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
                if len(emb_obj.embedding) != dimension:
                    raise RuntimeError(
                        f"Embedding dimension mismatch for model {model!r}: "
                        f"got {len(emb_obj.embedding)}, expected {dimension}"
                    )
                chunk.embedding = emb_obj.embedding
                if store is not None:
                    store.set_embed_cache(
                        _cache_key(chunk.content, model, base_url, dimension),
                        emb_obj.embedding,
                    )

    await asyncio.gather(*[_call_batch(b) for b in batches])
    return chunks
