# -*- coding: utf-8 -*-
"""微信提示弹框自动清理（操作频繁、解锁提示等）。

OpenCV 模板匹配检测弹框确认按钮并点击关闭。供 monitor 和 executor 共用，
避免任务间残留弹框导致下个任务在脏页面操作（如 send_text "查无此人"）。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import pyautogui

from .logger import log
from .paths import get_images_dir


@dataclass
class WxDialogResult:
    hit: bool = False
    dialog_type: str = ""
    text: str = ""
    dismissed: bool = False


class WxAddFriendRateLimited(RuntimeError):
    """\u5fae\u4fe1\u63d0\u793a\u6dfb\u52a0\u597d\u53cb\u64cd\u4f5c\u8fc7\u4e8e\u9891\u7e41\u3002"""


_RATE_LIMIT_PHRASES = ("\u64cd\u4f5c\u8fc7\u4e8e\u9891\u7e41", "\u8bf7\u7a0d\u540e\u518d\u8bd5")
_CONFIRM_TITLES = ("\u786e\u5b9a", "OK", "\u78ba\u5b9a")


_ADD_FRIEND_RATE_LIMIT_LOCK = threading.Lock()
_ADD_FRIEND_RATE_LIMIT_UNTIL = 0.0
_ADD_FRIEND_RATE_LIMIT_REASON = ""


def mark_add_friend_rate_limited(seconds: int, reason: str = "") -> int:
    """Record a process-wide add-friend cooldown and return remaining seconds."""
    global _ADD_FRIEND_RATE_LIMIT_UNTIL, _ADD_FRIEND_RATE_LIMIT_REASON
    try:
        seconds = int(seconds)
    except Exception:
        seconds = 0
    if seconds <= 0:
        return get_add_friend_rate_limit_retry_after()
    now = time.time()
    until = now + seconds
    with _ADD_FRIEND_RATE_LIMIT_LOCK:
        if until > _ADD_FRIEND_RATE_LIMIT_UNTIL:
            _ADD_FRIEND_RATE_LIMIT_UNTIL = until
            _ADD_FRIEND_RATE_LIMIT_REASON = reason or ""
        return max(1, int(_ADD_FRIEND_RATE_LIMIT_UNTIL - now + 0.999))


def get_add_friend_rate_limit_retry_after() -> int:
    """Return remaining process-wide add-friend cooldown seconds, or 0 if expired."""
    global _ADD_FRIEND_RATE_LIMIT_UNTIL, _ADD_FRIEND_RATE_LIMIT_REASON
    now = time.time()
    with _ADD_FRIEND_RATE_LIMIT_LOCK:
        remaining = int(_ADD_FRIEND_RATE_LIMIT_UNTIL - now + 0.999)
        if remaining <= 0:
            _ADD_FRIEND_RATE_LIMIT_UNTIL = 0.0
            _ADD_FRIEND_RATE_LIMIT_REASON = ""
            return 0
        return remaining


def _safe_window_text(ctrl) -> str:
    try:
        return (ctrl.window_text() or "").strip()
    except Exception:
        return ""


def _candidate_roots(main_window=None):
    seen = set()

    def _yield(root):
        if root is None:
            return
        try:
            handle = root.handle
        except Exception:
            handle = id(root)
        if handle in seen:
            return
        seen.add(handle)
        yield root

    yield from _yield(main_window)
    try:
        from pyweixin.WeChatTools import desktop
        for w in desktop.windows(visible_only=True):
            title = _safe_window_text(w)
            try:
                cls = w.class_name() or ""
            except Exception:
                cls = ""
            if "\u5fae\u4fe1" in title or "WeChat" in title or cls.startswith("mmui::"):
                yield from _yield(w)
    except Exception:
        return


def _collect_text(root) -> str:
    parts: list[str] = []
    for kwargs in ({"control_type": "Text"}, {}):
        try:
            ctrls = root.descendants(**kwargs)
        except Exception:
            continue
        for ctrl in ctrls[:80]:
            s = _safe_window_text(ctrl)
            if s and s not in parts:
                parts.append(s)
    return " ".join(parts).strip()


def _click_confirm(root) -> bool:
    for title in _CONFIRM_TITLES:
        try:
            btn = root.child_window(control_type="Button", title=title)
            if btn.exists(timeout=0.2):
                btn.click_input()
                time.sleep(0.3)
                return True
        except Exception:
            continue
    try:
        return dismiss_wx_dialog(root)
    except Exception:
        return False


def detect_and_dismiss_wx_dialog(main_window=None, dismiss: bool = True) -> WxDialogResult:
    """\u8bc6\u522b\u5fae\u4fe1\u63d0\u793a\u5f39\u7a97\u5e76\u6309\u9700\u70b9\u51fb\u786e\u8ba4\uff1b\u5931\u8d25\u5b89\u5168\u8fd4\u56de hit=False\u3002

    \u5f53\u524d\u91cd\u70b9\u8bc6\u522b\u6dfb\u52a0\u597d\u53cb\u98ce\u63a7\u5f39\u7a97\uff1a"\u64cd\u4f5c\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5"\u3002
    UIA \u8bfb\u4e0d\u5230\u6587\u672c\u65f6\uff0c\u4ecd\u4fdd\u7559\u65e7\u7684 OpenCV \u6e05\u7406\u80fd\u529b\uff0c\u4f46\u4e0d\u4f1a\u8bef\u5224\u4e3a\u98ce\u63a7\u3002
    """
    fallback_dismissed = False
    for root in _candidate_roots(main_window):
        text = _collect_text(root)
        if text and all(phrase in text for phrase in _RATE_LIMIT_PHRASES):
            dismissed = _click_confirm(root) if dismiss else False
            log.info(f"[\u5f39\u6846\u5904\u7406] \u5fae\u4fe1\u98ce\u63a7\u63d0\u793a: {text[:120]} dismissed={dismissed}")
            return WxDialogResult(True, "rate_limited", text, dismissed)
        try:
            if dismiss and not fallback_dismissed:
                fallback_dismissed = dismiss_wx_dialog(root)
        except Exception:
            pass
    if fallback_dismissed:
        return WxDialogResult(True, "unknown", "", True)
    return WxDialogResult(False, "", "", False)

# 弹框确认按钮模板（按优先级尝试）
_TEMPLATES = ('confirm_btn.png', 'unlock_btn.png')
# 匹配置信度阈值
_THRESHOLD = 0.6


def dismiss_wx_dialog(main_window) -> bool:
    """检测并点击微信提示弹框的确认按钮。命中返回 True，否则 False。全程不抛。

    在主窗口区域截图，匹配 config/images/ 下的确认按钮模板，
    命中(置信度≥阈值)则点击关闭。模板缺失安全返回 False。
    """
    import cv2
    import numpy as np
    from PIL import ImageGrab
    try:
        r = main_window.rectangle()
        shot = cv2.cvtColor(np.array(
            ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))),
            cv2.COLOR_RGB2BGR)
    except Exception as e:
        log.debug(f"[弹框处理] 截图失败: {e}")
        return False
    tpl_dir = get_images_dir()
    for tpl_name in _TEMPLATES:
        try:
            tpl = cv2.imread(os.path.join(tpl_dir, tpl_name), cv2.IMREAD_COLOR)
        except Exception:
            tpl = None
        if tpl is None:
            continue
        th, tw = tpl.shape[:2]
        if shot.shape[0] < th or shot.shape[1] < tw:
            continue
        res = cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= _THRESHOLD:
            cx = r.left + max_loc[0] + tw // 2
            cy = r.top + max_loc[1] + th // 2
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            log.info(f"[弹框处理] 点击确认按钮 {tpl_name} (置信度{max_val:.3f}) @ ({cx},{cy})")
            return True
    return False
