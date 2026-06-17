# -*- coding: utf-8 -*-
"""联系人解析器（pyweixin 适配版）。

将 wxid / 微信号 / 昵称片段 / 备注片段 解析为精确的微信展示名（备注优先）。

pyweixin 的 `Contacts.get_friends_detail()` 返回 list[dict]，键为中文：
{'昵称','微信号','地区','备注','电话','标签',...}。该方法不支持 n/callback，
因此本解析器改为：缓存未命中时一次性全量拉取建缓存（pyweixin 无定点查找能力）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from pyweixin import Contacts

from .common import emit

_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "contacts_cache.json")


@dataclass
class ResolveResult:
    success: bool = True
    display_name: str = ""
    matched_by: str = ""           # wxid | remark | nickname | substring
    wxid: str = ""
    error: str = ""
    candidates: list = field(default_factory=list)


class ContactResolver:
    # pyweixin 返回的字段（中文键为主，兼容 wxautox4 的英文字段名）
    _WXID_FIELDS = ("微信号", "wxid", "alias", "id", "username", "wx_id", "wechat_id")
    _REMARK_FIELDS = ("备注", "remark")
    _NICKNAME_FIELDS = ("昵称", "nickname")
    _REFRESH_COOLDOWN = 60  # 全量刷新最小间隔（秒），防止频繁 UI 操作

    def __init__(self, log_func=None, cache_path: str = "") -> None:
        self._log = log_func or emit
        self._cache_path = cache_path or _CACHE_PATH
        self._lock = threading.Lock()
        self._friends: list[dict] = []
        self._wxid_map: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._last_refresh = 0.0
        self._load_from_file()

    # ---- 公开 API ----
    def resolve(self, target: str) -> ResolveResult:
        if not target or not target.strip():
            return ResolveResult(success=False, error="target 为空")
        key = target.strip()
        with self._lock:
            friends = list(self._friends)
            wxid_map = dict(self._wxid_map)
        lk = key.lower()

        # 1) 精确 wxid/微信号
        if lk in wxid_map:
            f = wxid_map[lk]
            return ResolveResult(success=True, display_name=self._display(f), matched_by="wxid", wxid=self._get_wxid(f))
        # 2) 精确 备注
        for f in friends:
            if self._get_remark(f).lower() == lk:
                return ResolveResult(success=True, display_name=self._display(f), matched_by="remark", wxid=self._get_wxid(f))
        # 3) 精确 昵称
        for f in friends:
            if self._get_nickname(f).lower() == lk:
                return ResolveResult(success=True, display_name=self._display(f), matched_by="nickname", wxid=self._get_wxid(f))
        # 4) 子串唯一匹配
        matches = [f for f in friends if lk in self._get_remark(f).lower() or lk in self._get_nickname(f).lower()]
        if len(matches) == 1:
            f = matches[0]
            return ResolveResult(success=True, display_name=self._display(f), matched_by="substring", wxid=self._get_wxid(f))
        if len(matches) > 1:
            # 多候选时按优先级取最佳：备注精确 > 昵称精确 > 备注子串 > 昵称子串
            def _match_score(f):
                remark = self._get_remark(f).lower()
                nickname = self._get_nickname(f).lower()
                if remark == lk: return 0
                if nickname == lk: return 1
                if lk in remark: return 2
                if lk in nickname: return 3
                return 99
            matches.sort(key=_match_score)
            best = matches[0]
            return ResolveResult(success=True, display_name=self._display(best),
                                 matched_by="substring", wxid=self._get_wxid(best))
        # 5) 未找到 → 全量拉取缓存后重试一次（联系人可能新增，限一次防死循环）
        if not getattr(self, '_resolve_retried', False):
            self._resolve_retried = True
            try:
                self.refresh_cache()
                return self.resolve(target)
            finally:
                self._resolve_retried = False
        return ResolveResult(success=False, error=f"未找到匹配联系人: {key}")

    def refresh_cache(self, timeout: int = 60) -> dict:
        # 限速：防止频繁全量拉取（UI 操作），两次刷新间隔至少 _REFRESH_COOLDOWN 秒
        now = time.time()
        with self._lock:
            since_last = now - self._last_refresh if self._last_refresh else float('inf')
        if since_last < self._REFRESH_COOLDOWN:
            remain = round(self._REFRESH_COOLDOWN - since_last, 1)
            emit("INFO", f"联系人刷新限速: 距上次仅 {since_last:.0f}s，需等待 {remain}s")
            return {"loaded": len(self._friends), "elapsed": 0, "rate_limited": True, "retry_after": remain}

        emit("INFO", f"全量刷新联系人缓存（超时 {timeout}s）...")
        t0 = time.time()
        try:
            raw = Contacts.get_friends_detail(close_weixin=False)
        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            emit("ERROR", f"get_friends_detail 异常: {e}")
            return {"loaded": 0, "elapsed": elapsed, "error": str(e)}
        elapsed = round(time.time() - t0, 1)
        # 以 wxid 去重：同一微信号只保留一条记录
        friends, wxid_map = [], {}
        seen_wxid: set[str] = set()
        dup_count = 0
        if isinstance(raw, list):
            for f in raw:
                if not isinstance(f, dict):
                    continue
                norm = self._normalize(f)
                w = self._get_wxid(norm)
                if w:
                    wl = w.lower()
                    if wl in seen_wxid:
                        dup_count += 1
                        continue
                    seen_wxid.add(wl)
                    wxid_map[wl] = norm
                friends.append(norm)
        with self._lock:
            self._friends = friends
            self._wxid_map = wxid_map
            self._loaded_at = time.time()
            self._last_refresh = time.time()
        self._save_to_file()
        if dup_count:
            emit("INFO", f"联系人缓存已更新: {len(friends)} 人, 去重 {dup_count} 条 (耗时 {elapsed}s)")
        else:
            emit("INFO", f"联系人缓存已更新: {len(friends)} 人 (耗时 {elapsed}s)")
        return {"loaded": len(friends), "elapsed": elapsed, "duplicates_removed": dup_count}

    @property
    def cache_ready(self) -> bool:
        """联系人缓存是否已加载（非空）。调用方据此决定是否解析 wxid，
        避免在消息热路径触发 resolve() 的全量 get_friends_detail 拉取（耗时 UI 操作）。"""
        with self._lock:
            return len(self._friends) > 0

    def get_cache_info(self) -> dict:
        with self._lock:
            size = len(self._friends)
            age = time.time() - self._loaded_at if self._loaded_at else -1
        return {"size": size, "age_seconds": round(age, 1), "cache_file": self._cache_path}

    # ---- 内部 ----
    def _load_from_file(self) -> None:
        if not os.path.exists(self._cache_path):
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            emit("WARNING", f"读取联系人缓存失败: {e}")
            return
        friends_raw = data.get("friends", [])
        if not isinstance(friends_raw, list):
            return
        friends, wxid_map = [], {}
        seen_wxid: set[str] = set()
        for item in friends_raw:
            if not isinstance(item, dict):
                continue
            norm = self._normalize(item)
            w = self._get_wxid(norm)
            if w:
                wl = w.lower()
                if wl in seen_wxid:
                    continue
                seen_wxid.add(wl)
                wxid_map[wl] = norm
            friends.append(norm)
        with self._lock:
            self._friends = friends
            self._wxid_map = wxid_map
            self._loaded_at = data.get("updated_at", time.time())
        emit("INFO", f"从本地联系人缓存加载: {len(friends)} 人")

    def _save_to_file(self) -> bool:
        with self._lock:
            data = {"updated_at": time.time(), "count": len(self._friends),
                    "friends": list(self._friends)}
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            emit("ERROR", f"写入联系人缓存失败: {e}")
            return False

    @staticmethod
    def _normalize(raw: dict) -> dict:
        return {str(k): (str(v) if v is not None and str(v) != "无" else "") for k, v in raw.items()}

    @classmethod
    def _display(cls, f: dict) -> str:
        return cls._get_remark(f) or cls._get_nickname(f)

    # 微信备注占位文本（无实际备注时显示）
    _INVALID_REMARKS = {"添加备注名", "添加备注", "Add Remark", "Set Remark"}

    @classmethod
    def _get_remark(cls, f: dict) -> str:
        for fld in cls._REMARK_FIELDS:
            v = (f.get(fld) or "").strip()
            if v and v not in cls._INVALID_REMARKS:
                return v
        return ""

    @classmethod
    def _get_nickname(cls, f: dict) -> str:
        for fld in cls._NICKNAME_FIELDS:
            v = (f.get(fld) or "").strip()
            if v:
                return v
        return ""

    @classmethod
    def _get_wxid(cls, f: dict) -> str:
        for fld in cls._WXID_FIELDS:
            v = (f.get(fld) or "").strip()
            if v:
                return v
        return ""
