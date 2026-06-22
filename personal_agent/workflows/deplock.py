"""跨进程装包锁。

保证同一时刻全系统只有一个 `uv pip install` 在改写共享 venv，避免多个工作流
子进程（或主进程的知识库/paddle 引导）并发装包导致 site-packages 半装损坏。

用法：
    from personal_agent.workflows.deplock import install_lock
    with install_lock():
        subprocess.run([uv, "pip", "install", ...], check=True)

锁是文件级独占锁（Windows: msvcrt / POSIX: fcntl），跨进程有效。获取不到时自旋
等待，直到拿到或超时（默认 30 分钟，足够最慢的 torch 安装排队）。
"""

import importlib
import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK_FILE = Path(__file__).resolve().parents[2] / ".install.lock"

if os.name == "nt":
    import msvcrt

    def _try_lock(f) -> bool:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f) -> bool:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def install_lock(timeout: float = 1800.0):
    """获取跨进程装包锁；拿不到则自旋等待至超时（抛 TimeoutError）。"""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(_LOCK_FILE, "a+")
    deadline = time.monotonic() + timeout
    waited = False
    try:
        while not _try_lock(f):
            if time.monotonic() > deadline:
                raise TimeoutError("获取装包锁超时（另一个安装可能卡住）")
            if not waited:
                logger.info("[deplock] 已有安装在进行，排队等待装包锁...")
                waited = True
            time.sleep(0.5)
        yield
    finally:
        try:
            _unlock(f)
        except Exception:
            pass
        f.close()


def uv_pip_install(packages: list[str]) -> None:
    """带跨进程锁的 `uv pip install`：持锁安装后刷新导入缓存。

    运行时按需装包的统一入口——所有简单装包点都应走这里，未来新增装包点自动获得
    跨进程串行保护，不必各自再写 `with install_lock()`。
    （opencv 卸载切换、torch --index-url 等异形安装仍按需自行持 install_lock。）
    """
    uv = shutil.which("uv") or "uv"
    with install_lock():  # 跨进程串行装包，避免并发改写共享 venv
        subprocess.run([uv, "pip", "install"] + packages, check=True)
    importlib.invalidate_caches()
