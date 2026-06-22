"""
Auto-downloads HuggingFace models to local cache on first use.
Fast no-op if model is already cached. Mirrors java_bootstrap.py pattern.

Download strategy:
  1. If HTTPS_PROXY / HTTP_PROXY already set in env → use as-is
  2. Else read Windows system proxy from registry and inject into env
  3. Call snapshot_download — proxy handles any mirror→HF redirect transparently
"""
import importlib
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_ensured: set[str] = set()
_hub_installed = False


def _ensure_huggingface_hub() -> None:
    """Install huggingface-hub if absent. Called once before any hub import."""
    global _hub_installed
    if _hub_installed or importlib.util.find_spec("huggingface_hub") is not None:
        _hub_installed = True
        return
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "huggingface-hub"] if uv
           else [sys.executable, "-m", "pip", "install", "huggingface-hub"])
    logger.info("[hf_bootstrap] huggingface-hub missing, installing...")
    try:
        from personal_agent.workflows.deplock import install_lock
        with install_lock():  # 跨进程串行装包，避免与工作流子进程并发改写共享 venv
            subprocess.check_call(cmd, env={**os.environ, "UV_LINK_MODE": "copy"})
        importlib.invalidate_caches()
        _hub_installed = True
        logger.info("[hf_bootstrap] huggingface-hub installed")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "Failed to install huggingface-hub. "
            "Run manually: uv pip install huggingface-hub"
        ) from e


def ensure_hf_model(repo_id: str) -> None:
    """Download model to HF cache if absent. Thread-safe; subsequent calls are instant."""
    if repo_id in _ensured:
        return
    with _lock:
        if repo_id in _ensured:
            return
        _ensure_huggingface_hub()
        _download_if_needed(repo_id)
        _ensured.add(repo_id)


def _is_cached(repo_id: str) -> bool:
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        cache_dir = Path(HF_HUB_CACHE)
    except ImportError:
        from huggingface_hub.constants import HUGGINGFACE_HUB_CACHE
        cache_dir = Path(HUGGINGFACE_HUB_CACHE)

    folder = "models--" + repo_id.replace("/", "--")
    snapshots = cache_dir / folder / "snapshots"
    return snapshots.exists() and any(snapshots.iterdir())


def _get_windows_system_proxy() -> str | None:
    """Read enabled system proxy from Windows registry. Returns 'http://host:port' or None."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not server:
            return None
        return server if server.startswith("http") else f"http://{server}"
    except Exception:
        return None


def _ensure_proxy_env() -> None:
    """Set HTTPS_PROXY/HTTP_PROXY from Windows system proxy if not already configured."""
    proxy_vars = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    if any(os.environ.get(v) for v in proxy_vars):
        return  # already configured, leave as-is

    proxy = _get_windows_system_proxy()
    if proxy:
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["HTTP_PROXY"] = proxy
        logger.debug("[hf_bootstrap] Using system proxy: %s", proxy)


def _download_if_needed(repo_id: str) -> None:
    if _is_cached(repo_id):
        logger.debug("[hf_bootstrap] %s already cached, skipping download", repo_id)
        return

    _ensure_proxy_env()

    endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
    print(f"[hf_bootstrap] Downloading {repo_id} from {endpoint} ...", file=sys.stderr)

    from huggingface_hub import snapshot_download
    try:
        local_dir = snapshot_download(repo_id=repo_id, endpoint=endpoint)
        print(f"[hf_bootstrap] Model ready: {local_dir}", file=sys.stderr)
        logger.info("[hf_bootstrap] %s downloaded to %s", repo_id, local_dir)
    except Exception as e:
        logger.error("[hf_bootstrap] Failed to download %s: %s", repo_id, e)
        raise
