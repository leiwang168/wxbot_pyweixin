# -*- coding: utf-8 -*-
"""OpenAI 兼容 AI 接口实现（数字员工的大脑）。

一个实现覆盖所有 OpenAI Chat Completions 格式的服务：
DusAPI / DeepSeek / 通义千问 / 智谱 / OpenAI 等。只需配置 url/key/model。

参考 SiverWXbot `OpenAIAPI`（wxbot_core.py:1282）与 `DusAPI`（wxbot_core.py:1685）。
"""
from __future__ import annotations

import time
from typing import Optional

import requests

from .config import bot_config
from .logger import log
from .ai_base import AIProvider


class OpenAICompatProvider(AIProvider):
    name = "openai_compat"

    def __init__(self, api_config: dict) -> None:
        self.key = api_config.get("key", "")
        # url 为 chat completions 的根，自动补 /chat/completions
        base = (api_config.get("url") or "").rstrip("/")
        if not base.endswith("/chat/completions"):
            base = base.rstrip("/") + "/chat/completions"
        self.url = base
        self.model = api_config.get("model", "")
        self.timeout = api_config.get("timeout", 60)

    @property
    def ready(self) -> bool:
        return bool(self.key and self.url and self.model)

    def chat(self, friend: str, message: str, msg_type: str = "文本",
             history: Optional[list] = None, system_prompt: str = "") -> Optional[str]:
        """调用接口。history 为 memory.get_messages 返回的 dict 列表。"""
        if not self.ready:
            log.warning("[AI] 接口未配置完整（key/url/model），跳过")
            return None
        messages = self._build_messages(message, history, system_prompt)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0.7,
        }
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        # 梯度重试（2/4/8 秒），共 3 次
        for attempt, delay in enumerate([2, 4, 8], start=1):
            try:
                resp = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout)
                if resp.status_code != 200:
                    log.warning(f"[AI] HTTP {resp.status_code}: {resp.text[:200]}")
                    if resp.status_code >= 500 and attempt < 3:
                        time.sleep(delay)
                        continue
                    return None
                data = resp.json()
                return self._extract_text(data)
            except requests.RequestException as e:
                log.warning(f"[AI] 请求异常(第{attempt}次): {e}")
                if attempt < 3:
                    time.sleep(delay)
                    continue
                return None
            except Exception as e:
                log.error(f"[AI] 解析失败: {e}")
                return None
        return None

    @staticmethod
    def _build_messages(user_msg: str, history: Optional[list], system_prompt: str) -> list[dict]:
        msgs: list[dict] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        # history: [{"发送人","内容","类型"}, ...]，发送人非"我"/admin 视为 user
        admin = bot_config.get("admin", "我")
        for h in history or []:
            content = h.get("内容") if isinstance(h, dict) else None
            if not content:
                continue
            sender = h.get("发送人", "")
            role = "assistant" if sender in (admin, "我", "bot") else "user"
            msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": user_msg})
        return msgs

    @staticmethod
    def _extract_text(data: dict) -> Optional[str]:
        # OpenAI 标准格式
        try:
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content")
                if content:
                    return content.strip()
                # 某些返回把文本放在 text
                if msg.get("text"):
                    return msg["text"].strip()
        except Exception:
            pass
        return None


def build_active_provider() -> OpenAICompatProvider:
    """根据 config 当前 api_index 构造 provider。"""
    cfgs = bot_config.get("api_configs", [])
    idx = bot_config.get("api_index", 0)
    if not cfgs or idx >= len(cfgs):
        return OpenAICompatProvider({})
    return OpenAICompatProvider(cfgs[idx])
