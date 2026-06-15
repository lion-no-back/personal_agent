import os
from pathlib import Path

# Keep CloakBrowser's Chromium cache/download inside this workflow dir instead
# of ~/.cloakbrowser/, so a deleted chromium/ re-downloads here on next use.
_chromium_dir = Path(__file__).resolve().parent / "chromium"
os.environ.setdefault("CLOAKBROWSER_CACHE_DIR", str(_chromium_dir))
# Point CloakBrowser to the local Chromium binary directly when present.
_chromium = _chromium_dir / "chrome.exe"
if _chromium.exists():
    os.environ.setdefault("CLOAKBROWSER_BINARY_PATH", str(_chromium))
