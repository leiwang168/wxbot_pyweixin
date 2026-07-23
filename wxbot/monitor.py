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
from .wx_dialog import dismiss_wx_dialog
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
        time.sleep(0.5)
        try:
            edit_area.set_focus()  # Alt+S 前确保焦点在输入框,避免焦点丢失导致发送无效
        except Exception:
            pass
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


def _voice_message_delay_seconds() -> float:
    try:
        return float(bot_config.get("voice_message_delay", 5) or 0)
    except (TypeError, ValueError):
        return 0.0


def _refresh_list_item(chat_list, item):
    """等待后按 runtime_id 重新获取同一条消息，避免读到等待前的 UIA 对象。"""
    try:
        rid = item.element_info.runtime_id
    except Exception:
        rid = None
    if not rid:
        return item
    try:
        for candidate in chat_list.children(control_type="ListItem"):
            try:
                if candidate.element_info.runtime_id == rid:
                    return candidate
            except Exception:
                continue
    except Exception:
        pass
    return item


def classify_message_after_voice_delay(item, chat_list=None) -> tuple[str, str, str | None, object]:
    """语音消息按配置等待后重新读取同一条消息本身。"""
    display, mtype, mpath = classify_message(item)
    if mtype != "语音":
        return display, mtype, mpath, item

    delay = _voice_message_delay_seconds()
    if delay <= 0:
        return display, mtype, mpath, item

    log.info(f"[语音] 等待 {delay}s 后重新读取语音消息")
    time.sleep(delay)
    refreshed = _refresh_list_item(chat_list, item) if chat_list is not None else item
    display, mtype, mpath = classify_message(refreshed)
    return display, mtype, mpath, refreshed


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
        display, mtype, mpath, item = classify_message_after_voice_delay(item, chat_list)
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


def _dedupe_log_text(text: str, limit: int = 80) -> str:
    msg = (text or "").replace("\r", " ").replace("\n", " ")
    return msg if len(msg) <= limit else msg[:limit] + "..."


def _message_content_key(chat: str, sender: str, msg_type: str, text: str,
                         scope: str = "MSG") -> str:
    """5秒去重用的内容 key：同一会话/发送人/类型/文本才算相同消息。"""
    return "\x1f".join((scope or "MSG", chat or "", sender or "",
                          msg_type or "", text or ""))


def _dedupe_recent_message(processed, chat: str, sender: str, msg_type: str,
                           text: str, scope: str = "MSG") -> bool:
    """Return True when the same message appeared within the recent window."""
    key = _message_content_key(chat, sender, msg_type, text, scope=scope)
    if hasattr(processed, "check_and_add"):
        duplicated, age = processed.check_and_add(key)
    else:
        duplicated = key in processed
        age = 0.0 if duplicated else None
        if not duplicated:
            processed.add(key)
    if duplicated:
        age_text = f"{age:.2f}s" if age is not None else "unknown"
        log.info(
            f"[\u53bb\u91cd] 5\u79d2\u5185\u76f8\u540c\u6d88\u606f\uff0c\u8df3\u8fc7: "
            f"{chat}({sender}) [{msg_type}] scope={scope} age={age_text} "
            f"text={_dedupe_log_text(text)!r}"
        )
        return True
    return False


def _uia_text(ctrl) -> str:
    try:
        return ctrl.window_text() or ""
    except Exception:
        return ""


def _uia_control_type(ctrl) -> str:
    try:
        return ctrl.element_info.control_type or ""
    except Exception:
        return ""


def _uia_class_name(ctrl) -> str:
    try:
        return ctrl.element_info.class_name or ""
    except Exception:
        return ""


def _uia_automation_id(ctrl) -> str:
    try:
        return ctrl.element_info.automation_id or ""
    except Exception:
        return ""


def _uia_rect(ctrl):
    try:
        return ctrl.rectangle()
    except Exception:
        return None


def _uia_rect_str(ctrl) -> str:
    r = _uia_rect(ctrl)
    if not r:
        return "(unknown)"
    return f"({r.left},{r.top},{r.right},{r.bottom})"


