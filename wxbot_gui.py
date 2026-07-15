# -*- coding: utf-8 -*-
"""wxbot 微信机器人 — tkinter 配置界面 + 服务监控。

exe 入口：python wxbot_gui.py 或打包后 wxbot.exe。
功能：核心配置编辑/保存、服务启停、运行状态监控、实时日志查看。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# 开发模式下确保项目根目录在 sys.path
if not getattr(sys, 'frozen', False):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

from wxbot.paths import ensure_dirs, get_logs_dir, get_config_dir
ensure_dirs()

from wxbot.config import bot_config
from wxbot.logger import log


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _tail_file(path: str, n: int = 200) -> str:
    """读取文件最后 n 行。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return "".join(lines[-n:])
    except Exception:
        return ""


def _latest_log_path() -> str:
    """找到最新的日志文件路径。"""
    logs_dir = get_logs_dir()
    if not os.path.isdir(logs_dir):
        return ""
    files = [os.path.join(logs_dir, f) for f in os.listdir(logs_dir)
             if f.startswith("wxbot_") and f.endswith(".log")]
    if not files:
        return ""
    return max(files, key=os.path.getmtime)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class WxBotApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("wxbot 微信机器人")
        self.root.update_idletasks()
        # 启动定位到桌面右上角：尺寸先适配屏幕（固定 1100 高在 1080p 会超出，
        # Windows 会强制重定位窗口导致右上角定位失效），再算右上角坐标
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        w = min(600, max(400, screen_w - 40))
        h = min(1100, max(600, screen_h - 40))
        self.root.minsize(w, min(h, 800))
        x = max(0, screen_w - w - 20)
        y = 20
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self._running = False
        self._start_time: float | None = None
        self._monitor = None
        self._mqtt_worker = None

        # 加载配置
        self._cfg = bot_config.load()

        # 选项卡
        self._notebook = ttk.Notebook(root)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._build_config_tab()
        self._build_status_tab()
        self._build_log_tab()

        # 底部按钮栏
        btn_frame = ttk.Frame(root)
        btn_frame.pack(fill=tk.X, padx=6, pady=(0, 6))

        self._btn_save = ttk.Button(btn_frame, text="💾 保存配置", command=self._save_config)
        self._btn_save.pack(side=tk.LEFT, padx=4)

        self._btn_start = ttk.Button(btn_frame, text="▶ 启动服务", command=self._start_service)
        self._btn_start.pack(side=tk.LEFT, padx=4)

        self._btn_stop = ttk.Button(btn_frame, text="⏹ 停止服务", command=self._stop_service, state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        # 状态栏
        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self._status_var, relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X, padx=6, pady=(0, 6))

        # 定时刷新
        self._refresh_status()
        self._refresh_log()

    # ---- 配置页 ----

    def _build_config_tab(self) -> None:
        frame = ttk.Frame(self._notebook)
        self._notebook.add(frame, text=" 配置 ")

        canvas = tk.Canvas(frame)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=canvas.yview)
        self._config_inner = ttk.Frame(canvas)
        self._config_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._config_inner, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 鼠标滚轮
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        inner = self._config_inner
        row = 0

        # -- 监听设置 --
        lf1 = ttk.LabelFrame(inner, text="监听设置", padding=8)
        lf1.grid(row=row, column=0, sticky=tk.EW, padx=4, pady=4)
        row += 1

        ttk.Label(lf1, text="管理员:").grid(row=0, column=0, sticky=tk.W)
        self._var_admin = tk.StringVar(value=self._cfg.get("admin", ""))
        ttk.Entry(lf1, textvariable=self._var_admin, width=24).grid(row=0, column=1, sticky=tk.W, padx=4)

        self._var_all_listen = tk.BooleanVar(value=self._cfg.get("AllListen_switch", False))
        ttk.Radiobutton(lf1, text="白名单模式", variable=self._var_all_listen, value=False).grid(
            row=1, column=0, sticky=tk.W)
        ttk.Radiobutton(lf1, text="全局监听", variable=self._var_all_listen, value=True).grid(
            row=1, column=1, sticky=tk.W)

        # 白名单用户
        ttk.Label(lf1, text="白名单用户:").grid(row=2, column=0, sticky=tk.NW, pady=(6, 0))
        lf_user = ttk.Frame(lf1)
        lf_user.grid(row=2, column=1, sticky=tk.W, pady=(6, 0))
        self._list_listen = tk.Listbox(lf_user, height=4, width=18)
        self._list_listen.pack(side=tk.LEFT)
        for u in self._cfg.get("listen_list", []):
            self._list_listen.insert(tk.END, u)
        btn_user = ttk.Frame(lf_user)
        btn_user.pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_user, text="+", width=3, command=lambda: self._add_to_listbox(
            self._list_listen, "添加白名单用户")).pack(pady=1)
        ttk.Button(btn_user, text="-", width=3, command=lambda: self._del_from_listbox(
            self._list_listen)).pack(pady=1)

        # 监听群组
        ttk.Label(lf1, text="监听群组:").grid(row=3, column=0, sticky=tk.NW, pady=(6, 0))
        lf_group = ttk.Frame(lf1)
        lf_group.grid(row=3, column=1, sticky=tk.W, pady=(6, 0))
        self._list_group = tk.Listbox(lf_group, height=4, width=18)
        self._list_group.pack(side=tk.LEFT)
        for g in self._cfg.get("group", []):
            self._list_group.insert(tk.END, g)
        btn_group = ttk.Frame(lf_group)
        btn_group.pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_group, text="+", width=3, command=lambda: self._add_to_listbox(
            self._list_group, "添加群组")).pack(pady=1)
        ttk.Button(btn_group, text="-", width=3, command=lambda: self._del_from_listbox(
            self._list_group)).pack(pady=1)

        # -- 自动操作 --
        lf2 = ttk.LabelFrame(inner, text="自动操作", padding=8)
        lf2.grid(row=row, column=0, sticky=tk.EW, padx=4, pady=4)
        row += 1

        self._var_transfer = tk.BooleanVar(value=self._cfg.get("auto_collect_transfer", False))
        ttk.Checkbutton(lf2, text="转账自动收款", variable=self._var_transfer).grid(
            row=0, column=0, sticky=tk.W)

        self._var_red_packet = tk.BooleanVar(value=self._cfg.get("auto_open_red_packet", False))
        ttk.Checkbutton(lf2, text="红包自动拆开", variable=self._var_red_packet).grid(
            row=0, column=1, sticky=tk.W)

        self._var_group_monitor = tk.BooleanVar(value=self._cfg.get("group_monitor_switch", False))
        ttk.Checkbutton(lf2, text="群消息关键词监控", variable=self._var_group_monitor).grid(
            row=1, column=0, sticky=tk.W)

        self._var_new_friend = tk.BooleanVar(value=self._cfg.get("new_friend_switch", False))
        ttk.Checkbutton(lf2, text="新好友自动回复", variable=self._var_new_friend).grid(
            row=1, column=1, sticky=tk.W)

        self._var_input_block = tk.BooleanVar(value=self._cfg.get("input_block", {}).get("enabled", False))
        ttk.Checkbutton(lf2, text="屏蔽人工操作", variable=self._var_input_block).grid(
            row=2, column=0, sticky=tk.W)

        # -- 每日启停 --
        lf3 = ttk.LabelFrame(inner, text="每日启停", padding=8)
        lf3.grid(row=row, column=0, sticky=tk.EW, padx=4, pady=4)
        row += 1

        self._var_daily_switch = tk.BooleanVar(value=self._cfg.get("everyday_start_stop_bot_switch", False))
        ttk.Checkbutton(lf3, text="启用每日启停", variable=self._var_daily_switch).grid(
            row=0, column=0, columnspan=2, sticky=tk.W)

        ttk.Label(lf3, text="停止时间:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        self._var_stop_time = tk.StringVar(value=self._cfg.get("everyday_stop_bot_time", "23:00"))
        ttk.Entry(lf3, textvariable=self._var_stop_time, width=8).grid(row=1, column=1, sticky=tk.W, pady=(4, 0))

        ttk.Label(lf3, text="恢复时间:").grid(row=1, column=2, sticky=tk.W, padx=(12, 0), pady=(4, 0))
        self._var_start_time = tk.StringVar(value=self._cfg.get("everyday_start_bot_time", "08:00"))
        ttk.Entry(lf3, textvariable=self._var_start_time, width=8).grid(row=1, column=3, sticky=tk.W, pady=(4, 0))

        # -- MQTT设置 --
        lf4 = ttk.LabelFrame(inner, text="MQTT 设置", padding=8)
        lf4.grid(row=row, column=0, sticky=tk.EW, padx=4, pady=4)
        row += 1

        mqtt_cfg = self._cfg.get("mqtt_worker", {})
        self._var_mqtt_enabled = tk.BooleanVar(value=mqtt_cfg.get("enabled", False))
        ttk.Checkbutton(lf4, text="启用 MQTT", variable=self._var_mqtt_enabled).grid(
            row=0, column=0, columnspan=2, sticky=tk.W)

        broker = mqtt_cfg.get("broker", {})
        ttk.Label(lf4, text="Broker:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        host = broker.get("host", "localhost")
        port = broker.get("port", 1883)
        self._var_mqtt_broker = tk.StringVar(value=f"{host}:{port}")
        ttk.Entry(lf4, textvariable=self._var_mqtt_broker, width=20).grid(
            row=1, column=1, sticky=tk.W, pady=(4, 0))

        ttk.Label(lf4, text="用户名:").grid(row=2, column=0, sticky=tk.W, pady=(4, 0))
        self._var_mqtt_user = tk.StringVar(value=broker.get("username", ""))
        ttk.Entry(lf4, textvariable=self._var_mqtt_user, width=20).grid(
            row=2, column=1, sticky=tk.W, pady=(4, 0))

        ttk.Label(lf4, text="密码:").grid(row=3, column=0, sticky=tk.W, pady=(4, 0))
        self._var_mqtt_pass = tk.StringVar(value=broker.get("password", ""))
        ttk.Entry(lf4, textvariable=self._var_mqtt_pass, width=20, show="*").grid(
            row=3, column=1, sticky=tk.W, pady=(4, 0))

    # ---- 状态页 ----

    def _build_status_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text=" 状态 ")

        self._status_labels: dict[str, tk.StringVar] = {}

        fields = [
            ("service", "服务状态"),
            ("uptime", "运行时长"),
            ("messages", "消息处理"),
            ("transfer", "转账收款"),
            ("red_packet", "红包拆开"),
            ("mqtt", "MQTT 连接"),
            ("contacts", "联系人缓存"),
        ]
        for i, (key, label) in enumerate(fields):
            ttk.Label(frame, text=f"{label}:", font=("", 10, "bold")).grid(
                row=i, column=0, sticky=tk.W, pady=3)
            var = tk.StringVar(value="--")
            self._status_labels[key] = var
            ttk.Label(frame, textvariable=var, font=("", 10)).grid(
                row=i, column=1, sticky=tk.W, padx=(12, 0), pady=3)

    # ---- 日志页 ----

    def _build_log_tab(self) -> None:
        frame = ttk.Frame(self._notebook)
        self._notebook.add(frame, text=" 日志 ")

        self._log_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Consolas", 9),
                                                    state=tk.DISABLED, bg="#1e1e1e", fg="#d4d4d4")
        self._log_text.pack(fill=tk.BOTH, expand=True)

    # ---- 操作 ----

    def _add_to_listbox(self, listbox: tk.Listbox, title: str) -> None:
        """弹出输入框，向列表添加一项。"""
        from tkinter import simpledialog
        val = simpledialog.askstring(title, "请输入名称:")
        if val and val.strip():
            listbox.insert(tk.END, val.strip())

    def _del_from_listbox(self, listbox: tk.Listbox) -> None:
        """删除列表中选中项。"""
        sel = listbox.curselection()
        if sel:
            listbox.delete(sel[0])

    def _save_config(self) -> None:
        """将界面值写回 bot_config 并持久化。"""
        try:
            bot_config.set("admin", self._var_admin.get())
            bot_config.set("AllListen_switch", self._var_all_listen.get())
            bot_config.set("listen_list", list(self._list_listen.get(0, tk.END)))
            bot_config.set("group", list(self._list_group.get(0, tk.END)))
            bot_config.set("auto_collect_transfer", self._var_transfer.get())
            bot_config.set("auto_open_red_packet", self._var_red_packet.get())
            bot_config.set("group_monitor_switch", self._var_group_monitor.get())
            bot_config.set("new_friend_switch", self._var_new_friend.get())
            bot_config.set("everyday_start_stop_bot_switch", self._var_daily_switch.get())
            bot_config.set("everyday_stop_bot_time", self._var_stop_time.get())
            bot_config.set("everyday_start_bot_time", self._var_start_time.get())

            # input_block
            ib = dict(bot_config.get("input_block", {}))
            ib["enabled"] = self._var_input_block.get()
            bot_config.set("input_block", ib)

            # MQTT
            mqtt = dict(bot_config.get("mqtt_worker", {}))
            mqtt["enabled"] = self._var_mqtt_enabled.get()
            broker_str = self._var_mqtt_broker.get().strip()
            if ":" in broker_str:
                h, p = broker_str.rsplit(":", 1)
                try:
                    mqtt.setdefault("broker", {})["host"] = h.strip()
                    mqtt.setdefault("broker", {})["port"] = int(p.strip())
                except ValueError:
                    pass
            mqtt.setdefault("broker", {})["username"] = self._var_mqtt_user.get()
            mqtt.setdefault("broker", {})["password"] = self._var_mqtt_pass.get()
            bot_config.set("mqtt_worker", mqtt)

            bot_config.save()
            self._cfg = bot_config.cfg
            self._status_var.set("配置已保存 ✓")
            messagebox.showinfo("保存成功", "配置已保存到 config/config.json")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _start_service(self) -> None:
        """在子线程启动机器人服务。"""
        if self._running:
            return
        try:
            from wxbot import monitor as _monitor
            from wxbot.mqtt.worker import mqtt_worker

            # 初始化 MQTT（如果启用）
            mqtt_cfg = bot_config.get("mqtt_worker", {})
            if mqtt_cfg.get("enabled"):
                try:
                    mqtt_worker.initialize()
                except Exception as e:
                    log.warning(f"MQTT 初始化失败: {e}")

            self._monitor = _monitor
            self._mqtt_worker = mqtt_worker

            # 人工操作屏蔽（与 main.py 一致：勾选则装低级鼠标钩子并启用）
            _ib = bot_config.get("input_block", {}) or {}
            if _ib.get("enabled"):
                try:
                    from wxbot.input_blocker import input_blocker
                    input_blocker.configure(auto_release_minutes=_ib.get("auto_release_minutes", 30))
                    input_blocker.start()
                    input_blocker.enable(reason="GUI 启动")
                    log.info("🛡 人工操作屏蔽已启用（Ctrl+Alt+X 或 /解除屏蔽 解除）")
                except Exception as e:
                    log.warning(f"人工操作屏蔽启动失败: {e}")

            def _run():
                try:
                    _monitor.loop()
                except Exception as e:
                    log.error(f"服务异常: {e}")

            self._thread = threading.Thread(target=_run, daemon=True)
            self._thread.start()
            self._running = True
            self._start_time = time.time()
            self._btn_start.config(state=tk.DISABLED)
            self._btn_stop.config(state=tk.NORMAL)
            self._status_var.set("服务已启动 ▶")
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def _stop_service(self) -> None:
        """停止机器人服务。"""
        if not self._running:
            return
        try:
            if self._monitor:
                self._monitor.stop()
            # 停止人工操作屏蔽（解除并卸载钩子）
            try:
                from wxbot.input_blocker import input_blocker
                if input_blocker._started:
                    input_blocker.disable(reason="GUI 停止")
                    input_blocker.stop()
            except Exception as e:
                log.warning(f"人工操作屏蔽停止失败: {e}")
            self._running = False
            self._start_time = None
            self._btn_start.config(state=tk.NORMAL)
            self._btn_stop.config(state=tk.DISABLED)
            self._status_var.set("服务已停止 ⏹")
        except Exception as e:
            messagebox.showerror("停止失败", str(e))

    # ---- 定时刷新 ----

    def _refresh_status(self) -> None:
        """每2秒刷新状态页。"""
        try:
            if self._running:
                self._status_labels["service"].set("● 运行中")
                if self._start_time:
                    elapsed = int(time.time() - self._start_time)
                    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
                    self._status_labels["uptime"].set(f"{h:02d}:{m:02d}:{s:02d}")
            else:
                self._status_labels["service"].set("○ 已停止")
                self._status_labels["uptime"].set("--")

            # MQTT 状态
            try:
                from wxbot.mqtt.worker import mqtt_worker
                if mqtt_worker.enabled:
                    status = mqtt_worker.get_status()
                    connected = status.get("connected", False)
                    self._status_labels["mqtt"].set("已连接" if connected else "未连接")
                    contacts = status.get("contacts_cached", 0)
                    self._status_labels["contacts"].set(f"{contacts} 人" if contacts else "--")
                else:
                    self._status_labels["mqtt"].set("未启用")
                    self._status_labels["contacts"].set("--")
            except Exception:
                self._status_labels["mqtt"].set("--")
                self._status_labels["contacts"].set("--")

            self._status_labels["messages"].set("--")
            self._status_labels["transfer"].set("--")
            self._status_labels["red_packet"].set("--")
        except Exception:
            pass
        self.root.after(2000, self._refresh_status)

    def _refresh_log(self) -> None:
        """每1秒刷新日志页。"""
        try:
            log_path = _latest_log_path()
            if log_path:
                content = _tail_file(log_path, 200)
                self._log_text.config(state=tk.NORMAL)
                self._log_text.delete("1.0", tk.END)
                self._log_text.insert(tk.END, content)
                self._log_text.see(tk.END)
                self._log_text.config(state=tk.DISABLED)
        except Exception:
            pass
        self.root.after(1000, self._refresh_log)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    # DPI 感知：PyInstaller exe 默认不声明 DPI 感知，高 DPI 缩放下
    # winfo_screenwidth 与 geometry 坐标系不一致，导致窗口定位偏移。必须在 root 创建前调用。
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 1=系统级 DPI 感知
    except Exception:
        pass
    # Splash 文字更新（仅打包环境存在 pyi_splash，开发模式 try/except 兜底）
    try:
        import pyi_splash
        pyi_splash.update_text("正在初始化界面...")
    except Exception:
        pass
    root = tk.Tk()
    app = WxBotApp(root)
    # 主窗口就绪，关闭 splash
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass

    def _on_close():
        if app._running:
            if not messagebox.askyesno("确认退出", "服务正在运行，确定退出吗？"):
                return
            app._stop_service()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
