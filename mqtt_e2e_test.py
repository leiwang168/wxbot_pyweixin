# -*- coding: utf-8 -*-
"""MQTT 闭环端到端测试（方案 A）。

链路：上游模拟 client publish 任务到 subscribe topic
  → coordinator 收任务入队
  → executor 执行（pyweixin 真实发消息给目标）
  → 回调 publish 到 callback_prefix/<cid>
  → 上游模拟 client 收到回调 → 判定 status

副作用：会在「文件传输助手」真实发送一条 MESSAGE。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.environ.setdefault("PYTHONUTF8", "1")

import paho.mqtt.client as paho  # noqa: E402

from wxbot.config import bot_config  # noqa: E402
from wxbot.mqtt.worker import mqtt_worker  # noqa: E402

TARGET = "文件传输助手"
MESSAGE = "【测试】MQTT通道验证"
CID = f"e2e-{uuid.uuid4().hex[:8]}"
WAIT_TIMEOUT = 45  # 等待回调上限（任务执行含 pyweixin 操作微信，约 10-25s）


def main() -> int:
    bot_config.load()
    cfg = bot_config.get("mqtt_worker", {}) or {}
    broker = cfg.get("broker", {})
    host, port = broker.get("host"), broker.get("port")
    username = broker.get("username", "")
    password = broker.get("password", "")

    workers = cfg.get("workers", []) or []
    w = next((x for x in workers if x.get("enabled")), workers[0] if workers else {})
    topics = w.get("topics", {}) or {}
    sub_topic = (topics.get("subscribe") or "").replace("{role}", w.get("role", ""))
    cb_prefix = (topics.get("callback_prefix") or "").replace("{agent_id}", w.get("agent_id", ""))
    cb_wildcard = f"{cb_prefix}/#"

    print("=" * 60)
    print("MQTT 闭环端到端测试")
    print("=" * 60)
    print(f"subscribe(发任务) = {sub_topic}")
    print(f"callback(收回调)  = {cb_wildcard}")
    print(f"correlationId     = {CID}")
    print(f"target/message    = {TARGET} / {MESSAGE}")

    # [1/5] 初始化 mqtt_worker（连 broker + 订阅任务 topic）
    print("\n[1/5] 初始化 mqtt_worker（coordinator 连接 + 订阅）...")
    mqtt_worker.initialize()
    if not mqtt_worker.enabled:
        print("  ❌ mqtt_worker 未启用或初始化失败")
        return 1
    time.sleep(4)  # 等 on_connect → subscribe 完成

    callback_received = threading.Event()
    callback_data: dict = {}

    def on_message(_c, _u, msg):
        if not msg.topic.endswith(f"/{CID}"):
            return
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            data = {"raw": msg.payload.decode("utf-8", "replace")}
        callback_data.update(data)
        callback_data["topic"] = msg.topic
        callback_received.set()
        print(f"  ✓ 收到回调: {msg.topic}")

    client = None
    try:
        # [2/5] 上游模拟 client：连 broker + 订阅回调 topic
        print(f"\n[2/5] 上游模拟器连接 {host}:{port}，订阅 {cb_wildcard} ...")
        client = paho.Client(
            client_id=f"e2e_test_{uuid.uuid4().hex[:6]}",
            clean_session=True, protocol=paho.MQTTv311,
            callback_api_version=paho.CallbackAPIVersion.VERSION1,
        )
        if username:
            client.username_pw_set(username, password)
        client.on_message = on_message
        client.connect(host, port, keepalive=60)
        client.loop_start()
        client.subscribe(cb_wildcard, qos=1)
        time.sleep(1)

        # [3/5] 发布回复任务（event=wechat_message 反向，内部按 send_text 执行）
        task = {
            "event": "wechat_message",
            "correlationId": CID,
            "targetName": TARGET,
            "text": MESSAGE,
        }
        print(f"\n[3/5] 发布任务 -> {sub_topic}")
        print(f"  payload = {json.dumps(task, ensure_ascii=False)}")
        info = client.publish(sub_topic, json.dumps(task, ensure_ascii=False), qos=1)
        info.wait_for_publish(timeout=5)

        # [4/5] 等回调
        print(f"\n[4/5] 等待回调（最多 {WAIT_TIMEOUT}s，含 pyweixin 操作微信）...")
        start = time.time()
        while time.time() - start < WAIT_TIMEOUT:
            if callback_received.is_set():
                break
            time.sleep(0.5)
    finally:
        if client:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    # [5/5] 判定
    elapsed = time.time() - start
    print(f"\n[5/5] 结果（耗时 {elapsed:.1f}s）")
    st = mqtt_worker.get_status()
    print(f"  coordinator: tasks_processed={st.get('tasks_processed')} "
          f"failed={st.get('tasks_failed')} queue={st.get('queue_depth')}")
    if mqtt_worker._coordinator:
        mqtt_worker._coordinator.shutdown()

    if not callback_received.is_set():
        print("  ❌ 未收到回调（超时）— 任务可能未入队或执行卡住")
        return 2

    status = callback_data.get("status")
    result = callback_data.get("result")
    print(f"  回调 status = {status}")
    print(f"  回调 result = {json.dumps(result, ensure_ascii=False)}")
    if status == "success":
        print("\n  ✅ 闭环验证成功：MQTT 收任务 → pyweixin 发消息 → 回调发布")
        return 0
    print("\n  ❌ 回调非 success（见 result）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
