# -*- coding: utf-8 -*-
"""wxbot 微信机器人 — tkinter 配置界面 + 服务监控。

exe 入口：python wxbot_gui.py 或打包后 wxbot.exe。
功能：完整配置编辑(config.json + webhook.json)/保存、服务启停、运行状态监控、实时日志。
"""
from __future__ import annotations

import json
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
        # 启动定位到桌面右上角
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        w = min(640, max(440, screen_w - 40))
        h = min(900, max(600, screen_h - 40))
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
        from wxbot import webhook_send
        self._webhook_cfg = webhook_send.load_config()

        # 配置控件变量集中管理（key → tk 变量/控件）
        self._bool_vars: dict[str, tk.BooleanVar] = {}
        self._str_vars: dict[str, tk.StringVar] = {}
        self._int_vars: dict[str, tk.StringVar] = {}
        self._list_boxes: dict[str, tk.Listbox] = {}
        self._json_editors: dict[str, scrolledtext.ScrolledText] = {}
        self._text_editors: dict[str, scrolledtext.ScrolledText] = {}

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

    # ================================================================
    # 配置页（嵌套 Notebook 分组：基本信息 / 回复记忆 / 新好友 / 定时 / 朋友圈
    #   / 数字员工 / MQTT / Webhook / 高级设置）
    # ================================================================

    def _build_config_tab(self) -> None:
        frame = ttk.Frame(self._notebook)
        self._notebook.add(frame, text=" 配置 ")
        self._cfg_nb = ttk.Notebook(frame)
        self._cfg_nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._build_tab_basic()
        self._build_tab_mqtt()
        self._build_tab_webhook()
        self._build_tab_advanced()

    # ---- 滚动页与控件辅助 ----
    def _scroll_page(self, title: str) -> ttk.Frame:
        """新建一个可滚动的配置子页，返回内部 Frame。"""
        page = ttk.Frame(self._cfg_nb)
        self._cfg_nb.add(page, text=f" {title} ")
        canvas = tk.Canvas(page, highlightthickness=0)
        sb = ttk.Scrollbar(page, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        # 鼠标进入该页才接管滚轮，避免多 Canvas 互相抢占
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _lf(self, parent: ttk.Frame, title: str) -> ttk.LabelFrame:
        lf = ttk.LabelFrame(parent, text=title, padding=8)
        lf.pack(fill=tk.X, padx=4, pady=4)
        return lf

    def _bool(self, parent, key: str, label: str, r: int, c: int = 0, cs: int = 1) -> tk.BooleanVar:
        var = tk.BooleanVar(value=bool(self._cfg.get(key, False)))
        self._bool_vars[key] = var
        ttk.Checkbutton(parent, text=label, variable=var).grid(
            row=r, column=c, columnspan=cs, sticky=tk.W, pady=1)
        return var

    def _entry(self, parent, key: str, label: str, r: int, width: int = 24,
               default: str = "", show: str = "") -> tk.StringVar:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky=tk.W, pady=2)
        var = tk.StringVar(value=str(self._cfg.get(key, default)))
        self._str_vars[key] = var
        ttk.Entry(parent, textvariable=var, width=width, show=show).grid(
            row=r, column=1, sticky=tk.W, padx=4, pady=2)
        return var

    def _int(self, parent, key: str, label: str, r: int, width: int = 8, default=0) -> tk.StringVar:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky=tk.W, pady=2)
        var = tk.StringVar(value=str(self._cfg.get(key, default)))
        self._int_vars[key] = var
        ttk.Entry(parent, textvariable=var, width=width).grid(
            row=r, column=1, sticky=tk.W, padx=4, pady=2)
        return var

    def _list_edit(self, parent, key: str, label: str, r: int, height: int = 4) -> None:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky=tk.NW, pady=2)
        wrap = ttk.Frame(parent)
        wrap.grid(row=r, column=1, sticky=tk.W, pady=2)
        lb = tk.Listbox(wrap, height=height, width=22)
        lb.pack(side=tk.LEFT)
        for v in self._cfg.get(key, []):
            lb.insert(tk.END, v)
        bf = ttk.Frame(wrap)
        bf.pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="+", width=3,
                   command=lambda: self._add_to_listbox(lb, f"添加{label}")).pack(pady=1)
        ttk.Button(bf, text="-", width=3, command=lambda: self._del_from_listbox(lb)).pack(pady=1)
        self._list_boxes[key] = lb

    def _json_edit(self, parent, key: str, label: str, r: int, height: int = 8, value=None) -> None:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky=tk.NW, pady=2)
        txt = scrolledtext.ScrolledText(parent, width=52, height=height,
                                        font=("Consolas", 9), wrap=tk.NONE)
        txt.grid(row=r, column=1, sticky=tk.W, pady=2)
        if value is None:
            value = self._cfg.get(key, "")
        txt.insert("1.0", value if isinstance(value, str)
                   else json.dumps(value, ensure_ascii=False, indent=2))
        self._json_editors[key] = txt

    def _text_edit(self, parent, key: str, label: str, r: int, height: int = 4, value: str = "") -> None:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky=tk.NW, pady=2)
        txt = scrolledtext.ScrolledText(parent, width=52, height=height, font=("Consolas", 9))
        txt.grid(row=r, column=1, sticky=tk.W, pady=2)
        txt.insert("1.0", value or "")
        self._text_editors[key] = txt

    def _kv_entry(self, parent, key: str, label: str, r: int, value,
                  width: int = 24, show: str = "") -> int:
        """嵌套字段的键值 Entry（值由调用方传入，存入 _str_vars）。返回下一行号。"""
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky=tk.W, pady=2)
        var = tk.StringVar(value=str(value))
        self._str_vars[key] = var
        ttk.Entry(parent, textvariable=var, width=width, show=show).grid(
            row=r, column=1, sticky=tk.W, padx=4, pady=2)
        return r + 1

    # ---- 基本信息 ----
    def _build_tab_basic(self) -> None:
        p = self._scroll_page("基本信息")
        lf = self._lf(p, "身份与监听模式"); r = 0
        self._entry(lf, "admin", "管理员:", r); r += 1
        self._bool(lf, "AllListen_switch", "全局监听(黑名单)模式", r); r += 1
        self._bool(lf, "AllListen_filter_mute", "全局监听过滤免打扰", r); r += 1
        self._bool(lf, "chat_listen_only", "私聊只监听不回复", r); r += 1
        self._bool(lf, "group_switch", "群聊监听总开关", r); r += 1
        self._bool(lf, "group_listen_only", "群聊只监听不回复", r); r += 1

        lf2 = self._lf(p, "名单"); r = 0
        self._list_edit(lf2, "listen_list", "白名单用户", r); r += 1
        self._list_edit(lf2, "group", "监听群组", r); r += 1
        self._list_edit(lf2, "black_list", "黑名单", r); r += 1

    # ---- MQTT ----
    def _build_tab_mqtt(self) -> None:
        p = self._scroll_page("MQTT")
        m = self._cfg.get("mqtt_worker", {}) or {}
        broker = m.get("broker", {}) or {}

        lf = self._lf(p, "启用与 Broker"); r = 0
        ve = tk.BooleanVar(value=bool(m.get("enabled", False))); self._bool_vars["mqtt_enabled"] = ve
        ttk.Checkbutton(lf, text="启用 MQTT", variable=ve).grid(row=r, column=0, sticky=tk.W, pady=1); r += 1
        ttk.Label(lf, text="Host:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vh = tk.StringVar(value=str(broker.get("host", "localhost"))); self._str_vars["mqtt_host"] = vh
        ttk.Entry(lf, textvariable=vh, width=24).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        ttk.Label(lf, text="Port:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vp = tk.StringVar(value=str(broker.get("port", 1883))); self._int_vars["mqtt_port"] = vp
        ttk.Entry(lf, textvariable=vp, width=8).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        ttk.Label(lf, text="用户名:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vu = tk.StringVar(value=str(broker.get("username", ""))); self._str_vars["mqtt_user"] = vu
        ttk.Entry(lf, textvariable=vu, width=24).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        ttk.Label(lf, text="密码:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vpw = tk.StringVar(value=str(broker.get("password", ""))); self._str_vars["mqtt_pass"] = vpw
        ttk.Entry(lf, textvariable=vpw, width=24, show="*").grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        ttk.Label(lf, text="Vhost:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vv = tk.StringVar(value=str(broker.get("vhost", "/"))); self._str_vars["mqtt_vhost"] = vv
        ttk.Entry(lf, textvariable=vv, width=24).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        vt = tk.BooleanVar(value=bool(broker.get("tls", False))); self._bool_vars["mqtt_tls"] = vt
        ttk.Checkbutton(lf, text="TLS", variable=vt).grid(row=r, column=0, sticky=tk.W, pady=1); r += 1

        lf2 = self._lf(p, "MinIO 对象存储"); r = 0
        minio = m.get("minio", {}) or {}
        r = self._kv_entry(lf2, "minio_endpoint", "Endpoint:", r, minio.get("endpoint", ""))
        r = self._kv_entry(lf2, "minio_access_key", "Access Key:", r, minio.get("access_key", ""), show="*")
        r = self._kv_entry(lf2, "minio_secret_key", "Secret Key:", r, minio.get("secret_key", ""), show="*")
        r = self._kv_entry(lf2, "minio_bucket", "Bucket:", r, minio.get("bucket", "wbot"))
        r = self._kv_entry(lf2, "minio_public_url", "公开URL前缀:", r, minio.get("public_url_prefix", ""))
        vsec = tk.BooleanVar(value=bool(minio.get("secure", True))); self._bool_vars["minio_secure"] = vsec
        ttk.Checkbutton(lf2, text="Secure (HTTPS)", variable=vsec).grid(row=r, column=0, sticky=tk.W, pady=1)

        lf3 = self._lf(p, "Worker 身份（管理第 1 个身份；多身份请编辑 config.json）"); r = 0
        wk = (m.get("workers", []) or [{}])[0] or {}
        topics = wk.get("topics", {}) or {}
        vwe = tk.BooleanVar(value=bool(wk.get("enabled", True))); self._bool_vars["wk_enabled"] = vwe
        ttk.Checkbutton(lf3, text="启用", variable=vwe).grid(row=r, column=0, sticky=tk.W, pady=1); r += 1
        r = self._kv_entry(lf3, "wk_role", "Role:", r, wk.get("role", "default"))
        r = self._kv_entry(lf3, "wk_agent_id", "Agent ID:", r, wk.get("agent_id", "wx_001"))
        r = self._kv_entry(lf3, "wk_subscribe", "Subscribe Topic:", r, topics.get("subscribe", ""))
        r = self._kv_entry(lf3, "wk_callback", "Callback Prefix:", r, topics.get("callback_prefix", ""))
        r = self._kv_entry(lf3, "wk_forward", "Forward Topic:", r, topics.get("forward", ""))
        ttk.Label(lf3, text="转发联系人:").grid(row=r, column=0, sticky=tk.NW, pady=2)
        wrap = ttk.Frame(lf3); wrap.grid(row=r, column=1, sticky=tk.W, pady=2)
        lb = tk.Listbox(wrap, height=4, width=22); lb.pack(side=tk.LEFT)
        for c in (wk.get("forward_contacts", []) or []):
            lb.insert(tk.END, c)
        bf = ttk.Frame(wrap); bf.pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="+", width=3,
                   command=lambda: self._add_to_listbox(lb, "添加转发联系人")).pack(pady=1)
        ttk.Button(bf, text="-", width=3, command=lambda: self._del_from_listbox(lb)).pack(pady=1)
        self._list_boxes["wk_forward_contacts"] = lb

    # ---- Webhook ----
    def _build_tab_webhook(self) -> None:
        p = self._scroll_page("Webhook")
        w = self._webhook_cfg
        lf = self._lf(p, "飞书 / Webhook 通知"); r = 0
        ve = tk.BooleanVar(value=bool(w.get("enabled", False))); self._bool_vars["wh_enabled"] = ve
        ttk.Checkbutton(lf, text="启用 Webhook", variable=ve).grid(row=r, column=0, sticky=tk.W, pady=1); r += 1
        ttk.Label(lf, text="URL:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vu = tk.StringVar(value=str(w.get("url", ""))); self._str_vars["wh_url"] = vu
        ttk.Entry(lf, textvariable=vu, width=40).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        ttk.Label(lf, text="Method:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vm = tk.StringVar(value=str(w.get("method", "POST"))); self._str_vars["wh_method"] = vm
        ttk.Entry(lf, textvariable=vm, width=10).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        ttk.Label(lf, text="Content-Type:").grid(row=r, column=0, sticky=tk.W, pady=2)
        vc = tk.StringVar(value=str(w.get("content_type", "application/json"))); self._str_vars["wh_ct"] = vc
        ttk.Entry(lf, textvariable=vc, width=24).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1
        ttk.Label(lf, text="Timeout(秒):").grid(row=r, column=0, sticky=tk.W, pady=2)
        vt = tk.StringVar(value=str(w.get("timeout", 5))); self._int_vars["wh_timeout"] = vt
        ttk.Entry(lf, textvariable=vt, width=8).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1

        lf2 = self._lf(p, "Headers (JSON)")
        self._json_edit(lf2, "wh_headers", "headers", 0, height=5, value=w.get("headers", {}))
        lf3 = self._lf(p, "Body 模板 (支持 $title / $content)")
        self._text_edit(lf3, "wh_body", "body", 0, height=6, value=w.get("body", ""))

    # ---- 高级设置 ----
    def _build_tab_advanced(self) -> None:
        p = self._scroll_page("高级设置")
        lf = self._lf(p, "运行参数"); r = 0
        self._int(lf, "monitor_check_interval", "监听轮询间隔(秒)", r); r += 1
        self._int(lf, "monitor_run_timeout", "单轮超时(秒)", r); r += 1
        self._int(lf, "contacts_refresh_timeout", "联系人刷新超时(秒)", r); r += 1
        self._int(lf, "voice_message_delay", "语音转文字等待(秒)", r); r += 1

        lf2 = self._lf(p, "自动操作与转发"); r = 0
        self._bool(lf2, "auto_collect_transfer", "转账自动收款", r, c=0)
        self._bool(lf2, "auto_open_red_packet", "红包自动拆开", r, c=1); r += 1
        self._bool(lf2, "group_monitor_switch", "群关键词监控", r, c=0); r += 1
        self._json_edit(lf2, "group_monitor_list", "group_monitor_list", r, height=5); r += 1
        self._bool(lf2, "custom_forward_switch", "自定义转发", r); r += 1
        self._json_edit(lf2, "custom_forward_list", "custom_forward_list", r, height=5); r += 1

        lf3 = self._lf(p, "群欢迎"); r = 0
        self._bool(lf3, "group_welcome", "启用入群欢迎", r); r += 1
        self._entry(lf3, "group_welcome_msg", "欢迎语", r); r += 1
        self._entry(lf3, "group_welcome_random", "欢迎随机度(0~1)", r, width=8); r += 1

        lf4 = self._lf(p, "每日启停"); r = 0
        self._bool(lf4, "everyday_start_stop_bot_switch", "启用每日启停", r); r += 1
        self._entry(lf4, "everyday_stop_bot_time", "停止时间", r, width=8); r += 1
        self._entry(lf4, "everyday_start_bot_time", "恢复时间", r, width=8); r += 1

        lf5 = self._lf(p, "人工操作屏蔽"); r = 0
        ib = self._cfg.get("input_block", {}) or {}
        ve = tk.BooleanVar(value=bool(ib.get("enabled", False))); self._bool_vars["ib_enabled"] = ve
        ttk.Checkbutton(lf5, text="启用屏蔽", variable=ve).grid(row=r, column=0, sticky=tk.W, pady=1); r += 1
        ttk.Label(lf5, text="自动解除(分钟):").grid(row=r, column=0, sticky=tk.W, pady=2)
        va = tk.StringVar(value=str(ib.get("auto_release_minutes", 30))); self._int_vars["ib_auto_release"] = va
        ttk.Entry(lf5, textvariable=va, width=8).grid(row=r, column=1, sticky=tk.W, padx=4); r += 1

    # ================================================================
    # 状态页 / 日志页
    # ================================================================

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

    def _build_log_tab(self) -> None:
        frame = ttk.Frame(self._notebook)
        self._notebook.add(frame, text=" 日志 ")

        self._log_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Consolas", 9),
                                                    state=tk.DISABLED, bg="#1e1e1e", fg="#d4d4d4")
        self._log_text.pack(fill=tk.BOTH, expand=True)

    # ================================================================
    # 列表编辑 / 保存 / 启停
    # ================================================================

    def _add_to_listbox(self, listbox: tk.Listbox, title: str) -> None:
        from tkinter import simpledialog
        val = simpledialog.askstring(title, "请输入名称:")
        if val and val.strip():
            listbox.insert(tk.END, val.strip())

    def _del_from_listbox(self, listbox: tk.Listbox) -> None:
        sel = listbox.curselection()
        if sel:
            listbox.delete(sel[0])

    def _save_config(self) -> None:
        """将界面值写回 bot_config(→config.json) 与 webhook_send(→webhook.json) 并持久化。"""
        try:
            _FLOAT_KEYS = {"knowledge_threshold", "group_welcome_random"}

            # bool 标量（跳过特殊前缀，下面单独处理 mqtt/wh/ib/minio/wk）
            for k, v in self._bool_vars.items():
                if (k in ("mqtt_enabled", "mqtt_tls", "wh_enabled", "ib_enabled")
                        or k.startswith(("minio_", "wk_"))):
                    continue
                bot_config.set(k, v.get())
            # str 标量
            for k, v in self._str_vars.items():
                if k.startswith(("mqtt_", "wh_", "minio_", "wk_")):
                    continue
                val: object = v.get()
                if k in _FLOAT_KEYS:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
                bot_config.set(k, val)
            # int 标量
            for k, v in self._int_vars.items():
                if k.startswith("mqtt_") or k.startswith("wh_") or k.startswith("ib_"):
                    continue
                try:
                    bot_config.set(k, int(v.get()))
                except ValueError:
                    bot_config.set(k, v.get())
            # 列表（跳过 wk_ 前缀，归属 worker 单独处理）
            for k, lb in self._list_boxes.items():
                if k.startswith("wk_"):
                    continue
                bot_config.set(k, list(lb.get(0, tk.END)))
            # JSON 编辑器（跳过 wh_headers，单独处理）
            for k, txt in self._json_editors.items():
                if k == "wh_headers":
                    continue
                raw = txt.get("1.0", tk.END).strip()
                if raw:
                    try:
                        bot_config.set(k, json.loads(raw))
                    except json.JSONDecodeError:
                        raise ValueError(f"{k} 的 JSON 格式错误")
                else:
                    bot_config.set(k, [] if k.endswith("_list")
                                   else {} if (k.endswith("_map") or k.endswith("_dict")
                                               or k in ("friend_add", "api_configs")) else "")

            # ---- MQTT（合并 broker entry + extra JSON）----
            mqtt = dict(bot_config.get("mqtt_worker", {}))
            mqtt["enabled"] = self._bool_vars["mqtt_enabled"].get()
            broker = mqtt.setdefault("broker", {})
            broker["host"] = self._str_vars["mqtt_host"].get().strip() or "localhost"
            try:
                broker["port"] = int(self._int_vars["mqtt_port"].get())
            except ValueError:
                broker["port"] = 1883
            broker["username"] = self._str_vars["mqtt_user"].get()
            broker["password"] = self._str_vars["mqtt_pass"].get()
            broker["vhost"] = self._str_vars["mqtt_vhost"].get() or "/"
            broker["tls"] = self._bool_vars["mqtt_tls"].get()
            # MinIO（每个字段独立）
            mqtt["minio"] = {
                "endpoint": self._str_vars["minio_endpoint"].get(),
                "access_key": self._str_vars["minio_access_key"].get(),
                "secret_key": self._str_vars["minio_secret_key"].get(),
                "bucket": self._str_vars["minio_bucket"].get() or "wbot",
                "secure": self._bool_vars["minio_secure"].get(),
                "public_url_prefix": self._str_vars["minio_public_url"].get(),
            }
            # Worker 身份（更新第 1 个，保留其余）
            new_wk = {
                "enabled": self._bool_vars["wk_enabled"].get(),
                "role": self._str_vars["wk_role"].get() or "default",
                "agent_id": self._str_vars["wk_agent_id"].get(),
                "topics": {
                    "subscribe": self._str_vars["wk_subscribe"].get(),
                    "callback_prefix": self._str_vars["wk_callback"].get(),
                    "forward": self._str_vars["wk_forward"].get(),
                },
                "forward_contacts": list(self._list_boxes["wk_forward_contacts"].get(0, tk.END)),
            }
            workers = list(mqtt.get("workers", []) or [])
            if workers:
                workers[0] = new_wk
            else:
                workers = [new_wk]
            mqtt["workers"] = workers
            bot_config.set("mqtt_worker", mqtt)

            # ---- input_block ----
            ib = dict(bot_config.get("input_block", {}))
            ib["enabled"] = self._bool_vars["ib_enabled"].get()
            try:
                ib["auto_release_minutes"] = int(self._int_vars["ib_auto_release"].get())
            except ValueError:
                ib["auto_release_minutes"] = 30
            bot_config.set("input_block", ib)

            # ---- webhook.json ----
            wh = dict(self._webhook_cfg)
            wh["enabled"] = self._bool_vars["wh_enabled"].get()
            wh["url"] = self._str_vars["wh_url"].get().strip()
            wh["method"] = (self._str_vars["wh_method"].get() or "POST").upper()
            wh["content_type"] = self._str_vars["wh_ct"].get() or "application/json"
            try:
                wh["timeout"] = int(self._int_vars["wh_timeout"].get())
            except ValueError:
                wh["timeout"] = 5
            hdr_raw = self._json_editors["wh_headers"].get("1.0", tk.END).strip()
            if hdr_raw:
                try:
                    wh["headers"] = json.loads(hdr_raw)
                except json.JSONDecodeError:
                    raise ValueError("Webhook Headers 的 JSON 格式错误")
            else:
                wh["headers"] = {}
            wh["body"] = self._text_editors["wh_body"].get("1.0", tk.END).rstrip("\n")
            from wxbot import webhook_send
            webhook_send.save_config(wh)
            self._webhook_cfg = webhook_send.load_config()

            bot_config.save()
            self._cfg = bot_config.cfg
            self._status_var.set("配置已保存 ✓")
            messagebox.showinfo("保存成功", "配置已保存（config.json + webhook.json）")
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
            if self._mqtt_worker:
                self._mqtt_worker.shutdown()
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
