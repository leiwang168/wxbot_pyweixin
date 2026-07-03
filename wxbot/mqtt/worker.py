# -*- coding: utf-8 -*-
"""MQTT 数字员工扩展对外接口（facade）。

配置来源：`bot_config.get('mqtt_worker')` 嵌套字典。
微信事件入口：`on_wechat_message` / `on_friend_accepted`（由 monitor / friends 调用）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import pyautogui

from pyweixin import Contacts, GlobalConfig
from pyweixin.WeChatTools import Navigator, desktop, mouse
from pyweixin.Uielements import Windows

from ..config import bot_config
from ..input_blocker import input_blocker
from .adapter import MqttAdapter
from .common import MinioUploader, emit
from .coordinator import MqttCoordinator
from .executor import TaskExecutor
from .identity import WorkerIdentity
from .resolver import ContactResolver

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "config.json")
_OPERATE_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "operate_cache.json")


def _patch_open_friend_profile():
    """monkeypatch：修复 4.1.9.35 下 open_friend_profile 点头像弹资料卡不稳定。

    头像在好友按钮左侧，精确点击按钮宽度 1/8 处避免误触"添加"等按钮。
    不最大化窗口、点头像前延迟1秒等 COM 稳定。
    """
    def _patched(friend, is_maximize=None, search_pages=None):
        if search_pages is None:
            search_pages = GlobalConfig.search_pages
        # 不最大化：避免窗口缩放影响 monitor 轮询
        chatinfo_pane, main_window = Navigator.open_chatinfo(
            friend=friend, is_maximize=False, search_pages=search_pages)
        friend_button = chatinfo_pane.child_window(title=friend, control_type='Button')
        time.sleep(1)
        if not friend_button.exists(timeout=3):
            main_window.close()
            raise RuntimeError(f'找不到好友按钮：{friend}')
        # 用昵称 Text 控件推算头像中心：x=昵称水平中心-12(昵称窄于头像,中心偏右)，y=昵称.top-40
        name_ctrl = chatinfo_pane.child_window(title=friend, control_type='Text')
        if name_ctrl.exists(timeout=1):
            nr = name_ctrl.rectangle()
            avatar_x = nr.left + (nr.right - nr.left) // 2
            avatar_y = nr.top - 40
        else:
            br = friend_button.rectangle()  # 兜底：昵称 Text 找不到
            avatar_x = br.left + (br.right - br.left) // 2
            avatar_y = br.top + 40
        profile_pane = desktop.window(**Windows.PopUpProfileWindow)
        
        # 循环点击，x 向左、y 上下微调提高命中率（昵称中心偏头像右边缘，需左移）
        for dx, dy in ((0, 0), (-8, 0), (-16, 0), (-20, -0), (-24, -0)):
            mouse.click(coords=(avatar_x + dx, avatar_y + dy))
            if profile_pane.exists(timeout=3):
                time.sleep(1)
                return profile_pane, main_window
        raise RuntimeError('点击头像后资料卡弹窗未出现')
    Navigator.open_friend_profile = staticmethod(_patched)


class MqttWorkerExtension:
    """多数字员工身份管理器。"""

    def __init__(self) -> None:
        self._initialized = False
        self._adapter: MqttAdapter | None = None
        self._executor: TaskExecutor | None = None
        self._coordinator: MqttCoordinator | None = None
        self._identities: list[WorkerIdentity] = []
        self._uploader: MinioUploader | None = None
        self._resolver: ContactResolver | None = None
        self._wx_nickname = ""
        self._wx_id = ""
        self._wx_wechat_id = ""
        self._session_operate: dict[str, str] = {}  # {chat: operate} 会话级 operate 追踪
        self._last_sent: dict[str, tuple[str, float]] = {}  # {chat: (text, ts)} 防回环
        self._operate_lock = threading.Lock()

    # ---- operate 持久化（manual 接管重启不丢）----
    def set_session_operate(self, chat: str, val: str) -> None:
        """记录会话级 operate（"auto" 回退默认并移除，其他值写入），并持久化到缓存文件。"""
        with self._operate_lock:
            if val == "auto":
                self._session_operate.pop(chat, None)
            else:
                self._session_operate[chat] = val
            snapshot = dict(self._session_operate)
        try:
            with open(_OPERATE_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            emit("WARNING", f"写入 operate 缓存失败: {e}")

    def _load_operate_cache(self) -> dict:
        if not os.path.exists(_OPERATE_CACHE_PATH):
            return {}
        try:
            with open(_OPERATE_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception as e:
            emit("WARNING", f"读取 operate 缓存失败: {e}")
            return {}

    # ---- 配置迁移与校验 ----
    @staticmethod
    def _migrate_config(cfg: dict) -> list:
        if "workers" in cfg and isinstance(cfg["workers"], list):
            return cfg["workers"]
        if cfg.get("role") and cfg.get("agent_id"):
            return [{"enabled": True, "role": cfg["role"], "agent_id": cfg["agent_id"],
                     "topics": cfg.get("topics", {}), "forward_contacts": []}]
        return []

    def _validate_multi_identity(self) -> None:
        enabled = [i for i in self._identities if i.enabled]
        if len(enabled) < 2:
            return
        catchall = [i for i in enabled if not i.forward_contacts]
        if len(catchall) > 1:
            names = ", ".join(f"[{i.role}]" for i in catchall)
            emit("WARNING", f"多角色下最多 1 个兜底角色，当前 {len(catchall)} 个: {names}")
        seen: dict[str, str] = {}
        for ident in enabled:
            for c in ident.forward_contacts:
                if c in seen:
                    emit("WARNING", f"转发联系人冲突: '{c}' 同时出现在 [{seen[c]}] 和 [{ident.role}]",
                         ident.role)
                else:
                    seen[c] = ident.role

    def _refresh_wx_account_info(self) -> bool:
        """获取当前登录微信的账号信息。返回 True 表示成功。"""
        try:
            info = Contacts.check_my_info(close_weixin=False)
            self._wx_nickname = info.get("昵称", "") or ""
            self._wx_id = info.get("wxid", "") or ""
            self._wx_wechat_id = info.get("微信号", "") or ""
            if not self._wx_id:
                self._wx_id = self._wx_wechat_id
            emit("INFO", f"微信账号信息: 昵称={self._wx_nickname} wxid={self._wx_id} 微信号={self._wx_wechat_id}")
            return True
        except Exception as e:
            emit("WARNING", f"获取微信账号信息失败: {e}")
            return False

    @property
    def resolver(self) -> ContactResolver:
        if self._resolver is None:
            self._resolver = ContactResolver(log_func=emit)
        return self._resolver

    # ---- 生命周期 ----
    def initialize(self) -> None:
        # 恢复 operate 持久化状态（manual 接管等），重启后继续生效
        loaded = self._load_operate_cache()
        if loaded:
            self._session_operate = loaded
            emit("INFO", f"恢复 operate 状态 {len(loaded)} 条: {list(loaded.keys())}")
        # 修复 open_friend_profile 点头像不稳定（get_friend_profile 依赖它）
        try:
            _patch_open_friend_profile()
        except Exception as e:
            emit("WARNING", f"open_friend_profile monkeypatch 失败: {e}")
        self._refresh_wx_account_info()
        cfg = bot_config.get("mqtt_worker", {}) or {}
        self._uploader = MinioUploader(cfg.get("minio", {}) or {})

        if not cfg.get("enabled"):
            emit("INFO", "MQTT 数字员工未启用（MinIO 上传器已初始化）")
            self._initialized = True
            return

        workers_cfg = self._migrate_config(cfg)
        if not workers_cfg:
            emit("WARNING", "MQTT 已启用但无有效身份配置，跳过")
            self._initialized = True
            return

        self._identities = [WorkerIdentity(w) for w in workers_cfg]
        emit("INFO", f"初始化 MQTT 数字员工 共 {len(self._identities)} 个身份，"
              f"启用 {sum(1 for i in self._identities if i.enabled)} 个")
        self._validate_multi_identity()

        first = next((i for i in self._identities if i.enabled),
                     self._identities[0] if self._identities else None)
        agent_base = first.agent_id if first else "wbot"
        self._adapter = MqttAdapter(cfg, agent_base)
        self._executor = TaskExecutor(log_func=emit, resolver=self.resolver, uploader=self._uploader)
        self._coordinator = MqttCoordinator(cfg, self._adapter, self._executor, self._identities, extension=self)
        self._adapter.set_handlers(on_connect=lambda rc: self._on_connect(rc),
                                   on_disconnect=lambda rc: None,
                                   on_message=self._coordinator.enqueue_message)
        self._coordinator.start()
        self._initialized = True
        # 预热联系人缓存
        try:
            info = self.resolver.get_cache_info()
            emit("INFO", f"联系人缓存: {info['size']} 人 (文件: {info['cache_file']})")
        except Exception as e:
            emit("WARNING", f"联系人缓存预热失败: {e}")
        emit("INFO", "MQTT 数字员工初始化完成")

    def _on_connect(self, rc: int) -> None:
        if rc == 0 and self._coordinator:
            for t in self._coordinator.get_subscribe_topics():
                self._adapter.subscribe(t)
            emit("INFO", f"已订阅 {len(self._coordinator.get_subscribe_topics())} 个主题")

    @property
    def enabled(self) -> bool:
        return bool(self._initialized and self._coordinator)

    @property
    def wx_busy(self) -> bool:
        return self._coordinator.wx_busy if self._coordinator else False

    @property
    def ui_lock(self):
        """UI 互斥锁，monitor 和 MQTT 任务共享。"""
        return self._coordinator._ui_lock if self._coordinator else None

    def _enter_ui(self) -> None:
        """获取 UI 锁，标记进入 UI 操作（供异步媒体线程等使用）。"""
        if self._coordinator:
            if not self._coordinator._ui_lock.acquire(timeout=30):
                emit("WARNING", "UI 锁获取超时 (30s)，媒体保存跳过")
                raise RuntimeError("UI lock acquire timeout")
            self._coordinator._wx_busy_event.set()
            input_blocker.set_bot_active(True)  # 放行机器人点击

    def _exit_ui(self) -> None:
        """释放 UI 锁，标记退出 UI 操作。"""
        input_blocker.set_bot_active(False)
        if self._coordinator:
            self._coordinator._wx_busy_event.clear()
            try:
                self._coordinator._ui_lock.release()
            except RuntimeError:
                pass

    def reconfigure(self) -> None:
        self._refresh_wx_account_info()
        cfg = bot_config.get("mqtt_worker", {}) or {}
        self._uploader = MinioUploader(cfg.get("minio", {}) or {})
        if not cfg.get("enabled"):
            if self._coordinator:
                self._coordinator.shutdown()
            self._coordinator = None
            self._adapter = None
            self._executor = None
            self._identities = []
            self._initialized = True
            emit("INFO", "MQTT 数字员工已禁用（热重载）")
            return
        workers_cfg = self._migrate_config(cfg)
        if not workers_cfg:
            if self._coordinator:
                self._coordinator.shutdown()
            self._coordinator = None
            self._adapter = None
            self._executor = None
            self._identities = []
            self._initialized = True
            return
        if self._coordinator:
            self._coordinator.shutdown()
        self._identities = [WorkerIdentity(w) for w in workers_cfg]
        first = next((i for i in self._identities if i.enabled),
                     self._identities[0] if self._identities else None)
        agent_base = first.agent_id if first else "wbot"
        self._adapter = MqttAdapter(cfg, agent_base)
        self._executor = TaskExecutor(log_func=emit, resolver=self.resolver, uploader=self._uploader)
        self._coordinator = MqttCoordinator(cfg, self._adapter, self._executor, self._identities, extension=self)
        self._adapter.set_handlers(on_connect=lambda rc: self._on_connect(rc),
                                   on_disconnect=lambda rc: None,
                                   on_message=self._coordinator.enqueue_message)
        self._coordinator.start()
        self._initialized = True
        emit("INFO", f"MQTT 数字员工热重载完成 共 {len(self._identities)} 个身份")
        self._validate_multi_identity()

    def _fetch_wxid_from_profile(self, friend: str) -> str:
        """打开好友资料卡获取微信号+性别，写入缓存。新好友首条消息时调用（阻塞 UI 操作）。

        close_weixin=False 避免关闭微信窗口影响后续 monitor 轮询。
        返回微信号；失败返回空串。
        """
        try:
            emit("INFO", f"[新好友] 打开资料卡获取微信号: {friend}")
            self._enter_ui()  # 串行排队：微信客户端 UI 操作不并发
            try:
                # 直接拿 profile_pane：读微信号文本 + 截图识别性别
                profile_pane, main_window = Navigator.open_friend_profile(
                    friend=friend, is_maximize=False, search_pages=GlobalConfig.search_pages)
                import time as _t
                _t.sleep(2)  # 等资料卡控件渲染稳定
                profile = self._read_profile_pane(profile_pane, friend)
                # 关侧边栏，避免影响后续 monitor
                try:
                    from pyweixin.Uielements import Buttons
                    main_window.child_window(**Buttons.ChatInfoButton).click_input()
                except Exception:
                    pass
            finally:
                self._exit_ui()
            wxid = (profile.get("微信号") or "").strip()
            if wxid and wxid != "无":
                # 按备注更新缓存（覆盖全量缓存里不准的 wxid）
                self.resolver.update_or_add_by_remark(profile)
                emit("INFO", f"[新好友] 获取成功: {friend} -> wxid={wxid} 性别={profile.get('性别','')}")
                return wxid
            emit("WARNING", f"[新好友] 资料卡未取到微信号: {friend}")
        except Exception as e:
            emit("WARNING", f"[新好友] 获取微信号失败: {friend} -> {e}")
        return ""

    @staticmethod
    def _read_profile_pane(profile_pane, friend: str) -> dict:
        """从资料卡面板读微信号文本 + 颜色识别性别。"""
        profile = {"备注": friend, "微信号": "无", "性别": ""}
        try:
            texts = [t.window_text() for t in profile_pane.descendants(control_type='Text')]
            if "微信号：" in texts:
                profile["微信号"] = texts[texts.index("微信号：") + 1]
        except Exception:
            pass
        # 性别识别：昵称区域颜色（男蓝/女红）
        try:
            import numpy as _np
            rv = profile_pane.child_window(
                auto_id="right_v_view.nickname_button_view", control_type="Group")
            r = rv.rectangle()
            crop = _np.array(pyautogui.screenshot().crop((r.left, r.top, r.right, r.bottom)))
            if crop.size:
                pix = crop.reshape(-1, 3)  # RGB
                # 男蓝 RGB~[16,164,240]；女红 RGB~[187,61,61]
                blue = ((pix[:, 2] > 150) & (pix[:, 0] < 80) &
                        (pix[:, 1] > 80) & (pix[:, 1] < 200)).sum()
                red = ((pix[:, 0] > 150) & (pix[:, 1] < 100) & (pix[:, 2] < 100)).sum()
                if blue > 30:
                    profile["性别"] = "男"
                elif red > 30:
                    profile["性别"] = "女"
        except Exception as e:
            emit("WARNING", f"[新好友] 性别识别失败: {e}")
        return profile

    # ---- 微信事件转发 ----
    def on_wechat_message(self, chat: str, sender: str, content: str,
                          msg_type: str = "text", file_url: str | None = None,
                          file_name: str | None = None, file_size: int | None = None,
                          file_path: str | None = None) -> bool:
        """收到微信消息时转发给匹配的身份。返回 True 表示已转发。"""
        if not self._initialized or not self._adapter:
            return False
        # 延迟补获账号信息
        if not self._wx_nickname:
            emit("INFO", "账号信息为空，补调 _refresh_wx_account_info（微信窗口已就绪）")
            self._refresh_wx_account_info()
        # 过滤自身账号消息
        if sender in ("Self", "self", self._wx_nickname, self._wx_id, self._wx_wechat_id):
            return False

        # 富媒体处理：先保存上传，再转发（确保 fileUrl 包含在 payload 中）
        _media_types = ("图片", "视频", "文件")
        display_text = content
        if msg_type in _media_types:
            if msg_type == "文件" and file_path:
                p = Path(file_path)
                if p.is_file():
                    file_name = p.name
                    file_size = p.stat().st_size
                    if self._uploader and self._uploader.available:
                        file_url = self._uploader.upload(str(p), chat=chat) or ""
                    display_text = f"[{msg_type}] {file_name}"
                    emit("INFO", f"文件上传: {file_name} ({file_size} bytes) url={file_url}")
                else:
                    display_text = f"[{msg_type}]"
            elif msg_type in ("图片", "视频"):
                # 图片/视频：只走异步补发（带 fileUrl），不再同步发 "[图片]" 占位消息
                # 异步保存+上传：避免同步 UI 操作阻塞 monitor 线程导致后续消息丢失
                self._launch_async_media_followup(chat, sender, msg_type)
                return True
            else:
                display_text = f"[{msg_type}]"

        has_specific = any(i.enabled and i.forward_contacts and chat in i.forward_contacts
                           for i in self._identities)
        has_catchall = any(i.enabled and not i.forward_contacts for i in self._identities)

        # 解析发送者 wxid
        sender_wxid = sender
        chat_wxid = chat
        if not self.resolver.cache_ready:
            emit("INFO", "联系人缓存未就绪，尝试刷新...")
            try:
                self.resolver.refresh_cache()
            except Exception:
                pass
        if self.resolver.cache_ready:
            try:
                resolved = self.resolver.resolve(sender)
                if resolved.success and resolved.wxid:
                    sender_wxid = resolved.wxid
                else:
                    emit("WARNING", f"senderId 解析失败: {sender!r} -> {resolved.error}")
            except Exception:
                pass
            if chat != sender:
                try:
                    resolved = self.resolver.resolve(chat)
                    if resolved.success and resolved.wxid:
                        chat_wxid = resolved.wxid
                    else:
                        emit("WARNING", f"chatId 解析失败: {chat!r} -> {resolved.error}")
                except Exception:
                    pass
            else:
                chat_wxid = sender_wxid

        # 新好友查资料卡已由 monitor._clear_pending_if_match 的 _delayed 统一处理
        # （此处原 is_new_friend 分支会在模拟转发时被重复触发，导致资料卡查两遍，已移除）

        # 二次核查自身账号
        if sender_wxid in (self._wx_id, self._wx_wechat_id):
            emit("INFO", f"跳过自身账号消息: sender={sender} wxid={sender_wxid}")
            return False

        # 防回环
        last = self._last_sent.get(chat)
        if last and last[0] == content and time.time() - last[1] < 30:
            emit("INFO", f"跳过回环消息(刚由 bot 发出): {chat} -> {content[:50]}")
            return False

        forwarded = False
        for ident in self._identities:
            if not ident.enabled:
                continue
            forward_topic = ident.resolve_forward_topic()
            if not forward_topic:
                continue
            if ident.forward_contacts:
                if chat not in ident.forward_contacts:
                    continue
            elif has_specific:
                continue
            callback_prefix = ident.resolve_callback_prefix()
            msg_id = f"wechat-{uuid.uuid4().hex[:8]}"
            publish_topic = f"{forward_topic}/{msg_id}" if callback_prefix and forward_topic == callback_prefix else forward_topic
            # 优先级：per-chat 非默认值 > global > 默认 "auto"
            # 注意：不能用 `or`，因为 "auto" 是 truthy，会短路掉 global 的 "manual"
            session_operate = self._session_operate.get(chat)
            if not session_operate or session_operate == "auto":
                session_operate = self._session_operate.get("__global__", "auto")
            payload = {
                "event": "wechat_message", "correlationId": msg_id,
                "senderId": sender_wxid, "senderName": chat,
                "chatId": chat_wxid, "targetId": chat_wxid,
                "text": display_text, "chat": chat, "type": msg_type,
                "agentId": ident.agent_id, "role": ident.role,
                "selfWxName": self._wx_nickname, "selfWxId": self._wx_id,
                "ts": int(time.time() * 1000),
                "operate": session_operate,
            }
            if file_url:
                payload["fileUrl"] = file_url
            if file_name:
                payload["fileName"] = file_name
            if file_size:
                payload["fileSize"] = file_size
            payload_str = json.dumps(payload, ensure_ascii=False)
            if self._adapter.publish_safe(publish_topic, payload_str):
                emit("INFO", f"转发微信消息 -> {publish_topic} [{sender}] role={ident.role} payload={payload_str}", ident.role)
                forwarded = True

        if not forwarded and not has_catchall:
            enabled_identities = [i for i in self._identities if i.enabled]
            if len(enabled_identities) >= 2:
                try:
                    from .. import webhook_send
                    webhook_send.send_webhook(
                        title=f"未匹配转发联系人 - {chat}",
                        content=f"联系人: {chat}\n发送者: {sender}\n消息: {content[:200]}",
                    )
                    emit("INFO", f"未匹配转发联系人，已发送 Webhook 提醒: {chat}")
                except Exception as e:
                    emit("WARNING", f"Webhook 通知失败: {e}")
        if forwarded and self._executor:
            try:
                self._executor.record_forwarded(chat)  # 记录转发 sender,供 send_text 串线兜底告警
            except Exception:
                pass
        return forwarded

    def on_friend_accepted(self, nickname: str, remark: str = "", tags: list | None = None) -> None:
        if not self._initialized or not self._adapter:
            return
        for ident in self._identities:
            if not ident.enabled:
                continue
            forward_topic = ident.resolve_forward_topic()
            if not forward_topic:
                continue
            callback_prefix = ident.resolve_callback_prefix()
            msg_id = f"friend-{uuid.uuid4().hex[:8]}"
            publish_topic = f"{forward_topic}/{msg_id}" if callback_prefix and forward_topic == callback_prefix else forward_topic
            payload = {
                "event": "friend_accepted", "correlationId": msg_id,
                "senderId": remark or nickname, "senderName": remark or nickname,
                "text": "[系统] 好友已通过验证",
                "chat": remark or nickname, "type": "friend_accepted",
                "agentId": ident.agent_id, "role": ident.role,
                "nickname": nickname, "remark": remark, "tags": tags or [],
                "selfWxName": self._wx_nickname, "selfWxId": self._wx_id,
                "ts": int(time.time() * 1000),
            }
            payload_str = json.dumps(payload, ensure_ascii=False)
            ok = self._adapter.publish_safe(publish_topic, payload_str)
            emit("INFO", f"好友通过通知 -> {publish_topic} role={ident.role} {'成功' if ok else '失败'} payload={payload_str}", ident.role)

    def upload_file(self, local_path: str, chat: str = "") -> str | None:
        if not self._uploader or not self._uploader.available:
            return None
        return self._uploader.upload(str(local_path), chat=chat)

    def save_forward_contacts(self, identity: WorkerIdentity) -> None:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            workers = (config.setdefault("mqtt_worker", {})).get("workers", [])
            for w in workers:
                if w.get("role") == identity.role and w.get("agent_id") == identity.agent_id:
                    w["forward_contacts"] = list(identity.forward_contacts)
                    break
            else:
                return
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            emit("INFO", f"已保存 {identity.role} 的转发联系人", identity.role)
        except Exception as e:
            emit("ERROR", f"保存转发联系人失败: {e}")

    def get_status(self) -> dict:
        if not self._initialized or not self._coordinator:
            return {"initialized": self._initialized, "enabled": False}
        s = self._coordinator.get_status()
        s["initialized"] = self._initialized
        s["enabled"] = bool((bot_config.get("mqtt_worker", {}) or {}).get("enabled"))
        return s

    def handle_admin_command(self, command: str) -> str | None:
        if command == "/员工状态":
            return self._fmt_status()
        if command == "/员工重连":
            if self._coordinator:
                self._coordinator.stop()
                self._coordinator.start()
                return "MQTT 数字员工: 已触发重连"
            return "MQTT 数字员工: 服务未启动"
        return None

    def _fmt_status(self) -> str:
        if not self._initialized:
            return "MQTT 数字员工: 未初始化"
        st = self._coordinator.get_status() if self._coordinator else {}
        lines = ["MQTT 数字员工状态",
                 f"连接: {'已连接' if st.get('connected') else '已断开'}",
                 f"已处理任务: {st.get('tasks_processed', 0)} (失败: {st.get('tasks_failed', 0)})",
                 f"重连次数: {st.get('reconnect_count', 0)}"]
        for ident in self._identities:
            s = ident.get_status()
            lines.append(f"[{s['role']}] {'启用' if s['enabled'] else '禁用'} subscribe={s['subscribe_topic']}")
        return "\n".join(lines)

    def _launch_async_media_followup(self, chat: str, sender: str, msg_type: str) -> None:
        """后台线程：保存最新媒体 → 上传 MinIO → 补发带 fileUrl 的 MQTT 消息。"""
        def _run():
            try:
                self._enter_ui()
                try:
                    result = self._save_latest_media(chat, msg_type)
                finally:
                    self._exit_ui()
                if not result:
                    return
                file_name, file_size, file_url = result

                # 补发：格式与主转发一致，附带 fileUrl
                msg_id = f"wechat-{uuid.uuid4().hex[:8]}"
                sender_wxid = sender
                chat_wxid = chat
                if self.resolver.cache_ready:
                    try:
                        resolved = self.resolver.resolve(sender)
                        if resolved.success and resolved.wxid:
                            sender_wxid = resolved.wxid
                    except Exception:
                        pass
                    if chat != sender:
                        try:
                            resolved = self.resolver.resolve(chat)
                            if resolved.success and resolved.wxid:
                                chat_wxid = resolved.wxid
                        except Exception:
                            pass
                    else:
                        chat_wxid = sender_wxid

                display_text = f"[{msg_type}] {file_name}"
                has_specific = any(i.enabled and i.forward_contacts and chat in i.forward_contacts
                                   for i in self._identities)
                for ident in self._identities:
                    if not ident.enabled:
                        continue
                    forward_topic = ident.resolve_forward_topic()
                    if not forward_topic:
                        continue
                    if ident.forward_contacts:
                        if chat not in ident.forward_contacts:
                            continue
                    elif has_specific:
                        continue
                    publish_topic = forward_topic
                    session_operate = self._session_operate.get(chat)
                    if not session_operate or session_operate == "auto":
                        session_operate = self._session_operate.get("__global__", "auto")
                    payload = {
                        "event": "wechat_message", "correlationId": msg_id,
                        "senderId": sender_wxid, "senderName": chat,
                        "chatId": chat_wxid, "targetId": chat_wxid,
                        "text": display_text, "chat": chat, "type": msg_type,
                        "agentId": ident.agent_id, "role": ident.role,
                        "selfWxName": self._wx_nickname, "selfWxId": self._wx_id,
                        "ts": int(time.time() * 1000),
                        "operate": session_operate,
                        "fileUrl": file_url, "fileName": file_name, "fileSize": file_size,
                    }
                    payload_str = json.dumps(payload, ensure_ascii=False)
                    if self._adapter.publish_safe(publish_topic, payload_str):
                        emit("INFO", f"补发媒体消息 -> {publish_topic} file={file_name}", ident.role)
            except Exception as e:
                emit("ERROR", f"异步媒体处理异常: {e}")

        threading.Thread(target=_run, daemon=True, name=f"media-{chat[:8]}").start()

    def _save_latest_media(self, chat: str, msg_type: str) -> tuple[str, int, str] | None:
        """用 Messages.save_media 保存最新一张图片/视频 → 上传 MinIO。"""
        import tempfile
        from pyweixin import Messages as WeChatMessages

        if msg_type not in ("图片", "视频"):
            return None

        temp_dir = os.path.join(tempfile.gettempdir(), f"wxbot_media_{uuid.uuid4().hex[:6]}")
        os.makedirs(temp_dir, exist_ok=True)
        try:
            WeChatMessages.save_media(friend=chat, number=1, target_folder=temp_dir,
                                      close_weixin=False)
            # save_media 保存格式: "与{friend}的聊天图片1.png" / "与{friend}的聊天视频1.mp4"
            saved = sorted(Path(temp_dir).glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not saved:
                emit("WARNING", f"save_media 未生成文件: {temp_dir}")
                return None
            local_file = saved[0]
            file_name = local_file.name
            file_size = local_file.stat().st_size
            file_url = ""
            if self._uploader and self._uploader.available:
                file_url = self._uploader.upload(str(local_file), chat=chat) or ""
            # 清理
            try:
                os.remove(str(local_file))
                os.rmdir(temp_dir)
            except OSError:
                pass
            emit("INFO", f"媒体保存成功: {file_name} ({file_size} bytes) url={file_url}")
            return (file_name, file_size, file_url)
        except Exception as e:
            emit("ERROR", f"save_media 异常: {e}")
            try:
                os.rmdir(temp_dir)
            except OSError:
                pass
            return None


# 全局单例
mqtt_worker = MqttWorkerExtension()
