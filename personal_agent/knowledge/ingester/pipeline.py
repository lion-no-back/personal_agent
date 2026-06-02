import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..store import KBStore
from ..types import KBChunk, KBSource
from . import chunker, embedder
from . import cot as cot_module
from .parsers import get_parser

logger = logging.getLogger(__name__)

_SOURCE_TYPE_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "word", ".doc": "word",
    ".pptx": "word", ".ppt": "word",
    ".xlsx": "excel", ".xls": "excel", ".csv": "excel",
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".webp": "image", ".gif": "image",
    ".md": "markdown", ".txt": "markdown",
    ".html": "web", ".htm": "web",
}


@dataclass
class IngestResult:
    skipped: bool = False
    reason: str = ""
    chunks_added: int = 0

    def __str__(self) -> str:
        if self.skipped:
            return f"skipped ({self.reason})" if self.reason else "skipped"
        return f"{self.chunks_added} chunks"


class Pipeline:
    def __init__(
        self,
        store: KBStore,
        llm,
        wiki_dir: Path,
        llm_client_for_images=None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._wiki_dir = Path(wiki_dir)
        self._llm_client_for_images = llm_client_for_images

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()

    @staticmethod
    def _detect_type(path: Path) -> str:
        return _SOURCE_TYPE_MAP.get(path.suffix.lower(), "unknown")

    def _should_use_cot(self, chunks: list[KBChunk], source_type: str) -> bool:
        # Condition A: short document
        if len(chunks) < 5 or sum(len(c.content) for c in chunks) // 4 < 1000:
            return False
        # Condition B: pure structured data
        if source_type in {"excel", "csv"}:
            return False
        # Condition C (TODO): vector-based duplicate detection against existing kb_vec
        return True

    def _read_wiki_index(self) -> str:
        index_path = self._wiki_dir / "index.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return ""

    def _write_wiki_pages(self, wiki_pages: dict[str, str]) -> None:
        # keys are paths like "wiki/entities/xxx.md", relative to wiki_dir.parent
        for rel_path, content in wiki_pages.items():
            full_path = self._wiki_dir.parent / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

    # ── main entry points ──────────────────────────────────────────

    async def ingest(self, path: str | Path, force: bool = False) -> IngestResult:
        path = Path(path)
        if not path.exists():
            return IngestResult(skipped=True, reason="not found")

        # Step 1: Change detection
        current_hash = self._sha256_file(path)
        source = self._store.get_source(path)
        if not force and source and source.hash == current_hash:
            return IngestResult(skipped=True, reason="unchanged")

        # Step 2: Parse (run in thread — parsers are sync and may block for seconds/minutes)
        parser = get_parser(path)
        if parser is None:
            return IngestResult(skipped=True, reason="unsupported format")
        elements = await asyncio.to_thread(
            parser, path, llm_client=self._llm_client_for_images
        )

        # Step 3: Chunk
        source_type = self._detect_type(path)
        chunks = chunker.chunk(
            elements,
            source_path=str(path),
            source_type=source_type,
            source_hash=current_hash,
        )

        # Step 4: CoT routing
        cot_was_used = False
        if self._should_use_cot(chunks, source_type):
            wiki_index = self._read_wiki_index()
            chunks, wiki_pages = await cot_module.run(
                chunks, wiki_index, self._llm, wiki_dir=self._wiki_dir
            )
            self._write_wiki_pages(wiki_pages)
            cot_was_used = True

        # Step 5: Embed (uses kb_embed_cache for deduplication)
        # Parents are not embedded — only leaves (parent_id is not None) and standalones
        # (parent_id is None with no children) participate in vector search.
        _parent_ids = {c.parent_id for c in chunks if c.parent_id is not None}
        _to_embed = [c for c in chunks if c.parent_id is not None or c.id not in _parent_ids]
        _to_embed = await embedder.embed_chunks(_to_embed, store=self._store)
        _embedded = {c.id: c for c in _to_embed}
        chunks = [_embedded.get(c.id, c) for c in chunks]

        # Step 6: Write lineage & replace old chunks
        if source:
            self._store.delete_chunks(source.chunk_ids)
        self._store.upsert_chunks(chunks)
        self._store.upsert_source(KBSource(
            path=str(path),
            hash=current_hash,
            last_synced=datetime.now(timezone.utc).isoformat(),
            cot_used=cot_was_used,
            chunk_ids=[c.id for c in chunks],
        ))
        return IngestResult(chunks_added=len(chunks))

    async def ingest_batch(
        self, paths: list[str | Path], force: bool = False
    ) -> None:
        for path in paths:
            result = await self.ingest(path, force=force)
            status = "SKIP" if result.skipped else "OK"
            print(f"[{status}] {path} → {result}")
