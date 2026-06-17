# -*- coding: utf-8 -*-
"""MQTT ping/pong 心跳测试。

模拟 OpenClaw 心跳协议 {"event":"ping",...} 发到 subscribe topic，
验证 coordinator 识别后转 ping 任务 → executor 回 pong → 回调发布到 callback topic。
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

CID = f"pong-{uuid.uuid4().hex[:8]}"
WAIT = 15


def main() -> int:
    bot_config.load()
    cfg = bot_config.get("mqtt_worker", {}) or {}
    broker = cfg.get("broker", {})
    host, port = broker.get("host"), broker.get("port")
    user, pwd = broker.get("username", ""), broker.get("password", "")
    workers = cfg.get("workers", []) or []
    w = next((x for x in workers if x.get("enabled")), workers[0])
    agent_id = w.get("agent_id", "")
    sub = (w.get("topics", {}).get("subscribe") or "").replace("{role}", w.get("role", "")).replace("{agent_id}", agent_id)
    cb = (w.get("topics", {}).get("callback_prefix") or "").replace("{agent_id}", agent_id)
    cbwild = f"{cb}/#"

    print("=" * 56)
    print("MQTT ping/pong 心跳测试")
    print("=" * 56)
    print(f"subscribe = {sub}  callback = {cbwild}  cid = {CID}")

    mqtt_worker.initialize()
    if not mqtt_worker.enabled:
        print("❌ mqtt_worker 未启用")
        return 1
    time.sleep(3)

    got = threading.Event()
    data: dict = {}

    def on_msg(_c, _u, msg):
        if not msg.topic.endswith(f"/{CID}"):
            return
        try:
            d = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            d = {"raw": msg.payload.decode("utf-8", "replace")}
        data.update(d)
        data["topic"] = msg.topic
        got.set()
        print(f"  ✓ 收到回调: {msg.topic}")

    client = None
    try:
        client = paho.Client(
            client_id=f"ping_test_{uuid.uuid4().hex[:6]}",
            clean_session=True, protocol=paho.MQTTv311,
            callback_api_version=paho.CallbackAPIVersion.VERSION1,
        )
        if user:
            client.username_pw_set(user, pwd)
        client.on_message = on_msg
        client.connect(host, port, keepalive=60)
        client.loop_start()
        client.subscribe(cbwild, qos=1)
        time.sleep(1)

        payload = {"event": "ping", "correlationId": CID, "agentId": agent_id, "ts": int(time.time() * 1000)}
        print(f"\n发布 ping -> {sub}")
        print(f"  payload = {json.dumps(payload, ensure_ascii=False)}")
        client.publish(sub, json.dumps(payload, ensure_ascii=False), qos=1).wait_for_publish(timeout=5)

        print(f"\n等待 pong 回调（最多 {WAIT}s）...")
        t0 = time.time()
        while time.time() - t0 < WAIT:
            if got.is_set():
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

    if not got.is_set():
        print("❌ 未收到 pong 回调（超时）")
        return 2

    print(f"\n回调内容: {json.dumps(data, ensure_ascii=False)}")
    status = data.get("status")
    result = data.get("result") or {}
    pong = isinstance(result, dict) and result.get("pong") is True
    is_task_result = data.get("event") == "task_result"
    print(f"  event={data.get('event')} status={status}  result.pong={result.get('pong') if isinstance(result, dict) else result}")
    if status == "success" and pong and is_task_result:
        print("\n✅ ping/pong 验证成功：OpenClaw 心跳 {event:ping} 已识别并回 pong")
        return 0
    print("\n❌ 回调非预期 pong")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