def _uia_click_control(ctrl, reason: str, tag: str = "[UIA]") -> bool:
    """Click a UIA control, falling back to rectangle-center click when invoke fails."""
    try:
        if hasattr(ctrl, "is_visible") and not ctrl.is_visible():
            return False
    except Exception:
        pass
    try:
        if hasattr(ctrl, "is_enabled") and not ctrl.is_enabled():
            return False
    except Exception:
        pass

    text = _uia_text(ctrl)
    ctype = _uia_control_type(ctrl)
    cls = _uia_class_name(ctrl)
    auto_id = _uia_automation_id(ctrl)
    rect = _uia_rect(ctrl)
    log.info(
        f"{tag} UIA click candidate reason={reason} type={ctype} text={text!r} "
        f"class={cls!r} automation_id={auto_id!r} rect={_uia_rect_str(ctrl)}"
    )
    try:
        ctrl.click_input()
        return True
    except Exception as e:
        log.warning(f"{tag} UIA click_input failed; trying center click: {e}")
    if not rect:
        return False
    try:
        pyautogui.click(int((rect.left + rect.right) / 2), int((rect.top + rect.bottom) / 2))
        return True
    except Exception as e:
        log.warning(f"{tag} UIA center click failed: {e}")
        return False


def _transfer_collect_candidate_score(ctrl) -> int:
    """Return >0 when a UIA element looks like the transfer collect button."""
    text = _uia_text(ctrl).strip()
    if not text:
        return 0

    # Completed/status labels are not actionable collect buttons.
    reject_words = ("待你收款", "你已收款", "已收款", "已退还", "已被领取", "零钱")
    if any(word in text for word in reject_words):
        return 0

    collect_titles = ("收款", "确认收款", "確認收款", "立即收款")
    ctype = _uia_control_type(ctrl)

    # Be conservative: non-button controls must be exact labels, otherwise text such as
    # "收款方" or "收款账户" may be a status/description and should fall back to OpenCV.
    if text not in collect_titles and not (ctype == "Button" and any(title in text for title in collect_titles)):
        return 0

    score = 10
    if text in collect_titles:
        score += 40
    if ctype == "Button":
        score += 30
    elif ctype in ("Custom", "Text", "Pane", "Group"):
        score += 10
    return score


def _log_transfer_detail_uia(detail, limit: int = 80) -> None:
    """Log transfer-detail UIA controls for diagnosing WeChat/DPI changes."""
    try:
        controls = detail.descendants()
    except Exception as e:
        log.warning(f"[转账收款] UIA控件树读取失败: {e}")
        return

    rows = []
    for idx, ctrl in enumerate(controls[:limit]):
        text = _uia_text(ctrl)
        ctype = _uia_control_type(ctrl)
        score = _transfer_collect_candidate_score(ctrl)
        # Always keep likely candidates; otherwise only log non-empty text to avoid noise.
        if score > 0 or text:
            rows.append(
                f"#{idx} score={score} type={ctype} text={text!r} "
                f"class={_uia_class_name(ctrl)!r} automation_id={_uia_automation_id(ctrl)!r} "
                f"rect={_uia_rect_str(ctrl)}"
            )
    if rows:
        log.info("[转账收款] UIA详情控件摘要:\n" + "\n".join(rows[:limit]))
    else:
        log.info(f"[转账收款] UIA详情控件摘要为空 descendants={len(controls)}")


def _click_transfer_collect_by_uia(detail) -> bool:
    """Prefer UIA component lookup for the WeChat transfer collect button."""
    direct_specs = [
        {"title": "收款", "control_type": "Button"},
        {"title": "确认收款", "control_type": "Button"},
        {"title": "確認收款", "control_type": "Button"},
        {"title_re": ".*收款.*", "control_type": "Button"},
    ]
    for spec in direct_specs:
        try:
            btn = detail.child_window(**spec)
            if btn.exists(timeout=0.2):
                wrapper = btn.wrapper_object()
                if _transfer_collect_candidate_score(wrapper) > 0:
                    return _uia_click_control(wrapper, f"child_window({spec})", tag="[transfer]")
        except Exception as e:
            log.debug(f"[转账收款] UIA直接定位未命中 spec={spec}: {e}")

    try:
        controls = detail.descendants()
    except Exception as e:
        log.warning(f"[转账收款] UIA descendants读取失败: {e}")
        return False

    candidates = []
    for ctrl in controls:
        score = _transfer_collect_candidate_score(ctrl)
        if score > 0:
            candidates.append((score, ctrl))
    if not candidates:
        _log_transfer_detail_uia(detail)
        return False

    candidates.sort(key=lambda item: item[0], reverse=True)
    log.info(
        "[转账收款] UIA收款候选: "
        + "; ".join(
            f"score={score} type={_uia_control_type(ctrl)} text={_uia_text(ctrl)!r} "
            f"rect={_uia_rect_str(ctrl)}"
            for score, ctrl in candidates[:5]
        )
    )
    for score, ctrl in candidates[:3]:
        if _uia_click_control(ctrl, f"descendant_score={score}", tag="[transfer]"):
            return True
    return False


