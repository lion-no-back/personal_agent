import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .ingester.pipeline import Pipeline, _SOURCE_TYPE_MAP
from .paths import canonical_source_path
from .store import KBStore

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0
WATCH_EXTENSIONS = frozenset(_SOURCE_TYPE_MAP)

Action = Literal["ingest", "delete"]


class KBWatcher:
    def __init__(self, pipeline: Pipeline, store: KBStore) -> None:
        self._pipeline = pipeline
        self._store = store
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._observer: Observer | None = None

    def start(self, loop: asyncio.AbstractEventLoop, watch_dir: Path) -> None:
        self._loop = loop
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(watch_dir), recursive=True)
        self._observer.start()
        logger.info("KBWatcher started on %s", watch_dir)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    # ── called from watchdog thread ────────────────────────────────

    def _schedule(self, path: str, action: Action) -> None:
        canonical = str(canonical_source_path(path))
        self._loop.call_soon_threadsafe(self._debounce, canonical, action)

    # ── runs on loop thread ────────────────────────────────────────

    def _debounce(self, path: str, action: Action) -> None:
        existing = self._pending.pop(path, None)
        if existing:
            existing.cancel()
        self._pending[path] = self._loop.call_later(
            DEBOUNCE_SECONDS, self._fire, path, action
        )

    def _fire(self, path: str, action: Action) -> None:
        self._pending.pop(path, None)
        if action == "ingest":
            logger.info("detected new/modified file, ingesting %s", path)
            self._loop.create_task(self._ingest_logged(path))
        else:
            self._store.delete_source(path)
            logger.info("deleted lineage for %s", path)

    async def _ingest_logged(self, path: str) -> None:
        try:
            result = await self._pipeline.ingest(path)
            logger.info("ingest done %s → %s", path, result)
        except Exception:
            logger.exception("ingest failed for %s", path)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: KBWatcher) -> None:
        self._watcher = watcher

    @staticmethod
    def _relevant(path: str) -> bool:
        _, ext = os.path.splitext(path)
        return ext.lower() in WATCH_EXTENSIONS

    def on_created(self, event) -> None:
        if not event.is_directory and self._relevant(event.src_path):
            self._watcher._schedule(event.src_path, "ingest")

    on_modified = on_created

    def on_deleted(self, event) -> None:
        if not event.is_directory and self._relevant(event.src_path):
            self._watcher._schedule(event.src_path, "delete")

    def on_moved(self, event) -> None:
        if not event.is_directory:
            if self._relevant(event.src_path):
                self._watcher._schedule(event.src_path, "delete")
            if self._relevant(event.dest_path):
                self._watcher._schedule(event.dest_path, "ingest")
