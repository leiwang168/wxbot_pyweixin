# -*- coding: utf-8 -*-
"""客户档案 CRM（数字员工的客户管理）。

存储：customer/<bot_id>/<friend>.json
记录每位联系人的互动档案：
    {
      "昵称": ..., "首次联系": "...", "最近联系": "...",
      "消息数": N, "状态": "新客户|跟进中|意向|已成交|已转人工",
      "备注": "", "跟进记录": [{"时间":..., "内容":...}]
    }
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from typing import Optional

from .logger import log

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "customer")

STATUS_NEW = "新客户"
STATUS_FOLLOWING = "跟进中"
STATUS_INTENDED = "意向"
STATUS_DEAL = "已成交"
STATUS_ESCALATED = "已转人工"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CustomerCRM:
    def __init__(self, enabled: bool = True, bot_id: str = "default") -> None:
        self.enabled = enabled
        self.bot_id = bot_id
        self._base = os.path.join(_DATA_DIR, bot_id)
        self._lock = threading.Lock()

    def set_bot_id(self, bot_id: str) -> None:
        if bot_id and bot_id != self.bot_id:
            self.bot_id = bot_id
            self._base = os.path.join(_DATA_DIR, bot_id)

    def _path(self, friend: str) -> str:
        safe = hashlib.md5(friend.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._base, f"{safe}.json")

    def _load(self, friend: str) -> dict:
        p = self._path(friend)
        if not os.path.exists(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save(self, friend: str, data: dict) -> None:
        try:
            os.makedirs(self._base, exist_ok=True)
            tmp = self._path(friend) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(friend))
        except Exception as e:
            log.error(f"[CRM] 保存 {friend} 失败: {e}")

    def touch(self, friend: str) -> dict:
        """记录一次互动：更新消息数、最近联系时间；首次则建档。"""
        if not self.enabled or not friend:
            return {}
        with self._lock:
            data = self._load(friend)
            now = _now()
            if not data:
                data = {
                    "昵称": friend,
                    "首次联系": now,
                    "最近联系": now,
                    "消息数": 1,
                    "状态": STATUS_NEW,
                    "备注": "",
                    "跟进记录": [],
                }
            else:
                data["最近联系"] = now
                data["消息数"] = int(data.get("消息数", 0)) + 1
                # 新客户达到一定互动量自动转跟进中
                if data.get("状态") == STATUS_NEW and data["消息数"] >= 3:
                    data["状态"] = STATUS_FOLLOWING
            self._save(friend, data)
            return data

    def set_status(self, friend: str, status: str) -> dict:
        if not self.enabled or not friend:
            return {}
        with self._lock:
            data = self._load(friend)
            if not data:
                data = {"昵称": friend, "首次联系": _now(), "最近联系": _now(),
                        "消息数": 0, "状态": status, "备注": "", "跟进记录": []}
            else:
                data["状态"] = status
            self._save(friend, data)
            return data

    def add_note(self, friend: str, note: str) -> dict:
        if not self.enabled or not friend:
            return {}
        with self._lock:
            data = self._load(friend)
            if not data:
                data = {"昵称": friend, "首次联系": _now(), "最近联系": _now(),
                        "消息数": 0, "状态": STATUS_NEW, "备注": "", "跟进记录": []}
            rec = data.get("跟进记录") or []
            rec.append({"时间": _now(), "内容": note})
            data["跟进记录"] = rec
            self._save(friend, data)
            return data

    def get(self, friend: str) -> dict:
        if not self.enabled:
            return {}
        return self._load(friend)

    def list_all(self) -> list[dict]:
        """返回所有客户档案摘要。"""
        out = []
        if not os.path.isdir(self._base):
            return out
        for fn in os.listdir(self._base):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self._base, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    out.append(d)
            except Exception:
                pass
        return out


# 全局单例
crm = CustomerCRM()
