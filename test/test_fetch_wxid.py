# -*- coding: utf-8 -*-
"""验证资料卡性别识别（昵称区域颜色：男蓝/女红）。"""
import sys
import time
sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pyautogui
from pyweixin import GlobalConfig
from pyweixin.WeChatTools import Navigator
from wxbot.mqtt.worker import _patch_open_friend_profile

friend = 'AA锅圈东尚蜂鸟露营19991835767'
GlobalConfig.is_maximize = False
GlobalConfig.close_weixin = False
_patch_open_friend_profile()


def detect_gender(profile_pane) -> str:
    """昵称区域颜色识别性别：男(蓝)/女(红)/未设(空)。"""
    try:
        rv = profile_pane.child_window(
            auto_id='right_v_view.nickname_button_view', control_type='Group')
        r = rv.rectangle()
        crop = np.array(pyautogui.screenshot().crop((r.left, r.top, r.right, r.bottom)))
        if crop.size == 0:
            return ""
        pix = crop.reshape(-1, 3)  # RGB
        # 男蓝 RGB~[16,164,240]；女红 RGB~[187,61,61]
        blue = ((pix[:, 2] > 150) & (pix[:, 0] < 80) &
                (pix[:, 1] > 80) & (pix[:, 1] < 200)).sum()
        red = ((pix[:, 0] > 150) & (pix[:, 1] < 100) & (pix[:, 2] < 100)).sum()
        print(f'  蓝色像素: {blue}, 红色像素: {red}')
        if blue > 30:
            return "男"
        if red > 30:
            return "女"
    except Exception as e:
        print(f'  性别识别异常: {e}')
    return ""


print(f'>>> 打开 [{friend}] 资料卡 ...')
profile_pane, main_window = Navigator.open_friend_profile(
    friend=friend, is_maximize=False, search_pages=5)
time.sleep(2)
gender = detect_gender(profile_pane)
print(f'>>> 性别: {gender if gender else "未设"}')

# 识别完关闭资料卡（点聊天信息按钮关侧边栏）
try:
    from pyweixin.Uielements import Buttons
    main_window.child_window(**Buttons.ChatInfoButton).click_input()
    print('>>> 资料卡已关闭')
except Exception as e:
    print(f'>>> 关闭资料卡失败: {e}')
