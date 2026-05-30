import array
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from .types import KBChunk, KBSource


class KBStore:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0(
                chunk_id TEXT,
                embedding FLOAT[1536]
            );
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
                created_at  TEXT
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
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON kb_chunks(source_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_title  ON kb_chunks(title);
        """)
        self._conn.commit()

    @staticmethod
    def _placeholders(ids: list[str]) -> str:
        return ",".join("?" * len(ids))

    def _upsert_vec(self, cur: sqlite3.Cursor, chunk_id: str, embedding: list[float]) -> None:
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
                c.created_at or now,
            )
            for c in chunks
        ]
        cur = self._conn.cursor()
        cur.executemany(
            """INSERT OR REPLACE INTO kb_chunks
               (id, source_hash, source_path, source_type, wiki_page,
                title, content, compiled, chunk_index, page_num,
                line_start, tags, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        for chunk in chunks:
            if chunk.embedding is not None:
                self._upsert_vec(cur, chunk.id, chunk.embedding)
        self._conn.commit()

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
        self._conn.commit()

    def get_source(self, path: str | Path) -> KBSource | None:
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
        self._conn.execute(f"DELETE FROM kb_vec WHERE chunk_id IN ({ph})", ids)
        self._conn.commit()

    def delete_source(self, path: str | Path) -> None:
        p = str(path)
        rows = self._conn.execute(
            "SELECT id FROM kb_chunks WHERE source_path = ?", (p,)
        ).fetchall()
        if rows:
            ids = [r[0] for r in rows]
            ph = self._placeholders(ids)
            self._conn.execute(f"DELETE FROM kb_chunks WHERE id IN ({ph})", ids)
            self._conn.execute(f"DELETE FROM kb_vec WHERE chunk_id IN ({ph})", ids)
        self._conn.execute("DELETE FROM kb_sources WHERE path = ?", (p,))
        self._conn.commit()

    def get_chunks_by_ids(self, ids: list[str]) -> list[KBChunk]:
        if not ids:
            return []
        rows = self._conn.execute(
            f"""SELECT id, source_hash, source_path, source_type, wiki_page,
                       title, content, compiled, chunk_index, page_num,
                       line_start, tags, created_at
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
        self._conn.execute(
            """INSERT OR REPLACE INTO kb_embed_cache (content_hash, embedding, created_at)
               VALUES (?,?,?)""",
            (content_hash, array.array("f", embedding).tobytes(),
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def get_all_chunks(self) -> list[KBChunk]:
        rows = self._conn.execute(
            """SELECT id, source_hash, source_path, source_type, wiki_page,
                       title, content, compiled, chunk_index, page_num,
                       line_start, tags, created_at FROM kb_chunks"""
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def vector_search(self, embedding: list[float], n: int = 40) -> list[tuple[str, float]]:
        rows = self._conn.execute(
            "SELECT chunk_id, distance FROM kb_vec WHERE embedding MATCH ? LIMIT ?",
            (json.dumps(embedding), n),
        ).fetchall()
        return [(row[0], float(row[1])) for row in rows]

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
        print(f"Last sync: {row[3] or 'never'}")
