# -*- coding: utf-8 -*-
"""
给指定好友最新一条朋友圈点赞。

运行：python -u like_latest_moment.py
"""
import os
import re
import sys
import time
import pyautogui
import cv2
import numpy as np

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ellipsis_template.png')

from pyweixin import GlobalConfig
from pyweixin.WeChatTools import Navigator, Tools, desktop, mouse
from pyweixin.Uielements import Windows, Lists, Buttons, Regex_Patterns
from pyweixin.utils import SystemSettings, ColorMatch

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

FRIEND = '静静'
COMMENT_TEXT = '未来可期'
# 动作: 'like' 点赞 | 'comment' 评论 | 'both' 点赞+评论
ACTION = 'comment'


# ---------- monkeypatch：修复 open_friend_profile 精确点击头像 ----------
def _patched_open_friend_profile(friend, is_maximize=None, search_pages=None):
    if search_pages is None:
        search_pages = GlobalConfig.search_pages
    # 不最大化窗口
    chatinfo_pane, main_window = Navigator.open_chatinfo(
        friend=friend, is_maximize=False, search_pages=search_pages)
    friend_button = chatinfo_pane.child_window(title=friend, control_type='Button')
    if not friend_button.exists(timeout=3):
        main_window.close()
        raise RuntimeError(f'找不到好友按钮：{friend}')
    # 用昵称 Text 控件推算头像中心：x=昵称水平中心-12(昵称窄偏右)，y=昵称.top-40
    name_ctrl = chatinfo_pane.child_window(title=friend, control_type='Text')
    if name_ctrl.exists(timeout=1):
        nr = name_ctrl.rectangle()
        avatar_x = nr.left + (nr.right - nr.left) // 2 - 12
        avatar_y = nr.top - 40
    else:
        br = friend_button.rectangle()
        avatar_x = br.left + (br.right - br.left) // 2
        avatar_y = br.top + 40
    profile_pane = desktop.window(**Windows.PopUpProfileWindow)
    time.sleep(1)
    # 循环点击，x 向左、y 上下微调提高命中率（昵称中心偏头像右边缘，需左移）
    for dx, dy in ((0, 0), (-8, 0), (-16, 0), (-8, -8), (-16, -8)):
        mouse.click(coords=(avatar_x + dx, avatar_y + dy))
        time.sleep(1)
        if profile_pane.exists(timeout=3):
            time.sleep(1)
            return profile_pane, main_window
    raise RuntimeError('点击头像后资料卡弹窗未出现')


Navigator.open_friend_profile = staticmethod(_patched_open_friend_profile)


def _click_ellipsis(rect):
    """点击朋友圈内容右下角的省略号按钮，弹出赞/评论菜单"""
    cx = rect.right - 83
    cy = rect.bottom - 19
    mouse.move(coords=(cx, cy))
    time.sleep(0.6)
    mouse.click(coords=(cx, cy))
    time.sleep(0.6)


