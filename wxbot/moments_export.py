# -*- coding: utf-8 -*-
"""每日朋友圈导出调度(集成进 bot,复用 ui_lock 与 monitor/MQTT 任务互斥)。

每天 23:00-24:00 随机一个时刻,获取当天朋友圈内容保存到本地(好友/日期 结构,dedupe 去重)。
独立线程运行,不阻塞 scheduler 的 tick。获取期间持有 ui_lock + wx_busy,
monitor 轮询和 MQTT 任务的 UI 操作都会等待/跳过,完全不冲突。
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta

from pyweixin import Moments

from .config import bot_config
from .logger import log

# 默认保存目录,可被 config.moments_export.target_folder 覆盖
TARGET_FOLDER = r"E:\Desktop\朋友圈内容导出"
NUMBER = 50  # 当天上限(dedupe 后只存新的)


def _target_folder() -> str:
    cfg = bot_config.get("moments_export", {}) or {}
    return cfg.get("target_folder") or TARGET_FOLDER


def _number():
    """条数上限:None=当天全部(只按 recent='Today' 过滤);正整数=上限。"""
    cfg = bot_config.get("moments_export", {}) or {}
    n = cfg.get("number", None)
    if n is None:
        return None
    try:
        return int(n)
    except Exception:
        return None


def _next_run_time() -> datetime:
    """下一个 23:00-24:00 之间的随机时刻(今天未过用今天,否则明天)。"""
    now = datetime.now()
    base = now.replace(hour=23, minute=0, second=0, microsecond=0)
    target = base + timedelta(seconds=random.randint(0, 3600))
    if target <= now:
        target = (now + timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0) \
                 + timedelta(seconds=random.randint(0, 3600))
    return target


def _fetch_once() -> None:
    """获取一次今日朋友圈。持有 ui_lock + wx_busy,与 monitor/MQTT 互斥。"""
    from .mqtt.worker import mqtt_worker  # 局部 import 避免循环依赖
    folder = _target_folder()
    os.makedirs(folder, exist_ok=True)

    ui_lock = mqtt_worker.ui_lock
    if ui_lock and not ui_lock.acquire(timeout=30):
        log.info("[朋友圈导出] UI 锁被占用超时,跳过本次")
        return
    # set wx_busy 让 monitor 跳过(双保险,monitor 主要靠 ui_lock)
    if mqtt_worker._coordinator:
        mqtt_worker._coordinator._wx_busy_event.set()
    try:
        log.info(f"[朋友圈导出] 开始获取今日朋友圈 → {folder}")
        posts = Moments.dump_recent_posts(
            recent='Today', save_detail=True, number=_number(),
            target_folder=folder, dedupe=True, close_weixin=False)
        log.info(f"[朋友圈导出] 完成,本次获取 {len(posts)} 条")
    except Exception as e:
        log.error(f"[朋友圈导出] 异常: {e}")
    finally:
        if mqtt_worker._coordinator:
            mqtt_worker._coordinator._wx_busy_event.clear()
        if ui_lock:
            try:
                ui_lock.release()
            except RuntimeError:
                pass


def run_moments_export_loop(stop_event) -> None:
    """常驻循环:每天 23:00-24:00 随机点导出今日朋友圈。"""
    if not bot_config.get("moments_export_switch", True):
        log.info("[朋友圈导出] 开关关闭(moments_export_switch=false),不启动")
        return
    folder = _target_folder()
    log.info(f"[朋友圈导出] 调度启动:每天 23:00-24:00 随机点获取 → {folder}")
    while not stop_event.is_set():
        target = _next_run_time()
        wait = (target - datetime.now()).total_seconds()
        log.info(f"[朋友圈导出] 下次运行: {target:%Y-%m-%d %H:%M:%S}(等待 {wait/3600:.2f}h)")
        if stop_event.wait(max(wait, 0)):
            return  # 收到停止信号
        _fetch_once()
