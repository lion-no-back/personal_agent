K = 60.0


def _accumulate(scores: dict[str, float], results: list[tuple[str, float]]) -> None:
    for rank, (chunk_id, _) in enumerate(results):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (K + rank + 1)


def rrf_merge(
    kw_results: list[tuple[str, float]],
    vec_results: list[tuple[str, float]],
    top_n: int = 20,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: score += 1/(K + rank) for each signal."""
    scores: dict[str, float] = {}
    _accumulate(scores, kw_results)
    _accumulate(scores, vec_results)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
