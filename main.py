# -*- coding: utf-8 -*-
"""wxbot_pyweixin 入口。

前置：
  1. 微信 4.1.x 已登录
  2. 讲述人 trick 已处理（UI 树可见，见 pywechat/Weixin4.0.md）
  3. set PYTHONUTF8=1

运行：
  python main.py
"""
from __future__ import annotations

import os
import sys
import threading

# 强制 UTF-8 输出（Windows emoji/中文）
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

if os.environ.get("PYTHONUTF8") != "1":
    print("⚠️  建议先设置环境变量: set PYTHONUTF8=1 （中文/emoji 输出更稳）")

# pyweixin SDK 已内嵌为本项目本地包（./pyweixin/），确保项目根目录在 sys.path 最前
# PyInstaller 打包后跳过（自动处理包导入）
if not getattr(sys, 'frozen', False):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

from pyweixin import Tools, Contacts  # noqa: E402
from pyweixin.Config import GlobalConfig  # noqa: E402

from wxbot.config import bot_config  # noqa: E402
from wxbot.logger import log  # noqa: E402
from wxbot.reply import reply_engine  # noqa: E402
from wxbot.mqtt.worker import mqtt_worker  # noqa: E402
from wxbot import scheduler, monitor, persona, knowledge  # noqa: E402


def _resolve_bot_id() -> str:
    """获取当前微信账号标识（wxid），用于记忆目录隔离。失败则回退 'default'。"""
    try:
        info = Contacts.check_my_info(close_weixin=False)
        bid = info.get("wxid") or info.get("微信号") or info.get("昵称") or "default"
        log.info(f"✓ 当前账号: {info.get('昵称')} ({info.get('微信号')})")
        return str(bid)
    except Exception as e:
        log.warning(f"获取账号信息失败，记忆使用默认目录: {e}")
        return "default"


def main() -> int:
    log.info("=" * 56)
    log.info("wxbot_pyweixin 启动（基于 pyweixin SDK）")
    log.info("=" * 56)

    if not Tools.is_weixin_running():
        log.error("❌ 微信未运行，请先登录微信 4.1.x")
        return 1
    try:
        log.info(f"✓ 微信版本: {Tools.get_weixin_version()}")
    except Exception:
        pass

    # 共享单一微信会话，所有任务用 close_weixin=False
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False

    # 加载配置（首次自动生成默认 config.json）
    bot_config.load()
    log.info(f"admin={bot_config.get('admin')} | 全局监听={bot_config.get('AllListen_switch')}")
    log.info(f"配置文件: {os.path.join('config', 'config.json')}")

    # 初始化数字员工：默认岗位人设 + 知识库
    persona.init_default_persona()
    knowledge.knowledge.load()
    if bot_config.get("digital_employee_switch", True):
        log.info("🤖 数字员工已启用（知识库 + 岗位人设 + AI）")
    else:
        log.info("数字员工总开关关闭")

    # 解析当前账号 wxid，隔离记忆存储与客户档案
    reply_engine.set_bot_id(_resolve_bot_id())

    # 初始化 MQTT 数字员工（OpenClaw 通道，默认关闭）
    mqtt_worker.initialize()
    if mqtt_worker.enabled:
        log.info("🔌 MQTT 数字员工通道已启用")
    else:
        log.info("MQTT 数字员工通道未启用（config.mqtt_worker.enabled=false）")

    # 人工操作屏蔽（默认关闭；enabled 时屏蔽对微信窗口的鼠标点击，把微信交给机器人）
    _ib = bot_config.get("input_block", {}) or {}
    if _ib.get("enabled"):
        from wxbot.input_blocker import input_blocker
        input_blocker.configure(auto_release_minutes=_ib.get("auto_release_minutes", 30))
        input_blocker.start()
        input_blocker.enable(reason="服务启动")
        log.info("🛡 人工操作屏蔽已启用（Ctrl+Alt+X 或 /解除屏蔽 解除）")
    else:
        log.info("人工操作屏蔽未启用（config.input_block.enabled=false）")

    # 启动调度器（定时消息/朋友圈/新好友/点赞，后台线程）
    scheduler.start()

    # 主循环（消息收发）在前台运行——意外异常自动恢复，仅 Ctrl+C / 正常停止退出
    import time as _time
    try:
        while True:
            try:
                monitor.loop()
            except Exception as e:
                # monitor.loop 内部已兜底单轮异常；此分支为防御性双保险，
                # 确保 _in_pause_period 等非单轮异常也不致整程序退出
                if not monitor._stop.is_set():
                    log.error(f"⚠️ 主循环意外退出，5s 后自动恢复: {e}")
                    try:
                        from wxbot import webhook_send
                        webhook_send.send_webhook(
                            title=f"【主循环异常自动恢复】{bot_config.get('admin', '')}",
                            content=f"异常: {e}\n时间: {_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    except Exception:
                        pass
                    _time.sleep(5)
                    continue
            break
    finally:
        monitor.stop()
        scheduler.stop()
        if mqtt_worker._coordinator:
            mqtt_worker._coordinator.shutdown()
        log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
