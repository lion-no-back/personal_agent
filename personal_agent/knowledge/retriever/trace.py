from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_agent.knowledge.types import KBChunk, KBSearchResult

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TRACE_LOGGER_NAME = "personal_agent.knowledge.search_trace"
_DEFAULT_PREVIEW_CHARS = 240
_DEFAULT_MAX_ITEMS = 30


def _enabled() -> bool:
    raw = os.getenv("KB_TRACE_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _preview_chars() -> int:
    try:
        return max(0, int(os.getenv("KB_TRACE_PREVIEW_CHARS", str(_DEFAULT_PREVIEW_CHARS))))
    except ValueError:
        return _DEFAULT_PREVIEW_CHARS


def _max_items() -> int:
    try:
        return max(1, int(os.getenv("KB_TRACE_MAX_ITEMS", str(_DEFAULT_MAX_ITEMS))))
    except ValueError:
        return _DEFAULT_MAX_ITEMS


def _trace_logger() -> logging.Logger:
    logger = logging.getLogger(_TRACE_LOGGER_NAME)
    if logger.handlers:
        return logger

    log_dir = _PROJECT_ROOT / "logs" / "knowledge"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "search_trace.jsonl",
        maxBytes=int(os.getenv("KB_TRACE_MAX_BYTES", str(10 * 1024 * 1024))),
        backupCount=int(os.getenv("KB_TRACE_BACKUPS", "5")),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _chunk_summary(chunk: KBChunk, preview_chars: int) -> dict[str, Any]:
    body = chunk.compiled or chunk.content or ""
    return {
        "chunk_id": chunk.id,
        "title": chunk.title,
        "source_path": chunk.source_path,
        "source_type": chunk.source_type,
        "page_num": chunk.page_num,
        "chunk_index": chunk.chunk_index,
        "parent_id": chunk.parent_id,
        "content_preview": body[:preview_chars],
    }


def summarize_pairs(
    pairs: list[tuple[str, float]],
    id_map: dict[str, KBChunk] | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    max_items = limit or _max_items()
    preview_chars = _preview_chars()
    items: list[dict[str, Any]] = []
    for cid, score in pairs[:max_items]:
        item: dict[str, Any] = {
            "chunk_id": cid,
            "score": score,
        }
        if id_map and cid in id_map:
            item.update(_chunk_summary(id_map[cid], preview_chars))
            item["score"] = score
        items.append(item)
    return items


def summarize_results(
    results: list[KBSearchResult],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    max_items = limit or _max_items()
    preview_chars = _preview_chars()
    items: list[dict[str, Any]] = []
    for result in results[:max_items]:
        item = _chunk_summary(result.chunk, preview_chars)
        item.update(
            {
                "score": result.score,
                "match_type": result.match_type,
            }
        )
        items.append(item)
    return items


class KBSearchTrace:
    def __init__(self, mode: str, query: str, top_k: int) -> None:
        self.enabled = _enabled()
        self._start = time.perf_counter()
        self.data: dict[str, Any] = {
            "trace_id": uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "query": query,
            "top_k": top_k,
            "latency_ms": {},
            "errors": [],
        }

    def mark(self) -> float:
        return time.perf_counter()

    def latency(self, stage: str, started: float) -> None:
        if self.enabled:
            self.data["latency_ms"][stage] = round((time.perf_counter() - started) * 1000, 2)

    def set(self, key: str, value: Any) -> None:
        if self.enabled:
            self.data[key] = value

    def error(self, stage: str, exc: BaseException) -> None:
        if self.enabled:
            self.data["errors"].append(
                {
                    "stage": stage,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    def write(self) -> None:
        if not self.enabled:
            return
        self.data["latency_ms"]["total"] = round((time.perf_counter() - self._start) * 1000, 2)
        try:
            _trace_logger().info(json.dumps(self.data, ensure_ascii=False, default=str))
        except Exception:
            logging.getLogger(__name__).debug("failed to write KB search trace", exc_info=True)
