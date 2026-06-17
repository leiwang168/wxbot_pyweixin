# -*- coding: utf-8 -*-
"""MQTT 上行转发测试（数字员工闭环的「微信 → OpenClaw」方向）。

验证：模拟一条微信客户消息进入 mqtt_worker.on_wechat_message()
  → 转发 payload 发布到 forward topic
  → 上游模拟 client（扮演 OpenClaw）订阅并收到
  → 校验 payload 字段（event/text/senderName/chat/agentId/role/selfWxName）

无微信副作用：on_wechat_message 仅 publish MQTT，不操作微信 UI。
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

# 模拟客户消息（sender 不能等于本机昵称，否则被 self 过滤）
CHAT = "客户A"
SENDER = "客户A"
CONTENT = "你好，请问价格多少"
WAIT_TIMEOUT = 10


def main() -> int:
    bot_config.load()
    cfg = bot_config.get("mqtt_worker", {}) or {}
    broker = cfg.get("broker", {})
    host, port = broker.get("host"), broker.get("port")
    username, password = broker.get("username", ""), broker.get("password", "")

    workers = cfg.get("workers", []) or []
    w = next((x for x in workers if x.get("enabled")), workers[0] if workers else {})
    topics = w.get("topics", {}) or {}
    role, agent_id = w.get("role", ""), w.get("agent_id", "")
    fwd_topic = (topics.get("forward") or "").replace("{role}", role).replace("{agent_id}", agent_id)
    cb_prefix = (topics.get("callback_prefix") or "").replace("{agent_id}", agent_id)
    # on_wechat_message 在 forward==callback_prefix 时发到 f"{fwd}/{msg_id}"
    listen = f"{fwd_topic}/#" if fwd_topic else f"{cb_prefix}/#"

    print("=" * 60)
    print("MQTT 上行转发测试（微信 → OpenClaw 方向）")
    print("=" * 60)
    print(f"forward topic(监听) = {listen}")
    print(f"模拟消息: chat={CHAT} sender={SENDER} text={CONTENT!r}")

    # [1] 初始化 mqtt_worker
    print("\n[1/4] 初始化 mqtt_worker ...")
    mqtt_worker.initialize()
    if not mqtt_worker.enabled:
        print("  ❌ mqtt_worker 未启用")
        return 1
    print(f"  本机账号(过滤用) = {mqtt_worker._wx_nickname!r} ({mqtt_worker._wx_id})")
    time.sleep(3)

    received = threading.Event()
    data: dict = {}

    def on_message(_c, _u, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if payload.get("event") != "wechat_message":
            return
        data.update(payload)
        data["topic"] = msg.topic
        received.set()
        print(f"  ✓ 上游收到转发: {msg.topic}")

    client = None
    try:
        # [2] 上游模拟 client 订阅 forward topic
        print(f"\n[2/4] OpenClaw 模拟器订阅 {listen} ...")
        client = paho.Client(
            client_id=f"upstream_test_{uuid.uuid4().hex[:6]}",
            clean_session=True, protocol=paho.MQTTv311,
            callback_api_version=paho.CallbackAPIVersion.VERSION1,
        )
        if username:
            client.username_pw_set(username, password)
        client.on_message = on_message
        client.connect(host, port, keepalive=60)
        client.loop_start()
        client.subscribe(listen, qos=1)
        time.sleep(1)

        # [3] 触发上行转发
        print(f"\n[3/4] 调用 on_wechat_message 模拟客户消息 ...")
        forwarded = mqtt_worker.on_wechat_message(chat=CHAT, sender=SENDER,
                                                  content=CONTENT, msg_type="text")
        print(f"  on_wechat_message 返回 forwarded={forwarded}")
        if not forwarded:
            print("  ❌ 未转发（可能 sender 被判为 self，或无匹配身份）")
            return 1

        # [4] 等待上游收到
        print(f"\n[4/4] 等待上游接收（最多 {WAIT_TIMEOUT}s）...")
        start = time.time()
        while time.time() - start < WAIT_TIMEOUT:
            if received.is_set():
                break
            time.sleep(0.3)
    finally:
        if client:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        if mqtt_worker._coordinator:
            mqtt_worker._coordinator.shutdown()

    if not received.is_set():
        print("  ❌ 上游未收到转发（超时）")
        return 2

    print("\n收到转发 payload:")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    # 字段校验
    checks = [
        ("event", data.get("event") == "wechat_message"),
        ("text == 内容", data.get("text") == CONTENT),
        ("senderName == 会话", data.get("senderName") == CHAT),
        ("chat == 会话", data.get("chat") == CHAT),
        ("senderId 非空", bool(data.get("senderId"))),
        ("agentId", data.get("agentId") == agent_id),
        ("role", data.get("role") == role),
        ("selfWxName 非空", bool(data.get("selfWxName"))),
    ]
    print("\n字段校验:")
    all_ok = True
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")
        all_ok = all_ok and ok

    if all_ok:
        print("\n✅ 上行转发验证成功：微信消息 → forward topic（OpenClaw 可接收）")
        return 0
    print("\n❌ 部分 payload 字段不符预期")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
