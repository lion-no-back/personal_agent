"""
定时任务持久化

使用 Python 内置 sqlite3 模块记录每个 cron 任务的执行历史。
重启时 CronEngine 读取 job_runs，判断是否需要补跑错过的触发。

表结构：
  job_runs     — 最后一次成功执行时间（主键 job_id），供 misfire_catchup 使用
  job_attempts — 每次执行尝试的完整记录（触发时间、状态、耗时），供可观测性使用

设计约束：
- 不依赖 SQLAlchemy，仅使用标准库
- 写操作加锁，防止并发写入时数据库文件损坏
- 所有时间戳以 UTC ISO-8601 字符串存储，避免时区歧义
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class JobPersistence:
    """
    记录 cron 任务执行历史的轻量持久化层。

    job_runs     - 最后成功执行时间，供 misfire_catchup 使用（与原有逻辑兼容）
    job_attempts - 每次执行尝试记录，status 可为 running / success / timeout / error
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_runs (
                    job_id   TEXT PRIMARY KEY,
                    last_run TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_attempts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id       TEXT    NOT NULL,
                    triggered_at TEXT    NOT NULL,
                    attempt_num  INTEGER NOT NULL DEFAULT 1,
                    status       TEXT    NOT NULL DEFAULT 'running',
                    elapsed_s    REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_attempts_job_id ON job_attempts(job_id)"
            )
            # 旧表迁移：attempt_num 列不存在时自动补充
            cols = {row[1] for row in conn.execute("PRAGMA table_info(job_attempts)").fetchall()}
            if "attempt_num" not in cols:
                conn.execute("ALTER TABLE job_attempts ADD COLUMN attempt_num INTEGER NOT NULL DEFAULT 1")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    def get_last_run(self, job_id: str) -> datetime | None:
        """
        返回 job_id 上次成功执行的 UTC 时间；若无记录则返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_run FROM job_runs WHERE job_id = ?", (job_id,)
            ).fetchone()

        if row is None:
            return None

        try:
            dt = datetime.fromisoformat(row[0])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None

    def record_run(self, job_id: str, run_time: datetime | None = None) -> None:
        """
        记录 job_id 的最新执行时间（默认为调用时刻的 UTC 时间）。
        使用 INSERT OR REPLACE 保证幂等。
        """
        if run_time is None:
            run_time = datetime.now(timezone.utc)

        ts = run_time.astimezone(timezone.utc).isoformat()

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO job_runs (job_id, last_run) VALUES (?, ?)",
                    (job_id, ts),
                )

    # ── 执行尝试追踪（job_attempts） ────────────────────────────

    def record_attempt_start(self, job_id: str, attempt_num: int = 1) -> int:
        """
        在任务开始时写入一条 status='running' 的记录，返回 attempt id。
        调用方拿到 id 后在任务结束时调用 record_attempt_end 更新状态。
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO job_attempts (job_id, triggered_at, attempt_num, status) VALUES (?, ?, ?, 'running')",
                    (job_id, ts, attempt_num),
                )
                return cur.lastrowid  # type: ignore[return-value]

    def record_attempt_end(
        self,
        attempt_id: int,
        status: str,
        elapsed_s: float,
    ) -> None:
        """
        任务结束时更新 status 和 elapsed_s。
        status 应为 'success' / 'timeout' / 'error'。
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE job_attempts SET status = ?, elapsed_s = ? WHERE id = ?",
                    (status, elapsed_s, attempt_id),
                )

    def get_recent_attempts(self, job_id: str, limit: int = 10) -> list[dict]:
        """返回最近 N 次执行尝试，按触发时间倒序。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, triggered_at, attempt_num, status, elapsed_s
                FROM job_attempts
                WHERE job_id = ?
                ORDER BY triggered_at DESC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        return [
            {"id": r[0], "triggered_at": r[1], "attempt_num": r[2], "status": r[3], "elapsed_s": r[4]}
            for r in rows
        ]
