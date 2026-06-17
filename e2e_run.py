# -*- coding: utf-8 -*-
"""完整端到端临时启动脚本。

与 main.py 的差异：
  - AllListen_switch 仅在内存置 True（persist=False，不写 config.json），让测试号消息通过过滤
  - 只起 mqtt_worker + monitor 主循环，不起 scheduler（避免 random/scheduled 任务干扰）
  - 关闭本地数字员工回复兜底（依赖 MQTT 转发，skip_local_reply_when_forwarded 已生效）

用途：真实微信来消息 → monitor 捕获 → mqtt_worker.on_wechat_message → 转发到 OpenClaw。
用 Ctrl+C 或 kill 进程退出。
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from pyweixin import Contacts  # noqa: E402
from pyweixin.Config import GlobalConfig  # noqa: E402

from wxbot.config import bot_config  # noqa: E402
from wxbot.logger import log  # noqa: E402
from wxbot.reply import reply_engine  # noqa: E402
from wxbot.mqtt.worker import mqtt_worker  # noqa: E402
from wxbot import monitor, persona, knowledge  # noqa: E402


def main() -> int:
    log.info("=" * 56)
    log.info("【端到端】临时启动：全局监听 + MQTT + monitor（不启动 scheduler）")
    log.info("=" * 56)

    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False

    bot_config.load()
    bot_config.set("AllListen_switch", True, persist=False)  # 仅内存，让测试号通过过滤
    log.info("AllListen_switch 临时置 True（仅本次运行，不写 config.json）")

    persona.init_default_persona()
    knowledge.knowledge.load()

    try:
        info = Contacts.check_my_info(close_weixin=False)
        reply_engine.set_bot_id(info.get("wxid") or info.get("微信号") or "default")
        log.info(f"当前账号: {info.get('昵称')} ({info.get('微信号')}) wxid={info.get('wxid')}")
    except Exception as e:
        log.warning(f"获取账号信息失败: {e}")

    mqtt_worker.initialize()
    log.info(f"MQTT 数字员工: enabled={mqtt_worker.enabled}")
    if not mqtt_worker.enabled:
        log.error("MQTT 未启用，无法进行端到端验证")
        return 1

    log.info("📨 进入 monitor 主循环，等待真实微信消息（用另一个号发消息即可）...")
    try:
        monitor.loop()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，停止...")
    finally:
        monitor.stop()
        if mqtt_worker._coordinator:
            mqtt_worker._coordinator.shutdown()
        log.info("已退出端到端运行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
