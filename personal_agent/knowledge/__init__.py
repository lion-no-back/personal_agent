import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from .store import KBStore
from .ingester.pipeline import Pipeline
from .retriever.search import Retriever
from .watcher import KBWatcher

if TYPE_CHECKING:
    from personal_agent.core.llm_router import LLMRouter
    from personal_agent.knowledge.types import KBSearchResult

_SEARCH_TIMEOUT = 30  # seconds — budget for one retriever.search() call

# Module-level singleton set by main.py after initialization.
kb: "KnowledgeBase | None" = None


class KnowledgeBase:
    def __init__(
        self,
        db_path: Path,
        wiki_dir: Path,
        sources_dir: Path,
        llm: "LLMRouter",
    ) -> None:
        self.sources_dir = Path(sources_dir)
        self._wiki_dir = Path(wiki_dir)

        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self._wiki_dir.mkdir(parents=True, exist_ok=True)

        self._store = KBStore(db_path)
        self._pipeline = Pipeline(store=self._store, llm=llm, wiki_dir=self._wiki_dir)
        self.retriever = Retriever(store=self._store, llm=llm)
        self._watcher = KBWatcher(pipeline=self._pipeline, store=self._store)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="kb-loop",
        )
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start_watcher(self) -> None:
        self._watcher.start(self._loop, self.sources_dir)

    def search_sync(self, query: str, top_k: int = 5) -> "list[KBSearchResult]":
        future = asyncio.run_coroutine_threadsafe(
            self.retriever.search(query, top_k=top_k),
            self._loop,
        )
        return future.result(timeout=_SEARCH_TIMEOUT)
