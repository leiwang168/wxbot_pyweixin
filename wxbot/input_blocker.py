# -*- coding: utf-8 -*-
"""服务运行时屏蔽人工对微信窗口的鼠标操作（把微信交给机器人）。

WH_MOUSE_LL 低级鼠标钩子：吞掉落在微信进程窗口的鼠标左键点击；
机器人持 UI 锁操作期间（_bot_active=True）放行；键盘不拦（保证热键可用）。

解除（优先级从高到低）：
  ① 全局热键 Ctrl+Alt+X 切换（键盘始终可用，主解除方式）
  ② /屏蔽微信 /解除屏蔽 admin 指令（需能在微信输入框打字）
  ③ 杀进程兜底（低级钩子是进程级，进程退出 OS 自动摘除）

超时保险：连续屏蔽 N 分钟自动解除。状态变化写日志 + 向"文件传输助手"发通知。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from .logger import log

# ---- Win32 常量 ----
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_CONTROL = 0x0002
MOD_ALT = 0x0001
VK_X = 0x58  # 'X'
HOTKEY_ID = 0xB001


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


# LRESULT CALLBACK LowLevelMouseProc(int nCode, WPARAM wParam, LPARAM lParam)
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def _find_weixin_pid() -> int:
    """定位微信主窗口 → 返回其进程 pid；失败返回 0。

    优先窗口类名+标题（多类名兼容版本差异），再用进程名兜底（Weixin.exe/WeChat.exe）。
    """
    try:
        import win32gui
        for cls in ("Qt51514QWindowIcon", "WeChatMainWndForPC", "WeixinMainWndForPC"):
            for title in ("微信", "Weixin"):
                hwnd = win32gui.FindWindow(cls, title)
                if hwnd:
                    pid = wintypes.DWORD(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value:
                        return pid.value
    except Exception as e:
        log.warning(f"[InputBlocker] FindWindow 失败: {e}")
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name"]):
            n = (p.info.get("name") or "").lower()
            if n in ("weixin.exe", "wechat.exe", "微信.exe"):
                return p.info["pid"]
    except Exception as e:
        log.warning(f"[InputBlocker] psutil 进程名查找失败: {e}")
    return 0


class InputBlocker:
    def __init__(self):
        self._enabled = False
        self._bot_active = False
        self._wx_pid = 0
        self._auto_release_minutes = 30
        self._lock = threading.Lock()
        self._thread_id = 0
        self._hook = None
        self._hookproc = None  # 保持回调引用，防止被 GC
        self._hook_thread = None
        self._timer = None
        self._started = False
        self._call_count = 0   # 钩子回调触发次数（诊断用）
        self._block_count = 0  # 实际吞掉次数（诊断用）

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self, auto_release_minutes=30, hotkey="ctrl+alt+x"):
        self._auto_release_minutes = max(1, int(auto_release_minutes or 30))

    def start(self):
        """装载钩子 + 注册热键（独立消息循环线程）。幂等。"""
        if self._started:
            return
        self._wx_pid = _find_weixin_pid()
        if not self._wx_pid:
            log.warning("[InputBlocker] 未找到微信窗口，屏蔽器仍启动（微信启动后可 reload 重试）")
        self._started = True
        self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True, name="InputBlockerHook")
        self._hook_thread.start()

    def stop(self):
        if not self._started:
            return
        self._started = False
        self._enabled = False
        if self._thread_id:
            try:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def reload(self, auto_release_minutes=30):
        """热重载配置；微信 pid 丢失时重试定位。"""
        self.configure(auto_release_minutes=auto_release_minutes)
        if self._started and not self._wx_pid:
            self._wx_pid = _find_weixin_pid()

    def _hook_loop(self):
        self._thread_id = kernel32.GetCurrentThreadId()

        def _cb(nCode, wParam, lParam):
            if nCode == HC_ACTION:
                self._call_count += 1
                if self._should_block(wParam, lParam):
                    self._block_count += 1
                    return 1  # 吞掉
            return user32.CallNextHookEx(self._hook, nCode, wParam, lParam)

        self._hookproc = HOOKPROC(_cb)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hookproc, None, 0)
        if not self._hook:
            log.error(f"[InputBlocker] 装载鼠标钩子失败 (GetLastError={ctypes.get_last_error()})")
            return
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_X):
            log.warning("[InputBlocker] 注册热键 Ctrl+Alt+X 失败（可能已被其他程序占用）")
        log.info(f"[InputBlocker] 钩子已装载（微信pid={self._wx_pid}，热键=Ctrl+Alt+X）")

        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.toggle(reason="热键 Ctrl+Alt+X")
            elif msg.message == WM_QUIT:
                break
        try:
            user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        log.info(f"[InputBlocker] 钩子已卸载（回调触发 {self._call_count} 次，吞掉 {self._block_count} 次）")

    def _should_block(self, wParam, lParam) -> bool:
        """落在微信进程窗口的左键点击才吞；机器人操作中/未启用/无 pid → 放行。"""
        if not self._enabled or self._bot_active or not self._wx_pid:
            return False
        if wParam not in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            return False
        try:
            info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            hwnd = user32.WindowFromPoint(info.pt)
            if not hwnd:
                return False
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value == self._wx_pid
        except Exception:
            return False

    def set_bot_active(self, b: bool):
        """机器人持 UI 锁操作期间置 True，放行其点击。"""
        self._bot_active = bool(b)

    def enable(self, reason="") -> str:
        with self._lock:
            if not self._started:
                return "❌ 屏蔽器未启动"
            self._enabled = True
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._auto_release_minutes * 60, self._auto_release)
            self._timer.daemon = True
            self._timer.start()
        msg = (f"🛡 微信人工操作已屏蔽（{reason or '手动'}），{self._auto_release_minutes} 分钟后自动解除；"
               f"Ctrl+Alt+X 或 /解除屏蔽 可立即解除")
        self._notify(msg)
        return msg

    def disable(self, reason="") -> str:
        with self._lock:
            self._enabled = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
        msg = f"✅ 微信人工操作已放行（{reason or '手动'}），可正常操作微信"
        self._notify(msg)
        return msg

    def toggle(self, reason="") -> str:
        return self.disable(reason) if self._enabled else self.enable(reason)

    def _auto_release(self):
        try:
            self.disable(reason=f"超时 {self._auto_release_minutes}min 自动解除")
        except Exception as e:
            log.error(f"[InputBlocker] 超时解除异常: {e}")

    def status(self) -> str:
        return "屏蔽中" if self._enabled else "已放行"

    def _notify(self, msg: str):
        log.info(f"[InputBlocker] {msg}")
        try:
            from pyweixin import Messages
            threading.Thread(
                target=lambda: Messages.send_messages_to_friend(
                    friend="文件传输助手", messages=[msg], close_weixin=False),
                daemon=True, name="InputBlockerNotify").start()
        except Exception as e:
            log.warning(f"[InputBlocker] 通知发送失败: {e}")


# 全局单例
input_blocker = InputBlocker()
