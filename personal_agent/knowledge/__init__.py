import asyncio
import importlib
import importlib.util
import logging
import subprocess
import threading
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.core.llm_router import LLMRouter
    from personal_agent.knowledge.types import KBSearchResult

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 30  # seconds — budget for one retriever.search() call

_DEP_GROUP = "knowledge"
_CHECK_PACKAGES = ["opendataloader_pdf", "sqlite_vec"]
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level singleton set by main.py after initialization.
kb: "KnowledgeBase | None" = None


def _ensure_deps() -> None:
    """检查知识库依赖是否齐全且版本正确，否则自动安装/重装。

    故意不用 uv sync：sync 会删除 lockfile 外的包（含 paddle/opencv 等动态装入的包）。

    用 importlib.metadata.version 而非 find_spec 来检查包是否就绪：
    find_spec 只要 .pyd 存在就返回非 None，即使 dist-info 已被 _heal_venv 清除（包已损坏）。
    metadata.version 读取 dist-info，dist-info 缺失即视为未安装，触发重装。

    此函数必须在任何 sqlite_vec import 之前调用（即在 KnowledgeBase.__init__ 最开头），
    否则 Windows 会锁住 .pyd，子进程无法替换它。
    """
    import importlib.metadata

    def _is_healthy(pkg: str) -> bool:
        """dist-info 存在且可读即视为健康；.pyd 存在但 dist-info 缺失视为损坏。"""
        try:
            importlib.metadata.version(pkg.replace("_", "-"))
            return True
        except importlib.metadata.PackageNotFoundError:
            return False

    missing = [p for p in _CHECK_PACKAGES if not _is_healthy(p)]

    with open(_PROJECT_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    packages = data.get("dependency-groups", {}).get(_DEP_GROUP, [])
    if not packages:
        packages = missing

    reinstall_flags: list[str] = []
    wrong_version: list[str] = []
    for spec in packages:
        if "==" not in spec:
            continue
        name, expected = spec.split("==", 1)
        name, expected = name.strip(), expected.strip()
        try:
            installed = importlib.metadata.version(name)
            if installed != expected:
                wrong_version.append(f"{name} {installed}→{expected}")
                reinstall_flags += ["--reinstall-package", name]
        except importlib.metadata.PackageNotFoundError:
            pass  # 已在 missing 中处理

    if not missing and not wrong_version:
        return

    if missing:
        logger.info("[knowledge] 缺少依赖 %s，正在安装...", missing)
    if wrong_version:
        logger.warning("[knowledge] 依赖版本不匹配 %s，正在修正...", wrong_version)

    try:
        from personal_agent.workflows.deplock import uv_pip_install
        uv_pip_install(reinstall_flags + packages)  # 跨进程串行装包 + 刷新导入缓存
        logger.info("[knowledge] 依赖安装完成")
        if wrong_version:
            raise RuntimeError(
                f"原生扩展版本已修正 {wrong_version}，请重启进程后知识库将自动恢复正常。"
            )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"依赖组 {_DEP_GROUP!r} 安装失败，请手动执行：uv pip install {' '.join(packages)}"
        ) from exc


class KnowledgeBase:
    def __init__(
        self,
        db_path: Path,
        wiki_dir: Path,
        sources_dir: Path,
        llm: "LLMRouter",
    ) -> None:
        # 必须在所有 sqlite_vec import 之前运行，否则 Windows 锁住 DLL 导致重装无效
        _ensure_deps()

        # 延迟 import：store.py 顶层会 import sqlite_vec，必须在 _ensure_deps() 之后
        from .store import KBStore
        from .ingester.pipeline import Pipeline
        from .retriever.search import Retriever
        from .watcher import KBWatcher

        self.sources_dir = Path(sources_dir)
        self._wiki_dir = Path(wiki_dir)

        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self._wiki_dir.mkdir(parents=True, exist_ok=True)

        self._store = KBStore(db_path)
        self._pipeline = Pipeline(store=self._store, llm=llm, wiki_dir=self._wiki_dir)
        self.retriever = Retriever(store=self._store, llm=llm)
        self._watcher = KBWatcher(pipeline=self._pipeline, store=self._store)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="kb-loop",
        )
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start_watcher(self) -> None:
        self._watcher.start(self._loop, self.sources_dir)

    def stop_watcher(self) -> None:
        """停止文件监听（停 watchdog observer 并 join）。幂等、不抛出。"""
        try:
            self._watcher.stop()
        except Exception:
            pass

    def search_sync(self, query: str, top_k: int = 5) -> "list[KBSearchResult]":
        future = asyncio.run_coroutine_threadsafe(
            self.retriever.search(query, top_k=top_k),
            self._loop,
        )
        return future.result(timeout=_SEARCH_TIMEOUT)

    def quick_search_sync(self, query: str, top_k: int = 3) -> "list[KBSearchResult]":
        """No-LLM pre-retrieval: keyword + vector only. Returns empty list on any error."""
        future = asyncio.run_coroutine_threadsafe(
            self.retriever.search_lightweight(query, top_k=top_k),
            self._loop,
        )
        return future.result(timeout=10)
