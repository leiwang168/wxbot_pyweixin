# -*- coding: utf-8 -*-
"""好友添加扩展 — 1:1 迁移自 SiverWXbot_plus/extensions/friend_add_extension.py。

分层结构（与源一致）：
  FriendAddExtension      — 对外接口（命令处理、状态查询）
  _FriendAddService       — 业务策略层（限流、日配额、幂等、重试）
  _FriendAddAdapter       — 底层适配层（封装 pyweixin API 调用）

差异：源 `wx.AddNewFriend(keywords, addmsg, timeout=5)` → pyweixin
`FriendSettings.add_new_friend(number, greetings, remark, chat_only, close_weixin=False)`。
"""
from __future__ import annotations

import sys
import threading
import time

from .config import bot_config
from .mqtt.resolver import ContactResolver


def _emit(msg: str) -> None:
    """同时输出到 stdout 和 logger，确保面板日志可见。"""
    try:
        from .logger import log
        log.info(f"[friend_add] {msg}")
    except Exception:
        pass
    print(f"[friend_add] {msg}", file=sys.stdout, flush=True)


class _FriendAddAdapter:
    """仅封装 pyweixin 原生 add_new_friend 调用，不包含任何业务策略。"""

    def add_new_friend(self, target: str, verify_text: str, remark: str = "") -> dict:
        _emit(f"Adapter.add_new_friend 调用: target={target} "
              f"verify_text={verify_text[:50] if verify_text else ''}")
        try:
            from pyweixin import FriendSettings
            kwargs = {"number": target, "close_weixin": False}
            if verify_text:
                kwargs["greetings"] = verify_text
            if remark:
                kwargs["remark"] = remark
            _emit(f"Adapter: 准备调用 FriendSettings.add_new_friend(number={target})")
            nickname = FriendSettings.add_new_friend(**kwargs)
            _emit(f"Adapter: add_new_friend 返回成功, 昵称={nickname}")
            return {"status": "sent", "raw": None, "nickname": nickname}
        except Exception as e:
            _emit(f"Adapter: add_new_friend 异常: {type(e).__name__}: {e}")
            return {"status": "failed", "raw": str(e)}


