import os
import time
import logging
import threading
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass
from pathlib import Path

from lark_oapi.api.im.v1 import GetMessageResourceRequest

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"txt", "xlsx", "csv", "xls", "pdf", "zip", "7z"}

@dataclass
class StashedFile:
    file_key: str
    file_name: str
    file_type: str
    local_path: str
    message_id: str
    stashed_at: float

class FileStash:
    """
    文件归档与暂存管理器。
    - 提供统一的下载方法，将文件沉淀到 artifact/{ext}/ 下
    - 对单聊由于信息不对等提供按 session 的内存暂存区，并做超时清理
    """
    def __init__(self, artifact_dir: Path):
        self._artifact_dir = artifact_dir
        self._stash: Dict[str, List[StashedFile]] = {}
        self._lock = threading.Lock()
        
        self._remind_delay = int(os.getenv("FILE_STASH_REMIND_DELAY", "180"))
        self._expire_delay = int(os.getenv("FILE_STASH_EXPIRE_DELAY", "300"))
        self._disk_ttl = int(os.getenv("FILE_STASH_DISK_TTL", "86400"))
        
        # session_key -> Timer
        self._remind_timers: Dict[str, threading.Timer] = {}
        self._expire_timers: Dict[str, threading.Timer] = {}
        
        self._remind_callback: Optional[Callable[[str, str], None]] = None
        
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        
    def set_remind_callback(self, cb: Callable[[str, str], None]) -> None:
        """回调签名：cb(chat_id, file_name)"""
        self._remind_callback = cb

    def is_allowed(self, file_name: str) -> bool:
        ext = file_name.split(".")[-1].lower() if "." in file_name else ""
        return ext in ALLOWED_EXTENSIONS

    def download_direct(self, file_key: str, file_name: str, message_id: str, api_client) -> Optional[StashedFile]:
        """下载飞书文件到 artifact/{ext}/，返回描述对象"""
        ext = file_name.split(".")[-1].lower() if "." in file_name else "unknown"
        target_dir = self._artifact_dir / ext
        target_dir.mkdir(parents=True, exist_ok=True)
        
        safe_name = file_name.replace("/", "_").replace("\\", "_")
        local_filename = f"{time.strftime('%Y%m%d_%H%M%S')}_{message_id}_{safe_name}"
        local_path = target_dir / local_filename
        
        try:
            req = (GetMessageResourceRequest.builder()
                   .message_id(message_id)
                   .file_key(file_key)
                   .type("file")
                   .build())
            
            resp = api_client.im.v1.message_resource.get(req)
            if not resp.success():
                logger.error(f"下载文件资源失败 | code={resp.code} msg={resp.msg}")
                return None
            
            with open(local_path, "wb") as f:
                f.write(resp.file.read())
            
            logger.info(f"✅ 文件下载成功 | path={local_path}")
            return StashedFile(
                file_key=file_key,
                file_name=file_name,
                file_type=ext,
                local_path=str(local_path.absolute()),
                message_id=message_id,
                stashed_at=time.time()
            )
        except Exception as e:
            logger.error(f"下载文件异常 | error={e}", exc_info=True)
            return None

    def stash_file(self, session_key: str, chat_id: str, file: StashedFile) -> None:
        """加入单聊的暂存队列合并区，并重置超时计时器"""
        with self._lock:
            if session_key not in self._stash:
                self._stash[session_key] = []
            self._stash[session_key].append(file)
            
            # 取消旧的计时器
            if session_key in self._remind_timers:
                self._remind_timers[session_key].cancel()
            if session_key in self._expire_timers:
                self._expire_timers[session_key].cancel()
                
            # 启动新的计时器
            rt = threading.Timer(self._remind_delay, self._on_remind, args=(session_key, chat_id, file.file_name))
            rt.daemon = True
            rt.start()
            self._remind_timers[session_key] = rt
            
            et = threading.Timer(self._expire_delay, self._on_expire, args=(session_key,))
            et.daemon = True
            et.start()
            self._expire_timers[session_key] = et

    def pop_all(self, session_key: str) -> List[StashedFile]:
        """取出该 session 下所有暂存的文件并清空记录"""
        with self._lock:
            # 清理定时器
            if session_key in self._remind_timers:
                self._remind_timers.pop(session_key).cancel()
            if session_key in self._expire_timers:
                self._expire_timers.pop(session_key).cancel()
                
            return self._stash.pop(session_key, [])

    def _on_remind(self, session_key: str, chat_id: str, file_name: str) -> None:
        with self._lock:
            # 双重检查队列是否还有内容
            if session_key not in self._stash or not self._stash[session_key]:
                return
            self._remind_timers.pop(session_key, None)
            
        if self._remind_callback:
            try:
                self._remind_callback(chat_id, file_name)
            except Exception as e:
                logger.error(f"文件暂存提醒回调执行异常 | error={e}", exc_info=True)

    def _on_expire(self, session_key: str) -> None:
        with self._lock:
            if session_key in self._stash:
                logger.warning(f"⏰ 文件暂存过期，清理内存 | session={session_key}")
                self._stash.pop(session_key)
            self._expire_timers.pop(session_key, None)
            
    def build_file_context(self, files: List[StashedFile]) -> str:
        if not files:
            return ""
        lines = ["[附件信息]", "用户上传/关联了以下文件，已保存在本地环境中："]
        for f in files:
            lines.append(f"- 文件：{f.file_name}（类型：{f.file_type}）")
            lines.append(f"  文件主键: {f.file_key}")
            lines.append(f"  本地读取路径：{f.local_path}")
        return "\n".join(lines)

    def start_disk_cleanup_timer(self, extra_dirs: list[Path] | None = None) -> None:
        """
        启动磁盘清理：启动时立即执行一次，之后每小时循环。
        extra_dirs: 额外需要清理的目录列表（如 .playwright-mcp），
                    其中的文件同样按 FILE_STASH_DISK_TTL 判断是否过期。
        """
        _extra = list(extra_dirs or [])

        def _do_cleanup() -> None:
            now = time.time()
            try:
                # artifact/ 子目录
                scan_dirs: list[Path] = []
                if self._artifact_dir.is_dir():
                    scan_dirs += [d for d in self._artifact_dir.iterdir() if d.is_dir()]
                # 额外目录（扁平，直接扫目录下的文件）
                scan_dirs += [d for d in _extra if d.is_dir()]

                for d in scan_dirs:
                    for f in d.iterdir():
                        if f.is_file() and now - f.stat().st_ctime > self._disk_ttl:
                            f.unlink(missing_ok=True)
                            logger.info(f"🗑️ 已清理过期文件 | path={f}")
            except Exception as e:
                logger.error(f"磁盘清理异常 | error={e}")

        def _loop():
            _do_cleanup()          # 启动时立即清一次
            while True:
                time.sleep(3600)   # 之后每小时再清
                _do_cleanup()

        t = threading.Thread(target=_loop, daemon=True, name="file-stash-cleanup")
        t.start()
