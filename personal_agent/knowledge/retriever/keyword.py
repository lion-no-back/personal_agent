from collections import Counter
from pathlib import Path

from personal_agent.knowledge.text_tokenize import tokenize as _tokenize
from personal_agent.knowledge.types import KBChunk

FILENAME_EXACT_BONUS = 200.0
PHRASE_IN_TITLE_BONUS = 50.0
TITLE_TOKEN_WEIGHT = 5.0
CONTENT_TOKEN_WEIGHT = 1.0


def keyword_search_all(
    chunks: list[KBChunk], queries: list[str], n: int = 20
) -> list[tuple[str, float]]:
    """Score chunks against sub-queries; chunks are pre-tokenized once for all queries."""
    if not chunks or not queries:
        return []

    # Pre-index every chunk once (amortized across all queries)
    indexed = [
        (
            c.id,
            Path(c.source_path).stem.lower(),
            (c.title or "").lower(),
            Counter(_tokenize((c.title or "").lower())),
            Counter(_tokenize(c.content.lower())),
        )
        for c in chunks
    ]

    merged: dict[str, float] = {}
    for query in queries:
        q_lower = query.lower()
        q_tokens = _tokenize(query)
        if not q_tokens:
            continue
        for cid, filename, title, title_cnt, content_cnt in indexed:
            score = 0.0
            if q_lower in filename:
                score += FILENAME_EXACT_BONUS
            if q_lower in title:
                score += PHRASE_IN_TITLE_BONUS
            for token in q_tokens:
                score += title_cnt[token] * TITLE_TOKEN_WEIGHT
                score += content_cnt[token] * CONTENT_TOKEN_WEIGHT
            if score > merged.get(cid, 0.0):
                merged[cid] = score

    return sorted(merged.items(), key=lambda x: x[1], reverse=True)[:n]


def keyword_search(chunks: list[KBChunk], query: str, n: int = 20) -> list[tuple[str, float]]:
    return keyword_search_all(chunks, [query], n)
