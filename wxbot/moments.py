# -*- coding: utf-8 -*-
"""朋友圈：发布（图文）+ 随机点赞。

pyweixin `Moments.post_moments(text, medias)` 当前不暴露隐私/标签参数，
阶段一只支持公开朋友圈（已知 gap，阶段二评估扩展 SDK）。
"""
from __future__ import annotations

import random
import threading
import time

from pyweixin import Moments

from .config import bot_config
from .logger import log


def post(text: str = "", images: list[str] | None = None) -> bool:
    """发布朋友圈。文字和图片至少一项。"""
    images = images or []
    if not text and not images:
        log.warning("[朋友圈] 文字和图片均为空，跳过发布")
        return False
    try:
        Moments.post_moments(
            text=text or "",
            medias=images,
            close_weixin=False,
        )
        log.info(f"[朋友圈] 已发布: text={text!r}, images={len(images)} 张")
        return True
    except Exception as e:
        log.error(f"[朋友圈] 发布失败: {e}")
        return False


def like_once() -> None:
    """给今天的第一条朋友圈点赞（活跃账号）。"""
    try:
        Moments.like_posts(recent="Today", number=1, close_weixin=False)
        log.info("[朋友圈点赞] 已对今日朋友圈点赞")
    except Exception as e:
        log.error(f"[朋友圈点赞] 失败: {e}")


def like_loop(stop_event) -> None:
    """随机间隔点赞循环（分钟级，后台线程）。"""
    if not bot_config.get("moments_like_switch", False):
        log.info("[朋友圈点赞] 开关关闭，不启动")
        return
    log.info("[朋友圈点赞] 启动随机点赞")
    while not stop_event.is_set():
        lo = bot_config.get("moments_like_min", 60)
        hi = bot_config.get("moments_like_max", 120)
        wait_min = random.uniform(min(lo, hi), max(lo, hi))
        # 可中断等待
        if stop_event.wait(wait_min * 60):
            break
        if bot_config.get("moments_like_switch", False):
            like_once()
            # 拟人操作间隔
            time.sleep(random.uniform(1, 5))
    log.info("[朋友圈点赞] 已停止")
