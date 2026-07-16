import asyncio
import logging
from typing import TYPE_CHECKING

from personal_agent.knowledge.types import KBChunk, KBSearchResult

from .keyword import keyword_search_all
from .query_decomposer import decompose_query
from .rerank import rerank
from .rrf import rrf_merge
from .trace import KBSearchTrace, summarize_pairs, summarize_results
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
        trace = KBSearchTrace("full", query, top_k)
        try:
            started = trace.mark()
            sub_queries = await decompose_query(query, self._llm)
            trace.latency("decompose", started)
            trace.set("sub_queries", sub_queries)

            # Larger candidate pool so parent-expansion doesn't starve diversity.
            leaf_recall = max(top_k * 6, 30)
            trace.set("leaf_recall", leaf_recall)

            # Candidate generation from both indexes — each bounded by SQL LIMIT,
            # no full-table scan. Kick off vector (I/O-bound) before FTS (local).
            vector_started = trace.mark()
            vec_task = asyncio.create_task(
                vector_search_all(self._store, sub_queries, n=leaf_recall)
            )
            started = trace.mark()
            kw_cand_ids = self._store.fts_search_all(sub_queries, n=leaf_recall)
            trace.latency("fts_candidates", started)
            trace.set("keyword_candidate_ids", kw_cand_ids)

            try:
                vec_results = await vec_task
                trace.latency("vector", vector_started)
            except Exception as exc:
                trace.error("vector", exc)
                logger.warning("向量检索失败，降级为纯关键词模式", exc_info=True)
                vec_results = []
            trace.set("vector_candidate_ids", [cid for cid, _ in vec_results])

            # Fetch only the small candidate union, then run the bespoke keyword
            # scorer over it (preserves filename/title bonuses on a bounded set).
            cand_ids = set(kw_cand_ids) | {cid for cid, _ in vec_results}
            trace.set("candidate_union_ids", list(cand_ids))
            if not cand_ids:
                trace.set("final_results", [])
                return []

            started = trace.mark()
            cand_chunks = self._store.get_chunks_by_ids(list(cand_ids))
            trace.latency("fetch_candidates", started)
            id_map = {c.id: c for c in cand_chunks}

            trace.set("vector_results", summarize_pairs(vec_results, id_map))

            started = trace.mark()
            kw_results = keyword_search_all(cand_chunks, sub_queries, n=leaf_recall)
            trace.latency("keyword_score", started)
            trace.set("keyword_results", summarize_pairs(kw_results, id_map))

            started = trace.mark()
            fused = rrf_merge(kw_results, vec_results, top_n=leaf_recall)
            trace.latency("rrf", started)
            trace.set("rrf_top", summarize_pairs(fused, id_map))
            if not fused:
                trace.set("final_results", [])
                return []

            kw_ids = {cid for cid, _ in kw_results}
            vec_ids = {cid for cid, _ in vec_results}

            started = trace.mark()
            ranked = self._expand_parents(fused, id_map)
            self._fetch_parents(ranked[: top_k * 3], id_map)
            trace.latency("parent_expand", started)
            trace.set("expanded_top", summarize_pairs(ranked, id_map, limit=top_k * 3))

            candidates = self._build_results(ranked[: top_k * 3], id_map, kw_ids, vec_ids)
            trace.set("rerank_input", summarize_results(candidates, limit=top_k * 3))

            started = trace.mark()
            reranked = await rerank(query, candidates, self._llm)
            trace.latency("rerank", started)
            final = reranked[:top_k]
            trace.set("final_results", summarize_results(final, limit=top_k))
            return final
        except Exception as exc:
            trace.error("search", exc)
            raise
        finally:
            trace.write()

    async def search_lightweight(self, query: str, top_k: int = 3) -> list[KBSearchResult]:
        """No-LLM fast path: keyword + vector only, no decompose/rerank."""
        trace = KBSearchTrace("lightweight", query, top_k)
        try:
            sub_queries = [query]
            trace.set("sub_queries", sub_queries)
            vector_started = trace.mark()
            vec_task = asyncio.create_task(
                vector_search_all(self._store, sub_queries, n=20)
            )
            started = trace.mark()
            kw_cand_ids = self._store.fts_search_all(sub_queries, n=20)
            trace.latency("fts_candidates", started)
            trace.set("keyword_candidate_ids", kw_cand_ids)

            try:
                vec_results = await vec_task
                trace.latency("vector", vector_started)
            except Exception as exc:
                trace.error("vector", exc)
                vec_results = []
            trace.set("vector_candidate_ids", [cid for cid, _ in vec_results])

            cand_ids = set(kw_cand_ids) | {cid for cid, _ in vec_results}
            trace.set("candidate_union_ids", list(cand_ids))
            if not cand_ids:
                trace.set("final_results", [])
                return []

            started = trace.mark()
            cand_chunks = self._store.get_chunks_by_ids(list(cand_ids))
            trace.latency("fetch_candidates", started)
            id_map = {c.id: c for c in cand_chunks}

            trace.set("vector_results", summarize_pairs(vec_results, id_map))

            started = trace.mark()
            kw_results = keyword_search_all(cand_chunks, sub_queries, n=20)
            trace.latency("keyword_score", started)
            trace.set("keyword_results", summarize_pairs(kw_results, id_map))

            started = trace.mark()
            fused = rrf_merge(kw_results, vec_results, top_n=top_k * 4)
            trace.latency("rrf", started)
            trace.set("rrf_top", summarize_pairs(fused, id_map))
            if not fused:
                trace.set("final_results", [])
                return []

            kw_ids = {cid for cid, _ in kw_results}
            vec_ids = {cid for cid, _ in vec_results}

            started = trace.mark()
            ranked = self._expand_parents(fused, id_map)
            self._fetch_parents(ranked[:top_k], id_map)
            trace.latency("parent_expand", started)
            trace.set("expanded_top", summarize_pairs(ranked, id_map, limit=top_k))

            final = self._build_results(ranked[:top_k], id_map, kw_ids, vec_ids)
            trace.set("final_results", summarize_results(final, limit=top_k))
            return final
        except Exception as exc:
            trace.error("search", exc)
            raise
        finally:
            trace.write()
