# -*- coding: utf-8 -*-
"""诊断:私聊消息自己/对方区别。

中线法失效(ListItem rect 是整行)。打开有来回消息的好友,dump 最近消息 ListItem 的
rect/class_name/auto_id/子元素,找出区分自己(右)vs 对方(左)的可靠特征。

用法:把 FRIEND 改成有来回消息的好友(自己+对方都有),python -u test_group_monitor.py
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pyweixin import GlobalConfig  # noqa: E402
from pyweixin.WeChatTools import Navigator  # noqa: E402
from pyweixin.Uielements import Lists, SideBar  # noqa: E402

FRIEND = '李铎TS'  # 改成有来回消息的好友


def main():
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    print(f'>>> 打开 [{FRIEND}] ...')
    mw = Navigator.open_weixin(is_maximize=False)
    mw.child_window(**SideBar.Weixin).click_input()
    time.sleep(0.4)
    main_window = Navigator.open_dialog_window(friend=FRIEND, search_pages=5)
    chat_list = main_window.child_window(**Lists.FriendChatList)
    if not chat_list.exists(timeout=2):
        print('!! 消息列表未出现')
        return
    chat_list.type_keys('{END}')
    time.sleep(0.5)
    cl = chat_list.rectangle()
    list_cx = (cl.left + cl.right) // 2
    print(f'chat_list rect=({cl.left},{cl.top},{cl.right},{cl.bottom}) center_x={list_cx}\n')
    items = chat_list.children(control_type='ListItem')
    print(f'共 {len(items)} 条，dump 最后 12 条:\n')
    for it in items[-12:]:
        try:
            r = it.rectangle()
            cls = it.class_name()
            try:
                aid = it.element_info.automation_id
            except Exception:
                aid = ''
            wt = (it.window_text() or '').replace('\n', ' ')[:24]
            cx = (r.left + r.right) // 2
            side = '右' if cx > list_cx else '左'
            print(f'rect=({r.left},{r.top},{r.right},{r.bottom}) w={r.right - r.left} cx={cx} {side} class={cls} auto_id={aid!r}')
            print(f'   text={wt!r}')
            try:
                chs = it.children()
                for ch in chs:
                    try:
                        cr = ch.rectangle()
                        cc = ch.class_name()
                        print(f'   child rect=({cr.left},{cr.top},{cr.right},{cr.bottom}) w={cr.right - cr.left} class={cc}')
                    except Exception:
                        pass
            except Exception:
                pass
            print()
        except Exception as e:
            print(f'异常: {e}')


if __name__ == '__main__':
    main()
