# -*- coding: utf-8 -*-
"""新好友管理：自动通过申请 → 改备注 → 打招呼。

通过申请等价于 SiverWXbot `Pass_New_Friends`（wxbot_core.py:4475），
在 pyweixin 中对应 `Contacts.check_new_friends(verify=True)`。
备注生成移植自 SiverWXbot `build_new_friend_remark`（wxbot_core.py:4449）。
"""
from __future__ import annotations

import random
import time
from datetime import datetime

from pyweixin import Contacts, FriendSettings, Messages, Files

from .config import bot_config
from .logger import log
from .mqtt.worker import mqtt_worker
from .reply import is_image_path, split_long_text


# ---------------------------------------------------------------------------
# 备注生成（移植 wxbot_core.py:4421 / 4432 / 4449）
# ---------------------------------------------------------------------------
_REMARK_MAX_UNITS = 32  # 微信备注字符上限（保守取值）


def _remark_unit_len(text: str) -> int:
    """备注长度按字符计（中文 1，emoji 视 surrogate pair 为 1）。"""
    if not text:
        return 0
    # 统计 Unicode 码点数，但把 surrogate pair 计为 1
    return len(text)


def _truncate_remark_units(text: str, max_units: int) -> str:
    if not text:
        return ""
    return text[:max_units]


def build_new_friend_remark(nickname: str) -> str:
    """前缀 + 昵称 + 后缀（各自可选追加时间戳），并截断到上限。"""
    cfg = bot_config.cfg
    use_nick = cfg.get("new_friend_remark_use_nickname", True)
    prefix = cfg.get("new_friend_remark_prefix", "") or ""
    suffix = cfg.get("new_friend_remark_suffix", "") or ""

    if cfg.get("new_friend_remark_prefix_timestamp"):
        prefix = f"{prefix}{datetime.now():%Y%m%d}"
    if cfg.get("new_friend_remark_suffix_timestamp"):
        suffix = f"{suffix}{datetime.now():%Y%m%d}"

    body = nickname if use_nick else ""
    remark = f"{prefix}{body}{suffix}"
    return _truncate_remark_units(remark, _REMARK_MAX_UNITS)


# ---------------------------------------------------------------------------
# 打招呼
# ---------------------------------------------------------------------------
def send_greeting(friend: str) -> None:
    msgs = bot_config.get("new_friend_msg", []) or []
    if not msgs:
        return
    texts: list[str] = []
    images: list[str] = []
    for m in msgs:
        if is_image_path(m):
            images.append(m)
        elif m:
            texts.extend(split_long_text(m))
    try:
        if images:
            Files.send_files_to_friend(
                friend=friend,
                files=images,
                with_messages=bool(texts),
                messages=texts if texts else [""],
                close_weixin=False,
            )
        elif texts:
            Messages.send_messages_to_friend(
                friend=friend, messages=texts, close_weixin=False,
            )
        log.info(f"[新好友打招呼] 已向 {friend} 发送 {len(texts)} 文本 + {len(images)} 图片")
    except Exception as e:
        log.error(f"[新好友打招呼] 发送给 {friend} 失败: {e}")


# ---------------------------------------------------------------------------
# 改备注 + 标签（标签为已知 gap，记日志跳过）
# ---------------------------------------------------------------------------
def apply_remark_and_tags(friend: str, remark: str) -> None:
    try:
        FriendSettings.change_remark(friend=friend, remark=remark, close_weixin=False)
        log.info(f"[新好友备注] {friend} → {remark}")
    except Exception as e:
        log.error(f"[新好友备注] 修改 {friend} 失败: {e}")
    tags = bot_config.get("new_friend_tags", []) or []
    if tags:
        log.warning(f"[新好友标签] pyweixin 暂无打标签接口，跳过 tags={tags}（已知 gap）")


# ---------------------------------------------------------------------------
# 单次检查并处理新好友
# ---------------------------------------------------------------------------
def check_and_pass_once() -> list[str]:
    """调用 check_new_friends 通过申请。返回新通过的好友昵称列表。"""
    if not bot_config.get("new_friend_switch", False):
        return []
    try:
        # verify=True 自动通过；clear=True 清理验证消息
        new_friends = Contacts.check_new_friends(
            verify=True, limit=8, clear=True, close_weixin=False,
        )
    except Exception as e:
        log.error(f"[新好友检查] check_new_friends 失败: {e}")
        return []
    if not new_friends:
        return []
    log.info(f"[新好友检查] 本次通过 {len(new_friends)} 个好友: {new_friends}")
    passed: list[str] = []
    for nickname in new_friends:
        try:
            remark = build_new_friend_remark(nickname)
            apply_remark_and_tags(nickname, remark)
            if bot_config.get("new_friend_reply_switch", False):
                send_greeting(nickname)
            # 通知 MQTT 数字员工通道（OpenClaw）
            try:
                mqtt_worker.on_friend_accepted(nickname, remark, bot_config.get("new_friend_tags", []))
            except Exception as e:
                log.error(f"[新好友MQTT通知] {nickname} 异常: {e}")
            passed.append(nickname)
            time.sleep(random.uniform(1.5, 3.5))  # 拟人间隔
        except Exception as e:
            log.error(f"[新好友处理] {nickname} 异常: {e}")
    return passed


# ---------------------------------------------------------------------------
# 调度循环（由 scheduler 在后台线程调用）
# ---------------------------------------------------------------------------
def new_friend_loop(stop_event) -> None:
    log.info("[新好友调度] 启动")
    while not stop_event.is_set():
        try:
            if bot_config.get("new_friend_switch", False):
                check_and_pass_once()
        except Exception as e:
            log.error(f"[新好友调度] 异常: {e}")
        lo = bot_config.get("new_friend_check_min", 60)
        hi = bot_config.get("new_friend_check_max", 300)
        wait = random.uniform(min(lo, hi), max(lo, hi))
        # 可中断的等待
        stop_event.wait(wait)
    log.info("[新好友调度] 已停止")
