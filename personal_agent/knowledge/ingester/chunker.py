import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ..types import KBChunk
from .parsers.types import ParsedElement

TARGET_CHARS = 512 * 4   # ~512 tokens
OVERLAP_CHARS = 64 * 4   # ~64 tokens


def _chunk_id(source_path: str, index: int) -> str:
    return hashlib.sha256(f"{source_path}:{index}".encode()).hexdigest()


def _split_text(text: str) -> list[str]:
    """Split text into target-sized pieces; each piece after the first
    includes the last OVERLAP_CHARS of the previous piece as a prefix."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= TARGET_CHARS:
        return [text]
    pieces: list[str] = []
    pos = 0
    while pos < len(text):
        start = max(0, pos - OVERLAP_CHARS) if pieces else 0
        piece = text[start : pos + TARGET_CHARS].strip()
        if piece:
            pieces.append(piece)
        pos += TARGET_CHARS
    return pieces or [text]


def chunk(
    elements: list[ParsedElement],
    source_path: str,
    source_type: str,
    source_hash: str,
) -> list[KBChunk]:
    chunks: list[KBChunk] = []
    idx = 0
    title = Path(source_path).stem
    text_buf: list[str] = []
    buf_page: int | None = None
    now = datetime.now(timezone.utc).isoformat()

    def make_chunk(content: str, page_num: int | None) -> KBChunk:
        nonlocal idx
        c = KBChunk(
            id=_chunk_id(source_path, idx),
            source_hash=source_hash,
            source_path=source_path,
            source_type=source_type,
            wiki_page=None,
            title=title,
            content=content,
            compiled=None,
            chunk_index=idx,
            page_num=page_num,
            line_start=None,
            created_at=now,
        )
        idx += 1
        return c

    def flush_buf() -> None:
        nonlocal text_buf, buf_page
        if not text_buf:
            return
        combined = "\n".join(text_buf)
        text_buf = []
        page = buf_page
        buf_page = None
        for piece in _split_text(combined):
            chunks.append(make_chunk(piece, page))

    for elem in elements:
        if elem.type == "heading":
            flush_buf()
            title = elem.content
            continue

        if elem.type in ("table", "image_description", "image_ocr"):
            flush_buf()
            content = elem.content.strip()
            if content:
                chunks.append(make_chunk(content, elem.page_num))
            continue

        # paragraph, list_item, code
        content = elem.content.strip()
        if not content:
            continue
        if buf_page is None:
            buf_page = elem.page_num
        text_buf.append(content)
        if len("\n".join(text_buf)) >= TARGET_CHARS:
            flush_buf()

    flush_buf()
    return chunks
