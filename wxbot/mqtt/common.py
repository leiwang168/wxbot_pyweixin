# -*- coding: utf-8 -*-
"""MQTT 扩展公共组件：常量、日志适配、令牌桶限流、MinIO 上传器。"""
from __future__ import annotations

import mimetypes
import threading
import time
import uuid
from pathlib import Path

from ..logger import log

# ---- 运行时常量（与 SiverWXbot 一致）----
MAX_TARGET_LEN = 128
MAX_MESSAGE_LEN = 4096
MAX_VERIFY_TEXT_LEN = 256
MAX_CONTACT_LEN = 128
MAX_HISTORY_LIMIT = 200
TASK_TIMEOUT_DEFAULT = 60
DEDUP_WINDOW = 300
TASK_PRIORITY_DEFAULT = 5
TASK_PRIORITY_MAP = {
    "ping": 0, "wechat_message": 1, "add_friend": 2,
    "get_chat_history": 3, "refresh_contacts": 4,
    "get_friend_details": 5, "post_moments": 6,
}
WINDOW_OPENING_TASKS = {"send_text", "get_chat_history", "get_friend_details"}


def emit(level: str, message: str, role: str = "") -> None:
    """统一日志输出，支持角色前缀。level: DEBUG/INFO/WARNING/ERROR。"""
    prefix = f"[MqttWorker:{role}]" if role else "[MqttWorker]"
    msg = f"{prefix} {message}"
    lvl = level.upper()
    mapping = {"DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR"}
    log_level = mapping.get(lvl, "INFO")
    getattr(log, log_level.lower(), log.info)(msg)


class TokenBucket:
    """令牌桶限流器。"""

    def __init__(self, rate: float = 1.0, burst: int = 3) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._burst, self._tokens + (now - self._last_refill) * self._rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


class MinioUploader:
    """MinIO 文件上传器（富媒体消息转发用，独立于 MQTT 连接）。"""

    def __init__(self, cfg: dict) -> None:
        self._endpoint = (cfg.get("endpoint") or "").strip()
        self._access_key = (cfg.get("access_key") or "").strip()
        self._secret_key = (cfg.get("secret_key") or "").strip()
        self._bucket = (cfg.get("bucket") or "wbot").strip()
        self._secure = cfg.get("secure", True)
        self._public_url_prefix = (cfg.get("public_url_prefix") or "").strip()
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._endpoint and self._access_key and self._secret_key)

    def _ensure_client(self) -> bool:
        if self._client:
            return True
        if not self.available:
            return False
        try:
            from minio import Minio
            self._client = Minio(self._endpoint, access_key=self._access_key,
                                 secret_key=self._secret_key, secure=self._secure)
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
            emit("INFO", f"MinIO 已连接 {self._endpoint} bucket={self._bucket}")
            return True
        except Exception as e:
            emit("ERROR", f"MinIO 连接失败: {e}")
            self._client = None
            return False

    def upload(self, local_path: str, chat: str = "") -> str | None:
        if not self._ensure_client():
            return None
        try:
            local = Path(local_path)
            if not local.exists():
                emit("ERROR", f"文件不存在: {local_path}")
                return None
            ext = local.suffix or ".bin"
            ts = int(time.time())
            short = uuid.uuid4().hex[:8]
            object_name = f"{chat}/{ts}_{short}{ext}" if chat else f"{ts}_{short}{ext}"
            ct, _ = mimetypes.guess_type(str(local))
            self._client.fput_object(self._bucket, object_name, str(local),
                                     content_type=ct or "application/octet-stream")
            if self._public_url_prefix:
                url = f"{self._public_url_prefix.rstrip('/')}/{self._bucket}/{object_name}"
            else:
                scheme = "https" if self._secure else "http"
                url = f"{scheme}://{self._endpoint}/{self._bucket}/{object_name}"
            emit("INFO", f"MinIO 上传成功: {object_name}")
            return url
        except Exception as e:
            emit("ERROR", f"MinIO 上传失败: {e}")
            return None