def _red_packet_open_candidate_score(ctrl) -> int:
    """Return >0 when a UIA element looks like the red-packet open button."""
    text = _uia_text(ctrl).strip()
    if not text:
        return 0

    reject_words = (
        "已领取", "已领完", "已抢完",
        "已过期", "手慢了", "红包记录",
        "看看大家", "微信红包", "领取详情",
    )
    if any(word in text for word in reject_words):
        return 0

    open_titles = ("拆开", "开", "開", "Open", "open")
    ctype = _uia_control_type(ctrl)

    # In the red-packet popup, exact short labels are safe. For fuzzy Button text,
    # avoid matching generic one-character open labels inside unrelated labels.
    fuzzy_titles = ("拆开", "Open", "open")
    if text not in open_titles and not (ctype == "Button" and any(title in text for title in fuzzy_titles)):
        return 0

    score = 10
    if text in open_titles:
        score += 40
    if ctype == "Button":
        score += 30
    elif ctype in ("Custom", "Text", "Pane", "Group"):
        score += 10
    return score


def _find_red_packet_view(main_window):
    """Find the red-packet receive popup/group after clicking a red packet message."""
    specs = [
        {"class_name": "mmui::PayRedEnvelopeInfoView", "control_type": "Group"},
        {"class_name": "mmui::PayRedEnvelopReceiveWindow", "control_type": "Window"},
        {"class_name": "mmui::PayRedEnvelopeReceiveWindow", "control_type": "Window"},
    ]
    for spec in specs:
        try:
            view = main_window.child_window(**spec)
            if view.exists(timeout=0.2):
                return view.wrapper_object()
        except Exception:
            pass

    try:
        for ctrl in main_window.descendants():
            cls = _uia_class_name(ctrl)
            if cls in (
                "mmui::PayRedEnvelopeInfoView",
                "mmui::PayRedEnvelopReceiveWindow",
                "mmui::PayRedEnvelopeReceiveWindow",
            ):
                return ctrl
    except Exception as e:
        log.debug(f"[red-packet] UIA view scan failed: {e}")
    return None


def _log_red_packet_uia(root, limit: int = 80) -> None:
    """Log red-packet popup UIA controls for diagnosing WeChat/DPI changes."""
    try:
        controls = root.descendants()
    except Exception as e:
        log.warning(f"[red-packet] UIA tree read failed: {e}")
        return

    rows = []
    for idx, ctrl in enumerate(controls[:limit]):
        text = _uia_text(ctrl)
        ctype = _uia_control_type(ctrl)
        score = _red_packet_open_candidate_score(ctrl)
        if score > 0 or text:
            rows.append(
                f"#{idx} score={score} type={ctype} text={text!r} "
                f"class={_uia_class_name(ctrl)!r} automation_id={_uia_automation_id(ctrl)!r} "
                f"rect={_uia_rect_str(ctrl)}"
            )
    if rows:
        log.info("[red-packet] UIA popup controls:\n" + "\n".join(rows[:limit]))
    else:
        log.info(f"[red-packet] UIA popup controls empty descendants={len(controls)}")


def _click_red_packet_open_by_uia(red_view) -> bool:
    """Prefer UIA component lookup for the WeChat red-packet open button."""
    direct_specs = [
        {"title": "拆开", "control_type": "Button"},
        {"title": "开", "control_type": "Button"},
        {"title": "開", "control_type": "Button"},
        {"title": "Open", "control_type": "Button"},
        {"title_re": ".*拆开.*", "control_type": "Button"},
        {"title_re": ".*Open.*", "control_type": "Button"},
    ]
    for spec in direct_specs:
        try:
            btn = red_view.child_window(**spec)
            if btn.exists(timeout=0.2):
                wrapper = btn.wrapper_object()
                if _red_packet_open_candidate_score(wrapper) > 0:
                    return _uia_click_control(wrapper, f"red_packet_child_window({spec})", tag="[red-packet]")
        except Exception as e:
            log.debug(f"[red-packet] direct UIA lookup missed spec={spec}: {e}")

    try:
        controls = red_view.descendants()
    except Exception as e:
        log.warning(f"[red-packet] UIA descendants read failed: {e}")
        return False

    candidates = []
    for ctrl in controls:
        score = _red_packet_open_candidate_score(ctrl)
        if score > 0:
            candidates.append((score, ctrl))
    if not candidates:
        _log_red_packet_uia(red_view)
        return False

    candidates.sort(key=lambda item: item[0], reverse=True)
    log.info(
        "[red-packet] UIA open-button candidates: "
        + "; ".join(
            f"score={score} type={_uia_control_type(ctrl)} text={_uia_text(ctrl)!r} "
            f"rect={_uia_rect_str(ctrl)}"
            for score, ctrl in candidates[:5]
        )
    )
    for score, ctrl in candidates[:3]:
        if _uia_click_control(ctrl, f"red_packet_descendant_score={score}", tag="[red-packet]"):
            return True
    return False


