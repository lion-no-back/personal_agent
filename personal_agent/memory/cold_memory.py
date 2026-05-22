"""
冷层记忆：context.md（情境）和 events.md（事件）。

context.md 格式：
  # 情境记忆（Context Memory）
  <!-- Updated: 2026-05-20 14:32 -->

  ## 当前认知（Compiled Truth）
  {摘要内容}

  ---

  ## 历史记录（Timeline）
  - 2026-05-15 09:11: {条目}

events.md 格式（纯时间线，无 Compiled Truth）：
  # 事件记忆（Event Memory）
  <!-- Updated: 2026-05-20 14:32 -->

  ## 事件记录（Timeline Only）
  - 2026-05-10 14:32: {事件}
"""

import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SEP = "\n\n---\n\n"
_MAX_EVENTS = 200  # events.md 保留上限，超出时滚动删除最旧条目


class ColdMemory:
    """
    管理冷层记忆文件（context.md / events.md）。
    存储路径：{store_dir}/users/{user_id}/
    """

    def __init__(self, store_dir: Path) -> None:
        self._base = store_dir / "users"
        self._ctx_locks: dict[str, threading.Lock] = {}
        self._evt_locks: dict[str, threading.Lock] = {}
        self._global = threading.Lock()

    # ── 辅助 ───────────────────────────────────────────────────────

    def _user_dir(self, user_id: str) -> Path:
        d = self._base / user_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _lock(self, locks: dict[str, threading.Lock], user_id: str) -> threading.Lock:
        with self._global:
            if user_id not in locks:
                locks[user_id] = threading.Lock()
            return locks[user_id]

    # ── context.md ────────────────────────────────────────────────

    def _ctx_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "context.md"

    def _parse_ctx(self, text: str) -> tuple[str, list[str]]:
        if _SEP not in text:
            lines = [
                l for l in text.splitlines()
                if not l.startswith("#") and not l.startswith("<!--") and not l.startswith("##")
            ]
            return "\n".join(lines).strip(), []
        top, bottom = text.split(_SEP, 1)
        truth_lines = [
            l for l in top.splitlines()
            if not l.startswith("#") and not l.startswith("<!--") and not l.startswith("##")
        ]
        compiled = "\n".join(truth_lines).strip()
        entries = [l[2:] for l in bottom.splitlines() if l.strip().startswith("- ")]
        return compiled, entries

    def _write_ctx(self, user_id: str, compiled: str, timeline: list[str]) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = (
            f"# 情境记忆（Context Memory）\n"
            f"<!-- Updated: {ts} -->\n\n"
            f"## 当前认知（Compiled Truth）\n"
        )
        body = header + compiled
        if timeline:
            items = "\n".join(f"- {e}" for e in timeline)
            body += f"{_SEP}## 历史记录（Timeline）\n{items}"
        self._ctx_path(user_id).write_text(body, "utf-8")

    def read_context_both(self, user_id: str) -> tuple[str, list[str]]:
        """一次 IO 返回 (compiled_truth, timeline_entries)。读操作不创建目录。"""
        with self._lock(self._ctx_locks, user_id):
            p = self._base / user_id / "context.md"  # 不走 _user_dir()，避免 mkdir 副作用
            try:
                return self._parse_ctx(p.read_text("utf-8"))
            except FileNotFoundError:
                return "", []

    def read_context_compiled(self, user_id: str) -> str:
        compiled, _ = self.read_context_both(user_id)
        return compiled

    def context_timeline_count(self, user_id: str) -> int:
        _, entries = self.read_context_both(user_id)
        return len(entries)

    def get_context_timeline(self, user_id: str) -> list[str]:
        _, entries = self.read_context_both(user_id)
        return entries

    def append_context_timeline(self, user_id: str, entry: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._lock(self._ctx_locks, user_id):
            p = self._ctx_path(user_id)
            try:
                text = p.read_text("utf-8")
            except FileNotFoundError:
                text = ""
            compiled, timeline = self._parse_ctx(text)
            timeline.append(f"{ts}: {entry}")
            self._write_ctx(user_id, compiled, timeline)

    def update_context_compiled(self, user_id: str, new_compiled: str) -> None:
        with self._lock(self._ctx_locks, user_id):
            p = self._ctx_path(user_id)
            try:
                text = p.read_text("utf-8")
            except FileNotFoundError:
                text = ""
            _, timeline = self._parse_ctx(text)
            self._write_ctx(user_id, new_compiled, timeline)

    def replace_context_timeline(self, user_id: str, new_timeline: list[str]) -> None:
        with self._lock(self._ctx_locks, user_id):
            p = self._ctx_path(user_id)
            try:
                text = p.read_text("utf-8")
            except FileNotFoundError:
                text = ""
            compiled, _ = self._parse_ctx(text)
            self._write_ctx(user_id, compiled, new_timeline)

    # ── events.md ─────────────────────────────────────────────────

    def _evt_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "events.md"

    def _parse_evt(self, text: str) -> list[str]:
        return [l[2:] for l in text.splitlines() if l.strip().startswith("- ")]

    def _write_evt(self, user_id: str, entries: list[str]) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = (
            f"# 事件记忆（Event Memory）\n"
            f"<!-- Updated: {ts} -->\n\n"
            f"## 事件记录（Timeline Only）\n"
        )
        body = header + "\n".join(f"- {e}" for e in entries)
        self._evt_path(user_id).write_text(body, "utf-8")

    def append_event(self, user_id: str, event: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._lock(self._evt_locks, user_id):
            p = self._evt_path(user_id)
            try:
                entries = self._parse_evt(p.read_text("utf-8"))
            except FileNotFoundError:
                entries = []
            entries.append(f"{ts}: {event}")
            # 滚动删除最旧条目，防止无限增长
            if len(entries) > _MAX_EVENTS:
                entries = entries[-_MAX_EVENTS:]
            self._write_evt(user_id, entries)

    def get_recent_events(self, user_id: str, n: int = 5) -> list[str]:
        with self._lock(self._evt_locks, user_id):
            p = self._base / user_id / "events.md"  # 不走 _user_dir()，避免 mkdir 副作用
            if not p.exists():
                return []
            return self._parse_evt(p.read_text("utf-8"))[-n:]

    def list_user_ids(self) -> list[str]:
        if not self._base.exists():
            return []
        return [d.name for d in self._base.iterdir() if d.is_dir()]