def main():
    GlobalConfig.is_maximize = True
    GlobalConfig.close_weixin = False
    GlobalConfig.language = '简体中文'

    not_contents = ['mmui::AlbumBaseCell', 'mmui::AlbumTopCell']

    print(f'>>> 打开好友 [{FRIEND}] 朋友圈 ...')
    try:
        moments_window = Navigator.open_friend_moments(
            friend=FRIEND, is_maximize=False, close_weixin=False, search_pages=5)
    except Exception as e:
        if '朋友圈' in str(e) or 'ElementNotFound' in type(e).__name__:
            print(f'>>> ❌ 找不到好友 [{FRIEND}] 的朋友圈（对方可能未发朋友圈或已限制查看）')
        else:
            print(f'>>> ❌ 打开朋友圈失败: {e}')
        return
    backbutton = moments_window.child_window(**Buttons.BackButton)
    moments_list = moments_window.child_window(**Lists.MomentsList)
    sns_detail_list = moments_window.child_window(**Lists.SnsDetailList)

    # 滚到顶部，确保第一条就是最新的
    moments_list.type_keys('{END}')
    moments_list.type_keys('{HOME}')
    time.sleep(1)

    # 找第一条有效朋友圈内容（超时 10s）
    print('>>> 查找最新一条朋友圈 ...')
    liked = False
    _com_fails = 0
    _find_start = time.time()
    while True:
        if time.time() - _find_start > 10:
            print(f'>>> ❌ 10秒内未找到 [{FRIEND}] 的朋友圈内容，终止')
            try:
                moments_window.close()
            except Exception:
                pass
            return
        try:
            moments_list.type_keys('{DOWN}')
            selected = [li for li in moments_list.children(control_type='ListItem')
                        if li.has_keyboard_focus()]
        except Exception:
            _com_fails += 1
            if _com_fails > 3:
                print('[Moments] UI COM 错误连续多次,终止')
                break
            time.sleep(0.5)
            continue
        _com_fails = 0

        if not selected or selected[0].class_name() in not_contents:
            continue

        selected[0].click_input()
        if not sns_detail_list.exists(timeout=1):
            try:
                backbutton.click_input()
            except Exception:
                pass
            continue

        try:
            listitem = sns_detail_list.children(control_type='ListItem')[0]
            text = listitem.window_text().replace(FRIEND, '')
            post_time = Regex_Patterns.Snsdetail_Timestamp_pattern.findall(text)[-1]
            print(f'>>> 找到最新朋友圈: {post_time}')

            # '..' 按钮默认隐藏，鼠标悬停在内容区域才显示
            rect = listitem.rectangle()
            pyautogui.moveTo(rect.left + (rect.right - rect.left) // 2,
                             rect.top + (rect.bottom - rect.top) // 2)
            time.sleep(1.0)  # 等 '..' 按钮 hover 显示

            # OpenCV 模板匹配定位 '..' 按钮（直接内存转换，避免文件 IO）
            shot = cv2.cvtColor(np.array(pyautogui.screenshot()), cv2.COLOR_RGB2BGR)
            template = cv2.imread(TEMPLATE_PATH)
            if shot is None or template is None:
                print(f'>>> ❌ 读取截图/模板失败')
                backbutton.click_input()
                break
            res = cv2.matchTemplate(shot, template, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            print(f'>>> 模板匹配最高置信度: {max_val:.3f} 位置: {max_loc}')
            if max_val < 0.6:
                print(f">>> ❌ 未匹配到 '..' 按钮 (置信度不足)")
                backbutton.click_input()
                break
            # max_loc 是匹配左上角，中心点
            th, tw = template.shape[:2]
            click_x = max_loc[0] + tw // 2
            click_y = max_loc[1] + th // 2
            print(f">>> 点击 '..' 坐标: ({click_x}, {click_y})")
            pyautogui.click(click_x, click_y)
            time.sleep(1.0)

            # 点 '..' 弹出菜单，按 ACTION 控制点赞/评论
            commented = False
            try:
                def get_menu_buttons():
                    return {b.window_text().strip(): b
                            for b in moments_window.descendants(control_type='Button')
                            if b.window_text().strip() and b.window_text().strip() not in ('返回', '最小化', '关闭')}

                buttons = get_menu_buttons()
                for t in buttons:
                    print(f'    >>> 弹出: "{t}" rect={buttons[t].rectangle()}')

                # 点赞
                if ACTION in ('like', 'both') and '赞' in buttons:
                    buttons['赞'].click_input()
                    liked = True
                    print(f'>>> 点赞成功! ({post_time})')
                    time.sleep(1.0)
                    # 点赞后菜单关闭，重新点 '..' 再操作评论
                    if ACTION == 'both':
                        pyautogui.click(click_x, click_y)
                        time.sleep(1.0)
                        buttons = get_menu_buttons()

                # 评论
                if ACTION in ('comment', 'both') and '评论' in buttons:
                    buttons['评论'].click_input()
                    time.sleep(1.0)
                    SystemSettings.copy_text_to_clipboard(text=COMMENT_TEXT)
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(0.5)
                    # 点击评论区的绿色发送按钮（颜色识别）
                    comment_listitem = Tools.get_next_item(sns_detail_list, listitem)
                    ColorMatch.click_green_send_button(comment_listitem.rectangle())
                    commented = True
                    print(f'>>> 评论成功: {COMMENT_TEXT}')
                elif ACTION in ('comment', 'both') and '评论' not in buttons:
                    print(f'>>> 评论按钮未找到')
            except Exception as e:
                print(f'    检查失败: {e}')

            backbutton.click_input()
        except Exception as e:
            print(f'>>> 点赞异常: {e}')
            try:
                backbutton.click_input()
            except Exception:
                pass
        break

    try:
        moments_window.close()
    except Exception:
        pass

    print(f'>>> 完成: {"已点赞" if liked else "未点赞"}')


if __name__ == '__main__':
    main()
