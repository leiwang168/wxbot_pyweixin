# -*- coding: utf-8 -*-
"""Regression tests for friend-request reason and remark input."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pyweixin.WeChatAuto import FriendSettings, _replace_and_verify_edit_text


class _Clickable:
    def __init__(self, events=None, label=None):
        self.clicked = 0
        self.events = events
        self.label = label

    def click_input(self):
        self.clicked += 1
        if self.events is not None:
            self.events.append(("click", self.label))

    def exists(self, timeout=None):
        return True


class _SearchEdit:
    def set_text(self, value):
        pass

    def type_keys(self, keys, **kwargs):
        pass


class _KeyboardEdit:
    def __init__(self, events, label, readback):
        self.events = events
        self.label = label
        self.readback = readback

    def wrapper_object(self):
        return self

    def click_input(self):
        self.events.append(("click", self.label))

    def set_focus(self):
        self.events.append(("focus", self.label))

    def get_value(self):
        self.events.append(("read", self.label, self.readback))
        return self.readback


class _ContactProfile:
    def __init__(self, add_button):
        self.add_button = add_button

    def exists(self, timeout=None):
        return True

    def child_window(self, **kwargs):
        assert kwargs == {"kind": "add_button"}
        return self.add_button

    def descendants(self, control_type=None):
        assert control_type == "Text"
        return [SimpleNamespace(window_text=lambda: "Test Nickname")]


class _AddFriendPane:
    def __init__(self, profile):
        self.search_edit = _SearchEdit()
        self.profile = profile
        self.closed = 0

    def child_window(self, **kwargs):
        if kwargs == {"control_type": "Edit"}:
            return self.search_edit
        assert kwargs == {"kind": "profile"}
        return self.profile

    def close(self):
        self.closed += 1


class _VerifyWindow:
    def __init__(self, request_edit, remark_edit, chat_group, confirm_button):
        self.controls = {
            "request_edit": request_edit,
            "remark_edit": remark_edit,
            "chat_group": chat_group,
            "confirm_button": confirm_button,
        }
        self.lookups = []
        self.closed = 0

    def child_window(self, **kwargs):
        self.lookups.append(kwargs)
        if kwargs == {"control_type": "Edit", "found_index": 0}:
            return self.controls["request_edit"]
        if kwargs == {"control_type": "Edit", "found_index": 1}:
            return self.controls["remark_edit"]
        kind = kwargs.get("kind")
        if kind in self.controls:
            return self.controls[kind]
        raise AssertionError(f"unexpected selector: {kwargs}")

    def close(self):
        self.closed += 1


def test_add_new_friend_pastes_both_fields_then_waits_three_seconds_before_submit():
    events = []
    add_button = _Clickable(events, "add")
    add_friend_pane = _AddFriendPane(_ContactProfile(add_button))
    main_window = SimpleNamespace(close=lambda: None)
    request_edit = _KeyboardEdit(events, "request", "Reason once")
    remark_edit = _KeyboardEdit(events, "remark", "Customer remark")
    confirm_button = _Clickable(events, "confirm")
    verify_window = _VerifyWindow(request_edit, remark_edit, _Clickable(), confirm_button)

    with patch("pyweixin.WeChatAuto.Navigator.open_add_friend_panel",
               return_value=(add_friend_pane, main_window)), \
         patch("pyweixin.WeChatAuto.Tools.move_window_to_center", return_value=verify_window), \
         patch("pyweixin.WeChatAuto.Groups.ContactProfileViewGroup", {"kind": "profile"}), \
         patch("pyweixin.WeChatAuto.Groups.ChatOnlyGroup", {"kind": "chat_group"}), \
         patch("pyweixin.WeChatAuto.Buttons.AddToContactsButton", {"kind": "add_button"}), \
         patch("pyweixin.WeChatAuto.Buttons.ConfirmButton", {"kind": "confirm_button"}), \
         patch("pyweixin.WeChatAuto.Windows.VerifyFriendWindow", {"kind": "verify_window"}), \
         patch("pyweixin.WeChatAuto.SystemSettings.copy_text_to_clipboard",
               side_effect=lambda value: events.append(("clipboard", value))), \
         patch("pyweixin.WeChatAuto.pyautogui.hotkey",
               side_effect=lambda *keys, **kwargs: events.append(("hotkey", keys))), \
         patch("pyweixin.WeChatAuto.pyautogui.press",
               side_effect=lambda key, **kwargs: events.append(("press", key))), \
         patch("pyweixin.WeChatAuto.time.sleep",
               side_effect=lambda seconds: events.append(("sleep", seconds))):
        nickname = FriendSettings.add_new_friend(
            number="wxid_test",
            greetings="Reason once",
            remark="Customer remark",
            close_weixin=False,
        )

    assert nickname == "Test Nickname"
    assert events.count(("clipboard", "Reason once")) == 1
    assert events.count(("clipboard", "Customer remark")) == 1
    assert events.count(("hotkey", ("ctrl", "v"))) == 2
    assert events.index(("click", "request")) < events.index(("clipboard", "Reason once"))
    assert events.index(("click", "remark")) < events.index(("clipboard", "Customer remark"))
    assert events.index(("read", "request", "Reason once")) < events.index(("click", "remark"))
    assert events.index(("read", "remark", "Customer remark")) < events.index(("sleep", 3.0))
    assert events.index(("sleep", 3.0)) < events.index(("click", "confirm"))
    assert {"control_type": "Edit", "found_index": 0} in verify_window.lookups
    assert {"control_type": "Edit", "found_index": 1} in verify_window.lookups
    assert verify_window.closed == 1
    assert add_friend_pane.closed == 1


def test_keyboard_input_mismatch_blocks_submit_path():
    events = []
    edit = _KeyboardEdit(events, "request", "")

    with patch("pyweixin.WeChatAuto.SystemSettings.copy_text_to_clipboard"), \
         patch("pyweixin.WeChatAuto.pyautogui.hotkey"), \
         patch("pyweixin.WeChatAuto.pyautogui.press"), \
         patch("pyweixin.WeChatAuto.time.sleep"):
        with pytest.raises(RuntimeError, match="friend request reason input mismatch"):
            _replace_and_verify_edit_text(edit, "Reason once", "friend request reason")


@pytest.mark.parametrize(
    ("request_readback", "remark_readback", "error_field"),
    [
        ("", "Customer remark", "friend request reason"),
        ("Reason once", "", "friend remark"),
    ],
)
def test_add_new_friend_never_confirms_when_a_provided_field_is_not_visible(
    request_readback, remark_readback, error_field
):
    events = []
    add_friend_pane = _AddFriendPane(_ContactProfile(_Clickable(events, "add")))
    request_edit = _KeyboardEdit(events, "request", request_readback)
    remark_edit = _KeyboardEdit(events, "remark", remark_readback)
    confirm_button = _Clickable(events, "confirm")
    verify_window = _VerifyWindow(
        request_edit,
        remark_edit,
        _Clickable(),
        confirm_button,
    )

    with patch("pyweixin.WeChatAuto.Navigator.open_add_friend_panel",
               return_value=(add_friend_pane, SimpleNamespace(close=lambda: None))), \
         patch("pyweixin.WeChatAuto.Tools.move_window_to_center", return_value=verify_window), \
         patch("pyweixin.WeChatAuto.Groups.ContactProfileViewGroup", {"kind": "profile"}), \
         patch("pyweixin.WeChatAuto.Groups.ChatOnlyGroup", {"kind": "chat_group"}), \
         patch("pyweixin.WeChatAuto.Buttons.AddToContactsButton", {"kind": "add_button"}), \
         patch("pyweixin.WeChatAuto.Buttons.ConfirmButton", {"kind": "confirm_button"}), \
         patch("pyweixin.WeChatAuto.Windows.VerifyFriendWindow", {"kind": "verify_window"}), \
         patch("pyweixin.WeChatAuto.SystemSettings.copy_text_to_clipboard"), \
         patch("pyweixin.WeChatAuto.pyautogui.hotkey"), \
         patch("pyweixin.WeChatAuto.pyautogui.press"), \
         patch("pyweixin.WeChatAuto.time.sleep",
               side_effect=lambda seconds: events.append(("sleep", seconds))):
        with pytest.raises(RuntimeError, match=error_field):
            FriendSettings.add_new_friend(
                number="wxid_test",
                greetings="Reason once",
                remark="Customer remark",
                close_weixin=False,
            )

    assert confirm_button.clicked == 0
    assert ("click", "confirm") not in events
    assert ("sleep", 3.0) not in events
    assert verify_window.closed == 1
    assert add_friend_pane.closed == 1

