# -*- coding: utf-8 -*-
"""MQTT 协调器：单连接、多订阅、按 topic 路由、优先队列 + 令牌桶限流。"""
from __future__ import annotations

import json
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from .common import (TASK_PRIORITY_DEFAULT, TASK_PRIORITY_MAP, TASK_TIMEOUT_DEFAULT,
                     WINDOW_OPENING_TASKS, TokenBucket, emit)
from .executor import TaskExecutor


class MqttCoordinator:
    def __init__(self, config, adapter, executor: TaskExecutor, identities, extension=None) -> None:
        self._cfg = config
        self._adapter = adapter
        self._executor = executor
        self._identities = identities
        self._extension = extension
        self._identity_map: dict[str, object] = {}
        self._build_identity_map()

        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._reconnect_count = 0
        self._task_count = 0
        self._task_fail_count = 0
        self._last_error = ""
        self._started_at = None
        self._task_timeout = int(self._cfg.get("task_timeout", TASK_TIMEOUT_DEFAULT))

        throttle = self._cfg.get("throttle", {}) or {}
        self._task_queue: queue.PriorityQueue = queue.PriorityQueue(
            maxsize=int(throttle.get("queue_max_size", 100)))
        self._rate_limiter = TokenBucket(
            rate=float(throttle.get("rate_limit_per_second", 1.0)),
            burst=int(throttle.get("rate_limit_burst", 3)))
        self._queue_alert_threshold = int(throttle.get("queue_alert_threshold", 80))
        self._queue_alerted = False
        self._dropped_count = 0
        self._priority_map = dict(TASK_PRIORITY_MAP)
        self._priority_map.update(throttle.get("task_priorities", {}) or {})

        self._close_chat_window = bool(self._cfg.get("close_chat_window", True))
        self._close_chat_window_delay = float(self._cfg.get("close_chat_window_delay", 1.0))

        self._task_pool = ThreadPoolExecutor(max_workers=1)
        self._wx_busy_event = threading.Event()

    def _build_identity_map(self) -> None:
        for identity in self._identities:
            if identity.enabled and identity.subscribe_topic:
                self._identity_map[identity.resolve_subscribe_topic()] = identity

    def get_subscribe_topics(self) -> list[str]:
        return [i.resolve_subscribe_topic() for i in self._identities if i.enabled and i.subscribe_topic]

    @property
    def wx_busy(self) -> bool:
        return self._wx_busy_event.is_set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=10)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True, name="MqttCoordinator")
        self._thread.start()
        emit("INFO", "MQTT 协调器工作线程已启动")

    def stop(self) -> None:
        self._stop_event.set()
        if self._adapter:
            self._adapter.shutdown()
        while not self._task_queue.empty():
            try:
                self._task_queue.get_nowait()
            except queue.Empty:
                break

    def shutdown(self) -> None:
        self.stop()
        if self._task_pool:
            self._task_pool.shutdown(wait=False)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        emit("INFO", "MQTT 协调器已完全关闭")

    # ---- 消息适配（统一为 event 格式，兼容 OpenClaw wrapper 与旧 taskType）----
    @staticmethod
    def _adapt_payload(payload: str) -> str | None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            emit("ERROR", "入队前 JSON 校验失败，丢弃")
            return None

        # ── OpenClaw wrapper：text 嵌套 JSON（自身无 event/taskType）──
        if "text" in data and "event" not in data and "taskType" not in data:
            text = data["text"]
            try:
                inner = json.loads(text)
                if isinstance(inner, dict) and ("taskType" in inner or "event" in inner):
                    cid = data.get("correlationId", "")
                    if cid and "correlationId" not in inner:
                        inner["correlationId"] = cid
                    data = inner
                else:
                    emit("INFO", f"忽略非任务消息(纯文本): {text[:100]}")
                    return None
            except (json.JSONDecodeError, TypeError, ValueError):
                emit("INFO", f"忽略非任务消息(纯文本): {text[:100]}")
                return None

        # ── 旧 taskType → 新 event 映射 ──
        if "taskType" in data and "event" not in data:
            old_type = data.pop("taskType")
            if old_type == "send_text":
                data["event"] = "wechat_message"
                params = data.pop("params", {}) or {}
                if "target" in params:
                    data["targetName"] = params["target"]
                if "message" in params:
                    data["text"] = params["message"]
                if "fileUrl" in params:
                    data["fileUrl"] = params["fileUrl"]
            elif old_type == "add_friend":
                data["event"] = "add_friend"
                params = data.pop("params", {}) or {}
                if "target" in params:
                    data["targetName"] = params["target"]
                for k in ("verifyText", "remark", "tags", "permission"):
                    if k in params:
                        data[k] = params[k]
            else:
                data["event"] = old_type
                params = data.pop("params", {}) or {}
                data.update(params)

        # ── 新格式：wechat_message 反向回复 → 内部按 send_text 执行 ──
        if data.get("event") == "wechat_message" and ("targetId" in data or "targetName" in data):
            data["_internal_task"] = "send_text"

        # ── 确保有 event 字段 ──
        if "event" not in data:
            emit("WARNING", f"无法识别的消息格式，缺少 event: {payload[:200]}")
            return None

        emit("INFO", f"消息适配 -> event={data.get('event')}")
        return json.dumps(data, ensure_ascii=False)

    def enqueue_message(self, topic: str, payload: str) -> None:
        identity = self._identity_map.get(topic)
        if not identity:
            emit("WARNING", f"收到未匹配的主题消息 topic={topic}")
            return
        adapted = self._adapt_payload(payload)
        if not adapted:
            return
        if identity.is_self_target(adapted):
            return
        emit("INFO", f"收到任务 topic={topic} role={identity.role} payload={adapted}", identity.role)
        event_type = ""
        try:
            event_type = json.loads(adapted).get("event", "")
        except Exception:
            pass
        priority = self._priority_map.get(event_type, TASK_PRIORITY_DEFAULT)
        try:
            self._task_queue.put_nowait((priority, time.monotonic(), identity, adapted))
            self._check_queue_alert()
        except queue.Full:
            with self._lock:
                self._dropped_count += 1
            emit("WARNING", f"任务队列已满，丢弃 event={event_type} role={identity.role} payload={adapted}", identity.role)

    def _run_event_loop(self) -> None:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            self._run_loop_inner()
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _run_loop_inner(self) -> None:
        reconnect_delays = [5, 10, 20, 40, 80, 160, 300]
        delay_index = 0
        self._started_at = time.time()
        while not self._stop_event.is_set():
            try:
                self._adapter.connect()
                while not self._stop_event.is_set() and not self._adapter.is_connected():
                    self._stop_event.wait(0.5)
                if self._stop_event.is_set():
                    break
                delay_index = 0
                self._reconnect_count = 0
                while not self._stop_event.is_set():
                    if not self._adapter.is_connected():
                        emit("WARNING", "检测到连接断开")
                        break
                    try:
                        _, _, identity, payload = self._task_queue.get(timeout=1)
                        if not self._rate_limiter.acquire(timeout=30):
                            emit("WARNING", "速率限制等待超时，跳过任务")
                            continue
                        self._process_task(identity, payload)
                    except queue.Empty:
                        continue
            except Exception as e:
                emit("ERROR", f"MQTT 连接异常: {e}")
                self._last_error = str(e)
            if self._stop_event.is_set():
                break
            delay = reconnect_delays[min(delay_index, len(reconnect_delays) - 1)]
            wait = delay * random.uniform(0.75, 1.25)
            self._reconnect_count += 1
            emit("INFO", f"重连等待 {wait:.1f}s (第 {self._reconnect_count} 次)")
            self._stop_event.wait(wait)
            delay_index += 1

    def _check_queue_alert(self) -> None:
        """队列深度告警：超过阈值触发日志和 webhook 通知。"""
        size = self._task_queue.qsize()
        if size >= self._queue_alert_threshold and not self._queue_alerted:
            self._queue_alerted = True
            emit("WARNING", f"任务队列深度告警: {size}/{self._task_queue.maxsize} 个任务排队")
            try:
                from .. import webhook_send
                webhook_send.send_webhook(
                    title=f"MQTT 任务队列告警 - {size} 个任务排队",
                    content=(f"队列深度: {size}/{self._task_queue.maxsize}\n"
                             "请检查上游消息发送频率或调整 queue_max_size 配置"),
                )
            except Exception:
                pass
        elif size < self._queue_alert_threshold * 0.8:
            self._queue_alerted = False

    def _process_task(self, identity, payload: str) -> None:
        task = json.loads(payload)
        cid = task.get("correlationId", "")
        emit("INFO", f"处理任务 event={task.get('event')} cid={cid} role={identity.role} task={payload}", identity.role)
        self._wx_busy_event.set()
        try:
            future = self._task_pool.submit(self._executor.execute_task, task)
            try:
                result = future.result(timeout=self._task_timeout)
            except FuturesTimeout:
                result = {"correlationId": cid, "status": "error",
                          "result": {"error": f"任务超时 ({self._task_timeout}s)"}}
                emit("WARNING", "任务超时，重建线程池")
                try:
                    self._task_pool.shutdown(wait=False)
                except Exception:
                    pass
                self._task_pool = ThreadPoolExecutor(max_workers=1)
            except Exception as e:
                result = {"correlationId": cid, "status": "error",
                          "result": {"error": f"任务执行异常: {e}"}}
        finally:
            self._wx_busy_event.clear()

        with self._lock:
            self._task_count += 1
            if result.get("status") == "error":
                self._task_fail_count += 1

        callback_topic = f"{identity.resolve_callback_prefix()}/{cid}"
        result["event"] = "task_result"
        result["executedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        result["agentId"] = identity.agent_id
        result["role"] = identity.role
        # senderId 使用实际微信号，而非 agent 标识
        result["senderId"] = self._extension._wx_id if self._extension and self._extension._wx_id else identity.agent_id
        result["text"] = json.dumps(result.get("result", {}), ensure_ascii=False)
        result["operate"] = task.get("operate", "")
        # 携带原始任务的 targetId（微信号），方便后端追踪
        task_target_id = task.get("targetId", "")
        if task_target_id:
            result["targetId"] = task_target_id
        task_target_name = task.get("targetName", "")
        if task_target_name:
            result["targetName"] = task_target_name
        callback_payload = json.dumps(result, ensure_ascii=False)
        ok = self._adapter.publish_safe(callback_topic, callback_payload)
        emit("INFO", f"回调发布 {'成功' if ok else '失败'} topic={callback_topic} payload={callback_payload}", identity.role)

        # add_friend 成功后加入该角色转发联系人并持久化
        # 仅单 agent 兜底模式（forward_contacts 为空）时不追加，否则会从"转发全部"变成"只转发这一个"
        if task.get("event") == "add_friend" and result.get("status") == "success":
            listen_name = result.get("result", {}).get("listen_name", "")
            enabled_count = sum(1 for i in self._identities if i.enabled)
            is_catchall = not identity.forward_contacts
            if enabled_count <= 1 and is_catchall:
                emit("INFO", f"单 agent 兜底模式，跳过追加 forward_contacts: {listen_name}", identity.role)
            elif listen_name and identity.forward_topic and listen_name not in identity.forward_contacts:
                identity.forward_contacts.append(listen_name)
                emit("INFO", f"已将 {listen_name} 加入 {identity.role} 的转发联系人", identity.role)
                if self._extension:
                    try:
                        self._extension.save_forward_contacts(identity)
                    except Exception as e:
                        emit("WARNING", f"保存转发联系人失败: {e}", identity.role)

        # wechat_message 发送成功后记录 operate + 消息指纹（防回环），供后续上行转发时使用
        if task.get("event") == "wechat_message" and result.get("status") == "success":
            target_name = task.get("targetName", "") or task.get("targetId", "") or ""
            if self._extension:
                operate_val = task.get("operate", "")
                if operate_val:
                    if target_name:
                        self._extension._session_operate[target_name] = operate_val
                        emit("INFO", f"已记录会话 operate: {target_name} -> {operate_val}", identity.role)
                    else:
                        # targetName 为空 → 全局 operate，对所有联系人生效
                        self._extension._session_operate["__global__"] = operate_val
                        emit("INFO", f"已记录全局 operate: __global__ -> {operate_val}", identity.role)
                if target_name:
                    sent_text = task.get("text", "")
                    if sent_text:
                        self._extension._last_sent[target_name] = (sent_text, time.time())
                        emit("INFO", f"已记录发送指纹: {target_name} -> {sent_text[:40]}", identity.role)

    def get_status(self) -> dict:
        with self._lock:
            return {
                "connected": bool(self._adapter and self._adapter.is_connected()),
                "reconnect_count": self._reconnect_count,
                "tasks_processed": self._task_count,
                "tasks_failed": self._task_fail_count,
                "tasks_dropped": self._dropped_count,
                "queue_depth": self._task_queue.qsize(),
                "queue_max_size": self._task_queue.maxsize,
                "last_error": self._last_error,
                "uptime": time.time() - self._started_at if self._started_at else 0,
                "identities": [i.get_status() for i in self._identities],
            }
