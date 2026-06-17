# -*- coding: utf-8 -*-
"""回复编排：关键词、只监听、转发骨架、延时、分段。

移植自 SiverWXbot `wxbot_core.py`:
  - split_long_text (860)
  - human_delay (890)
  - 关键词 / 只监听 / 自定义转发 逻辑
"""
from __future__ import annotations

import os
import random
import time
from typing import Optional

from . import ai_base
from .config import bot_config
from .logger import log
from .memory import MemoryManager
from . import customer, persona
from .employee import employee

IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp")


def is_image_path(s: str) -> bool:
    """判断字符串是否为指向本地图片的绝对路径。"""
    if not isinstance(s, str) or not s:
        return False
    return os.path.isabs(s) and s.lower().endswith(IMG_EXTS) and os.path.exists(s)


def split_long_text(text: str, chunk_size: int = 2000) -> list[str]:
    """超长文本分段（参考 wxbot_core.py:860）。"""
    if not text:
        return []
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def human_delay() -> None:
    """随机延时，模拟人工（参考 wxbot_core.py:890）。"""
    if not bot_config.get("reply_delay_switch", True):
        return
    lo = bot_config.get("reply_delay_min", 1)
    hi = bot_config.get("reply_delay_max", 5)
    if hi <= 0:
        return
    time.sleep(random.uniform(max(0, lo), max(max(0, lo), hi)))


# ---------------------------------------------------------------------------
# 监听判定
# ---------------------------------------------------------------------------
def is_listened_chat(chat: str, is_group: bool) -> bool:
    """该会话是否在监听范围内。

    AllListen_switch=False → 白名单模式：listen_list / group 是允许列表
    AllListen_switch=True  → 全局监听模式：监听所有，black_list 中的除外
    """
    if is_group:
        if not bot_config.get("group_switch", False):
            return False
        if bot_config.get("AllListen_switch", False):
            # 全局监听：black_list 中的群聊被排除
            return chat not in bot_config.get("black_list", [])
        return chat in bot_config.get("group", [])
    # 私聊
    if bot_config.get("AllListen_switch", False):
        # 全局监听：black_list 中的私聊被排除
        return chat not in bot_config.get("black_list", [])
    return chat in bot_config.get("listen_list", [])


# ---------------------------------------------------------------------------
# 转发骨架（custom_forward_list）
# ---------------------------------------------------------------------------
def match_forward(chat: str, sender: str, text: str, is_group: bool) -> list[tuple[str, bool]]:
    """返回需要转发的 (target, with_source) 列表（按 target 去重，保留首次 with_source）。"""
    if not bot_config.get("custom_forward_switch", False):
        return []
    targets: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for rule in bot_config.get("custom_forward_list", []):
        # 来源判定
        if rule.get("all_sources"):
            if not is_listened_chat(chat, is_group):
                continue
        else:
            if chat not in (rule.get("sources") or []):
                continue
        # 触发判定
        rtype = rule.get("type", "all")
        triggered = False
        if rtype == "all":
            triggered = True
        elif rtype == "keyword":
            kws = rule.get("keywords") or []
            triggered = any(k and k in text for k in kws)
        elif rtype == "sender":
            triggered = sender in (rule.get("senders") or [])
        if not triggered:
            continue
        with_src = bool(rule.get("forward_with_source"))
        for t in rule.get("targets") or []:
            if t and t not in seen:
                seen.add(t)
                targets.append((t, with_src))
    return targets


# ---------------------------------------------------------------------------
# 核心：决定回复内容
# ---------------------------------------------------------------------------
class ReplyEngine:
    def __init__(self) -> None:
        self.ai = ai_base.default_ai
        self.memory = MemoryManager(
            enabled=bot_config.get("memory_switch", True),
            max_count=bot_config.get("memory_max_count", 3000),
            context_count=bot_config.get("memory_context_count", 1000),
        )

    def reload_memory_settings(self) -> None:
        self.memory.reload_settings(
            enabled=bot_config.get("memory_switch", True),
            max_count=bot_config.get("memory_max_count", 3000),
            context_count=bot_config.get("memory_context_count", 1000),
        )

    def set_bot_id(self, bot_id: str) -> None:
        self.memory.set_bot_id(bot_id)
        customer.crm.set_bot_id(bot_id)

    def decide_reply(self, chat: str, sender: str, text: str,
                     msg_type: str, is_group: bool) -> Optional[list[str]]:
        """返回要发送的消息列表（可能含图片路径），None 表示不回复。"""
        if not text:
            return None

        # 0) 客户档案互动记录（私聊；群聊不计入个人 CRM）
        if not is_group and bot_config.get("customer_crm_switch", True):
            customer.crm.touch(chat)

        # 1) 记忆写入（不受 listen_only 影响）
        self.memory.save_message(chat, sender, text, msg_type)

        # 2) 关键词回复（最高优先级，不受 listen_only / @ 限制）
        kw_reply = self._match_keyword(text, is_group)
        if kw_reply:
            self.memory.save_message(chat, bot_config.get("admin", "我"), kw_reply, "回复")
            return [kw_reply]

        # 3) 只监听模式：到此为止，不 AI 回复
        listen_only = bot_config.get("group_listen_only", False) if is_group else bot_config.get("chat_listen_only", False)
        if listen_only:
            return None

        # 4) 数字员工（知识库 → AI）
        emp_reply = employee.reply(chat, text, msg_type, is_group, self.memory.get_messages(chat))
        if emp_reply:
            self.memory.save_message(chat, bot_config.get("admin", "我"), emp_reply, "回复")
            return [emp_reply]

        return None

    def _match_keyword(self, text: str, is_group: bool) -> Optional[str]:
        kw_switch = bot_config.get("group_keyword_switch", False) if is_group \
            else bot_config.get("chat_keyword_switch", False)
        if not kw_switch:
            return None
        # 群聊关键词 @ 才触发（可选）
        if is_group and bot_config.get("group_keyword_at_only", False):
            # 群消息内容里的 @ 标记由 monitor 解析后传入；此处简化：依赖上层已判定
            pass
        for kw, reply in bot_config.get("keyword_dict", {}).items():
            if kw and kw in text:
                return reply
        return None


# 全局单例
reply_engine = ReplyEngine()
