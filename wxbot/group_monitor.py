# -*- coding: utf-8 -*-
"""群消息发送人提取 + 关键词监控转发。

read_group_sender: 点击消息气泡左侧头像,弹资料卡读发送人昵称(耗时 UI,按需调用)。
match_group_monitor: 群消息命中配置关键词 → 返回转发目标列表。

群聊 ListItem.window_text 不含发送人(非多选),头像不在 ListItem 子树(children/descendants
均空),实测位于 ListItem 左上角内部(偏移约 +70/+40)。点头像弹出 PopUpProfileWindow
(mmui::ProfileUniquePop),Text[0] 即群里显示的发送人名;连非好友群成员都能识别。
"""
from __future__ import annotations

import time

import pyautogui
from pyweixin.WeChatTools import mouse
from pyweixin.Uielements import Windows
from pywinauto import Desktop

from .config import bot_config
from .logger import log

# 头像中心相对消息 ListItem rect 的偏移(实测命中),小范围循环容错坐标波动
_AVATAR_OFFSETS = ((70, 40), (60, 40), (80, 40), (70, 30), (70, 50))

_desktop = Desktop(backend='uia')


def read_group_sender(msg_item) -> str:
    """点击群消息头像,弹资料卡读发送人昵称。返回昵称,失败返回空串。

    Args:
        msg_item: 群消息气泡 ListItem(pywinauto wrapper,来自 FriendChatList)
    Returns:
        发送人在群里显示的名字;点头像未弹资料卡或读取失败时返回空串
    """
    try:
        r = msg_item.rectangle()
    except Exception as e:
        log.warning(f"[群发送人] 取消息矩形失败: {e}")
        return ""

    profile = None
    for dx, dy in _AVATAR_OFFSETS:
        try:
            mouse.click(coords=(r.left + dx, r.top + dy))
        except Exception:
            continue
        time.sleep(0.9)
        cand = _desktop.window(**Windows.PopUpProfileWindow)
        if cand.exists(timeout=1.0):
            profile = cand
            break
        try:
            pyautogui.press('esc')
        except Exception:
            pass
        time.sleep(0.15)

    if profile is None:
        log.info("[群发送人] 点头像未弹出资料卡")
        return ""

    nickname = ""
    time.sleep(0.7)  # 等资料卡控件渲染稳定
    try:
        texts = [t.window_text() for t in profile.descendants(control_type='Text')]
        for t in texts:
            if t and t.strip():
                nickname = t.strip()  # 第一个非空 Text = 群显示名(发送人)
                break
    except Exception as e:
        log.warning(f"[群发送人] 读资料卡失败: {e}")
    finally:
        try:
            pyautogui.press('esc')  # 关闭资料卡,避免影响后续 UI
        except Exception:
            pass
    return nickname


def match_group_monitor(chat: str, text: str) -> list[str]:
    """群消息命中监控关键词 → 返回去重后的转发目标列表;未命中/未开启返回 []。

    配置 group_monitor_list: [{"group":"群名","keywords":["车找人"],"forward_to":["文件传输助手"]}]
    独立于监听白名单,只按 group_monitor_list 里的群+关键词匹配。
    """
    if not bot_config.get("group_monitor_switch", False):
        return []
    if not text:
        return []
    targets: list[str] = []
    for rule in bot_config.get("group_monitor_list", []):
        if rule.get("group") != chat:
            continue
        kws = rule.get("keywords") or []
        if any(k and k in text for k in kws):
            for t in rule.get("forward_to") or []:
                if t and t not in targets:
                    targets.append(t)
    return targets
