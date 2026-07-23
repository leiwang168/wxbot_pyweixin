# -*- coding: utf-8 -*-
"""单个数字员工身份的配置与状态。"""
from __future__ import annotations

import json
import threading
import time

from .common import DEFAULT_DEDUP_WINDOW, emit


class WorkerIdentity:
    def __init__(self, cfg: dict, log_func=None, dedup_window: float | None = None) -> None:
        self._log = log_func or emit
        self.enabled = cfg.get("enabled", True)
        self.role = (cfg.get("role") or "").strip()
        self.agent_id = (cfg.get("agent_id") or "").strip()

        topics = cfg.get("topics", {}) or {}
        self.subscribe_topic = (topics.get("subscribe") or "").strip()
        self.callback_prefix = (topics.get("callback_prefix") or "").strip()
        self.forward_topic = (topics.get("forward") or "").strip()
        fc = cfg.get("forward_contacts", [])
        self.forward_contacts = fc if isinstance(fc, list) else []

        self._dedup_window = self._coerce_dedup_window(dedup_window)
        self._seen_ids: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _coerce_dedup_window(value: float | int | str | None) -> float:
        try:
            window = float(value if value is not None else DEFAULT_DEDUP_WINDOW)
        except (TypeError, ValueError):
            window = float(DEFAULT_DEDUP_WINDOW)
        return max(1.0, window)

    def _dedup_window_text(self) -> str:
        return f"{self._dedup_window:g}s"

    def resolve_subscribe_topic(self) -> str:
        if self.subscribe_topic and "{role}" in self.subscribe_topic:
            return self.subscribe_topic.replace("{role}", self.role)
        return self.subscribe_topic

    def resolve_callback_prefix(self) -> str:
        if self.callback_prefix and "{agent_id}" in self.callback_prefix:
            return self.callback_prefix.replace("{agent_id}", self.agent_id)
        return self.callback_prefix

    def resolve_forward_topic(self) -> str:
        if self.forward_topic:
            if "{role}" in self.forward_topic:
                return self.forward_topic.replace("{role}", self.role)
            if "{agent_id}" in self.forward_topic:
                return self.forward_topic.replace("{agent_id}", self.agent_id)
        return self.forward_topic

    def should_forward_chat(self, chat: str) -> bool:
        if not self.forward_contacts:
            return True
        return chat in self.forward_contacts

    def is_duplicate(self, payload: str) -> bool:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return False
        cid = data.get("correlationId", "")
        if not cid:
            return False
        now = time.time()
        with self._lock:
            seen_at = self._seen_ids.get(cid)
            if seen_at is not None:
                age = now - seen_at
                if age <= self._dedup_window:
                    self._log("INFO", f"忽略重复消息 correlationId={cid} age={age:.1f}s window={self._dedup_window_text()}", self.role)
                    return True
                self._log("INFO", f"correlationId 去重已过期，允许重新处理 correlationId={cid} age={age:.1f}s window={self._dedup_window_text()}", self.role)
            cutoff = now - self._dedup_window
            self._seen_ids = {k: v for k, v in self._seen_ids.items() if v > cutoff}
            self._seen_ids[cid] = now
            return False

    def is_self_target(self, payload: str) -> bool:
        """检查 wechat_message（反向回复）任务是否指向自身，避免循环。

        新格式 event=wechat_message（带 targetName/targetId），旧格式 _internal_task/
        taskType=send_text 也兼容。
        """
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return False
        event = data.get("event", "")
        internal_task = data.get("_internal_task", "")
        is_send_task = (
            (event == "wechat_message" and ("targetId" in data or "targetName" in data))
            or internal_task == "send_text"
            or data.get("taskType") == "send_text"
        )
        if not is_send_task:
            return False
        target = (data.get("targetName") or data.get("targetId")
                  or (data.get("params", {}) or {}).get("target", "") or "").strip()
        if target and self.agent_id and target == self.agent_id:
            self._log("INFO", f"忽略自指向消息 target={target}", self.role)
            return True
        return False

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "role": self.role,
            "agent_id": self.agent_id,
            "subscribe_topic": self.resolve_subscribe_topic(),
            "callback_prefix": self.resolve_callback_prefix(),
            "forward_topic": self.resolve_forward_topic(),
            "forward_contacts": self.forward_contacts,
        }
