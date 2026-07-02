# -*- coding: utf-8 -*-
"""任务执行引擎（pyweixin 适配版）。

将 SiverWXbot 的 wxautox4 调用映射为 pyweixin SDK：

  wxautox4                              →  pyweixin
  ------------------------------------------------------------------
  wx.SendMsg(msg, who)                  →  Messages.send_messages_to_friend
  wx.SendFiles(who, filepath)           →  Files.send_files_to_friend
  wx.ChatWith(who) + wx.GetAllMessage() →  Messages.pull_messages
  wx.AddNewFriend(keywords,...)         →  FriendSettings.add_new_friend
  wx.GetFriendDetails(n, callback)      →  Contacts.get_friends_detail (Python 端切片/过滤)
  wx.Moments().Publish(text,media,...)  →  Moments.post_moments
  wx.GetMyInfo()/wx.nickname            →  Contacts.check_my_info

消息格式：event 模型（参数在顶层 targetName/text/...），兼容旧 taskType+params。
wechat_message（反向回复，含 targetName/targetId）内部按 send_text 执行。

已知 gap（pyweixin 限制）：
  - add_friend 的 tags / permission(朋友圈) 不支持，记日志跳过
  - post_moments 的 privacy/tags 不支持，仅发公开朋友圈
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import requests

from pyweixin import Messages, Files, FriendSettings, Contacts, Moments, Navigator
from pyweixin.Uielements import Main_window, SideBar

from ..config import bot_config
from ..input_blocker import input_blocker
from .common import (MAX_CONTACT_LEN, MAX_HISTORY_LIMIT, MAX_MESSAGE_LEN,
                     MAX_TARGET_LEN, MAX_VERIFY_TEXT_LEN, emit)
from .resolver import ContactResolver


def _field(task: dict, key: str, default=None):
    """优先取顶层字段，回退到 params.<key>。用于兼容 event(顶层) 与旧 taskType(params) 格式。"""
    val = task.get(key)
    if val not in (None, ""):
        return val
    params = task.get("params", {}) or {}
    return params.get(key, default)


class TaskExecutor:
    def __init__(self, log_func=None, wx_busy_event=None, resolver=None, uploader=None) -> None:
        self._log = log_func or emit
        self._resolver: ContactResolver | None = resolver  # 可共享实例
        self._uploader = uploader  # MinioUploader 实例（get_friend_moments 截图上传用，可共享）
        self._wx_busy_event = wx_busy_event  # threading.Event, set during UI ops
        self._ui_lock: threading.Lock | None = None  # UI 互斥锁（由 coordinator 注入）

    def _enter_ui(self) -> None:
        """获取 UI 锁 + 重置微信到聊天主页，确保后续操作从干净状态开始。"""
        if self._ui_lock:
            if not self._ui_lock.acquire(timeout=30):
                raise RuntimeError("UI 锁获取超时 (30s)，可能有残留任务占用")
        # 强制回到聊天列表主页，消除上一个操作（如 add_friend）遗留的页面状态
        try:
            mw = Navigator.open_weixin(is_maximize=False)
            mw.child_window(**SideBar.Weixin).click_input()
            time.sleep(0.15)
        except Exception as e:
            self._log("WARNING", f"UI 窗口重置失败，后续操作可能受影响: {e}")
        if self._wx_busy_event:
            self._wx_busy_event.set()
        input_blocker.set_bot_active(True)  # 放行机器人点击

    def _exit_ui(self) -> None:
        """释放 UI 锁，标记退出 UI 操作。"""
        input_blocker.set_bot_active(False)
        if self._wx_busy_event:
            self._wx_busy_event.clear()
        if self._ui_lock:
            try:
                self._ui_lock.release()
            except RuntimeError:
                pass

    @staticmethod
    def _click_session(who: str) -> None:
        """发完消息后在会话列表单击当前联系人，取消选中关闭聊天面板。"""
        try:
            time.sleep(0.3)
            main_window = Navigator.open_weixin(is_maximize=False)
            main_window.child_window(**SideBar.Weixin).click_input()
            time.sleep(0.2)
            session_list = main_window.child_window(**Main_window.SessionList)
            session_list.type_keys("{HOME}")
            time.sleep(0.15)
            for _ in range(30):
                for item in session_list.children(control_type="ListItem"):
                    if who in item.window_text():
                        item.click_input()
                        return
                session_list.type_keys("{PGDN}")
                time.sleep(0.1)
        except Exception:
            pass

    @property
    def resolver(self) -> ContactResolver:
        if self._resolver is None:
            self._resolver = ContactResolver(log_func=self._log)
        return self._resolver

    # ---- 文件下载（发送 fileUrl / post_moments 媒体共用）----
    @staticmethod
    def download_file(url: str) -> Path | None:
        try:
            emit("INFO", f"正在下载文件: {url[:120]}")
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            filename = None
            cd = resp.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip("\"'")
            if not filename:
                filename = url.split("/")[-1].split("?")[0] or "download"
                if "." not in filename:
                    ct = resp.headers.get("Content-Type", "")
                    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                               "application/pdf": ".pdf", "application/zip": ".zip",
                               "application/octet-stream": ".bin"}
                    filename += ext_map.get(ct, ".bin")
            local = Path(tempfile.gettempdir()) / filename
            with open(str(local), "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            emit("INFO", f"文件下载完成: {local} ({local.stat().st_size} bytes)")
            return local
        except Exception as e:
            emit("ERROR", f"文件下载异常: {e}")
            return None

    # ---- 任务分发 ----
    def execute_task(self, task_json: dict) -> dict:
        cid = task_json.get("correlationId", "unknown")
        event = task_json.get("event", "")
        # wechat_message 反向回复 → 内部按 send_text 执行；其余按 event 派发
        internal_task = task_json.get("_internal_task", "")
        task_type = internal_task or event or task_json.get("taskType", "")
        method = self.TASK_METHOD_MAP.get(task_type)
        if not method:
            return {"correlationId": cid, "status": "error",
                    "result": {"error": f"未知事件类型: {event}"}}
        try:
            self._log("INFO", f"执行任务 [{task_type}] event={event} correlationId={cid}")
            result = method(self, task_json)
            return {"correlationId": cid, "status": result.get("status", "success"), "result": result}
        except Exception as e:
            self._log("ERROR", f"任务执行异常 [{task_type}]: {e}")
            return {"correlationId": cid, "status": "error", "result": {"error": str(e)}}

    @staticmethod
    def _validate_str(value, field_name, max_len) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} 必须为字符串")
        v = value.strip()
        if len(v) > max_len:
            raise ValueError(f"{field_name} 长度超过限制 ({max_len})")
        return v

    # ---- send_text ----
    def _execute_send_text(self, task: dict) -> dict:
        target_raw = _field(task, "targetName") or _field(task, "targetId", "")
        message_raw = _field(task, "text", "")
        file_url_raw = _field(task, "fileUrl", "")
        file_url = file_url_raw.strip() if isinstance(file_url_raw, str) else ""

        # 指令消息：text 为空、无文件，但 operate 存在 → 不实际发送微信消息，仅返回状态
        if not message_raw and not file_url:
            operate = _field(task, "operate", "")
            if operate:
                target_name = _field(task, "targetName") or _field(task, "targetId") or target_raw or ""
                self._log("INFO", f"收到指令消息 operate={operate} target={target_name}，不发送微信")
                return {"status": "success", "wechatResult": False, "operate": operate, "action": "instruction", "target": target_name}

        target = self._validate_str(target_raw, "target", MAX_TARGET_LEN)
        message = self._validate_str(message_raw, "message", MAX_MESSAGE_LEN)
        if not target:
            return {"status": "error", "error": "缺少 target 参数"}

        resolved = self.resolver.resolve(target)
        if not resolved.success:
            # 多个匹配仍报错（避免歧义发送）；未命中则回退用原 target（pyweixin 会自行搜索会话列表）
            if resolved.candidates:
                return {"status": "error", "error": resolved.error, "candidates": resolved.candidates}
            effective = target
            self._log("WARNING", f"联系人解析未命中 {target!r}，回退直接使用原 target")
        else:
            effective = resolved.display_name
            self._log("INFO", f"联系人解析: {target} → {effective} (matched_by={resolved.matched_by})")

        results = []
        local_file = None
        if file_url:
            local_file = self.download_file(file_url)
            if not local_file:
                return {"status": "error", "error": f"文件下载失败: {file_url[:100]}"}
        try:
            self._enter_ui()
            if local_file:
                Files.send_files_to_friend(friend=effective, files=[str(local_file)],
                                           with_messages=bool(message),
                                           messages=[message] if message else [""],
                                           close_weixin=False)
                results.append("file: ok")
            if message and not file_url:
                Messages.send_messages_to_friend(friend=effective, messages=[message], close_weixin=False)
                results.append("text: ok")
            if not results:
                return {"status": "error", "error": "既无 message 也无 fileUrl"}
            self._click_session(effective)
        finally:
            self._exit_ui()
        return {"status": "success", "wechatResult": True, "wechatRaw": "; ".join(results)}

    # ---- add_friend ----
    def _execute_add_friend(self, task: dict) -> dict:
        target = self._validate_str(_field(task, "targetName") or _field(task, "targetId", ""),
                                    "target", MAX_TARGET_LEN)
        verify_text = self._validate_str(_field(task, "verifyText", ""), "verifyText", MAX_VERIFY_TEXT_LEN)
        remark_raw = _field(task, "remark", "")
        remark = remark_raw.strip() if isinstance(remark_raw, str) else ""
        tags_raw = _field(task, "tags", [])
        tags = tags_raw if isinstance(tags_raw, list) else []
        permission_raw = _field(task, "permission", "")
        permission = permission_raw.strip() if isinstance(permission_raw, str) else ""
        if not target:
            return {"status": "error", "error": "缺少 target 参数"}

        listen_name = remark or target
        chat_only = permission == "仅聊天"

        # 同步执行 UI（2-5s），异常直接返回；max_workers=2 保证不阻塞 send_text
        try:
            self._enter_ui()
            try:
                pre_delay = int(bot_config.get("friend_add", {}).get("pre_delay", 3) or 0)
                if pre_delay > 0:
                    self._log("INFO", f"加好友前等待 {pre_delay}s（拟人延迟）")
                    time.sleep(pre_delay)
                nickname = FriendSettings.add_new_friend(number=target, greetings=verify_text or None,
                                              remark=remark or None, chat_only=chat_only, close_weixin=False)
            finally:
                self._exit_ui()
        except Exception as e:
            return {"status": "error", "error": f"添加好友失败: {e}"}

        self._log("INFO", f"好友请求已发送: {target} (昵称: {nickname})")
        if tags or permission == "朋友圈":
            self._log("WARNING", "pyweixin 暂不支持 tags/朋友圈权限，已跳过（已知 gap）")

        # 追加联系人缓存
        contact_info = {
            "昵称": nickname, "微信号": target,
            "地区": "", "备注": remark if remark else "",
            "电话": "", "标签": "", "描述": "",
            "朋友权限": permission, "共同群聊": "",
            "个性签名": "", "来源": "",
        }
        try:
            self.resolver.add_contact(contact_info)
        except Exception as e:
            self._log("WARNING", f"追加联系人到缓存失败: {e}")

        # 加入监听列表（后台写 config，避免 os.replace 卡住回执）
        def _bg_listen():
            try:
                bot_config.add_listen_user(listen_name)
                self._log("INFO", f"已将 {listen_name} 加入监听列表")
            except Exception as e:
                self._log("WARNING", f"加入监听列表失败: {e}")
        threading.Thread(target=_bg_listen, daemon=True, name=f"listen-{listen_name[:8]}").start()

        # 标记为"待通过":monitor 扫到该好友出现在会话列表时模拟"已通过好友请求"转发 MQTT
        try:
            from ..pending_friends import add_pending
            add_pending(remark or nickname)
        except Exception as e:
            self._log("WARNING", f"写入待通过标记失败: {e}")

        return {"status": "success", "action": "add_friend_sent",
                "target": target, "listen_name": listen_name}

    # ---- get_chat_history ----
    def _execute_get_chat_history(self, task: dict) -> dict:
        contact_raw = _field(task, "targetName") or _field(task, "targetId") or _field(task, "contact", "")
        if not contact_raw or not isinstance(contact_raw, str):
            return {"status": "error", "error": "缺少 contact 参数"}
        contact = contact_raw.strip()
        if len(contact) > MAX_CONTACT_LEN:
            return {"status": "error", "error": f"contact 参数长度超过限制 ({MAX_CONTACT_LEN})"}
        limit_val = _field(task, "limit", 20)
        try:
            limit = max(1, min(int(limit_val), MAX_HISTORY_LIMIT))
        except (TypeError, ValueError):
            limit = 20

        resolved = self.resolver.resolve(contact)
        if not resolved.success:
            try:
                from .. import webhook_send
                title = f"联系人解析失败 - {'多个匹配' if resolved.candidates else '未找到'}: {contact}"
                content = f"目标: {contact}\n任务类型: get_chat_history\n"
                if resolved.candidates:
                    content += f"匹配到 {len(resolved.candidates)} 个联系人:\n" + "\n".join(resolved.candidates[:20])
                webhook_send.send_webhook(title=title, content=content)
            except Exception:
                pass
            return {"status": "error", "error": resolved.error, "candidates": resolved.candidates}
        effective = resolved.display_name

        try:
            # pyweixin pull_messages 自动打开会话并返回消息 dict 列表
            raw = Messages.pull_messages(friend=effective, number=limit, close_weixin=False)
        except Exception as e:
            return {"status": "error", "error": f"获取消息失败: {e}"}

        messages = []
        for m in raw if isinstance(raw, list) else [raw]:
            if not isinstance(m, dict):
                continue
            messages.append({
                "type": str(m.get("消息类型", "text")),
                "content": str(m.get("消息内容", "")),
                "sender": str(m.get("消息发送人", "")),
                "time": str(m.get("消息时间", "")),
            })
        return {"status": "success", "messages": messages, "count": len(messages)}

    # ---- get_friend_details ----
    def _execute_get_friend_details(self, task: dict) -> dict:
        # 优先读联系人缓存(瞬时),缓存空才全量 UI 扫描
        details = None
        if self.resolver.cache_ready:
            with self.resolver._lock:
                details = list(self.resolver._friends)
        if not details:
            self._log("INFO", "[get_friend_details] 缓存空,尝试全量 UI 扫描")
            try:
                details = Contacts.get_friends_detail(close_weixin=False)
            except Exception as e:
                # 全量扫描失败(如 4.1.9.35 UI 异常)不报 error,返回空列表 + warning
                self._log("WARNING", f"[get_friend_details] 全量扫描失败,返回空: {e}")
                return {"status": "success", "friends": [], "count": 0,
                        "warning": f"联系人缓存为空且全量扫描失败: {e}"}
        if not details:
            return {"status": "success", "friends": [], "count": 0}
        name_prefix_raw = _field(task, "name_prefix", "")
        name_prefix = name_prefix_raw.strip() if isinstance(name_prefix_raw, str) else ""
        friends = []
        for fr in details:
            if not isinstance(fr, dict):
                continue
            if name_prefix:
                nick = str(fr.get("昵称", ""))
                remark = str(fr.get("备注", ""))
                if not (nick.startswith(name_prefix) or remark.startswith(name_prefix)):
                    continue
            friends.append({k: ("" if v is None else str(v)) for k, v in fr.items()})
        n_val = _field(task, "n")
        if isinstance(n_val, int) and n_val > 0:
            friends = friends[:n_val]
        return {"status": "success", "friends": friends, "count": len(friends)}

    # ---- get_contacts_cache ----
    def _execute_get_contacts_cache(self, task: dict) -> dict:
        """只读通讯录缓存，排除 pending_friends.json 里的待通过好友。缓存空返回空+warning。"""
        if not self.resolver.cache_ready:
            return {"status": "success", "contacts": [], "count": 0,
                    "excluded_pending": 0, "warning": "通讯录缓存为空"}

        # pending.match 语义：备注优先，无备注用昵称（pending_friends.py:7）
        try:
            from ..pending_friends import load_pending
            pending_matches = {p.get("match", "").strip()
                               for p in load_pending() if p.get("match")}
        except Exception:
            pending_matches = set()

        contacts = []
        excluded = 0
        for fr in self.resolver.get_all_contacts():
            if not isinstance(fr, dict):
                continue
            remark = str(fr.get("备注", "") or "").strip()
            nickname = str(fr.get("昵称", "") or "").strip()
            # 与 pending.match 同语义：有备注比备注，无备注比昵称
            key = remark if remark else nickname
            if key and key in pending_matches:
                excluded += 1
                continue
            contacts.append({k: ("" if v is None else str(v)) for k, v in fr.items()})

        return {"status": "success", "contacts": contacts,
                "count": len(contacts), "excluded_pending": excluded}

    # ---- refresh_contacts ----
    def _execute_refresh_contacts(self, task: dict) -> dict:
        result = self.resolver.refresh_cache()
        if result.get("error"):
            return {"status": "error", "error": result["error"]}
        return {"status": "success", "loaded": result["loaded"], "elapsed_seconds": result["elapsed"]}

    # ---- post_moments ----
    def _execute_post_moments(self, task: dict) -> dict:
        text_raw = _field(task, "text", "") or ""
        text = text_raw.strip() if isinstance(text_raw, str) else ""
        media_urls = _field(task, "media_files")
        if media_urls is None or media_urls == "":
            media_urls = _field(task, "images", []) or []
        privacy_raw = _field(task, "privacy", "public") or "public"
        privacy = privacy_raw.strip() if isinstance(privacy_raw, str) else "public"
        tags_raw = _field(task, "tags", [])
        tags = tags_raw if isinstance(tags_raw, list) else []
        if not text and not media_urls:
            return {"status": "error", "error": "text 和 media_files 至少需要提供一项"}

        local_media: list[str] = []
        failed_urls: list[str] = []
        if media_urls:
            if not isinstance(media_urls, list):
                return {"status": "error", "error": "media_files 必须为字符串数组"}
            if len(media_urls) > 9:
                return {"status": "error", "error": "媒体文件数量不能超过 9 个"}
            for url in media_urls:
                if not isinstance(url, str) or not url.strip():
                    continue
                local = self.download_file(url.strip())
                if local:
                    local_media.append(str(local))
                else:
                    failed_urls.append(url[:100])
        # 配图资源不可用 → 降级：全部失败且有文字则纯文字发；全部失败且无文字才报错
        if failed_urls and not local_media:
            if text:
                self._log("WARNING", f"配图全部下载失败，降级纯文字发布: {failed_urls}")
            else:
                return {"status": "error", "error": f"配图下载失败且无文字内容: {failed_urls}"}
        elif failed_urls:
            self._log("WARNING", f"部分配图下载失败已跳过: {failed_urls}")

        if privacy != "public" or tags:
            self._log("WARNING", "pyweixin post_moments 暂不支持隐私/标签，按公开发布（已知 gap）")
        try:
            # 发朋友圈是耗时高优操作：持 UI 锁独占，期间 monitor 轮询与点赞/加好友等让位
            self._enter_ui()
            try:
                pre_delay = int(bot_config.get("moments_post_pre_delay", 3) or 0)
                if pre_delay > 0:
                    self._log("INFO", f"发朋友圈前等待 {pre_delay}s（拟人延迟）")
                    time.sleep(pre_delay)
                Moments.post_moments(text=text, medias=local_media, close_weixin=False)
            finally:
                self._exit_ui()
        except Exception as e:
            return {"status": "error", "error": str(e)}
        self._log("INFO", f"朋友圈已发布 文案{len(text)}字 媒体{len(local_media)}个")
        return {"status": "success", "action": "post_moments",
                "textLength": len(text), "mediaCount": len(local_media)}

    # ---- get_friend_moments ----
    def _execute_get_friend_moments(self, task: dict) -> dict:
        """按时间范围导出指定好友朋友圈，内容截图上传 MinIO 后回调。"""
        target_raw = _field(task, "targetName") or _field(task, "targetId", "")
        if not isinstance(target_raw, str) or not target_raw.strip():
            return {"status": "error", "error": "缺少 target 参数"}
        target = self._validate_str(target_raw, "target", MAX_TARGET_LEN)
        start_raw = _field(task, "startDate", "")
        end_raw = _field(task, "endDate", "")
        start = start_raw.strip() if isinstance(start_raw, str) else ""
        end = end_raw.strip() if isinstance(end_raw, str) else ""
        if not start or not end:
            return {"status": "error", "error": "缺少 startDate/endDate 参数"}
        try:
            limit = max(1, min(int(_field(task, "limit", 50)), 100))
        except (TypeError, ValueError):
            limit = 50

        resolved = self.resolver.resolve(target)
        if not resolved.success:
            return {"status": "error", "error": resolved.error, "candidates": resolved.candidates}
        effective = resolved.display_name

        if not self._uploader or not getattr(self._uploader, "available", False):
            return {"status": "error", "error": "MinIO 未配置，无法上传朋友圈截图"}

        try:
            from ..moments_export import dump_friend_moments_range
            self._enter_ui()
            try:
                posts = dump_friend_moments_range(
                    friend=effective, start=start, end=end,
                    uploader=self._uploader, limit=limit, log_func=self._log)
            finally:
                self._exit_ui()
        except Exception as e:
            self._log("ERROR", f"获取朋友圈异常 {effective}: {e}")
            return {"status": "error", "error": f"获取朋友圈失败: {e}"}
        if posts is None:
            # dump_friend_moments_range 异常（打不开/解析失败）→ 无法查看
            return {"status": "error", "friend": effective,
                    "error": f"无法查看 {effective} 的朋友圈（获取内容异常）"}
        self._log("INFO", f"朋友圈导出 {effective} [{start}~{end}] 共 {len(posts)} 条")
        return {"status": "success", "friend": effective,
                "range": {"start": start, "end": end},
                "count": len(posts), "moments": posts}

    # ---- ping ----
    def _execute_ping(self, task: dict) -> dict:
        return {"status": "success", "pong": True}

    TASK_METHOD_MAP = {
        "send_text": _execute_send_text,
        "add_friend": _execute_add_friend,
        "get_chat_history": _execute_get_chat_history,
        "get_friend_details": _execute_get_friend_details,
        "get_contacts_cache": _execute_get_contacts_cache,
        "ping": _execute_ping,
        "post_moments": _execute_post_moments,
        "refresh_contacts": _execute_refresh_contacts,
        "get_friend_moments": _execute_get_friend_moments,
    }
