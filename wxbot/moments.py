# -*- coding: utf-8 -*-
"""朋友圈：发布（图文）+ 随机点赞。

pyweixin `Moments.post_moments(text, medias)` 当前不暴露隐私/标签参数，
阶段一只支持公开朋友圈（已知 gap，阶段二评估扩展 SDK）。
"""
from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager

from pyweixin import Moments

from .config import bot_config
from .logger import log


@contextmanager
def _ui_lock():
    """获取全局 UI 锁（与 monitor / MQTT 任务 / 点赞循环互斥）。

    发朋友圈、点赞都是耗时 UI 操作；持锁期间其他轮询（monitor 消息轮询、点赞循环）
    让位，避免抢鼠标 / 页面状态冲突。锁不可用（MQTT 未启用）时不阻塞，仅记录。
    """
    from .mqtt.worker import mqtt_worker
    lock = mqtt_worker.ui_lock
    acquired = False
    if lock:
        acquired = lock.acquire(timeout=30)
        if not acquired:
            log.warning("[朋友圈] UI 锁获取超时(30s)，仍继续执行")
    try:
        yield
    finally:
        if acquired:
            try:
                lock.release()
            except RuntimeError:
                pass


def post(text: str = "", images: list[str] | None = None) -> bool:
    """发布朋友圈。文字和图片至少一项。"""
    images = images or []
    if not text and not images:
        log.warning("[朋友圈] 文字和图片均为空，跳过发布")
        return False
    try:
        with _ui_lock():
            pre_delay = int(bot_config.get("moments_post_pre_delay", 3) or 0)
            if pre_delay > 0:
                log.info(f"[朋友圈] 发圈前等待 {pre_delay}s（拟人延迟）")
                time.sleep(pre_delay)
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
        with _ui_lock():
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
