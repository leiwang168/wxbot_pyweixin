# -*- coding: utf-8 -*-
"""数字员工编排：知识库 → AI（岗位人设+记忆+客户上下文）→ 转人工。

决策顺序：
  0) 转人工关键词命中 → 返回转人工提示并标记客户状态、通知人工
  1) 知识库精确/模糊命中 → 直接返回业务答案
  2) AI（带岗位 system prompt + 记忆历史 + 客户档案摘要）→ 回复
  3) 都没有 → None（不回复）
"""
from __future__ import annotations

import time
from typing import Optional

from pyweixin import Messages

from . import persona, knowledge
from .ai_openai import build_active_provider
from .config import bot_config
from .customer import crm, STATUS_ESCALATED
from .logger import log


class DigitalEmployee:
    def __init__(self) -> None:
        self._provider = None  # 懒加载

    @property
    def provider(self):
        if self._provider is None:
            self._provider = build_active_provider()
        return self._provider

    def reload(self) -> None:
        """配置热重载后重建 provider 与知识库。"""
        self._provider = build_active_provider()
        knowledge.reload()

    # ---- 客户上下文 ----
    def _customer_context(self, friend: str, is_group: bool) -> str:
        """把客户档案摘要拼进 system prompt，让 AI 知道在跟谁聊。"""
        if is_group:
            return ""
        info = crm.get(friend)
        if not info:
            return ""
        return (
            f"\n\n【当前客户信息】昵称：{info.get('昵称','')}；"
            f"状态：{info.get('状态','')}；累计消息：{info.get('消息数',0)}；"
            f"首次联系：{info.get('首次联系','')}。请结合客户状态调整沟通策略。"
        )

    # ---- 转人工 ----
    def _is_escalation(self, message: str) -> bool:
        if not bot_config.get("escalation_switch", True):
            return None
        kws = bot_config.get("escalation_keywords", []) or []
        return any(k and k in message for k in kws)

    def _notify_human(self, friend: str, message: str) -> None:
        target = bot_config.get("escalation_target") or bot_config.get("admin", "文件传输助手")
        notice = f"🔔 转人工提醒：客户【{friend}】请求人工服务。\n最近消息：{message}"
        try:
            Messages.send_messages_to_friend(friend=target, messages=[notice], close_weixin=False)
            log.info(f"[转人工] 已通知 {target}：{friend}")
        except Exception as e:
            log.error(f"[转人工] 通知失败: {e}")

    # ---- 核心 ----
    def reply(self, friend: str, message: str, msg_type: str,
              is_group: bool, history: Optional[list] = None) -> Optional[str]:
        """生成数字员工回复。"""
        if not bot_config.get("digital_employee_switch", True):
            return None

        # 0) 转人工
        if self._is_escalation(message):
            crm.set_status(friend, STATUS_ESCALATED)
            self._notify_human(friend, message)
            return "好的，正在为您转接人工客服，请稍等。"

        # 1) 知识库
        kb_ans = knowledge.knowledge.match(message)
        if kb_ans:
            log.info(f"[数字员工] 知识库命中 → {friend}")
            return kb_ans

        # 2) AI
        if not self.provider.ready:
            log.warning("[数字员工] AI 接口未配置，知识库未命中，暂不回复")
            return None
        system_prompt = persona.resolve_system_prompt(friend, is_group) + self._customer_context(friend, is_group)
        try:
            ans = self.provider.chat(friend, message, msg_type, history, system_prompt)
        except Exception as e:
            log.error(f"[数字员工] AI 异常: {e}")
            return None
        if ans:
            log.info(f"[数字员工] AI 回复 → {friend}")
        return ans


# 全局单例
employee = DigitalEmployee()
