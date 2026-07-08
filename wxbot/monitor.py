# -*- coding: utf-8 -*-
"""消息主循环（双轨监听）。

直接照搬用户已验证的 `test_global_monitor_simple.global_monitor_simple` 结构：
  ① 轮询"当前停留会话"的聊天列表（无未读红点也能捕获）
  ② get_new_message_num > 0 → scan_for_new_messages 扫红点好友
     → _find_and_click_session 翻页点击 → read_chat_messages → classify_message
在此基础上注入：
  - config 驱动的监听过滤（白名单/全局）
  - reply_engine 决定回复（关键词 / 只监听 / AI 占位）
  - /指令 处理（仅 admin）
  - 自定义转发骨架
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional

import pyautogui
from pyweixin import Navigator, Messages
from pyweixin.Uielements import Main_window, SideBar, Lists, Edits
from pyweixin.utils import scan_for_new_messages, get_new_message_num, classify_message
from pyweixin.WinSettings import SystemSettings

from . import commands
from .config import bot_config
from .logger import log
from .input_blocker import input_blocker
from .mqtt.worker import mqtt_worker
from .reply import reply_engine, is_listened_chat, match_forward, human_delay, split_long_text


# ---------------------------------------------------------------------------
# 兼容性：群聊判定（阶段一：按是否在 group 列表判定）
# ---------------------------------------------------------------------------
def _is_group(chat: str) -> bool:
    return chat in bot_config.get("group", [])


def _parse_hhmm(s: str) -> int | None:
    """解析 HH:MM 为当日分钟数(0~1439)，失败返回 None。"""
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 当前窗口直接发送（移植 test_global_monitor_simple.send_message_in_current_window）
# ---------------------------------------------------------------------------
def send_in_current_window(main_window, message: str) -> bool:
    edit_area = main_window.child_window(**Edits.CurrentChatEdit)
    if not edit_area.exists(timeout=0.5):
        log.warning("找不到当前聊天输入框，跳过发送")
        return False
    try:
        edit_area.set_text("")
        SystemSettings.copy_text_to_clipboard(message)
        pyautogui.hotkey("ctrl", "v", _pause=False)
        time.sleep(0.3)
        pyautogui.hotkey("alt", "s", _pause=False)
        time.sleep(0.5)
        return True
    except Exception as e:
        log.error(f"当前窗口发送失败: {e}")
        return False


def _send_to_chat(main_window, chat: str, messages: list[str], current_friend: Optional[str]) -> bool:
    """优先当前窗口直发；若目标不是当前停留会话，则用 Messages.send_messages_to_friend。"""
    if current_friend and chat == current_friend:
        ok = True
        for seg in messages:
            human_delay()
            ok = send_in_current_window(main_window, seg) and ok
        return ok
    # 跨会话发送
    try:
        human_delay()
        Messages.send_messages_to_friend(friend=chat, messages=messages, close_weixin=False)
        return True
    except Exception as e:
        log.error(f"发送给 {chat} 失败: {e}")
        return False


# ---------------------------------------------------------------------------
# 会话翻页查找点击（移植 test_global_monitor_simple._find_and_click_session）
# ---------------------------------------------------------------------------
def _find_and_click_session(session_list, friend, max_pages: int = 10) -> bool:
    session_list.type_keys("{HOME}")
    time.sleep(0.2)
    prev_last = None
    for _ in range(max_pages):
        items = session_list.children(control_type="ListItem")
        for item in items:
            if friend in item.window_text():
                item.click_input()
                return True
        cur_last = items[-1].window_text() if items else ""
        if cur_last == prev_last:
            break
        prev_last = cur_last
        session_list.type_keys("{PGDN}")
        time.sleep(0.2)
    session_list.type_keys("{HOME}")
    return False


def read_chat_messages(main_window, number: int = 5) -> list[tuple]:
    chat_list = main_window.child_window(**Lists.FriendChatList)
    if not chat_list.exists(timeout=1):
        return []
    items = chat_list.children(control_type="ListItem")
    out = []
    i = len(items) - number
    while i < len(items):
        if i < 0:
            i += 1
            continue
        item = items[i]
        display, mtype, mpath = classify_message(item)
        # 语音消息：检查下一条是否为转文字内容
        if mtype == "语音" and i + 1 < len(items):
            next_display, next_mtype, _ = classify_message(items[i + 1])
            if next_mtype == "文本" and next_display:
                display = f"{display} | 转文字: {next_display}"
                i += 1  # 跳过下一条（已合并）
        out.append((display, mtype, mpath, item))
        i += 1
    return out


def _is_self_message(item, main_window) -> bool:
    """自己（机器人）发的消息气泡靠右、对方靠左。

    ListItem 的 UIA rect 是整行（自己/对方 rect/class/auto_id 完全相同，且无子元素），
    无法用 rect/属性区分方向。只能截图判断：裁出 ListItem 区域，背景=该区域最常见颜色，
    中间行里非背景像素（=气泡+文字+头像）的水平中心 > 区域中线 → 自己（右），否则对方（左）。
    系统消息（时间戳）非背景像素极少，返回 False（不当自己），且 _process_one 已先过滤。
    """
    try:
        import numpy as np
        r = item.rectangle()
        crop = np.array(pyautogui.screenshot().crop((r.left, r.top, r.right, r.bottom)))
        if crop.size == 0:
            return False
        h, w = crop.shape[:2]
        # 背景 = 区域最常见颜色（背景面积大于气泡）；下采样加速
        small = crop[::4, ::4]
        colors, counts = np.unique(small.reshape(-1, small.shape[-1]), axis=0, return_counts=True)
        bg = colors[counts.argmax()].astype(int)
        # 只看左右边缘窄带（头像所在侧），排除中部气泡干扰
        # （图片消息气泡很大、会污染"左右半"判断，导致对方图片误判为自已）
        diff_all = np.abs(crop.astype(int) - bg).sum(axis=2)
        edge = max(40, w // 10)
        left_n = int((diff_all[:, :edge] > 40).sum())
        right_n = int((diff_all[:, -edge:] > 40).sum())
        txt = (item.window_text() or '').replace('\n', ' ')[:20]
        total = left_n + right_n
        if total < 20:
            log.info(f"[消息判断] {txt!r} 非背景像素={total} 判=对方(像素少)")
            return False  # 几乎纯背景（时间戳/空行），不当自己
        is_self = right_n > left_n  # 头像/内容在右 → 自己
        log.info(f"[消息判断] {txt!r} bg={bg.tolist()} 左非背景={left_n} 右非背景={right_n} 判={'自己' if is_self else '对方'}")
        return is_self
    except Exception as e:
        log.warning(f"[消息判断] 判断异常: {e}")
        return False


# ---------------------------------------------------------------------------
# 单条消息处理
# ---------------------------------------------------------------------------
def _is_system_greeting(text: str) -> bool:
    """对方通过好友验证后微信自动发的系统问候,不该当对话内容转发(否则与模拟通知重复)。"""
    kws = ("我通过了你的朋友验证请求", "已通过你的朋友验证请求", "已通过",
           "现在可以开始聊天", "已添加", "accepted")
    return any(k in (text or '') for k in kws)


def _confirm_transfer(main_window, msg_item, chat: str) -> bool:
    """确认收款好友转账。返回是否收款成功。

    转账消息文本含"待你收款"（待收款状态）。点击转账消息弹出独立详情窗口 →
    坐标循环点击"收款"按钮 → 检测确认弹窗 → 点击确认。
    """
    from pywinauto import Desktop
    try:
        # 点击转账消息打开详情窗口
        msg_item.click_input()
        time.sleep(1.5)
        # 详情窗口是独立弹出，UIA 找不到"收款"按钮（mmui 自绘），用坐标点击
        desktop = Desktop(backend='uia')
        detail = None
        detail_cn = ''
        for w in desktop.windows():
            try:
                if not w.is_visible():
                    continue
                cn = w.element_info.class_name or ''
                # 只匹配 mmui:: 开头的窗口，避免误选浏览器/记事本等
                if cn.startswith('mmui::') and cn != 'mmui::MainWindow':
                    detail = w
                    detail_cn = cn
                    break
            except Exception:
                continue
        if not detail:
            log.warning(f"[转账收款] {chat} 详情窗口未弹出")
            return False
        r = detail.rectangle()
        log.info(f"[转账收款] 详情窗口 rect=({r.left},{r.top},{r.right},{r.bottom})")
        click_x = (r.left + r.right) // 2
        # "收款"按钮在窗口下半部，mmui 自绘 UIA 不可见，用坐标循环点击尝试
        for offset in range(80, 500, 20):  # bottom-80 到 bottom-480，逐步上移
            click_y = r.bottom - offset
            pyautogui.click(click_x, click_y)
            time.sleep(1.0)
            # 点中"收款"会弹出确认框，检测是否有新的 mmui 弹出窗口
            try:
                for w2 in Desktop(backend='uia').windows():
                    try:
                        if not w2.is_visible():
                            continue
                        wcn = w2.element_info.class_name or ''
                        if wcn.startswith('mmui::') and wcn != 'mmui::MainWindow' and wcn != detail_cn:
                            # 新弹出窗口 = 确认框，点击其中心确认
                            cr = w2.rectangle()
                            log.info(f"[转账收款] 确认弹窗 rect=({cr.left},{cr.top},{cr.right},{cr.bottom})")
                            pyautogui.click((cr.left + cr.right) // 2, (cr.top + cr.bottom) // 2)
                            time.sleep(0.5)
                            log.info(f"[转账收款] 收款成功: {chat} (offset={offset})")
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
        log.warning(f"[转账收款] {chat} 循环点击未命中收款按钮")
        pyautogui.press('esc')
        return False
    except Exception as e:
        log.warning(f"[转账收款] 异常: {chat} -> {e}")
        try:
            pyautogui.press('esc')
        except Exception:
            pass
        return False


def _open_red_packet(main_window, msg_item, chat: str) -> bool:
    """拆开好友红包。点击红包消息 → 聊天对话区域中心弹出红包详情 → 从中心向下点"开"按钮。"""
    try:
        # 点击红包消息，红包详情会显示在聊天对话区域正中心
        msg_item.click_input()
        time.sleep(1.5)
        # 以聊天消息列表区域（FriendChatList）中心为基准
        chat_list = main_window.child_window(**Lists.FriendChatList)
        if not chat_list.exists(timeout=1):
            log.warning(f"[红包] {chat} 找不到聊天消息列表")
            return False
        r = chat_list.rectangle()
        center_x = (r.left + r.right) // 2
        center_y = (r.top + r.bottom) // 2
        log.info(f"[红包] 聊天区域 rect=({r.left},{r.top},{r.right},{r.bottom}) 中心=({center_x},{center_y})")
        # "开"按钮在中心点偏下，逐步下移点击尝试
        clicked = False
        for offset in range(200, 300, 20):
            click_y = center_y + offset
            if click_y >= r.bottom:
                break
            log.info(f"[红包] 尝试点击 ({center_x}, {click_y}) offset=+{offset}")
            pyautogui.click(center_x, click_y)
            clicked = True
            time.sleep(0.5)
        if not clicked:
            log.warning(f"[红包] {chat} 聊天区域太小，无法点击开按钮")
            pyautogui.press('esc')
            return False
        log.info(f"[红包] 点击完成，按Esc退出")
        pyautogui.press('esc')
        return True
    except Exception as e:
        log.warning(f"[红包] 异常: {chat} -> {e}")
        try:
            pyautogui.press('esc')
        except Exception:
            pass
        return False


def _clear_pending_if_match(name: str, sender: str = None,
                             text: str = None, msg_type: str = None) -> bool:
    """若 name 匹配某个待通过好友:模拟转发"已通过请求" + 移除标记 + 异步查资料卡
    拿微信号后,再用微信号作 targetId 转发原消息,返回 True。

    对方通过后主动发来消息(带红点,被 ② 处理):先模拟"已通过好友请求"通知,
    再异步打开资料卡获取微信号(写缓存),然后用微信号 targetId 转发原消息。
    资料卡查询是耗时 UI 操作,放异步线程(等 monitor 释放 UI 锁后执行),不阻塞主循环。
    """
    if not name:
        return False
    try:
        from .pending_friends import load_pending, remove_pending
        for p in load_pending():
            m = p.get("match", "")
            if m and (m == name or m in name or name in m):
                # 先移除待通过标记：避免下方模拟转发触发 on_wechat_message 的 is_new_friend
                # 再查一次资料卡（资料卡查两遍的根因）
                remove_pending(m)
                # 模拟好友通过通知转发 MQTT(延迟10秒,更自然)
                time.sleep(10)
                try:
                    mqtt_worker.on_wechat_message(
                        chat=m, sender=m,
                        content="我通过了你的朋友验证请求，现在我们可以开始聊天了",
                        msg_type="文本")
                    log.info(f"[新好友通过] {m} 主动发来消息,已立即模拟转发")
                except Exception as e:
                    log.error(f"[新好友通过] 模拟转发 {m} 失败: {e}")
                # 异步:查资料卡拿微信号(写缓存);若原消息非系统问候,再用微信号 targetId 转发
                if text:
                    _sender = sender or m
                    _text = text
                    _mtype = msg_type or "文本"

                    def _delayed(_m=m, _sender=_sender, _text=_text, _mtype=_mtype):
                        try:
                            wxid = mqtt_worker._fetch_wxid_from_profile(_m)
                            log.info(f"[新好友] 资料卡查得 {_m} 微信号={wxid}")
                        except Exception as e:
                            log.error(f"[新好友] 查资料卡失败 {_m}: {e}")
                        # 系统问候(对方通过验证的微信系统消息）不当对话内容转发,避免与上面模拟通知重复
                        if _is_system_greeting(_text):
                            log.info(f"[新好友] {_m} 首条为系统问候,跳过转发原消息")
                            return
                        try:
                            mqtt_worker.on_wechat_message(
                                chat=_m, sender=_sender, content=_text, msg_type=_mtype)
                        except Exception as e:
                            log.error(f"[新好友] 延迟转发原消息失败 {_m}: {e}")

                    threading.Thread(target=_delayed, daemon=True,
                                     name=f"newfwd-{m[:8]}").start()
                return True
    except Exception:
        pass
    return False


def _process_one(main_window, chat: str, sender: str, text: str,
                 msg_type: str, current_friend: Optional[str],
                 processed: set[str], file_path: str | None = None,
                 msg_item=None) -> None:
    if msg_type == "系统消息":
        # 好友通过验证的系统消息(带红点)忽略,统一由 ③ _check_pending_friends 模拟转发
        if any(kw in text for kw in ("已通过", "现在可以开始聊天", "已添加", "accepted")):
            log.info(f"[系统消息] 好友通过验证,忽略红点,由 pending 机制统一模拟转发")
        return
    if msg_type in ("被拉黑", "被删除"):
        log.warning(f"⚠️ {chat} 可能{msg_type}: {text!r}")
        processed.add(f"{chat}:{msg_type}:{text}")
        return

    is_group = _is_group(chat)

    # 跳过自己（机器人）发的消息：气泡靠右。classify_message 不分方向，靠 rect 判断，
    # 避免把自己的回复/转发误当成对方新消息再处理（转发/回复/群监控）
    if msg_item is not None and _is_self_message(msg_item, main_window):
        return

    # /指令（仅 admin，不受监听过滤限制）
    if text.startswith("/") and commands.is_admin(sender):
        reply = commands.handle(text)
        processed.add(f"{chat}:CMD:{text}")  # 无论是否有回复都去重，避免每轮重复执行
        if reply:
            _send_to_chat(main_window, chat, split_long_text(reply), current_friend)
        return

    msg_id = f"{chat}:{msg_type}:{text}"
    # 媒体消息(图片/视频/文件)text 可能相同(如多张图片都是"图片")，用 runtime_id 区分避免误去重
    if msg_item is not None and msg_type in ("图片", "视频", "文件"):
        try:
            msg_id = f"{chat}:{msg_type}:{msg_item.element_info.runtime_id}"
        except Exception:
            pass
    if msg_id in processed:
        return
    processed.add(msg_id)

    log.info(f"[收到] {chat}({sender}) [{msg_type}]: {text!r}")

    # 转账自动收款（收到好友转账 → 确认收款 → 飞书提醒）
    if msg_type == "微信转账" and bot_config.get("auto_collect_transfer", False) and msg_item is not None:
        if _confirm_transfer(main_window, msg_item, chat):
            log.info(f"[转账收款] 已确认收款: {chat}")
            try:
                from . import webhook_send
                webhook_send.send_webhook(
                    title=f"【转账收款】{chat}",
                    content=f"已确认收款\n来源: {chat}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as e:
                log.warning(f"[转账收款] 飞书提醒失败: {e}")

    # 红包自动拆开（收到红包 → 点"开" → 飞书提醒）
    if msg_type == "微信红包" and bot_config.get("auto_open_red_packet", False) and msg_item is not None:
        if _open_red_packet(main_window, msg_item, chat):
            log.info(f"[红包] 已拆开: {chat}")
            try:
                from . import webhook_send
                webhook_send.send_webhook(
                    title=f"【红包拆开】{chat}",
                    content=f"已拆开红包\n来源: {chat}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as e:
                log.warning(f"[红包] 飞书提醒失败: {e}")

    # 群消息关键词监控(命中 → 点头像读真实发送人 → 转发;独立于监听白名单)
    # 不依赖 is_group:match_group_monitor 自带群匹配,私聊/非配置群直接返回 False
    if msg_item is not None:
        if _group_monitor_forward(main_window, chat, sender, text, msg_item, processed):
            return  # 命中并已转发,不再走 MQTT/回复,避免重复

    # 若是待通过好友发来消息,忽略(不转发不回复),统一由 ③ pending 机制模拟转发
    if _clear_pending_if_match(chat, sender=sender, text=text, msg_type=msg_type):
        return

    # 监听过滤：白名单/黑名单同时控制本地回复和 MQTT 转发
    if not is_listened_chat(chat, is_group):
        return

    # MQTT 数字员工：转发到上游 OpenClaw
    forwarded = False
    if mqtt_worker.enabled:
        try:
            forwarded = mqtt_worker.on_wechat_message(chat, sender, text, msg_type, file_path=file_path)
        except Exception as e:
            log.error(f"[MQTT转发] 异常: {e}")
    # 转发成功且配置跳过本地回复 → 仅执行自定义转发后返回
    if forwarded and bot_config.get("mqtt_worker", {}).get("skip_local_reply_when_forwarded", True):
        _do_custom_forward(main_window, chat, sender, text, is_group, current_friend)
        return

    # 决定回复
    reply_msgs = reply_engine.decide_reply(chat, sender, text, msg_type, is_group)
    if reply_msgs:
        _send_to_chat(main_window, chat, reply_msgs, current_friend)
        processed.add(f"{chat}:REPLY:{reply_msgs[0]}")
        log.info(f"[已回复] {chat}: {reply_msgs[0]!r}")

    # 自定义转发（在 AI/关键词回复之后执行）
    _do_custom_forward(main_window, chat, sender, text, is_group, current_friend)


def _do_custom_forward(main_window, chat: str, sender: str, text: str,
                       is_group: bool, current_friend) -> None:
    """执行本地自定义转发规则（custom_forward_list）。"""
    for tgt, with_src in match_forward(chat, sender, text, is_group):
        try:
            human_delay()
            forward_text = f"来源窗口：{chat}，发送人：{sender}\n{text}" if with_src else text
            Messages.send_messages_to_friend(friend=tgt, messages=[forward_text], close_weixin=False)
            time.sleep(1)  # 多目标间隔 1 秒
            log.info(f"[转发] {chat} → {tgt}")
        except Exception as e:
            log.error(f"[转发] → {tgt} 失败: {e}")


def _group_monitor_forward(main_window, chat: str, sender: str, text: str,
                           msg_item, processed: set[str]) -> bool:
    """群消息关键词监控:命中 → 点头像读真实发送人 → 转发到配置目标。

    独立于监听白名单,按 group_monitor_list 配置的群+关键词触发。
    read_group_sender 为耗时 UI 操作(持 UI 锁 + bot_active 由 run_once 保证放行)。
    Returns: True=命中并已转发;False=未命中。
    """
    from .group_monitor import match_group_monitor, read_group_sender
    targets = match_group_monitor(chat, text)
    if not targets:
        return False
    gmon_id = f"{chat}:GMON:{text}"
    if gmon_id in processed:
        return False
    processed.add(gmon_id)
    sender_real = ""
    try:
        sender_real = read_group_sender(msg_item)
    except Exception as e:
        log.warning(f"[群监控] 读发送人失败: {e}")
    sender_real = sender_real or sender
    fwd = f"【群消息】{chat}\n发送人：{sender_real}\n{text}"
    for tgt in targets:
        try:
            human_delay()
            Messages.send_messages_to_friend(friend=tgt, messages=[fwd], close_weixin=False)
            log.info(f"[群监控] {chat}({sender_real}) → {tgt}: {text[:40]!r}")
            time.sleep(1)  # 多目标间隔
        except Exception as e:
            log.error(f"[群监控] 转发 {tgt} 失败: {e}")
    return True


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
class _BoundedSet:
    """大小受限的去重集合，基于 OrderedDict 实现 LRU 淘汰。"""
    def __init__(self, maxsize: int = 5000) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxsize = maxsize

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def add(self, key: str) -> None:
        if key in self._data:
            self._data.move_to_end(key)  # 刷新为最近使用
        else:
            self._data[key] = None
            if len(self._data) > self._maxsize:
                self._data.popitem(last=False)  # 淘汰最早的


class Monitor:
    def __init__(self, check_interval: float = 10.0) -> None:
        self.check_interval = check_interval
        self._run_timeout = 30.0
        self._stop = threading.Event()
        self._last_loop_alert = ("", 0.0)  # (异常文本, 时间戳)，节流防刷屏
        self.processed: _BoundedSet = _BoundedSet(maxsize=5000)
        self.current_friend: Optional[str] = None
        self.current_last_rid = None
        self._first_run = True
        self._last_pending_scan = 0.0  # 待通过好友扫描节流时间戳

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        # 获取 UI 互斥锁：与 MQTT 任务/异步媒体线程互斥，避免抢鼠标
        ui_lock = mqtt_worker.ui_lock
        if ui_lock and not ui_lock.acquire(timeout=0.5):
            log.info("UI 锁被占用，跳过本轮消息轮询")
            return
        input_blocker.set_bot_active(True)  # 放行机器人点击
        # 标记 monitor 正在处理消息（检测→转发MQTT），executor 应等待完成后再操作UI
        if mqtt_worker._wx_busy_event:
            mqtt_worker._wx_busy_event.set()
        try:
            self._run_once_locked()
        finally:
            if mqtt_worker._wx_busy_event:
                mqtt_worker._wx_busy_event.clear()
            input_blocker.set_bot_active(False)
            if ui_lock:
                try:
                    ui_lock.release()
                except RuntimeError:
                    pass

    def _run_once_locked(self) -> None:
        main_window = Navigator.open_weixin(is_maximize=False)
        main_window.child_window(**SideBar.Weixin).click_input()
        time.sleep(0.3)

        # 首次启动默认切换到文件传输助手
        if self._first_run:
            self._first_run = False
            try:
                session_list = main_window.child_window(**Main_window.SessionList)
                session_list.type_keys("{HOME}")
                time.sleep(0.2)
                for _ in range(30):
                    items = session_list.children(control_type="ListItem")
                    for item in items:
                        if "文件传输助手" in item.window_text():
                            item.click_input()
                            log.info("启动默认切换 → 文件传输助手")
                            time.sleep(0.2)
                            return
                    session_list.type_keys("{PGDN}")
                    time.sleep(0.1)
            except Exception as e:
                log.warning(f"切换到文件传输助手失败: {e}")

        # ① 轮询当前停留会话（不依赖未读红点）
        if self.current_friend is not None:
            chat_list = main_window.child_window(**Lists.FriendChatList)
            if chat_list.exists(timeout=0.5):
                items = chat_list.children(control_type="ListItem")
                if items:
                    last_rid = items[-1].element_info.runtime_id
                    if self.current_last_rid is None:
                        self.current_last_rid = last_rid
                    elif last_rid != self.current_last_rid:
                        # 在当前聊天列表里定位旧基线 runtime_id
                        new_items = []
                        base_found = False
                        for idx, it in enumerate(items):
                            if it.element_info.runtime_id == self.current_last_rid:
                                new_items = items[idx + 1:]
                                base_found = True
                                break
                        if not base_found:
                            # 旧基线不在当前聊天列表 → 当前停留会话已不是 current_friend
                            # (用户手动切换了聊天窗口，或 runtime_id 失效)。
                            # 此时绝不能把新会话的历史消息当新消息转发——重置基线并暂停 ①，
                            # 等待 ② 红点机制重新锁定会话。
                            log.info("[监听] 当前会话与记录的不一致(疑似切换聊天窗口)，重置基线，暂停当前会话轮询")
                            self.current_friend = None
                            self.current_last_rid = None
                        else:
                            for item in new_items:
                                msg_text, msg_type, file_path = classify_message(item)
                                _process_one(main_window, self.current_friend,
                                             self.current_friend, msg_text, msg_type,
                                             self.current_friend, self.processed, file_path=file_path,
                                             msg_item=item)
                            items2 = chat_list.children(control_type="ListItem")
                            self.current_last_rid = items2[-1].element_info.runtime_id if items2 else last_rid

        # ② 扫描带未读红点的会话
        new_num = get_new_message_num(main_window, close_weixin=False)
        if new_num > 0:
            log.info(f"检测到 {new_num} 条新消息")
            new_msg_dict = scan_for_new_messages(main_window=main_window, is_maximize=False, close_weixin=False)
            for friend, num in new_msg_dict.items():
                main_window.child_window(**SideBar.Weixin).click_input()
                time.sleep(0.5)
                session_list = main_window.child_window(**Main_window.SessionList)
                if not _find_and_click_session(session_list, friend):
                    log.warning(f"未找到 {friend} 的会话")
                    continue
                time.sleep(1)
                msgs = read_chat_messages(main_window, number=num)
                for msg_text, msg_type, file_path, msg_item in msgs:
                    _process_one(main_window, friend, friend, msg_text, msg_type,
                                 self.current_friend, self.processed, file_path=file_path,
                                 msg_item=msg_item)
                # 记录为当前停留会话
                chat_list = main_window.child_window(**Lists.FriendChatList)
                chat_items = chat_list.children(control_type="ListItem")
                self.current_friend = friend
                self.current_last_rid = chat_items[-1].element_info.runtime_id if chat_items else None

        # ③ 待通过好友主动检测(不依赖红点,主动遍历会话列表;有 pending 才执行)
        self._check_pending_friends(main_window)

    def _check_pending_friends(self, main_window) -> None:
        """主动遍历会话列表,发现待通过好友出现则模拟'已通过好友请求'转发 MQTT。

        节流(默认60s)+ 只读 window_text 不点击,避免和 ①② 抢 UI/改变停留会话。
        冲突保障:开头HOME、结束HOME、全程不 click_input 会话条目。
        """
        from .pending_friends import load_pending, remove_pending
        pending = load_pending()
        if not pending:
            return
        # 节流
        interval = float(bot_config.get("monitor_pending_interval", 60) or 60)
        now = time.time()
        if now - self._last_pending_scan < interval:
            return
        self._last_pending_scan = now
        matches = [p.get("match") for p in pending if p.get("match")]
        if not matches:
            return
        hit: set[str] = set()
        try:
            session_list = main_window.child_window(**Main_window.SessionList)
            if not session_list.exists(timeout=0.5):
                return
            session_list.type_keys("{HOME}")
            time.sleep(0.2)
            try:
                items = session_list.children(control_type="ListItem")
            except Exception:
                items = []
            for item in items[:10]:  # 只看会话列表前10个(最近的会话)
                try:
                    wt = item.window_text() or ""
                except Exception:
                    continue
                for m in matches:
                    if m not in hit and m in wt:
                        hit.add(m)
            session_list.type_keys("{HOME}")  # 复位
        except Exception as e:
            log.error(f"待通过好友扫描异常: {e}")
        # 命中的逐个模拟转发(遍历结束后统一处理,不并发)
        for m in hit:
            try:
                mqtt_worker.on_wechat_message(
                    chat=m, sender=m,
                    content="我通过了你的朋友验证请求，现在我们可以开始聊天了",
                    msg_type="文本")
                remove_pending(m)
                log.info(f"[新好友通过] {m} 出现在会话列表,已模拟通知并转发 MQTT")
            except Exception as e:
                log.error(f"[新好友通过] 模拟转发 {m} 失败: {e}")

    def loop(self) -> None:
        self._stop.clear()  # 重置停止标志，支持 stop() 后再次启动
        self.check_interval = float(bot_config.get("monitor_check_interval", 10) or 10)
        self._run_timeout = float(bot_config.get("monitor_run_timeout", 30) or 30)
        log.info(f"📨 消息主循环启动（轮询间隔 {self.check_interval}s，单轮超时 {self._run_timeout}s）")
        in_pause = False
        try:
            while not self._stop.is_set():
                if self._in_pause_period():
                    if not in_pause:
                        in_pause = True
                        log.info(f"📨 进入消息监听暂停时段"
                                 f"(停止 {bot_config.get('everyday_stop_bot_time')} ~ 恢复 {bot_config.get('everyday_start_bot_time')})，停止轮询")
                    self._stop.wait(self.check_interval)
                    continue
                if in_pause:
                    in_pause = False
                    log.info("📨 暂停时段结束，恢复消息监听")
                self._run_once_guarded()
                self._stop.wait(self.check_interval)
        except KeyboardInterrupt:
            self._stop.set()
        log.info("📨 消息主循环已停止")

    def _in_pause_period(self) -> bool:
        """是否在消息监听暂停时段（everyday_stop_bot_time ~ everyday_start_bot_time，可跨夜）。"""
        if not bot_config.get("everyday_start_stop_bot_switch", False):
            return False
        start = _parse_hhmm(bot_config.get("everyday_start_bot_time", ""))  # 恢复监听时间
        stop = _parse_hhmm(bot_config.get("everyday_stop_bot_time", ""))    # 停止监听时间
        if start is None or stop is None or start == stop:
            return False
        now = time.localtime()
        now_min = now.tm_hour * 60 + now.tm_min
        # 停止时段 = stop_time ~ start_time
        if stop < start:
            return stop <= now_min < start  # 同日,如 01:00~08:00
        return now_min >= stop or now_min < start  # 跨夜,如 23:00~次日08:00

    def _run_once_guarded(self) -> None:
        """单轮 run_once 放入子线程执行，超时则放弃本轮。

        pywinauto 的 click_input/type_keys 是前台操作，与微信自身或用户操作冲突时
        会 COM 死锁（曾观测到单轮卡死 9 分钟）。用线程 join(timeout) 兜底，
        保证主循环不被拖垮、日志持续、能响应停止。卡死的子线程为守护线程，
        随进程退出回收（Python 无法强制 kill 线程）。
        """
        t = threading.Thread(target=self._run_once_safe, daemon=True, name="MonitorRunOnce")
        t.start()
        t.join(timeout=self._run_timeout)
        if t.is_alive():
            log.warning(f"⚠️ 单轮处理超时（{self._run_timeout}s），放弃本轮 — 疑似 UI 操作卡死")

    def _run_once_safe(self) -> None:
        try:
            self.run_once()
        except Exception as e:
            log.error(f"主循环异常: {e}")
            self._alert_loop_exception(e)

    def _alert_loop_exception(self, e: Exception) -> None:
        """主循环异常推飞书：夜间(23-6点)静默，同异常文本 1 小时内只推 1 次。"""
        now = time.time()
        # 夜间静默：23:00 ~ 次日 6:00 不推送
        hour = time.localtime(now).tm_hour
        if hour >= 23 or hour < 6:
            return
        msg = str(e)
        last_msg, last_ts = self._last_loop_alert
        if msg == last_msg and now - last_ts < 3600:
            return  # 同异常 1 小时内已推过，跳过
        self._last_loop_alert = (msg, now)
        nickname = getattr(mqtt_worker, "_wx_nickname", "") or "未知"
        try:
            from . import webhook_send
            webhook_send.send_webhook(
                title=f"【{nickname}】微信机器人异常",
                content=f"异常: {msg}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            )
        except Exception as we:
            log.error(f"主循环异常 webhook 推送失败: {we}")


# 全局单例
monitor = Monitor()
