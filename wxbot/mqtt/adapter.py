# -*- coding: utf-8 -*-
"""paho-mqtt 协议封装层（网络线程，不做任何 UI 调用）。"""
from __future__ import annotations

import uuid

import paho.mqtt.client as paho

from .common import emit


class MqttAdapter:
    def __init__(self, config: dict, agent_id: str) -> None:
        self._cfg = config
        self._agent_id = agent_id
        self._client = None
        self._message_handler = None
        self._connect_handler = None
        self._disconnect_handler = None

    def set_handlers(self, on_connect=None, on_disconnect=None, on_message=None) -> None:
        self._connect_handler = on_connect
        self._disconnect_handler = on_disconnect
        self._message_handler = on_message

    def connect(self) -> bool:
        self.disconnect()
        broker = self._cfg["broker"]
        host = broker.get("host", "localhost")
        port = broker.get("port", 1883)
        client_id = f"wbot_{self._agent_id}_{uuid.uuid4().hex[:8]}"
        # paho 2.x 默认 callback API v2（5 参），此处显式用 v1（4 参）以兼容回调签名
        try:
            client = paho.Client(client_id=client_id, clean_session=True,
                                 protocol=paho.MQTTv311,
                                 callback_api_version=paho.CallbackAPIVersion.VERSION1)
        except (AttributeError, TypeError):
            # paho 1.x 无 callback_api_version 参数
            client = paho.Client(client_id=client_id, clean_session=True, protocol=paho.MQTTv311)
        self._client = client
        username = broker.get("username", "")
        password = broker.get("password", "")
        vhost = broker.get("vhost", "/")
        if username:
            if vhost and vhost != "/":
                username = f"{vhost}:{username}"
            self._client.username_pw_set(username, password)
        if broker.get("tls"):
            self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        emit("INFO", f"正在连接 {host}:{port} (vhost={vhost}, tls={broker.get('tls')})")
        self._client.connect(host, port, keepalive=60)
        self._client.loop_start()
        return True

    def disconnect(self) -> None:
        if self._client:
            try:
                self._client.loop_stop()
            except Exception:
                pass
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def subscribe(self, topic: str) -> None:
        if self._client:
            emit("INFO", f"订阅主题: {topic}")
            self._client.subscribe(topic, qos=1)

    def publish_safe(self, topic: str, payload: str) -> bool:
        if self._client:
            emit("INFO", f"⬆ MQTT 发布 topic={topic} payload={payload}")
            info = self._client.publish(topic, payload, qos=1)
            return info.rc == paho.MQTT_ERR_SUCCESS
        return False

    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def _on_connect(self, client, userdata, flags, rc):
        emit("INFO", f"MQTT 连接结果: rc={rc} ({paho.connack_string(rc)})")
        if self._connect_handler:
            self._connect_handler(rc)

    def _on_disconnect(self, client, userdata, rc):
        emit("INFO", f"MQTT 断开: rc={rc}")
        if self._disconnect_handler:
            self._disconnect_handler(rc)

    def _on_message(self, client, userdata, msg):
        """paho 网络线程回调 — 仅解析入队。"""
        try:
            payload = msg.payload.decode("utf-8")
            emit("INFO", f"⬇ MQTT 收到 topic={msg.topic} payload={payload}")
            if self._message_handler:
                self._message_handler(msg.topic, payload)
        except Exception as e:
            emit("ERROR", f"on_message 异常: {e}")

    def shutdown(self) -> None:
        self.disconnect()
