# -*- coding: utf-8 -*-
"""MQTT 数字员工扩展（多身份支持）。

从 SiverWXbot_plus `extensions/mqtt_worker_extension.py` 迁移而来，
将 wxautox4 调用全部替换为 pyweixin SDK。

架构：_MqttAdapter → _MqttCoordinator → [_WorkerIdentity...] → _TaskExecutor → MqttWorkerExtension
线程模型：
  - paho 网络线程：仅接收 MQTT 消息，解析后入队，不做任何 UI 调用
  - MqttCoordinator 线程：取任务，执行 pyweixin 调用（pyweixin 内部已处理 COM）
  - Bot 主线程：管理扩展生命周期
"""
from .worker import MqttWorkerExtension

__all__ = ["MqttWorkerExtension"]
