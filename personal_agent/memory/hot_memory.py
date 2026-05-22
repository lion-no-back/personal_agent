"""
热层记忆：MEMORY.md（经验）和 USER.md（偏好）。

文件格式：
  # MEMORY.md
  <!-- MAX 2200 chars | 经验记忆 | Updated: 2026-05-20 14:32 -->

  ## 当前认知（Compiled Truth）
  {摘要内容}

  ---

  ## 历史记录（Timeline）
  - 2026-05-10 14:32: {条目}
"""

import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 两条记忆类型的配置：(文件名, 字符上限, 中文描述)
_FILE_CONFIG: dict[str, tuple[str, int, str]] = {
    "experience": ("MEMORY.md", 2200, "经验记忆"),
    "preference": ("USER.md",   1375, "用户偏好"),
}

# Compiled Truth 与 Timeline 之间的分隔符
_SEP = "\n\n---\n\n"


class HotMemory:
    """
    管理热层记忆文件（MEMORY.md / USER.md）。
    每个用户独立目录：{store_dir}/users/{user_id}/
    """

    def __init__(self, store_dir: Path) -> None:
        self._base = store_dir / "users"
        self._locks: dict[str, threading.Lock] = {}
        self._global = threading.Lock()

    # ── 内部路径 / 锁 ──────────────────────────────────────────────

    def _user_dir(self, user_id: str) -> Path:
        d = self._base / user_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, user_id: str, memory_type: str) -> Path:
        filename, _, _ = _FILE_CONFIG[memory_type]
        return self._user_dir(user_id) / filename

    def _lock(self, user_id: str, memory_type: str) -> threading.Lock:
        key = f"{user_id}:{memory_type}"
        with self._global:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    # ── 解析 ───────────────────────────────────────────────────────

    def _parse(self, text: str) -> tuple[str, list[str]]:
        """返回 (compiled_truth, [timeline_entries])"""
        if _SEP not in text:
            # 文件刚创建或还没有 timeline
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

    # ── 写入 ───────────────────────────────────────────────────────

    def _write(self, user_id: str, memory_type: str, compiled: str, timeline: list[str]) -> None:
        filename, max_chars, label = _FILE_CONFIG[memory_type]

        # 硬字符上限：按换行符截断，不截断在行中间
        if len(compiled) > max_chars:
            compiled = compiled[:max_chars].rsplit("\n", 1)[0].strip()
            logger.warning(
                "HotMemory: compiled truth 超限截断 | user=%s type=%s max=%d",
                user_id, memory_type, max_chars,
            )

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = (
            f"# {filename}\n"
            f"<!-- MAX {max_chars} chars | {label} | Updated: {ts} -->\n\n"
            f"## 当前认知（Compiled Truth）\n"
        )
        body = header + compiled

        if timeline:
            items = "\n".join(f"- {e}" for e in timeline)
            body += f"{_SEP}## 历史记录（Timeline）\n{items}"

        self._path(user_id, memory_type).write_text(body, "utf-8")

    # ── 公开接口 ───────────────────────────────────────────────────

    @staticmethod
    def max_chars(memory_type: str) -> int:
        return _FILE_CONFIG[memory_type][1]

    def read_both(self, user_id: str, memory_type: str) -> tuple[str, list[str]]:
        """一次 IO 返回 (compiled_truth, timeline_entries)。读操作不创建目录。"""
        with self._lock(user_id, memory_type):
            filename, _, _ = _FILE_CONFIG[memory_type]
            p = self._base / user_id / filename  # 不走 _user_dir()，避免 mkdir 副作用
            try:
                return self._parse(p.read_text("utf-8"))
            except FileNotFoundError:
                return "", []

    def read_compiled_truth(self, user_id: str, memory_type: str) -> str:
        compiled, _ = self.read_both(user_id, memory_type)
        return compiled

    def timeline_count(self, user_id: str, memory_type: str) -> int:
        _, entries = self.read_both(user_id, memory_type)
        return len(entries)

    def get_timeline(self, user_id: str, memory_type: str) -> list[str]:
        _, entries = self.read_both(user_id, memory_type)
        return entries

    def append_timeline(self, user_id: str, memory_type: str, entry: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._lock(user_id, memory_type):
            p = self._path(user_id, memory_type)
            try:
                text = p.read_text("utf-8")
            except FileNotFoundError:
                text = ""
            compiled, timeline = self._parse(text)
            timeline.append(f"{ts}: {entry}")
            self._write(user_id, memory_type, compiled, timeline)

    def update_compiled_truth(self, user_id: str, memory_type: str, new_truth: str) -> None:
        with self._lock(user_id, memory_type):
            p = self._path(user_id, memory_type)
            try:
                text = p.read_text("utf-8")
            except FileNotFoundError:
                text = ""
            _, timeline = self._parse(text)
            self._write(user_id, memory_type, new_truth, timeline)

    def replace_timeline(self, user_id: str, memory_type: str, new_timeline: list[str]) -> None:
        with self._lock(user_id, memory_type):
            p = self._path(user_id, memory_type)
            try:
                text = p.read_text("utf-8")
            except FileNotFoundError:
                text = ""
            compiled, _ = self._parse(text)
            self._write(user_id, memory_type, compiled, new_timeline)

    def list_user_ids(self) -> list[str]:
        if not self._base.exists():
            return []
        return [d.name for d in self._base.iterdir() if d.is_dir()]
