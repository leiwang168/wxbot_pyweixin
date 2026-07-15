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

from ..paths import get_config_dir
from .common import emit

_CACHE_PATH = os.path.join(get_config_dir(), "contacts_cache.json")


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
        self._save_lock = threading.Lock()  # 串行化文件写入,防止并发 os.replace 竞态
        self._friends: list[dict] = []
        self._wxid_map: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._last_refresh = 0.0
        self._load_from_file()
        # 缓存文件不存在或为空 → 启动后自动全量拉取一次
        if not self._friends:
            from ..config import bot_config
            _timeout = int(bot_config.get("contacts_refresh_timeout", 300))
            emit("INFO", f"联系人缓存为空，启动后自动刷新（超时{_timeout}s）...")
            try:
                self.refresh_cache(timeout=_timeout)
            except Exception as e:
                emit("ERROR", f"启动时联系人缓存自动刷新失败: {e}")

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
        # 5) 未找到 → 文件重载（其他 resolver 实例可能已追加，瞬时完成）
        if not getattr(self, '_resolve_retried', False):
            self._resolve_retried = True
            try:
                self._load_from_file()
                retried = self.resolve(target)
                if retried.success:
                    return retried
            finally:
                self._resolve_retried = False
        return ResolveResult(success=False, error=f"未找到匹配联系人: {key}")

    def find_wxid_by_remark(self, remark: str) -> ResolveResult:
        """通过好友备注名查找微信号。

        匹配顺序：精确备注 > 备注子串。备注子串有多个候选时，
        返回第一个满足条件的（按缓存顺序，稳定可预期）。
        找不到返回 success=False。
        """
        if not remark or not remark.strip():
            return ResolveResult(success=False, error="remark 为空")
        key = remark.strip()
        with self._lock:
            friends = list(self._friends)
        lk = key.lower()
        # 1) 精确备注
        for f in friends:
            if self._get_remark(f).lower() == lk:
                return ResolveResult(success=True, display_name=self._display(f),
                                     matched_by="remark", wxid=self._get_wxid(f))
        # 2) 备注子串：多候选取第一个满足条件的
        for f in friends:
            r = self._get_remark(f).lower()
            if r and lk in r:
                return ResolveResult(success=True, display_name=self._display(f),
                                     matched_by="remark-substring", wxid=self._get_wxid(f))
        # 3) 缓存可能为空或陈旧，重载一次本地文件再找（与其他 resolver 实例追加的条目对齐）
        if not getattr(self, '_remark_retried', False):
            self._remark_retried = True
            try:
                self._load_from_file()
                retried = self.find_wxid_by_remark(remark)
                if retried.success:
                    return retried
            finally:
                self._remark_retried = False
        return ResolveResult(success=False, error=f"未找到备注匹配的联系人: {key}")

    def refresh_cache(self, timeout: int = 120) -> dict:
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
                if '已停用' in (norm.get('昵称') or ''):
                    continue  # 跳过已停用账号（脏数据）
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

    def get_all_contacts(self) -> list[dict]:
        """返回全部联系人缓存的浅拷贝（线程安全）。"""
        with self._lock:
            return list(self._friends)

    def add_contact(self, info: dict) -> bool:
        """追加单个联系人到缓存并持久化，避免全量刷新。

        线程安全。wxid 已存在则跳过；wxid 缺失则返回 False。
        内存更新同步，文件写入异步（防止 os.replace 卡住 MQTT 任务）。
        """
        norm = self._normalize(info)
        wxid = self._get_wxid(norm)
        if not wxid:
            emit("WARNING", "add_contact: info dict 中未找到微信号，跳过")
            return False
        wl = wxid.lower()
        with self._lock:
            if wl in self._wxid_map:
                return False
            self._friends.append(norm)
            self._wxid_map[wl] = norm
        # 后台写文件：Windows 下 os.replace 可能被杀软卡住
        threading.Thread(target=self._save_to_file, daemon=True, name="cache-save").start()
        emit("INFO", f"联系人缓存已追加: wxid={wxid} (共 {len(self._friends)} 人)")
        return True

    def update_or_add_by_remark(self, info: dict) -> bool:
        """按备注查找：存在则更新（含微信号），不存在则新增。

        用于新好友查资料卡后回填真实微信号——全量缓存里该好友可能已有
        不准的 wxid（如 bb15129562650），需按备注定位并覆盖。
        线程安全；内存更新同步，文件写入异步。
        """
        norm = self._normalize(info)
        remark = self._get_remark(norm)
        if not remark:
            emit("WARNING", "update_or_add_by_remark: info dict 无备注，跳过")
            return False
        with self._lock:
            target = None
            for f in self._friends:
                if self._get_remark(f) == remark:
                    target = f
                    break
            if target is not None:
                # 删除旧 wxid 索引（指向该条目），更新字段后重建索引
                old_wxid = self._get_wxid(target).lower()
                if old_wxid and self._wxid_map.get(old_wxid) is target:
                    del self._wxid_map[old_wxid]
                for k, v in norm.items():
                    if v:
                        target[k] = v
                new_wxid = self._get_wxid(target).lower()
                if new_wxid:
                    self._wxid_map[new_wxid] = target
            else:
                wxid = self._get_wxid(norm)
                if not wxid:
                    emit("WARNING", f"update_or_add_by_remark: 备注未匹配且无微信号，跳过: {remark}")
                    return False
                self._friends.append(norm)
                self._wxid_map[wxid.lower()] = norm
        threading.Thread(target=self._save_to_file, daemon=True, name="cache-save").start()
        emit("INFO", f"联系人缓存已更新(按备注={remark}) 共 {len(self._friends)} 人")
        return True

    # ---- 内部 ----
    def _load_from_file(self) -> None:
        """从缓存文件加载,采用 merge 语义(不覆盖内存中已有条目)。

        关键:内存里 add_contact 追加但尚未写盘的条目优先保留,
        仅追加文件里有而内存没有的条目。防止 resolve 重试触发的
        reload 覆盖掉刚追加、还没落盘的联系人。
        """
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
        with self._lock:
            # merge:内存现有条目优先(可能比文件新),再追加文件独有条目
            merged_friends = list(self._friends)
            merged_map = dict(self._wxid_map)
            existing = set(merged_map.keys())  # 已有 wxid (小写)
            file_only = 0
            for item in friends_raw:
                if not isinstance(item, dict):
                    continue
                norm = self._normalize(item)
                if '已停用' in (norm.get('昵称') or ''):
                    continue  # 跳过已停用账号（脏数据）
                w = self._get_wxid(norm)
                if w:
                    wl = w.lower()
                    if wl in existing:
                        continue  # 内存已有,保留内存版本
                    existing.add(wl)
                    merged_map[wl] = norm
                    file_only += 1
                merged_friends.append(norm)
            self._friends = merged_friends
            self._wxid_map = merged_map
            self._loaded_at = data.get("updated_at", time.time())
        emit("INFO", f"从本地联系人缓存合并加载: 新增 {file_only} 条, 共 {len(merged_friends)} 人")

    def _save_to_file(self) -> bool:
        with self._lock:
            data = {"updated_at": time.time(), "count": len(self._friends),
                    "friends": list(self._friends)}
        with self._save_lock:  # 串行化:避免多个后台线程并发写同一文件导致半截 JSON
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
