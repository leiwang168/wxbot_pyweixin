# -*- coding: utf-8 -*-
"""岗位人设（Persona）管理。

每个岗位是一个 system prompt 文件：config/persona/<岗位名>.md
数字员工按 chat/group 绑定不同岗位，实现"客服群用客服、销售群用销售"。

优先级：chat/group 专属 > default_persona > 内置默认。
"""
from __future__ import annotations

import os
from typing import Optional

from .config import bot_config
from .logger import log

_PERSONA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "persona")

# 内置默认人设（数字员工通用客服）
_BUILTIN_DEFAULT = (
    "你是一名专业的微信数字员工，负责接待客户咨询。\n"
    "要求：礼貌、简洁、专业；用中文回答；不要编造不确定的信息；\n"
    "遇到无法解决的问题或客户明确要求，主动提示可以转接人工客服。\n"
    "回答尽量控制在 200 字以内，适合即时通讯阅读。"
)


def persona_dir() -> str:
    return _PERSONA_DIR


def list_personas() -> list[str]:
    """返回所有可用岗位名（不含 .md）。"""
    if not os.path.isdir(_PERSONA_DIR):
        return []
    out = []
    for fn in os.listdir(_PERSONA_DIR):
        if fn.lower().endswith(".md"):
            out.append(fn[:-3])
    return sorted(out)


def get_persona_content(name: str) -> Optional[str]:
    """读取指定岗位内容，不存在返回 None。"""
    if not name:
        return None
    path = os.path.join(_PERSONA_DIR, f"{name}.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        log.error(f"[岗位] 读取 {name} 失败: {e}")
        return None


def save_persona(name: str, content: str) -> bool:
    if not name:
        return False
    try:
        os.makedirs(_PERSONA_DIR, exist_ok=True)
        with open(os.path.join(_PERSONA_DIR, f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        log.error(f"[岗位] 保存 {name} 失败: {e}")
        return False


def resolve_persona(chat: str, is_group: bool) -> str:
    """解析指定会话应使用的岗位名。"""
    if is_group:
        m = bot_config.get("group_persona_map", {})
        if chat in m:
            return m[chat]
    else:
        m = bot_config.get("chat_persona_map", {})
        if chat in m:
            return m[chat]
    return bot_config.get("default_persona", "默认客服")


def resolve_system_prompt(chat: str, is_group: bool) -> str:
    """解析会话对应的 system prompt 内容（含回退）。"""
    name = resolve_persona(chat, is_group)
    content = get_persona_content(name)
    if content:
        return content
    # 回退到默认岗位文件
    default_name = bot_config.get("default_persona", "默认客服")
    if default_name != name:
        c = get_persona_content(default_name)
        if c:
            return c
    return _BUILTIN_DEFAULT


def init_default_persona() -> None:
    """首次启动写入默认岗位文件（若目录为空）。"""
    try:
        os.makedirs(_PERSONA_DIR, exist_ok=True)
        default_name = bot_config.get("default_persona", "默认客服")
        path = os.path.join(_PERSONA_DIR, f"{default_name}.md")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_BUILTIN_DEFAULT)
            log.info(f"[岗位] 已生成默认岗位: {default_name}")
    except Exception as e:
        log.warning(f"[岗位] 初始化默认岗位失败: {e}")
