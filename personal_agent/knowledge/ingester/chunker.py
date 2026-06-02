import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ..types import KBChunk
from .parsers.types import ParsedElement

TARGET_CHARS = 512 * 4    # ~512 tokens
OVERLAP_CHARS = 64 * 4    # ~64 tokens
MIN_LEAF_CHARS = 60        # merge short adjacent OCR lines into one leaf


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

    # Current visual-group state (for table / image_* elements)
    cur_gid: str | None = None
    cur_group_elems: list[ParsedElement] = []
    cur_group_page: int | None = None

    def make_chunk(content: str, page_num: int | None, parent_id: str | None = None) -> KBChunk:
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
            parent_id=parent_id,
            created_at=now,
        )
        idx += 1
        return c

    def flush_group() -> None:
        nonlocal cur_gid, cur_group_elems, cur_group_page
        if not cur_group_elems:
            cur_gid = None
            return

        elems = cur_group_elems
        page = cur_group_page
        cur_gid = None
        cur_group_elems = []
        cur_group_page = None

        combined = "\n".join(e.content.strip() for e in elems if e.content.strip())
        if not combined:
            return

        if len(elems) == 1:
            # Single element: standalone, no two-layer split needed
            chunks.append(make_chunk(combined, page))
            return

        # Multiple elements from the same visual unit → parent + leaves
        parent = make_chunk(combined, page, parent_id=None)
        chunks.append(parent)

        leaf_buf: list[str] = []
        leaf_page: int | None = page
        for e in elems:
            c = e.content.strip()
            if not c:
                continue
            leaf_buf.append(c)
            if sum(len(s) for s in leaf_buf) >= MIN_LEAF_CHARS:
                leaf = make_chunk("\n".join(leaf_buf), e.page_num, parent_id=parent.id)
                chunks.append(leaf)
                leaf_buf = []
                leaf_page = None
        if leaf_buf:
            leaf = make_chunk("\n".join(leaf_buf), leaf_page, parent_id=parent.id)
            chunks.append(leaf)

    def flush_text_buf() -> None:
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
            flush_group()
            flush_text_buf()
            title = elem.content
            continue

        if elem.type in ("table", "image_description", "image_ocr"):
            flush_text_buf()
            # group_id from parser; fall back to a per-element sentinel so each
            # ungrouped element becomes its own standalone chunk.
            gid = elem.group_id or f"_solo_{id(elem)}"
            if cur_gid is None:
                cur_gid = gid
                cur_group_page = elem.page_num
            elif cur_gid != gid:
                # Different visual group: flush the previous one first
                flush_group()
                cur_gid = gid
                cur_group_page = elem.page_num
            cur_group_elems.append(elem)
            continue

        # paragraph, list_item, code — flush any open visual group first
        flush_group()
        content = elem.content.strip()
        if not content:
            continue
        if buf_page is None:
            buf_page = elem.page_num
        text_buf.append(content)
        if len("\n".join(text_buf)) >= TARGET_CHARS:
            flush_text_buf()

    flush_group()
    flush_text_buf()
    return chunks
