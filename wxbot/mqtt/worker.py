# -*- coding: utf-8 -*-
"""MQTT 数字员工扩展对外接口（facade）。

配置来源：`bot_config.get('mqtt_worker')` 嵌套字典。
微信事件入口：`on_wechat_message` / `on_friend_accepted`（由 monitor / friends 调用）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from pyweixin import Contacts

from ..config import bot_config
from .adapter import MqttAdapter
from .common import MinioUploader, emit
from .coordinator import MqttCoordinator
from .executor import TaskExecutor
from .identity import WorkerIdentity
from .resolver import ContactResolver

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "config.json")


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
        self._executor = TaskExecutor(log_func=emit)
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
        self._executor = TaskExecutor(log_func=emit)
        self._coordinator = MqttCoordinator(cfg, self._adapter, self._executor, self._identities, extension=self)
        self._adapter.set_handlers(on_connect=lambda rc: self._on_connect(rc),
                                   on_disconnect=lambda rc: None,
                                   on_message=self._coordinator.enqueue_message)
        self._coordinator.start()
        self._initialized = True
        emit("INFO", f"MQTT 数字员工热重载完成 共 {len(self._identities)} 个身份")
        self._validate_multi_identity()

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
                display_text = f"[{msg_type}]"
                # save_media 保存最新一张 → 上传 MinIO
                try:
                    result = self._save_latest_media(chat, msg_type)
                    if result:
                        file_name, file_size, file_url = result
                        display_text = f"[{msg_type}] {file_name}"
                except Exception as e:
                    emit("WARNING", f"保存媒体失败: {e}")
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
            session_operate = self._session_operate.get(chat) or self._session_operate.get("__global__", "auto")
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
