import array
from contextlib import contextmanager
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from .paths import canonical_source_path
from .types import KBChunk, KBSource

logger = logging.getLogger(__name__)

# FTS5 trigram requires each query term to be >= 3 characters.
# Shorter queries fall back to LIKE on title + source_path (both short fields).
_FTS_MIN_CHARS = 3

# Single source of truth for the FTS table schema — used in both _create_tables
# and _migrate_schema so they can never drift apart.
_FTS_SCHEMA = "chunk_id UNINDEXED, title, content, path_stem, tokenize = 'trigram'"


class KBStore:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._write_lock = threading.RLock()
        self._tx_depth = 0
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._create_tables()
        self._migrate_schema()

    def _create_tables(self) -> None:
        self._conn.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5({_FTS_SCHEMA});
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id          TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_type TEXT NOT NULL,
                wiki_page   TEXT,
                title       TEXT,
                content     TEXT NOT NULL,
                compiled    TEXT,
                chunk_index INTEGER,
                page_num    INTEGER,
                line_start  INTEGER,
                tags        TEXT DEFAULT '[]',
                created_at  TEXT,
                parent_id   TEXT
            );
            CREATE TABLE IF NOT EXISTS kb_sources (
                path        TEXT PRIMARY KEY,
                hash        TEXT NOT NULL,
                last_synced TEXT,
                cot_used    INTEGER DEFAULT 0,
                chunk_ids   TEXT DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS kb_embed_cache (
                content_hash TEXT PRIMARY KEY,
                embedding    BLOB NOT NULL,
                created_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS kb_meta (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON kb_chunks(source_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_title  ON kb_chunks(title);
            CREATE INDEX IF NOT EXISTS idx_chunks_parent ON kb_chunks(parent_id);
        """)
        self._conn.commit()

    def _migrate_schema(self) -> None:
        # 1. kb_chunks: add parent_id column if missing (legacy DBs)
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(kb_chunks)")}
        if "parent_id" not in existing:
            self._conn.execute("ALTER TABLE kb_chunks ADD COLUMN parent_id TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_parent ON kb_chunks(parent_id)"
            )
            self._conn.commit()

        # 2. kb_fts: rebuild if tokenizer is not trigram (old unicode61 schema)
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'kb_fts'"
        ).fetchone()
        if row is None or "trigram" not in (row[0] or ""):
            self._conn.executescript(
                f"DROP TABLE IF EXISTS kb_fts;"
                f"CREATE VIRTUAL TABLE kb_fts USING fts5({_FTS_SCHEMA});"
            )
            self._conn.commit()

        # 3. Backfill: populate FTS from kb_chunks if FTS is empty (covers both
        #    the first-ever startup and the just-rebuilt case above).
        self._backfill_fts()

    def _backfill_fts(self) -> None:
        """Populate kb_fts from kb_chunks when the index is empty but data exists.
        Fetches only the four columns needed for FTS — avoids loading embeddings
        and other unused fields that get_all_chunks() would pull in."""
        fts_empty = self._conn.execute("SELECT 1 FROM kb_fts LIMIT 1").fetchone() is None
        has_chunks = self._conn.execute("SELECT 1 FROM kb_chunks LIMIT 1").fetchone() is not None
        if not fts_empty or not has_chunks:
            return
        rows = self._conn.execute(
            "SELECT id, title, content, source_path FROM kb_chunks"
        ).fetchall()
        self._conn.executemany(
            "INSERT INTO kb_fts (chunk_id, title, content, path_stem) VALUES (?,?,?,?)",
            [(r[0], r[1] or "", r[2] or "", Path(r[3]).stem) for r in rows],
        )
        self._conn.commit()

    @staticmethod
    def _placeholders(ids: list[str]) -> str:
        return ",".join("?" * len(ids))

    def _commit_if_needed(self) -> None:
        if self._tx_depth == 0:
            self._conn.commit()

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        """Serialize writes and make a single ingest file commit atomically.

        BEGIN IMMEDIATE takes SQLite's write lock up front, so concurrent writers
        wait instead of interleaving chunk deletion, vector writes, and lineage
        updates. Nested calls join the outer transaction.
        """
        with self._write_lock:
            if self._tx_depth > 0:
                self._tx_depth += 1
                try:
                    yield
                finally:
                    self._tx_depth -= 1
                return

            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._tx_depth = 0

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def get_vector_dim(self) -> int | None:
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'kb_vec'"
        ).fetchone()
        if row is None or not row[0]:
            return None
        match = re.search(r"embedding\s+FLOAT\[(\d+)\]", row[0], re.IGNORECASE)
        return int(match.group(1)) if match else None

    def vector_count(self) -> int:
        if not self._table_exists("kb_vec"):
            return 0
        return int(self._conn.execute("SELECT COUNT(1) FROM kb_vec").fetchone()[0])

    def _create_vector_table(self, dimension: int) -> None:
        self._conn.execute(
            f"""CREATE VIRTUAL TABLE kb_vec USING vec0(
                chunk_id TEXT,
                embedding FLOAT[{dimension}]
            )"""
        )
        self._commit_if_needed()

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM kb_meta WHERE key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kb_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._commit_if_needed()

    def ensure_embedding_config(
        self,
        dimension: int,
        model: str,
        base_url: str | None,
    ) -> None:
        """Ensure kb_vec matches the active embedding provider/model/dimension.

        sqlite-vec tables are dimension-fixed. If the active embedding config
        changes while vectors exist, the old vectors must be rebuilt rather
        than mixed with the new coordinate space.
        """
        with self._write_lock:
            if dimension <= 0:
                raise ValueError(f"invalid embedding dimension: {dimension}")

            active = {
                "model": model,
                "base_url": base_url or "",
                "dimension": dimension,
            }
            existing_json = self._get_meta("embedding_config")
            if existing_json:
                try:
                    existing = json.loads(existing_json)
                except json.JSONDecodeError:
                    existing = {}
                if existing != active and self.vector_count() > 0:
                    raise RuntimeError(
                        "Embedding config changed; rebuild the knowledge base vectors "
                        f"before searching or ingesting. existing={existing}, active={active}"
                    )

            current_dim = self.get_vector_dim()
            if current_dim != dimension:
                if current_dim is not None and self.vector_count() > 0:
                    raise RuntimeError(
                        "Embedding dimension changed; rebuild the knowledge base vectors "
                        f"before searching or ingesting. existing={current_dim}, active={dimension}"
                    )
                self._conn.execute("DROP TABLE IF EXISTS kb_vec")
                self._commit_if_needed()
                self._create_vector_table(dimension)

            self._set_meta("embedding_config", json.dumps(active, ensure_ascii=False))

    def _upsert_vec(self, cur: sqlite3.Cursor, chunk_id: str, embedding: list[float]) -> None:
        dim = self.get_vector_dim()
        if dim is None:
            raise RuntimeError("kb_vec has not been created for the active embedding dimension")
        if len(embedding) != dim:
            raise RuntimeError(
                f"Embedding dimension mismatch for chunk {chunk_id}: got {len(embedding)}, expected {dim}"
            )
        cur.execute("DELETE FROM kb_vec WHERE chunk_id = ?", (chunk_id,))
        cur.execute(
            "INSERT INTO kb_vec (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, json.dumps(embedding)),
        )

    def upsert_chunks(self, chunks: list[KBChunk]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                c.id, c.source_hash, c.source_path, c.source_type, c.wiki_page,
                c.title, c.content, c.compiled, c.chunk_index, c.page_num,
                c.line_start, json.dumps(c.tags, ensure_ascii=False),
                c.created_at or now, c.parent_id,
            )
            for c in chunks
        ]
        cur = self._conn.cursor()
        cur.executemany(
            """INSERT OR REPLACE INTO kb_chunks
               (id, source_hash, source_path, source_type, wiki_page,
                title, content, compiled, chunk_index, page_num,
                line_start, tags, created_at, parent_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        # FTS: batch delete then insert (2 statements regardless of chunk count).
        # FTS covers every chunk independent of embedding readiness.
        cur.executemany("DELETE FROM kb_fts WHERE chunk_id = ?", [(c.id,) for c in chunks])
        cur.executemany(
            "INSERT INTO kb_fts (chunk_id, title, content, path_stem) VALUES (?,?,?,?)",
            [(c.id, c.title or "", c.content or "", Path(c.source_path).stem) for c in chunks],
        )
        for chunk in chunks:
            if chunk.embedding is not None:
                self._upsert_vec(cur, chunk.id, chunk.embedding)
        self._commit_if_needed()

    def upsert_source(self, source: KBSource) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO kb_sources
               (path, hash, last_synced, cot_used, chunk_ids)
               VALUES (?,?,?,?,?)""",
            (
                source.path, source.hash, source.last_synced,
                int(source.cot_used),
                json.dumps(source.chunk_ids),
            ),
        )
        self._commit_if_needed()

    def get_source(self, path: str | Path) -> KBSource | None:
        path = canonical_source_path(path)
        row = self._conn.execute(
            "SELECT path, hash, last_synced, cot_used, chunk_ids FROM kb_sources WHERE path = ?",
            (str(path),),
        ).fetchone()
        if row is None:
            return None
        return KBSource(
            path=row[0], hash=row[1], last_synced=row[2],
            cot_used=bool(row[3]), chunk_ids=json.loads(row[4]),
        )

    def delete_chunks(self, ids: list[str]) -> None:
        if not ids:
            return
        ph = self._placeholders(ids)
        self._conn.execute(f"DELETE FROM kb_chunks WHERE id IN ({ph})", ids)
        if self._table_exists("kb_vec"):
            self._conn.execute(f"DELETE FROM kb_vec WHERE chunk_id IN ({ph})", ids)
        self._conn.execute(f"DELETE FROM kb_fts WHERE chunk_id IN ({ph})", ids)
        self._commit_if_needed()

    def delete_source(self, path: str | Path) -> None:
        p = str(canonical_source_path(path))
        if self._table_exists("kb_vec"):
            self._conn.execute(
                "DELETE FROM kb_vec WHERE chunk_id IN (SELECT id FROM kb_chunks WHERE source_path = ?)", (p,)
            )
        self._conn.execute(
            "DELETE FROM kb_fts WHERE chunk_id IN (SELECT id FROM kb_chunks WHERE source_path = ?)", (p,)
        )
        self._conn.execute("DELETE FROM kb_chunks WHERE source_path = ?", (p,))
        self._conn.execute("DELETE FROM kb_sources WHERE path = ?", (p,))
        self._commit_if_needed()

    def get_chunks_by_ids(self, ids: list[str]) -> list[KBChunk]:
        if not ids:
            return []
        rows = self._conn.execute(
            f"""SELECT id, source_hash, source_path, source_type, wiki_page,
                       title, content, compiled, chunk_index, page_num,
                       line_start, tags, created_at, parent_id
                FROM kb_chunks WHERE id IN ({self._placeholders(ids)})""",
            ids,
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def _row_to_chunk(self, row) -> KBChunk:
        return KBChunk(
            id=row[0], source_hash=row[1], source_path=row[2],
            source_type=row[3], wiki_page=row[4], title=row[5],
            content=row[6], compiled=row[7], chunk_index=row[8],
            page_num=row[9], line_start=row[10],
            tags=json.loads(row[11] or "[]"),
            created_at=row[12] or "",
            parent_id=row[13] if len(row) > 13 else None,
        )

    def get_embed_cache(self, content_hash: str) -> list[float] | None:
        row = self._conn.execute(
            "SELECT embedding FROM kb_embed_cache WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        return array.array("f", row[0]).tolist()

    def set_embed_cache(self, content_hash: str, embedding: list[float]) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO kb_embed_cache (content_hash, embedding, created_at)
                   VALUES (?,?,?)""",
                (content_hash, array.array("f", embedding).tobytes(),
                 datetime.now(timezone.utc).isoformat()),
            )
            self._commit_if_needed()

    def get_all_chunks(self) -> list[KBChunk]:
        rows = self._conn.execute(
            """SELECT id, source_hash, source_path, source_type, wiki_page,
                       title, content, compiled, chunk_index, page_num,
                       line_start, tags, created_at, parent_id FROM kb_chunks"""
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def vector_search(self, embedding: list[float], n: int = 40) -> list[tuple[str, float]]:
        dim = self.get_vector_dim()
        if dim is None:
            return []
        if len(embedding) != dim:
            raise RuntimeError(
                f"Query embedding dimension mismatch: got {len(embedding)}, expected {dim}"
            )
        rows = self._conn.execute(
            "SELECT chunk_id, distance FROM kb_vec WHERE embedding MATCH ? LIMIT ?",
            (json.dumps(embedding), n),
        ).fetchall()
        return [(row[0], float(row[1])) for row in rows]

    def fts_search_all(self, queries: list[str], n: int = 40) -> list[str]:
        """Keyword candidate generation — sparse twin of vector_search.

        Long queries (>= 3 chars): FTS5 trigram MATCH, BM25-ranked, no full-table scan.
        Short queries / FTS parse errors: LIKE fallback on title + source_path.

        Returns a deduped list of chunk_ids preserving BM25 order within each
        sub-query and first-seen order across sub-queries.
        """
        seen: dict[str, None] = {}
        for q in queries:
            q = q.strip()
            if not q:
                continue
            if len(q) >= _FTS_MIN_CHARS:
                try:
                    rows = self._conn.execute(
                        "SELECT chunk_id FROM kb_fts WHERE kb_fts MATCH ?"
                        " ORDER BY bm25(kb_fts) LIMIT ?",
                        (q, n),
                    ).fetchall()
                except sqlite3.OperationalError:
                    logger.debug("FTS MATCH failed for %r, falling back to LIKE", q)
                else:
                    for (cid,) in rows:
                        seen.setdefault(cid, None)
                    continue
            # Short-query or FTS-error fallback: LIKE on title + source_path.
            # Both are short strings; source_path coverage is needed so that
            # filename-keyed queries reach the bespoke scorer's FILENAME_EXACT_BONUS.
            for (cid,) in self._conn.execute(
                "SELECT id FROM kb_chunks WHERE title LIKE ? OR source_path LIKE ? LIMIT ?",
                (f"%{q}%", f"%{q}%", n),
            ).fetchall():
                seen.setdefault(cid, None)
        return list(seen.keys())

    def print_stats(self) -> None:
        row = self._conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM kb_sources),
                (SELECT COUNT(*) FROM kb_chunks),
                (SELECT COUNT(*) FROM kb_embed_cache),
                (SELECT MAX(last_synced) FROM kb_sources)
        """).fetchone()
        print(f"Sources  : {row[0]}")
        print(f"Chunks   : {row[1]}")
        print(f"EmbCache : {row[2]} entries")
        print(f"VecDim   : {self.get_vector_dim() or 'not initialized'}")
        print(f"VecRows  : {self.vector_count()}")
        print(f"Last sync: {row[3] or 'never'}")
