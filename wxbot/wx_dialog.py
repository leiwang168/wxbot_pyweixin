# -*- coding: utf-8 -*-
"""微信提示弹框自动清理（操作频繁、解锁提示等）。

OpenCV 模板匹配检测弹框确认按钮并点击关闭。供 monitor 和 executor 共用，
避免任务间残留弹框导致下个任务在脏页面操作（如 send_text "查无此人"）。
"""
from __future__ import annotations

import os
import time

import pyautogui

from .logger import log
from .paths import get_images_dir

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
