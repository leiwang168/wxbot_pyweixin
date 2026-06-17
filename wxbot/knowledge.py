# -*- coding: utf-8 -*-
"""FAQ 知识库（数字员工的业务知识）。

存储：config/knowledge.json
格式：
    [
      {"q": ["价格多少", "多少钱"], "a": "我们的产品定价为..."},
      {"q": ["怎么退款"], "a": "退款请..."}
    ]

匹配策略：
  1) 精确包含命中（用户消息包含任一 q）→ 直接返回 a
  2) 关键词重合度 ≥ threshold → 返回 a（模糊兜底）
  3) 否则返回 None，交给 AI
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional

from .config import bot_config
from .logger import log

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "knowledge.json")
_BUILTIN = [
    {"q": ["你好", "在吗", "有人吗"], "a": "您好，我是数字员工，请问有什么可以帮您？"},
    {"q": ["转人工", "人工客服"], "a": "好的，正在为您转接人工客服，请稍等。"},
]


class KnowledgeBase:
    def __init__(self) -> None:
        self._items: list[dict] = []
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not os.path.exists(_PATH):
                try:
                    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
                    with open(_PATH, "w", encoding="utf-8") as f:
                        json.dump(_BUILTIN, f, ensure_ascii=False, indent=2)
                    self._items = list(_BUILTIN)
                    log.info(f"[知识库] 已生成默认知识库: {_PATH}")
                    return
                except Exception as e:
                    log.error(f"[知识库] 初始化失败: {e}")
                    self._items = []
                    return
            try:
                with open(_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._items = data if isinstance(data, list) else []
                log.info(f"[知识库] 已加载 {len(self._items)} 条")
            except Exception as e:
                log.error(f"[知识库] 加载失败: {e}")
                self._items = []

    def reload(self) -> None:
        self.load()

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """简易中文分词：2-gram + 单字。"""
        text = text.strip()
        tokens = set(text)
        for i in range(len(text) - 1):
            tokens.add(text[i:i + 2])
        return {t for t in tokens if t.strip()}

    def match(self, message: str) -> Optional[str]:
        """返回命中答案，未命中返回 None。"""
        if not bot_config.get("knowledge_switch", True):
            return None
        if not message or not self._items:
            return None
        msg = message.strip()
        # 1) 精确包含
        with self._lock:
            items = list(self._items)
        for it in items:
            for q in it.get("q", []) or []:
                if q and q in msg:
                    return it.get("a")
        # 2) 模糊重合度
        threshold = bot_config.get("knowledge_threshold", 0.6)
        msg_tokens = self._tokenize(msg)
        if not msg_tokens:
            return None
        best_score = 0.0
        best_ans: Optional[str] = None
        for it in items:
            qs = it.get("q", []) or []
            qtext = " ".join(str(x) for x in qs)
            q_tokens = self._tokenize(qtext)
            if not q_tokens:
                continue
            overlap = len(msg_tokens & q_tokens)
            score = overlap / len(q_tokens)
            if score > best_score:
                best_score = score
                best_ans = it.get("a")
        if best_score >= threshold and best_ans:
            return best_ans
        return None


# 全局单例
knowledge = KnowledgeBase()