def _confirm_transfer(main_window, msg_item, chat: str) -> bool:
    """确认收款好友转账。返回是否收款成功。

    转账消息文本含"待你收款"（待收款状态）。点击转账消息弹出独立详情窗口 →
    优先 UIA 控件定位"收款"按钮 → OpenCV 模板兜底 → Esc 关闭结果弹窗。
    """
    import os as _os
    from pywinauto import Desktop
    from .paths import get_images_dir
    _TEMPLATE_DIR = get_images_dir()
    try:
        # 点击转账消息卡片打开详情窗口：对方卡片靠左，ListItem 几何中心可能落空，
        # 按 rectangle 换点（左1/4→中→右1/4）点击，每次后检查详情窗口是否弹出
        mr = msg_item.rectangle()
        mw, mh = mr.right - mr.left, mr.bottom - mr.top
        detail = None
        for frac in (0.25, 0.5, 0.75):
            cx, cy = int(mr.left + mw * frac), int(mr.top + mh / 2)
            log.info(f"[转账收款] 点击消息卡片 ({cx},{cy}) rect=({mr.left},{mr.top},{mr.right},{mr.bottom}) frac={frac}")
            try:
                pyautogui.click(cx, cy)
            except Exception as ce:
                log.warning(f"[转账收款] 点击异常: {ce}")
            time.sleep(1.5)
            desktop = Desktop(backend='uia')
            for w in desktop.windows():
                try:
                    if not w.is_visible():
                        continue
                    cn = w.element_info.class_name or ''
                    if cn.startswith('mmui::') and cn != 'mmui::MainWindow':
                        detail = w
                        break
                except Exception:
                    continue
            if detail:
                break
        if not detail:
            log.warning(f"[转账收款] {chat} 详情窗口未弹出（已尝试3个点击位置）")
            return False
        r = detail.rectangle()
        log.info(
            f"[转账收款] 详情窗口 title={_uia_text(detail)!r} class={_uia_class_name(detail)!r} "
            f"automation_id={_uia_automation_id(detail)!r} rect=({r.left},{r.top},{r.right},{r.bottom})"
        )

        # UIA component lookup is resolution/DPI independent. Use OpenCV only as fallback.
        if _click_transfer_collect_by_uia(detail):
            time.sleep(3)
            pyautogui.press('esc')
            log.info(f"[转账收款] UIA收款成功: {chat}")
            return True

        log.warning("[转账收款] UIA未定位到收款按钮，降级OpenCV模板匹配")

        # OpenCV 模板匹配定位"收款"按钮
        import cv2
        import numpy as np
        from PIL import ImageGrab

        collect_template = cv2.imread(_os.path.join(_TEMPLATE_DIR, 'shoukuan_btn.png'),
                                      cv2.IMREAD_COLOR)
        if collect_template is None:
            log.warning("[转账收款] 收款按钮模板图不存在")
            pyautogui.press('esc')
            return False

        # 截取详情窗口区域，模板匹配找"收款"按钮
        shot_pil = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
        shot = cv2.cvtColor(np.array(shot_pil), cv2.COLOR_RGB2BGR)
        th, tw = collect_template.shape[:2]
        if shot.shape[0] < th or shot.shape[1] < tw:
            log.warning(f"[转账收款] 详情窗口截图({shot.shape})小于模板({collect_template.shape})")
            pyautogui.press('esc')
            return False
        res = cv2.matchTemplate(shot, collect_template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        log.info(f"[转账收款] 模板匹配置信度={max_val:.3f} 位置={max_loc}")
        if max_val < 0.6:
            log.warning(f"[转账收款] 未匹配到收款按钮(置信度{max_val:.3f}<0.6)")
            pyautogui.press('esc')
            return False

        # 点击"收款"按钮（max_loc 是模板左上角在截图中的位置，转为屏幕坐标）
        click_x = r.left + max_loc[0] + tw // 2
        click_y = r.top + max_loc[1] + th // 2
        log.info(f"[转账收款] 点击收款按钮 ({click_x}, {click_y})")
        pyautogui.click(click_x, click_y)
        time.sleep(3)
        # 收款成功后弹出结果提示（"你已收款,资金已存入零钱"），Esc 关闭
        pyautogui.press('esc')
        log.info(f"[转账收款] 收款成功: {chat}")
        return True
    except Exception as e:
        log.warning(f"[转账收款] 异常: {chat} -> {e}")
        try:
            pyautogui.press('esc')
        except Exception:
            pass
        return False

def _open_red_packet(main_window, msg_item, chat: str) -> str | None:
    """Open a WeChat red packet. Return uploaded screenshot URL, or None on failure.

    Flow: click red-packet message -> prefer UIA component lookup for the open button ->
    fall back to OpenCV template matching -> screenshot result -> upload/forward -> Esc.
    """
    import os as _os
    import tempfile
    from pywinauto import Desktop
    from PIL import ImageGrab
    from .paths import get_images_dir
    _TEMPLATE_DIR = get_images_dir()
    try:
        chat_list = main_window.child_window(**Lists.FriendChatList)
        mr = msg_item.rectangle()
        mw, mh = mr.right - mr.left, mr.bottom - mr.top
        fallback_rect = None
        opened = False
        open_template = None
        template_shape = None

        for frac in (0.25, 0.5, 0.75):
            cx, cy = int(mr.left + mw * frac), int(mr.top + mh / 2)
            log.info(f"[red-packet] click message card ({cx},{cy}) rect=({mr.left},{mr.top},{mr.right},{mr.bottom}) frac={frac}")
            try:
                pyautogui.click(cx, cy)
            except Exception as ce:
                log.warning(f"[red-packet] message-card click failed: {ce}")
            time.sleep(1.2)

            red_view = _find_red_packet_view(main_window)
            if red_view:
                vr = _uia_rect(red_view)
                if vr:
                    fallback_rect = vr
                log.info(
                    f"[red-packet] popup/view title={_uia_text(red_view)!r} class={_uia_class_name(red_view)!r} "
                    f"automation_id={_uia_automation_id(red_view)!r} rect={_uia_rect_str(red_view)}"
                )
                if _click_red_packet_open_by_uia(red_view):
                    opened = True
                    log.info(f"[red-packet] UIA open click succeeded: {chat}")
                    break
                log.warning("[red-packet] UIA did not find open button; trying OpenCV fallback")

            # OpenCV fallback: kept for icon-only buttons or older WeChat builds whose UIA tree is incomplete.
            try:
                if open_template is None:
                    import cv2
                    import numpy as np
                    open_template = cv2.imread(_os.path.join(_TEMPLATE_DIR, 'hongbao_btn.png'), cv2.IMREAD_COLOR)
                    if open_template is None:
                        log.warning("[red-packet] hongbao_btn.png missing; OpenCV fallback unavailable")
                        continue
                    template_shape = open_template.shape[:2]
                else:
                    import cv2
                    import numpy as np

                if not chat_list.exists(timeout=1):
                    continue
                rr = chat_list.rectangle()
                fallback_rect = rr
                shot_pil = ImageGrab.grab(bbox=(rr.left, rr.top, rr.right, rr.bottom))
                shot = cv2.cvtColor(np.array(shot_pil), cv2.COLOR_RGB2BGR)
                th, tw = template_shape
                if shot.shape[0] < th or shot.shape[1] < tw:
                    continue
                res = cv2.matchTemplate(shot, open_template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                log.info(f"[red-packet] OpenCV confidence={max_val:.3f} loc={max_loc} frac={frac} rect=({rr.left},{rr.top},{rr.right},{rr.bottom})")
                if max_val >= 0.6:
                    click_x = rr.left + max_loc[0] + tw // 2
                    click_y = rr.top + max_loc[1] + th // 2
                    log.info(f"[red-packet] OpenCV click open button ({click_x}, {click_y})")
                    pyautogui.click(click_x, click_y)
                    opened = True
                    break
            except Exception as e:
                log.warning(f"[red-packet] OpenCV fallback failed: {e}")

        if not opened:
            log.warning(f"[red-packet] {chat} not opened; no open button found after 3 card clicks")
            pyautogui.press('esc')
            return None

        time.sleep(3)

        screenshot_url = None
        detail_win = None
        try:
            desktop = Desktop(backend='uia')
            detail_win = desktop.window(class_name='mmui::PayRedEnvelopDetailWindow',
                                        control_type='Window')
            if detail_win.exists(timeout=3):
                wr = detail_win.rectangle()
                log.info(f"[red-packet] detail window rect=({wr.left},{wr.top},{wr.right},{wr.bottom})")
            elif fallback_rect:
                wr = fallback_rect
                log.warning(f"[red-packet] detail window not found; screenshot fallback rect=({wr.left},{wr.top},{wr.right},{wr.bottom})")
            else:
                wr = chat_list.rectangle()
                log.warning(f"[red-packet] detail/fallback rect missing; screenshot chat rect=({wr.left},{wr.top},{wr.right},{wr.bottom})")

            tmp = _os.path.join(tempfile.gettempdir(), f'hongbao_{int(time.time())}.png')
            ImageGrab.grab(bbox=(wr.left, wr.top, wr.right, wr.bottom)).save(tmp)
            log.info(f"[red-packet] screenshot saved: {tmp}")
            pyautogui.press('esc')

            uploader = mqtt_worker._uploader if mqtt_worker._coordinator else None
            if uploader and getattr(uploader, 'available', False):
                result = uploader.upload(tmp, chat=chat)
                screenshot_url = result if result else None
                if screenshot_url:
                    log.info(f"[red-packet] screenshot uploaded: {screenshot_url}")
                else:
                    log.warning("[red-packet] screenshot upload failed; fallback to text forwarding")
            else:
                log.warning("[red-packet] MinIO not configured; screenshot not uploaded")

            try:
                _os.remove(tmp)
            except OSError:
                pass
            if detail_win is not None and detail_win.exists(timeout=0.5):
                try:
                    detail_win.close()
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"[red-packet] screenshot/upload failed: {e}")

        time.sleep(0.3)

        if screenshot_url:
            try:
                mqtt_worker.on_wechat_message(
                    chat, chat, "[微信红包已查收]",
                    msg_type="微信红包",
                    file_url=screenshot_url,
                    file_name=f"hongbao_{chat}.png")
                log.info(f"[red-packet] screenshot forwarded to MQTT: {chat}")
            except Exception as e:
                log.warning(f"[red-packet] MQTT forwarding failed: {e}")

        log.info(f"[red-packet] opened: {chat} screenshot_url={screenshot_url}")
        return screenshot_url
    except Exception as e:
        log.warning(f"[red-packet] exception: {chat} -> {e}")
        try:
            pyautogui.press('esc')
        except Exception:
            pass
        return None

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
        if _dedupe_recent_message(processed, chat, sender, msg_type, text, scope="ALERT"):
            return
        log.warning(f"⚠️ {chat} 可能{msg_type}: {text!r}")
        kind = "blocked" if msg_type == "被拉黑" else "deleted"
        try:
            mqtt_worker.notify_contact_unreachable(
                chat=chat, kind=kind, message=text, source="monitor")
        except Exception as e:
            log.warning(f"[contact-unreachable] Feishu/MQTT alert failed: {e}")
        return

    is_group = _is_group(chat)

    # 跳过自己（机器人）发的消息：气泡靠右。classify_message 不分方向，靠 rect 判断，
    # 避免把自己的回复/转发误当成对方新消息再处理（转发/回复/群监控）
    if msg_item is not None and _is_self_message(msg_item, main_window):
        return

    # /指令（仅 admin，不受监听过滤限制）
    if text.startswith("/") and commands.is_admin(sender):
        if _dedupe_recent_message(processed, chat, sender, "command", text, scope="CMD"):
            return
        reply = commands.handle(text)
        if reply:
            _send_to_chat(main_window, chat, split_long_text(reply), current_friend)
        return

    # 按用户要求：仅 5 秒内同一会话/发送人/类型/文本完全相同才去重。
    if _dedupe_recent_message(processed, chat, sender, msg_type, text):
        return


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

    # 红包自动拆开（收到红包 → 点"开" → 截图上传 → MQTT转发 → 飞书提醒）
    # 截图转发成功后直接返回，不再发红包文本消息
    if msg_type == "微信红包" and bot_config.get("auto_open_red_packet", False) and msg_item is not None:
        screenshot_url = _open_red_packet(main_window, msg_item, chat)
        if screenshot_url is not None:
            log.info(f"[微信红包] 已拆开: {chat} 截图={screenshot_url}")
            try:
                from . import webhook_send
                url_line = f"\n截图: {screenshot_url}" if screenshot_url else ""
                webhook_send.send_webhook(
                    title=f"【微信红包】{chat}",
                    content=f"已拆开红包{url_line}\n来源: {chat}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as e:
                log.warning(f"[微信红包] 飞书提醒失败: {e}")
            return

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
    if _dedupe_recent_message(processed, chat, sender, "group_monitor", text, scope="GMON"):
        return False
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
def _activate_weixin_by_cv() -> bool:
    """OpenCV 截屏匹配任务栏/托盘微信图标并点击激活（open_weixin 失败兜底）。

    覆盖场景：微信窗口不在前台、被最小化到托盘、句柄丢失但进程还在——
    点任务栏图标重新唤出窗口，再让 open_weixin 重试。
    注意：对"无障碍树/UI 树不可见"(讲述人 trick 失效) 无效，那需重启无障碍服务。
    模板缺失(config/images/weixin_icon.png)安全返回 False。
    """
    import cv2
    import numpy as np
    import os as _os
    from PIL import ImageGrab
    from .paths import get_images_dir
    try:
        tpl = cv2.imread(_os.path.join(get_images_dir(), 'weixin_icon.png'), cv2.IMREAD_COLOR)
        if tpl is None:
            return False
        shot = cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2BGR)
        th, tw = tpl.shape[:2]
        if shot.shape[0] < th or shot.shape[1] < tw:
            return False
        res = cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= 0.75:
            cx, cy = max_loc[0] + tw // 2, max_loc[1] + th // 2
            pyautogui.click(cx, cy)
            time.sleep(1.0)
            log.info(f"[微信] OpenCV 激活微信窗口 (置信度{max_val:.3f}) @ ({cx},{cy})")
            return True
    except Exception as e:
        log.debug(f"[微信] OpenCV 激活失败: {e}")
    return False


def _open_weixin_safe(retries: int = 3, interval: float = 2.0):
    """打开微信主界面，失败(NotFoundError 等瞬时异常)重试，全失败返回 None。

    微信窗口被短暂遮挡/最小化/句柄丢失时 open_weixin 可能抛 NotFoundError
    （"无法识别定位到微信主界面"）。重试间用 OpenCV 匹配任务栏图标点击激活，
    覆盖窗口未激活场景；持续失败返回 None，由调用方跳过本轮，避免中断主循环。
    """
    for i in range(retries):
        try:
            return Navigator.open_weixin(is_maximize=False)
        except Exception as e:
            log.warning(f"[微信] 打开主界面失败({i + 1}/{retries}): {e}")
            # OpenCV 激活兜底：尝试点任务栏图标唤出微信窗口（窗口未激活/在托盘时有效）
            _activate_weixin_by_cv()
            if i < retries - 1:
                time.sleep(interval)
    return None


class _BoundedSet:
    """5秒窗口消息去重表，基于 OrderedDict 维护最近消息。"""
    def __init__(self, maxsize: int = 5000, window_seconds: float = 5.0) -> None:
        self._data: OrderedDict[str, float] = OrderedDict()
        self._maxsize = maxsize
        self._window_seconds = float(window_seconds)

    def _prune(self, now: float) -> None:
        expire_before = now - self._window_seconds
        while self._data:
            _, ts = next(iter(self._data.items()))
            if ts >= expire_before:
                break
            self._data.popitem(last=False)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def add(self, key: str) -> None:
        now = time.monotonic()
        self._prune(now)
        self._data[key] = now
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def check_and_add(self, key: str) -> tuple[bool, float | None]:
        now = time.monotonic()
        self._prune(now)
        if key in self._data:
            age = now - self._data[key]
            self._data[key] = now
            self._data.move_to_end(key)
            return True, age
        self._data[key] = now
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)
        return False, None


