"""
钉钉媒体上传工具

oapi.dingtalk.com 与 api.dingtalk.com 是两套域名、两套 token：
  - oapi token：GET oapi.dingtalk.com/gettoken   （消耗 API 调用配额）
  - api  token：POST api.dingtalk.com/v1.0/oauth2/accessToken

此模块只管 oapi 这条链路：上传媒体文件拿 media_id。
api token 由 DingTalkGateway 自己维护（发消息时需要）。

media_id 现传现用，不缓存（钉钉 media 有过期时间且不保证幂等）。
oapi token 进程内缓存，TTL 到期前复用，减少 gettoken 调用频率。
"""

import logging
import mimetypes
import os
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_OAPI_BASE = "https://oapi.dingtalk.com"
_TOKEN_URL = f"{_OAPI_BASE}/gettoken"
_UPLOAD_URL = f"{_OAPI_BASE}/media/upload"

# 钉钉 oapi token TTL 官方为 7200s，提前 5min 刷新
_TOKEN_TTL_BUFFER = 300


class DingTalkMediaClient:
    """
    钉钉媒体上传客户端。

    线程安全：内部用锁保护 token 刷新，可跨线程共享同一实例。
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._oapi_token: str = ""
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    # ──────────────────────────────────────────────────────────
    #   Token 管理
    # ──────────────────────────────────────────────────────────

    def _refresh_token(self) -> None:
        """向 oapi.dingtalk.com 获取 access_token，结果写入实例缓存。"""
        resp = httpx.get(
            _TOKEN_URL,
            params={"appkey": self._client_id, "appsecret": self._client_secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"获取 oapi token 失败: {data}")
        self._oapi_token = data["access_token"]
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = time.monotonic() + expires_in - _TOKEN_TTL_BUFFER
        logger.debug("oapi token 已刷新，有效期 %ds", expires_in)

    def _get_token(self) -> str:
        with self._lock:
            if time.monotonic() >= self._token_expires_at:
                self._refresh_token()
            return self._oapi_token

    # ──────────────────────────────────────────────────────────
    #   媒体上传
    # ──────────────────────────────────────────────────────────

    def upload(self, file_path: str | Path) -> str:
        """
        上传文件到钉钉媒体服务，返回 media_id（现传现用，调用方不应缓存）。

        支持的媒体类型（钉钉限制）：
          image  — jpg/png/gif/bmp，<20MB
          voice  — amr/mp3/m4a，<2MB
          video  — mp4，<20MB
          file   — 其他，<20MB
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        media_type = _infer_media_type(path)
        token = self._get_token()

        with open(path, "rb") as f:
            resp = httpx.post(
                _UPLOAD_URL,
                params={"access_token": token, "type": media_type},
                files={"media": (path.name, f, _infer_mime(path))},
                timeout=60,
            )

        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"媒体上传失败: {data}")

        media_id: str = data["media_id"]
        logger.info("媒体上传成功 | file=%s media_type=%s media_id=%s", path.name, media_type, media_id)
        return media_id


# ──────────────────────────────────────────────────────────────
#   辅助函数
# ──────────────────────────────────────────────────────────────

def _infer_media_type(path: Path) -> str:
    """根据文件扩展名推断钉钉 media type 分类。"""
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp"}:
        return "image"
    if ext in {".amr", ".mp3", ".m4a"}:
        return "voice"
    if ext in {".mp4"}:
        return "video"
    return "file"


def _infer_mime(path: Path) -> str:
    """推断 MIME 类型，找不到时回退为 application/octet-stream。"""
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"
