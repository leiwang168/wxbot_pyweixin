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
from typing import Optional

import pyautogui
from pyweixin import Navigator, Messages
from pyweixin.Uielements import Main_window, SideBar, Lists, Edits
from pyweixin.utils import scan_for_new_messages, get_new_message_num, classify_message
from pyweixin.WinSettings import SystemSettings

from . import commands
from .config import bot_config
from .logger import log
from .mqtt.worker import mqtt_worker
from .reply import reply_engine, is_listened_chat, match_forward, human_delay, split_long_text


# ---------------------------------------------------------------------------
# 兼容性：群聊判定（阶段一：按是否在 group 列表判定）
# ---------------------------------------------------------------------------
def _is_group(chat: str) -> bool:
    return chat in bot_config.get("group", [])


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
def _find_and_click_session(session_list, friend, max_pages: int = 60) -> bool:
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


def read_chat_messages(main_window, number: int = 5) -> list[tuple[str, str, str]]:
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
        out.append((display, mtype, mpath))
        i += 1
    return out


# ---------------------------------------------------------------------------
# 单条消息处理
# ---------------------------------------------------------------------------
def _process_one(main_window, chat: str, sender: str, text: str,
                 msg_type: str, current_friend: Optional[str],
                 processed: set[str], file_path: str | None = None) -> None:
    if msg_type == "系统消息":
        # 好友通过验证消息 → 主动触发新好友回调
        if any(kw in text for kw in ("已通过", "现在可以开始聊天", "已添加", "accepted")):
            log.info(f"[新好友回调] 检测到好友通过: {text[:80]}")
            try:
                from .friends import check_and_pass_once
                check_and_pass_once()
            except Exception as e:
                log.error(f"[新好友回调] 异常: {e}")
        return
    if msg_type in ("被拉黑", "被删除"):
        log.warning(f"⚠️ {chat} 可能{msg_type}: {text!r}")
        processed.add(f"{chat}:{msg_type}:{text}")
        return

    is_group = _is_group(chat)

    # /指令（仅 admin，不受监听过滤限制）
    if text.startswith("/") and commands.is_admin(sender):
        reply = commands.handle(text)
        if reply:
            _send_to_chat(main_window, chat, split_long_text(reply), current_friend)
            processed.add(f"{chat}:CMD:{text}")
        return

    msg_id = f"{chat}:{msg_type}:{text}"
    if msg_id in processed:
        return
    processed.add(msg_id)

    log.info(f"[收到] {chat}({sender}) [{msg_type}]: {text!r}")

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


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, check_interval: float = 10.0) -> None:
        self.check_interval = check_interval
        self._run_timeout = 30.0
        self._stop = threading.Event()
        self.processed: set[str] = set()
        self.current_friend: Optional[str] = None
        self.current_last_rid = None
        self._first_run = True

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        # MQTT 任务执行中（发送消息等），跳过本轮避免 UI 冲突
        if mqtt_worker.wx_busy:
            log.info("MQTT 任务执行中，跳过本轮消息轮询")
            return

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
                        new_items = []
                        for idx, it in enumerate(items):
                            if it.element_info.runtime_id == self.current_last_rid:
                                new_items = items[idx + 1:]
                                break
                        if not new_items:
                            new_items = [items[-1]]
                        for item in new_items:
                            msg_text, msg_type, file_path = classify_message(item)
                            _process_one(main_window, self.current_friend,
                                         self.current_friend, msg_text, msg_type,
                                         self.current_friend, self.processed, file_path=file_path)
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
                for msg_text, msg_type, file_path in msgs:
                    _process_one(main_window, friend, friend, msg_text, msg_type,
                                 self.current_friend, self.processed, file_path=file_path)
                # 记录为当前停留会话
                chat_list = main_window.child_window(**Lists.FriendChatList)
                chat_items = chat_list.children(control_type="ListItem")
                self.current_friend = friend
                self.current_last_rid = chat_items[-1].element_info.runtime_id if chat_items else None

    def loop(self) -> None:
        self.check_interval = float(bot_config.get("monitor_check_interval", 10) or 10)
        self._run_timeout = float(bot_config.get("monitor_run_timeout", 30) or 30)
        log.info(f"📨 消息主循环启动（轮询间隔 {self.check_interval}s，单轮超时 {self._run_timeout}s）")
        try:
            while not self._stop.is_set():
                self._run_once_guarded()
                self._stop.wait(self.check_interval)
        except KeyboardInterrupt:
            self._stop.set()
        log.info("📨 消息主循环已停止")

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


# 全局单例
monitor = Monitor()
