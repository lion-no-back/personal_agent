"""
定时任务持久化

使用 Python 内置 sqlite3 模块记录每个 cron 任务的最后一次成功执行时间。
重启时 CronEngine 读取该记录，判断是否需要补跑错过的触发。

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
    记录 cron 任务最后成功执行时间的轻量持久化层。

    使用 SQLite 单文件数据库，表结构极简：
        job_runs(job_id TEXT PRIMARY KEY, last_run TEXT)
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
