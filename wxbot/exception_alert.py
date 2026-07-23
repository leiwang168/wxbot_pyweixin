# -*- coding: utf-8 -*-
"""Exception alert helpers: capture screenshots, upload to MinIO, notify webhook."""
from __future__ import annotations

import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Optional, Tuple

from .logger import log
from .paths import get_logs_dir


def _safe_name(value: str, default: str = "client_exception") -> str:
    text = str(value or "").strip() or default
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._")
    return (text or default)[:80]


def get_exception_screenshot_dir() -> str:
    """Return the directory used to persist client-exception screenshots."""
    return os.path.join(get_logs_dir(), "screenshots")


def capture_exception_screenshot(reason: str = "client_exception") -> Tuple[Optional[str], str]:
    """Capture and persist a full-screen screenshot.

    Returns (path, error). path is None when capture failed. This function must
    never raise because it is called from exception handlers.
    """
    try:
        os.makedirs(get_exception_screenshot_dir(), exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(get_exception_screenshot_dir(), f"{ts}_{_safe_name(reason)}.png")
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=True)
            img.save(path)
        except TypeError:
            # Older Pillow may not support all_screens.
            from PIL import ImageGrab
            img = ImageGrab.grab()
            img.save(path)
        except Exception:
            # Fallback for environments where ImageGrab is unavailable but
            # pyautogui screenshot works.
            import pyautogui
            img = pyautogui.screenshot()
            img.save(path)
        return path, ""
    except Exception as exc:
        return None, str(exc)



def upload_exception_screenshot(local_path: str, reason: str = "client_exception") -> Tuple[Optional[str], str]:
    """Upload an exception screenshot to MinIO and return (url, error)."""
    if not local_path:
        return None, "screenshot path is empty"
    try:
        from .config import bot_config
        from .mqtt.common import MinioUploader

        uploader = None
        try:
            from .mqtt.worker import mqtt_worker
            uploader = getattr(mqtt_worker, "_uploader", None)
        except Exception:
            uploader = None

        if uploader is None or not getattr(uploader, "available", False):
            cfg = bot_config.get("mqtt_worker", {}) or {}
            uploader = MinioUploader(cfg.get("minio", {}) or {})

        if uploader is None or not getattr(uploader, "available", False):
            return None, "MinIO not configured"

        local = Path(local_path)
        object_name = (
            f"exception-screenshots/{time.strftime('%Y/%m/%d')}/"
            f"{int(time.time())}_{_safe_name(reason)}{local.suffix or '.png'}"
        )
        url = uploader.upload_named(str(local), object_name)
        if not url:
            return None, "MinIO upload failed"
        return url, ""
    except Exception as exc:
        return None, str(exc)

def send_client_exception_alert(
    *,
    title: str,
    exc: BaseException,
    nickname: str = "",
    screenshot_reason: str = "client_exception",
    include_traceback: bool = True,
) -> Tuple[bool, str]:
    """Save screenshot, upload it to MinIO, and push an exception alert."""
    screenshot_path, screenshot_error = capture_exception_screenshot(screenshot_reason)
    screenshot_url = ""
    upload_error = ""
    if screenshot_path:
        log.info(f"[异常截图] 已保存: {screenshot_path}")
        screenshot_url, upload_error = upload_exception_screenshot(screenshot_path, screenshot_reason)
        if screenshot_url:
            log.info(f"[异常截图] MinIO 上传成功: {screenshot_url}")
        else:
            log.warning(f"[异常截图] MinIO 上传失败: {upload_error}")
    else:
        log.warning(f"[异常截图] 保存失败: {screenshot_error}")

    now_text = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"异常: {exc}",
        f"时间: {now_text}",
    ]
    if nickname:
        lines.insert(0, f"账号: {nickname}")
    if screenshot_url:
        lines.append(f"截图URL: {screenshot_url}")
    elif screenshot_path:
        lines.append(f"截图上传失败: {upload_error}")
    else:
        lines.append(f"截图保存失败: {screenshot_error}")
    if include_traceback:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        if tb:
            # Keep webhook text compact; full traceback remains in logs.
            lines.append("Traceback:")
            lines.append(tb[-3000:])

    try:
        from . import webhook_send
        return webhook_send.send_webhook(title=title, content="\n".join(lines))
    except Exception as send_exc:
        return False, f"Webhook alert failed: {send_exc}"

_global_hooks_installed = False
_prev_sys_excepthook = None
_prev_threading_excepthook = None


def install_global_exception_hooks(context: str = "client") -> None:
    """Install best-effort hooks for uncaught main-thread/background-thread exceptions."""
    global _global_hooks_installed, _prev_sys_excepthook, _prev_threading_excepthook
    if _global_hooks_installed:
        return
    _global_hooks_installed = True

    _prev_sys_excepthook = sys.excepthook
    title = "【客户端未捕获异常】"

    def _sys_hook(exc_type, exc, tb):
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            if _prev_sys_excepthook:
                _prev_sys_excepthook(exc_type, exc, tb)
            return
        try:
            exc.__traceback__ = tb
            send_client_exception_alert(
                title=f"{title}{context}",
                exc=exc,
                screenshot_reason="uncaught_exception",
            )
        except Exception:
            pass
        if _prev_sys_excepthook:
            _prev_sys_excepthook(exc_type, exc, tb)

    sys.excepthook = _sys_hook

    try:
        import threading
        _prev_threading_excepthook = getattr(threading, "excepthook", None)

        def _thread_hook(args):
            exc_type = getattr(args, "exc_type", None)
            exc = getattr(args, "exc_value", None)
            tb = getattr(args, "exc_traceback", None)
            thread = getattr(args, "thread", None)
            if exc_type and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                if _prev_threading_excepthook:
                    _prev_threading_excepthook(args)
                return
            if exc is not None:
                try:
                    exc.__traceback__ = tb
                    thread_name = getattr(thread, "name", "") or "unknown"
                    send_client_exception_alert(
                        title=f"{title}{context}/{thread_name}",
                        exc=exc,
                        screenshot_reason=f"uncaught_thread_{thread_name}",
                    )
                except Exception:
                    pass
            if _prev_threading_excepthook:
                _prev_threading_excepthook(args)

        threading.excepthook = _thread_hook
    except Exception:
        pass

