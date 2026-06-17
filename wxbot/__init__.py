# -*- coding: utf-8 -*-
"""wxbot_pyweixin — 基于 pyweixin SDK 的可配置微信机器人（复刻 SiverWXbot_plus 核心配置功能）。

阶段一交付三大优先功能的最小闭环：收发消息、添加好友、发朋友圈。
配置驱动（config.json + 微信 /指令），不做 Web UI、不做 AI（预留扩展点）。

包级导出单例，方便 `from wxbot import scheduler` 直接获取单例而非模块。
"""

__version__ = "0.1.0"

# 延迟导入单例，避免循环依赖
from .scheduler import scheduler  # noqa: E402
from .monitor import monitor  # noqa: E402
