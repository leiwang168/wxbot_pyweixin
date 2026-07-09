# -*- coding: utf-8 -*-
"""待通过好友标记 — 持久化到 config/pending_friends.json。

加好友成功后写入标记,monitor 主动遍历会话列表发现该好友出现时
模拟一条"已通过好友请求"消息转发 MQTT,触发后续对话。

格式: [{"match": "王小娟", "added_at": 1781857945}, ...]
match 用备注优先(无备注用昵称),与会话列表 window_text 子串匹配。
"""
from __future__ import annotations

import json
import os
import threading
import time

from .paths import get_config_dir

_PENDING_PATH = os.path.join(get_config_dir(), "pending_friends.json")
_EXPIRE_SECONDS = 7 * 24 * 3600  # 超过 7 天的标记视为过期(对方未通过)
_lock = threading.Lock()


def _load_raw() -> list:
    """读取并清理过期标记,返回 list。"""
    if not os.path.exists(_PENDING_PATH):
        return []
    try:
        with open(_PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    now = time.time()
    return [d for d in data if isinstance(d, dict) and now - float(d.get("added_at", 0)) < _EXPIRE_SECONDS]


def _write_raw(data: list) -> None:
    """原子写入(临时文件 + os.replace)。"""
    try:
        os.makedirs(os.path.dirname(_PENDING_PATH), exist_ok=True)
        tmp = _PENDING_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PENDING_PATH)
    except Exception:
        pass  # 持久化失败不阻塞业务流程


def load_pending() -> list:
    """返回当前所有待通过标记(list[dict],含 match/added_at)。"""
    with _lock:
        return _load_raw()


def add_pending(match_name: str) -> None:
    """新增一个待通过标记(去重:同 match 不重复)。"""
    if not match_name:
        return
    with _lock:
        data = _load_raw()
        if any(d.get("match") == match_name for d in data):
            return
        data.append({"match": match_name, "added_at": time.time()})
        _write_raw(data)


def remove_pending(match_name: str) -> None:
    """移除一个待通过标记(命中并转发后调用,避免重复触发)。"""
    if not match_name:
        return
    with _lock:
        data = _load_raw()
        new_data = [d for d in data if d.get("match") != match_name]
        if len(new_data) != len(data):
            _write_raw(new_data)
