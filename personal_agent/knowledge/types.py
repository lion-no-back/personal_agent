from dataclasses import dataclass, field


@dataclass
class KBChunk:
    id: str
    source_hash: str
    source_path: str
    source_type: str          # pdf|word|excel|markdown|image|web
    wiki_page: str | None
    title: str
    content: str
    compiled: str | None      # CoT 合成内容，未走 CoT 时为 None
    chunk_index: int
    page_num: int | None
    line_start: int | None
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    embedding: list[float] | None = None


@dataclass
class KBSource:
    path: str
    hash: str
    last_synced: str
    cot_used: bool
    chunk_ids: list[str] = field(default_factory=list)


@dataclass
class KBSearchResult:
    chunk: KBChunk
    score: float
    match_type: str           # keyword|vector|hybrid
