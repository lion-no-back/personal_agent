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

        # Kick off vector search (I/O-bound) before keyword scoring (CPU-bound) to overlap them
        vec_task = asyncio.create_task(vector_search_all(self._store, sub_queries, n=20))
        kw_results = keyword_search_all(all_chunks, sub_queries, n=20)

        try:
            vec_results = await vec_task
        except Exception:
            logger.warning("向量检索失败，降级为纯关键词模式", exc_info=True)
            vec_results = []

        fused = rrf_merge(kw_results, vec_results, top_n=20)
        if not fused:
            return []

        chunk_ids = [cid for cid, _ in fused]
        id_to_score = dict(fused)
        chunks = self._store.get_chunks_by_ids(chunk_ids)
        id_to_chunk = {c.id: c for c in chunks}

        kw_ids = {cid for cid, _ in kw_results}
        vec_ids = {cid for cid, _ in vec_results}

        candidates: list[KBSearchResult] = []
        for cid in chunk_ids:
            if cid not in id_to_chunk:
                continue
            if cid in kw_ids and cid in vec_ids:
                match_type = "hybrid"
            elif cid in vec_ids:
                match_type = "vector"
            else:
                match_type = "keyword"
            candidates.append(
                KBSearchResult(
                    chunk=id_to_chunk[cid],
                    score=id_to_score[cid],
                    match_type=match_type,
                )
            )

        reranked = await rerank(query, candidates, self._llm)
        return reranked[:top_k]
