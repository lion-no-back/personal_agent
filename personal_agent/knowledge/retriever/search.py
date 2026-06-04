import asyncio
import logging
from typing import TYPE_CHECKING

from personal_agent.knowledge.types import KBChunk, KBSearchResult

from .keyword import keyword_search_all
from .query_decomposer import decompose_query
from .rerank import rerank
from .rrf import rrf_merge
from .vector import vector_search_all

if TYPE_CHECKING:
    from personal_agent.core.llm_router import LLMRouter
    from personal_agent.knowledge.store import KBStore

logger = logging.getLogger(__name__)


class Retriever:
    def __init__(self, store: "KBStore", llm: "LLMRouter | None" = None):
        self._store = store
        self._llm = llm

    def _expand_parents(
        self,
        fused: list[tuple[str, float]],
        id_map: dict[str, KBChunk],
    ) -> list[tuple[str, float]]:
        """Bubble leaf hits up to their parent; standalones/direct parent hits
        pass through. Parent score = max(child scores) + multi-hit bonus."""
        parent_max_score: dict[str, float] = {}
        parent_hit_count: dict[str, int] = {}
        final_scores: dict[str, float] = {}

        for cid, score in fused:
            chunk = id_map.get(cid)
            if chunk is None:
                continue
            if chunk.parent_id is not None:
                pid = chunk.parent_id
                if score > parent_max_score.get(pid, 0.0):
                    parent_max_score[pid] = score
                parent_hit_count[pid] = parent_hit_count.get(pid, 0) + 1
            else:
                final_scores[chunk.id] = max(final_scores.get(chunk.id, 0.0), score)

        for pid, score in parent_max_score.items():
            bonus = 0.05 * (parent_hit_count[pid] - 1)
            final_scores[pid] = max(final_scores.get(pid, 0.0), score + bonus)

        return sorted(final_scores.items(), key=lambda x: x[1], reverse=True)

    def _fetch_parents(
        self,
        ranked: list[tuple[str, float]],
        id_map: dict[str, KBChunk],
    ) -> None:
        """Fetch any parent chunks promoted by _expand_parents that aren't in id_map.
        Mutates id_map in place so _build_results can stay a pure formatter."""
        missing = [cid for cid, _ in ranked if cid not in id_map]
        if missing:
            for c in self._store.get_chunks_by_ids(missing):
                id_map[c.id] = c

    def _build_results(
        self,
        ranked: list[tuple[str, float]],
        id_map: dict[str, KBChunk],
        kw_ids: set[str],
        vec_ids: set[str],
    ) -> list[KBSearchResult]:
        """Pure formatter: ranked (cid, score) pairs → KBSearchResult list."""
        results: list[KBSearchResult] = []
        for cid, score in ranked:
            chunk = id_map.get(cid)
            if chunk is None:
                continue
            if cid in kw_ids and cid in vec_ids:
                mt = "hybrid"
            elif cid in vec_ids:
                mt = "vector"
            elif cid in kw_ids:
                mt = "keyword"
            else:
                mt = "hybrid"  # expanded from a child match
            results.append(KBSearchResult(chunk=chunk, score=score, match_type=mt))
        return results

    async def search(self, query: str, top_k: int = 5) -> list[KBSearchResult]:
        sub_queries = await decompose_query(query, self._llm)

        # Larger candidate pool so parent-expansion doesn't starve diversity.
        leaf_recall = max(top_k * 6, 30)

        # Candidate generation from both indexes — each bounded by SQL LIMIT,
        # no full-table scan. Kick off vector (I/O-bound) before FTS (local).
        vec_task = asyncio.create_task(
            vector_search_all(self._store, sub_queries, n=leaf_recall)
        )
        kw_cand_ids = self._store.fts_search_all(sub_queries, n=leaf_recall)

        try:
            vec_results = await vec_task
        except Exception:
            logger.warning("向量检索失败，降级为纯关键词模式", exc_info=True)
            vec_results = []

        # Fetch only the small candidate union, then run the bespoke keyword
        # scorer over it (preserves filename/title bonuses on a bounded set).
        cand_ids = set(kw_cand_ids) | {cid for cid, _ in vec_results}
        if not cand_ids:
            return []
        cand_chunks = self._store.get_chunks_by_ids(list(cand_ids))
        id_map = {c.id: c for c in cand_chunks}

        kw_results = keyword_search_all(cand_chunks, sub_queries, n=leaf_recall)
        fused = rrf_merge(kw_results, vec_results, top_n=leaf_recall)
        if not fused:
            return []

        kw_ids = {cid for cid, _ in kw_results}
        vec_ids = {cid for cid, _ in vec_results}

        ranked = self._expand_parents(fused, id_map)
        self._fetch_parents(ranked[: top_k * 3], id_map)
        candidates = self._build_results(ranked[: top_k * 3], id_map, kw_ids, vec_ids)

        reranked = await rerank(query, candidates, self._llm)
        return reranked[:top_k]

    async def search_lightweight(self, query: str, top_k: int = 3) -> list[KBSearchResult]:
        """No-LLM fast path: keyword + vector only, no decompose/rerank."""
        sub_queries = [query]
        vec_task = asyncio.create_task(
            vector_search_all(self._store, sub_queries, n=20)
        )
        kw_cand_ids = self._store.fts_search_all(sub_queries, n=20)

        try:
            vec_results = await vec_task
        except Exception:
            vec_results = []

        cand_ids = set(kw_cand_ids) | {cid for cid, _ in vec_results}
        if not cand_ids:
            return []
        cand_chunks = self._store.get_chunks_by_ids(list(cand_ids))
        id_map = {c.id: c for c in cand_chunks}

        kw_results = keyword_search_all(cand_chunks, sub_queries, n=20)
        fused = rrf_merge(kw_results, vec_results, top_n=top_k * 4)
        if not fused:
            return []

        kw_ids = {cid for cid, _ in kw_results}
        vec_ids = {cid for cid, _ in vec_results}

        ranked = self._expand_parents(fused, id_map)
        self._fetch_parents(ranked[:top_k], id_map)
        return self._build_results(ranked[:top_k], id_map, kw_ids, vec_ids)
