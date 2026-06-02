import asyncio
import logging
from typing import TYPE_CHECKING

from personal_agent.knowledge.types import KBSearchResult

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

    async def search(self, query: str, top_k: int = 5) -> list[KBSearchResult]:
        sub_queries = await decompose_query(query, self._llm)

        all_chunks = self._store.get_all_chunks()
        id_to_all_chunk = {c.id: c for c in all_chunks}

        # Larger candidate pool so parent-expansion doesn't starve diversity.
        # e.g. top_k=5 → recall 30 leaves; after expanding to parents,
        # we still have enough distinct parents for rerank to choose from.
        leaf_recall = max(top_k * 6, 30)

        # Kick off vector search (I/O-bound) before keyword scoring (CPU-bound)
        vec_task = asyncio.create_task(
            vector_search_all(self._store, sub_queries, n=leaf_recall)
        )
        kw_results = keyword_search_all(all_chunks, sub_queries, n=leaf_recall)

        try:
            vec_results = await vec_task
        except Exception:
            logger.warning("向量检索失败，降级为纯关键词模式", exc_info=True)
            vec_results = []

        fused = rrf_merge(kw_results, vec_results, top_n=leaf_recall)
        if not fused:
            return []

        chunk_ids = [cid for cid, _ in fused]
        id_to_score = dict(fused)
        matched = self._store.get_chunks_by_ids(chunk_ids)

        kw_ids = {cid for cid, _ in kw_results}
        vec_ids = {cid for cid, _ in vec_results}

        # ── Parent expansion ──────────────────────────────────────────────────
        # Leaves bubble up to their parent; score = max(child scores) + multi-hit bonus.
        # Standalones and direct parent hits pass through unchanged.
        parent_max_score: dict[str, float] = {}
        parent_hit_count: dict[str, int] = {}
        final_scores: dict[str, float] = {}

        for chunk in matched:
            score = id_to_score.get(chunk.id, 0.0)
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

        sorted_ids = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)

        # Build candidates from parent/standalone chunks (cap at top_k * 3 for rerank)
        candidates: list[KBSearchResult] = []
        for cid, score in sorted_ids[: top_k * 3]:
            chunk = id_to_all_chunk.get(cid)
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
            candidates.append(KBSearchResult(chunk=chunk, score=score, match_type=mt))

        reranked = await rerank(query, candidates, self._llm)
        return reranked[:top_k]

    async def search_lightweight(self, query: str, top_k: int = 3) -> list[KBSearchResult]:
        """No-LLM fast path: keyword + vector only, no decompose/rerank."""
        all_chunks = self._store.get_all_chunks()
        if not all_chunks:
            return []

        id_to_all_chunk = {c.id: c for c in all_chunks}
        sub_queries = [query]
        vec_task = asyncio.create_task(vector_search_all(self._store, sub_queries, n=20))
        kw_results = keyword_search_all(all_chunks, sub_queries, n=20)
        try:
            vec_results = await vec_task
        except Exception:
            vec_results = []

        fused = rrf_merge(kw_results, vec_results, top_n=top_k * 4)
        if not fused:
            return []

        chunk_ids = [cid for cid, _ in fused]
        id_to_score = dict(fused)
        matched = self._store.get_chunks_by_ids(chunk_ids)

        kw_ids = {cid for cid, _ in kw_results}
        vec_ids = {cid for cid, _ in vec_results}

        # Same parent expansion for consistency
        parent_max_score: dict[str, float] = {}
        parent_hit_count: dict[str, int] = {}
        final_scores: dict[str, float] = {}

        for chunk in matched:
            score = id_to_score.get(chunk.id, 0.0)
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

        sorted_ids = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)

        results = []
        for cid, score in sorted_ids[:top_k]:
            chunk = id_to_all_chunk.get(cid)
            if chunk is None:
                continue
            if cid in kw_ids and cid in vec_ids:
                mt = "hybrid"
            elif cid in vec_ids:
                mt = "vector"
            elif cid in kw_ids:
                mt = "keyword"
            else:
                mt = "hybrid"
            results.append(KBSearchResult(chunk=chunk, score=score, match_type=mt))
        return results
