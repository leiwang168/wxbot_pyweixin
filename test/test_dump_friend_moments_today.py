# -*- coding: utf-8 -*-
"""
导出指定好友【当天】的朋友圈，每条保存【内容截图 + 文本】。
不下载图片/视频原文件（避开库 MousePos 右键坐标 bug），只用控件截图。

"""
import os
import re
import sys
import time
import pyautogui

from pyweixin import GlobalConfig
from pyweixin.WeChatTools import Navigator, Tools, desktop, mouse
from pyweixin.Uielements import Windows, Lists, Buttons, Regex_Patterns

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

FRIEND = '浊浪精酿全国业务'
TARGET_FOLDER = r'moments_today'


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


def parse_post(listitem):
    video_num = 0
    photo_num = 0
    text = listitem.window_text().replace(FRIEND, '')
    post_time = Regex_Patterns.Snsdetail_Timestamp_pattern.findall(text)[-1]
    contain_video = re.compile(rf'\s视频\s{post_time}')
    content_pat = re.compile(rf'((\s包含\d+张图片\s)|(\s视频\s)).*{post_time}')
    m = Regex_Patterns.Contain_Images_pattern.search(text)
    if m:
        photo_num = int(m.group(1))
    if contain_video.search(text):
        video_num = 1
    content = content_pat.sub('', text)
    content = re.sub(r'^\s+', '', content)
    return content, photo_num, video_num, post_time


def is_today_post(post_time):
    """好友朋友圈详情页当天发布的时间戳为纯时分（HH:MM）；
    形如"昨天 HH:MM / 星期X HH:MM / 月日 HH:MM / 年月日 HH:MM"的都不是当天。"""
    return bool(re.fullmatch(r'\d{1,2}:\d{2}', post_time.strip()))


def main():
    GlobalConfig.is_maximize = False
    GlobalConfig.close_weixin = False
    GlobalConfig.language = '简体中文'

    friend_folder = os.path.join(TARGET_FOLDER, FRIEND)
    os.makedirs(friend_folder, exist_ok=True)
    not_contents = ['mmui::AlbumBaseCell', 'mmui::AlbumTopCell']

    print(f'>>> 打开好友 [{FRIEND}] 朋友圈 ...')
    moments_window = Navigator.open_friend_moments(
        friend=FRIEND, is_maximize=False, close_weixin=False, search_pages=5)
    backbutton = moments_window.child_window(**Buttons.BackButton)
    Tools.cancel_pin(moments_window)
    moments_list = moments_window.child_window(**Lists.MomentsList)
    sns_detail_list = moments_window.child_window(**Lists.SnsDetailList)
    moments_list.type_keys('{END}')
    moments_list.type_keys('{HOME}')
    time.sleep(1)

    MAX_SAFE = 50  # 安全上限，防止异常情况下无限遍历
    posts = []
    recorded = 0
    print(f'>>> 开始遍历并截图（仅保留当天发布）...')
    while recorded < MAX_SAFE:
        moments_list.type_keys('{DOWN}')
        selected = [li for li in moments_list.children(control_type='ListItem') if li.has_keyboard_focus()]
        if not selected or selected[0].class_name() in not_contents:
            continue
        selected[0].click_input()
        if not sns_detail_list.exists(timeout=0.3):
            pyautogui.press('esc')
            continue
        listitem = sns_detail_list.children(control_type='ListItem')[0]
        content, photo_num, video_num, post_time = parse_post(listitem)

        if not is_today_post(post_time):
            print(f'    遇到非当天内容（{post_time}），停止遍历。')
            if sns_detail_list.exists(timeout=0.1):
                backbutton.click_input()
            break

        detail_folder = os.path.join(friend_folder, str(recorded))
        os.makedirs(detail_folder, exist_ok=True)
        # 截图：直接对父容器 sns_detail_list 截图，确保宽度完整。
        try:
            from PIL import Image
            detail_rect = sns_detail_list.rectangle()
            full = pyautogui.screenshot()
            full.crop((detail_rect.left-20, detail_rect.top, detail_rect.right-50, detail_rect.bottom)).save(
                os.path.join(detail_folder, '内容截图.png'))
        except Exception as e:
            print(f'    !! 截图失败：{e}，回退 capture_as_image')
            try:
                listitem.capture_as_image().save(os.path.join(detail_folder, '内容截图.png'))
            except Exception as e2:
                print(f'    !! 回退也失败：{e2}')
        with open(os.path.join(detail_folder, '内容.txt'), 'w', encoding='utf-8') as f:
            f.write(content) if content else f.write('无文本内容')

        posts.append({'内容': content, '图片数量': photo_num, '视频数量': video_num, '发布时间': post_time})
        print(f'    [{recorded+1}] {post_time} | 图{photo_num} 视频{video_num} | 已保存到 {detail_folder}')
        recorded += 1

        if sns_detail_list.exists(timeout=0.1):
            backbutton.click_input()
        if Tools.is_sns_at_bottom(moments_list, selected[0]):
            print('    已到朋友圈底部。')
            break

    moments_window.close()

    print(f'\n==== 共导出 {len(posts)} 条，保存在 {friend_folder} ====')
    for i, p in enumerate(posts, 1):
        text = (p['内容'] or '').strip().replace('\n', ' ')
        print(f'[{i}] {p["发布时间"]} | 图{p["图片数量"]} 视频{p["视频数量"]}')
        print(f'    {text[:80]}{"..." if len(text) > 80 else ""}')


if __name__ == '__main__':
    main()