class _FriendAddService:
    """业务策略：限流、日配额、幂等、重试。不直接接触 pyweixin API。"""

    def __init__(self, config: dict, adapter: _FriendAddAdapter,
                 log_func, notify_func) -> None:
        self._cfg = config
        self._adapter = adapter
        self._log = log_func
        self._notify = notify_func
        self._lock = threading.RLock()
        self._last_add: dict[str, float] = {}   # target -> timestamp
        self._today_count = 0
        self._today_date: str | None = None
        _emit(f"Service 初始化: cfg={config}")

    def _reset_daily_if_needed(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._today_date != today:
            _emit(f"Service: 日期切换 {self._today_date} -> {today}，计数归零（原={self._today_count}）")
            self._today_count = 0
            self._today_date = today

    def can_add_now(self, target: str) -> tuple[bool, str]:
        with self._lock:
            self._reset_daily_if_needed()
            if not self._cfg.get("enabled", False):
                return False, "好友添加功能未启用"

            daily_limit = int(self._cfg.get("daily_limit", 20))
            if self._today_count >= daily_limit:
                return False, f"已达今日添加上限（{daily_limit}）"

            rate_limit = int(self._cfg.get("rate_limit_seconds", 60))
            last_ts = self._last_add.get(target)
            if last_ts and (time.time() - last_ts) < rate_limit:
                remaining = int(rate_limit - (time.time() - last_ts))
                return False, f"目标 {target} 在限流期内（{remaining}s 后可重试）"

            return True, "ok"

    def add_friend(self, target: str, verify_text: str,
                   remark: str = "", source: str = "admin_command") -> dict:
        with self._lock:
            ok, reason = self.can_add_now(target)
            if not ok:
                self._log("WARNING", f"[好友添加] 拒绝: target={target} reason={reason}")
                return {"status": "rejected", "reason": reason, "target": target,
                        "source": source, "ts": time.time()}
            self._reset_daily_if_needed()

        retry_count = int(self._cfg.get("retry_count", 3))
        last_result = None

        for attempt in range(1 + retry_count):
            if attempt > 0:
                delay = min(2 ** attempt, 32)
                self._log("WARNING", f"[好友添加] 第{attempt}次重试 target={target}，等待{delay}s")
                time.sleep(delay)

            result = self._adapter.add_new_friend(target, verify_text or "", remark or "")
            if result["status"] == "sent":
                with self._lock:
                    self._last_add[target] = time.time()
                    self._today_count += 1
                self._log("SUCCESS", f"[好友添加] 申请已发送: target={target}")
                return {"status": "sent", "reason": "ok", "target": target,
                        "source": source, "ts": time.time(),
                        "nickname": result.get("nickname", target),
                        "remark": remark}

            last_result = result
            self._log("WARNING",
                      f"[好友添加] 失败(target={target}, attempt={attempt}): {result.get('raw', 'unknown')}")

        self._log("ERROR", f"[好友添加] 最终失败: target={target} err={last_result.get('raw', 'unknown')}")
        if self._notify:
            try:
                self._notify(
                    f"好友添加最终失败 - {target}",
                    f"目标: {target}\n来源: {source}\n原因: {last_result.get('raw', '')}",
                )
            except Exception:
                pass

        return {"status": "failed", "reason": last_result.get("raw", "unknown"),
                "target": target, "source": source, "ts": time.time()}

    def get_status(self) -> dict:
        with self._lock:
            self._reset_daily_if_needed()
            return {
                "enabled": self._cfg.get("enabled", False),
                "daily_limit": self._cfg.get("daily_limit", 20),
                "rate_limit_seconds": self._cfg.get("rate_limit_seconds", 60),
                "today_count": self._today_count,
                "today_date": self._today_date,
            }


class FriendAddExtension:
    """好友添加扩展 — 对外最小接口。

    用法：
      ext = FriendAddExtension()
      ext.initialize()
      # 在 commands.process_command 中：
      reply = ext.handle_admin_command("/添加好友 wxid_xxx")
    """

    def __init__(self) -> None:
        self._service: _FriendAddService | None = None
        self._initialized = False
        self._resolver: ContactResolver | None = None

    @property
    def _contact_resolver(self) -> ContactResolver:
        if self._resolver is None:
            self._resolver = ContactResolver(log_func=None)
        return self._resolver

    def initialize(self) -> None:
        cfg = bot_config.get("friend_add", {}) or {}
        _emit(f"Extension.initialize: cfg={cfg}")
        adapter = _FriendAddAdapter()

        def _log(level: str, message: str) -> None:
            try:
                from .logger import log
                getattr(log, level.lower(), log.info)(message)
            except Exception:
                pass

        def _notify(title: str, content: str) -> None:
            try:
                from . import webhook_send
                webhook_send.send_webhook(title=title, content=content)
            except Exception:
                pass

        self._service = _FriendAddService(cfg, adapter, _log, _notify)
        self._initialized = True
        _emit("Extension.initialize 完成")

    def handle_admin_command(self, text: str) -> str | None:
        """/添加好友 <微信号或wxid>  → 返回给 admin 的回复（None 表示不处理此指令）。"""
        if not text or not text.startswith("/"):
            return None
        cmd, _, arg = text[1:].strip().partition(" ")
        if cmd != "添加好友":
            return None
        if not self._initialized or not self._service:
            return "好友添加扩展未初始化"

        target = (arg or "").strip()
        if not target:
            return "请提供目标微信号或 wxid，如：/添加好友 wxid_abc123"

        verify_text = (bot_config.get("friend_add", {}) or {}).get("verify_text", "")
        result = self._service.add_friend(target, verify_text, source="admin_command")

        status = result["status"]
        if status == "sent":
            # 直接追加到联系人缓存，避免全量刷新通讯录
            nickname = result.get("nickname", target)
            remark = result.get("remark", "")
            contact_info = {
                "昵称": nickname,
                "微信号": target,
                "地区": "",
                "备注": remark,
                "电话": "",
                "标签": "",
                "描述": "",
                "朋友权限": "",
                "共同群聊": "",
                "个性签名": "",
                "来源": "",
            }
            try:
                self._contact_resolver.add_contact(contact_info)
            except Exception as e:
                _emit(f"追加联系人到缓存失败: {e}")
            # 标记为"待通过":monitor 扫到该好友出现在会话列表时模拟"已通过好友请求"转发 MQTT
            try:
                from .pending_friends import add_pending
                add_pending(remark or nickname)
            except Exception as e:
                _emit(f"写入待通过标记失败: {e}")
            return f"好友申请已发送给 {target}"
        if status == "rejected":
            return f"无法添加 {target}：{result['reason']}"
        return f"添加 {target} 失败：{result['reason']}"

    def get_status(self) -> dict:
        if not self._service:
            return {"initialized": False}
        return self._service.get_status()


# 全局单例（懒初始化）
friend_add_ext = FriendAddExtension()
