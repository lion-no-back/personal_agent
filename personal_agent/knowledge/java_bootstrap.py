"""
Auto-downloads a JRE from Adoptium on first use and adds it to PATH.
Fast no-op if java is already available in PATH.
"""
import os
import platform
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import requests

_JRE_CACHE = Path.home() / ".cache" / "personalagent" / "jre"

_OS_MAP = {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}
_ARCH_MAP = {
    "AMD64": "x64", "x86_64": "x64",
    "aarch64": "aarch64", "arm64": "aarch64",
}

_ADOPTIUM_URL = (
    "https://api.adoptium.net/v3/binary/latest/21/ga"
    "/{os}/{arch}/jre/hotspot/normal/eclipse"
)


def ensure_java() -> None:
    """No-op if java is in PATH; otherwise fetch JRE and prepend to PATH."""
    if shutil.which("java"):
        return

    bin_dir = _find_cached_jre()
    if bin_dir is None:
        bin_dir = _download_jre()

    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def _find_cached_jre() -> Path | None:
    if not _JRE_CACHE.exists():
        return None
    exe_name = "java.exe" if platform.system() == "Windows" else "java"
    for exe in _JRE_CACHE.rglob(exe_name):
        if exe.parent.name == "bin":
            return exe.parent
    return None


def _download_jre() -> Path:
    os_name = _OS_MAP.get(platform.system())
    arch = _ARCH_MAP.get(platform.machine())
    if not os_name or not arch:
        raise RuntimeError(
            f"Unsupported platform {platform.system()}/{platform.machine()}. "
            "Please install Java 11+ manually and add it to PATH."
        )

    url = _ADOPTIUM_URL.format(os=os_name, arch=arch)
    _JRE_CACHE.mkdir(parents=True, exist_ok=True)

    is_windows = platform.system() == "Windows"
    archive_ext = ".zip" if is_windows else ".tar.gz"
    archive = _JRE_CACHE / f"jre{archive_ext}"

    print(f"[knowledge] Java not found. Fetching JRE 21 ({os_name}/{arch})...", file=sys.stderr)
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(archive, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = min(downloaded * 100 // total, 100)
                    print(f"\r[knowledge] Downloading JRE {pct}%  ", end="", flush=True, file=sys.stderr)
    print(file=sys.stderr)

    print("[knowledge] Extracting...", file=sys.stderr)
    if is_windows:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(_JRE_CACHE)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(_JRE_CACHE, filter="data")
    archive.unlink()

    bin_dir = _find_cached_jre()
    if bin_dir is None:
        raise RuntimeError("JRE extracted but java executable not found. Check archive structure.")
    print(f"[knowledge] JRE ready: {bin_dir}", file=sys.stderr)
    return bin_dir