class Monitor:
    def __init__(self, check_interval: float = 10.0) -> None:
        self.check_interval = check_interval
        self._run_timeout = 120.0
        self._stop = threading.Event()
        self._last_loop_alert = ("", 0.0)  # (异常文本, 时间戳)，节流防刷屏
        self.processed: _BoundedSet = _BoundedSet(maxsize=5000)
        self.current_friend: Optional[str] = None
        self.current_last_rid = None
        self._first_run = True
        self._last_pending_scan = 0.0  # 待通过好友扫描节流时间戳
        self._lock_fail_count = 0  # UI 锁连续获取失败计数

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        # 获取 UI 互斥锁：与 MQTT 任务/异步媒体线程互斥，避免抢鼠标
        ui_lock = mqtt_worker.ui_lock
        if ui_lock and not ui_lock.acquire(timeout=0.5):
            self._lock_fail_count += 1
            # 连续失败超过阈值（约 5 分钟）→ 调 coordinator 统一入口重建锁恢复轮询
            if self._lock_fail_count >= 30:
                log.warning(f"UI 锁连续 {self._lock_fail_count} 轮获取失败，调用统一入口重建锁")
                try:
                    coord = mqtt_worker._coordinator
                    if coord and hasattr(coord, '_rebuild_ui_lock'):
                        coord._rebuild_ui_lock(
                            f"monitor 连续 {self._lock_fail_count} 轮获取失败")
                    else:
                        log.warning("coordinator 不支持 _rebuild_ui_lock，无法重建")
                    self._lock_fail_count = 0
                except Exception as e:
                    log.error(f"重建 UI 锁失败: {e}")
            elif self._lock_fail_count == 1 or self._lock_fail_count % 6 == 0:
                # 节流日志：首次 + 每约 1 分钟
                log.info(f"UI 锁被占用，已连续跳过 {self._lock_fail_count} 轮")
            return
        self._lock_fail_count = 0  # 成功获取，重置
        input_blocker.set_bot_active(True)  # 放行机器人点击
        # 标记 monitor 正在处理消息（检测→转发MQTT），executor 应等待完成后再操作UI
        wx_busy_event = getattr(mqtt_worker._coordinator, '_wx_busy_event', None) if mqtt_worker._coordinator else None
        if wx_busy_event:
            wx_busy_event.set()
        try:
            self._run_once_locked()
        finally:
            if wx_busy_event:
                wx_busy_event.clear()
            input_blocker.set_bot_active(False)
            if ui_lock:
                try:
                    ui_lock.release()
                except RuntimeError:
                    pass

    def _run_once_locked(self) -> None:
        main_window = _open_weixin_safe()
        if main_window is None:
            log.error("[监听] 无法打开微信主界面(无障碍服务/讲述人可能失效或微信异常)，跳过本轮")
            return
        dismiss_wx_dialog(main_window)  # 先清理可能的提示弹框（操作频繁等）
        try:
            main_window.child_window(**SideBar.Weixin).click_input()
        except Exception as e:
            log.warning(f"[监听] 点击微信侧栏失败(疑似弹框): {e}")
            if dismiss_wx_dialog(main_window):
                main_window.child_window(**SideBar.Weixin).click_input()  # 清弹框后重试
            else:
                raise
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
        # current_friend 理论上只由 ② 设置（已过滤白名单），但手动切换/历史状态可能残留
        # 非白名单私聊会话 → 不读取其消息
        if (self.current_friend is not None
                and not _is_group(self.current_friend)
                and not is_listened_chat(self.current_friend, False)):
            log.info(f"[监听] 当前停留会话 {self.current_friend} 不在白名单，停止轮询")
            self.current_friend = None
            self.current_last_rid = None
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
                                msg_text, msg_type, file_path, item = classify_message_after_voice_delay(item, chat_list)
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
                # 不在监听白名单的私聊好友：不点开阅读（避免读取非关注消息、节省 UI 操作）
                # 群聊仍读取（群监控关键词匹配需要先拿到消息文本）
                if not _is_group(friend) and not is_listened_chat(friend, False):
                    log.info(f"[监听] {friend} 不在白名单，跳过阅读其新消息")
                    continue
                try:
                    main_window.child_window(**SideBar.Weixin).click_input()
                except Exception as e:
                    log.warning(f"[监听] 点击微信侧栏失败(疑似弹框) {friend}: {e}")
                    if dismiss_wx_dialog(main_window):
                        try:
                            main_window.child_window(**SideBar.Weixin).click_input()
                        except Exception as e2:
                            log.warning(f"[监听] 清弹框后仍失败，跳过 {friend}: {e2}")
                            continue
                    else:
                        continue
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
                # 记录为当前停留会话：先无条件记录 current_friend（红点已点开该会话），
                # current_last_rid 允许暂缺（消息列表可能因媒体预览/资料卡等暂不可见）
                self.current_friend = friend
                self.current_last_rid = None
                chat_list = main_window.child_window(**Lists.FriendChatList)
                if chat_list.exists(timeout=0.5):
                    try:
                        chat_items = chat_list.children(control_type="ListItem")
                        self.current_last_rid = chat_items[-1].element_info.runtime_id if chat_items else None
                    except Exception as e:
                        log.warning(f"[监听] 读取 {friend} 消息列表基线失败，current_last_rid 置空: {e}")

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
        self._run_timeout = float(bot_config.get("monitor_run_timeout", 120) or 120)
        log.info(f"📨 消息主循环启动（轮询间隔 {self.check_interval}s，单轮超时 {self._run_timeout}s）")
        in_pause = False
        try:
            while not self._stop.is_set():
                try:
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
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    # 单轮任意异常都不应中断主循环：_run_once_guarded 已兜底 run_once，
                    # 此处防御 _in_pause_period / _stop.wait 等意外异常，记日志+飞书后继续轮询
                    log.error(f"主循环单轮意外异常（已兜底，继续轮询）: {e}")
                    try:
                        self._alert_loop_exception(e)
                    except Exception:
                        pass
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
            from .exception_alert import send_client_exception_alert
            ok, info = send_client_exception_alert(
                title=f"【{nickname}】微信机器人异常",
                exc=e,
                nickname=nickname,
                screenshot_reason="monitor_loop_exception",
            )
            if not ok:
                log.error(f"主循环异常 webhook 推送失败: {info}")
        except Exception as we:
            log.error(f"主循环异常 webhook 推送失败: {we}")


# 全局单例
monitor = Monitor()
