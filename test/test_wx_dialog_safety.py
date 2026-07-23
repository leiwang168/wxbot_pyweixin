# -*- coding: utf-8 -*-
"""Safety checks for WeChat dialog detection."""
from unittest.mock import patch

from wxbot.wx_dialog import detect_and_dismiss_wx_dialog


def test_unknown_add_friend_window_is_never_auto_confirmed():
    root = object()
    with patch("wxbot.wx_dialog._candidate_roots", return_value=[root]), \
         patch("wxbot.wx_dialog._collect_text", return_value="\u7533\u8bf7\u6dfb\u52a0\u670b\u53cb"), \
         patch("wxbot.wx_dialog._click_confirm") as click_confirm, \
         patch("wxbot.wx_dialog._click_any_confirm_button") as click_any, \
         patch("wxbot.wx_dialog.dismiss_wx_dialog") as dismiss_by_image:
        result = detect_and_dismiss_wx_dialog(dismiss=True)

    assert result.hit is False
    assert result.dismissed is False
    click_confirm.assert_not_called()
    click_any.assert_not_called()
    dismiss_by_image.assert_not_called()


def test_known_rate_limit_window_can_still_be_dismissed():
    root = object()
    text = "\u64cd\u4f5c\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5"
    with patch("wxbot.wx_dialog._candidate_roots", return_value=[root]), \
         patch("wxbot.wx_dialog._collect_text", return_value=text), \
         patch("wxbot.wx_dialog._click_confirm", return_value=True) as click_confirm:
        result = detect_and_dismiss_wx_dialog(dismiss=True)

    assert result.hit is True
    assert result.dialog_type == "rate_limited"
    assert result.text == text
    assert result.dismissed is True
    click_confirm.assert_called_once_with(root, known_rate_limit=True)
