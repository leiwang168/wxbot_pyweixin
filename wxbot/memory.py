# -*- coding: utf-8 -*-
"""对话记忆（文件化，阶段二落地）。

存储结构参考 SiverWXbot `MemoryManager`（wxbot_core.py:912）：
    memory/<bot_wxid>/<chat_safe>/<chat_safe>_memory.json
其中 chat_safe 为聊天名的稳定哈希目录名，避免中文/特殊字符路径问题，
并写入一个 `_origin_name.txt` 记录原始聊天名便于回溯。

每条记录格式：
    {"时间": "YYYY-MM-DD HH:MM:SS", "发送人": ..., "内容": ..., "类型": ...}
按时间顺序追加，超出 max_count 截断最旧的。
线程安全（每个 chat 一个锁文件级互斥）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from typing import Optional

from .logger import log

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_storage_name(chat: str) -> str:
    """聊天名 → 稳定目录名（hash），避免中文/非法字符路径问题。"""
    return hashlib.md5(chat.encode("utf-8")).hexdigest()[:16]


class MemoryManager:
    def __init__(self, enabled: bool = True, max_count: int = 3000,
                 context_count: int = 1000, bot_id: str = "default") -> None:
        self.enabled = enabled
        self.max_count = max_count
        self.context_count = context_count
        self.bot_id = bot_id
        self._base = os.path.join(_DATA_DIR, bot_id)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- 配置热更新 ----
    def reload_settings(self, enabled: bool, max_count: int, context_count: int) -> None:
        self.enabled = enabled
        self.max_count = max_count
        self.context_count = context_count

    def set_bot_id(self, bot_id: str) -> None:
        if bot_id and bot_id != self.bot_id:
            self.bot_id = bot_id
            self._base = os.path.join(_DATA_DIR, bot_id)

    # ---- 锁 ----
    def _lock_for(self, chat: str) -> threading.Lock:
        with self._locks_guard:
            lk = self._locks.get(chat)
            if lk is None:
                lk = threading.Lock()
                self._locks[chat] = lk
            return lk

    def _paths(self, chat: str) -> tuple[str, str]:
        safe = _safe_storage_name(chat)
        d = os.path.join(self._base, safe)
        return d, os.path.join(d, f"{safe}_memory.json")

    def _write_origin(self, d: str, chat: str) -> None:
        try:
            with open(os.path.join(d, "_origin_name.txt"), "w", encoding="utf-8") as f:
                f.write(chat)
        except Exception:
            pass

    # ---- 读写 ----
    def save_message(self, chat: str, sender: str, content: str, msg_type: str = "文本") -> None:
        if not self.enabled or not chat or content is None:
            return
        with self._lock_for(chat):
            d, path = self._paths(chat)
            try:
                os.makedirs(d, exist_ok=True)
                self._write_origin(d, chat)
                data: list = []
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        if isinstance(loaded, list):
                            data = loaded
                data.append({"时间": _now(), "发送人": sender, "内容": content, "类型": msg_type})
                # 截断
                if len(data) > self.max_count:
                    data = data[-self.max_count:]
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as e:
                log.error(f"[记忆] 写入 {chat} 失败: {e}")

    def get_messages(self, chat: str, count: Optional[int] = None) -> list[dict]:
        if not self.enabled or not chat:
            return []
        n = count if count is not None else self.context_count
        with self._lock_for(chat):
            _, path = self._paths(chat)
            if not os.path.exists(path):
                return []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data[-n:] if n > 0 else data
            except Exception as e:
                log.error(f"[记忆] 读取 {chat} 失败: {e}")
            return []

    def clear(self, chat: Optional[str] = None) -> None:
        """清空：指定 chat 清单文件；None 清空整个 bot_id 目录。"""
        if chat is not None:
            with self._lock_for(chat):
                d, path = self._paths(chat)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                    log.info(f"[记忆] 已清空 {chat}")
                except Exception as e:
                    log.error(f"[记忆] 清空 {chat} 失败: {e}")
            return
        try:
            import shutil
            if os.path.isdir(self._base):
                shutil.rmtree(self._base)
            log.info(f"[记忆] 已清空全部 ({self.bot_id})")
        except Exception as e:
            log.error(f"[记忆] 清空全部失败: {e}")

    def list_chats(self) -> list[tuple[str, str]]:
        """返回 [(原始聊天名, 消息条数)]，供 /记忆 列表指令使用。"""
        out: list[tuple[str, str]] = []
        if not os.path.isdir(self._base):
            return out
        for safe in os.listdir(self._base):
            d = os.path.join(self._base, safe)
            if not os.path.isdir(d):
                continue
            name = safe
            origin = os.path.join(d, "_origin_name.txt")
            if os.path.exists(origin):
                try:
                    with open(origin, "r", encoding="utf-8") as f:
                        name = f.read().strip() or safe
                except Exception:
                    pass
            count = "0"
            try:
                with open(os.path.join(d, f"{safe}_memory.json"), "r", encoding="utf-8") as f:
                    data = json.load(f)
                    count = str(len(data)) if isinstance(data, list) else "0"
            except Exception:
                pass
            out.append((name, count))
        return out
