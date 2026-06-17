# -*- coding: utf-8 -*-
"""AI 抽象基类（扩展点）。

本期不实现任何具体 AI 平台。`NullAI.chat` 永远返回 None，表示"不回复"。
阶段三接入 DusAPI/OpenAI/Coze/Dify 时，实现新的 `AIProvider` 子类并在
`reply.decide_reply` 中替换即可。
"""
from __future__ import annotations

from typing import Optional


class AIProvider:
    """AI 接口抽象。子类实现 chat。"""

    name: str = "base"

    def chat(self, friend: str, message: str, msg_type: str = "文本",
             history: Optional[list] = None) -> Optional[str]:
        """根据消息生成回复，返回 None 表示不回复。"""
        raise NotImplementedError


class NullAI(AIProvider):
    """空实现：永远不回复。阶段一默认使用。"""

    name = "null"

    def chat(self, friend: str, message: str, msg_type: str = "文本",
             history: Optional[list] = None) -> Optional[str]:
        return None


# 全局默认 provider（阶段一为 NullAI）
default_ai: AIProvider = NullAI()
