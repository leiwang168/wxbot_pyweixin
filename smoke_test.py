# -*- coding: utf-8 -*-
"""冒烟测试：逐层验证 wxbot_pyweixin 环境与模块。

安全保证：
  - 不发送任何真实微信消息
  - 不订阅 MQTT 任务、不启动 coordinator（仅测 broker 连通性）
  - 不发起好友申请
  - 唯一的 UI 只读操作：check_my_info 读取已登录账号信息
"""
from __future__ import annotations

import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.environ.setdefault("PYTHONUTF8", "1")


STEPS: list[tuple[str, object]] = []


def step(name: str):
    def deco(fn):
        STEPS.append((name, fn))
        return fn
    return deco


@step("微信运行/版本/账号")
def s_wechat():
    from pyweixin import Tools, Contacts
    if not Tools.is_weixin_running():
        raise RuntimeError("微信未运行，请先登录微信 4.1.x")
    version = Tools.get_weixin_version()
    info = Contacts.check_my_info(close_weixin=False)
    return (f"v{version} 账号={info.get('昵称')}({info.get('微信号')}) "
            f"wxid={info.get('wxid')}")


@step("配置加载")
def s_config():
    from wxbot.config import bot_config
    cfg = bot_config.load()
    api = (cfg.get("api_configs") or [{}])[0]
    return (f"admin={cfg.get('admin')} 数字员工={cfg.get('digital_employee_switch')} "
            f"AI_key已填={bool(api.get('key'))}(model={api.get('model')}) "
            f"MQTT={cfg.get('mqtt_worker', {}).get('enabled')} "
            f"friend_add={cfg.get('friend_add', {}).get('enabled')}")


@step("岗位 persona")
def s_persona():
    from wxbot import persona
    persona.init_default_persona()
    names = persona.list_personas()
    prompt = persona.resolve_system_prompt("某人", False)
    return f"可用岗位={names} 默认prompt长度={len(prompt)}"


@step("知识库 FAQ 匹配")
def s_knowledge():
    from wxbot import knowledge as kb
    kb.knowledge.load()
    probes = ["你好", "在吗有人吗", "转人工", "今天天气真好啊"]
    out = []
    for q in probes:
        ans = kb.knowledge.match(q)
        out.append(f"{q!r}→{'命中' if ans else '未命中'}")
    return " | ".join(out)


@step("好友添加扩展初始化")
def s_friend():
    from wxbot.friend_add import FriendAddExtension
    ext = FriendAddExtension()
    ext.initialize()
    st = ext.get_status()
    return f"enabled={st.get('enabled')} daily_limit={st.get('daily_limit')}"


@step("MQTT broker 连通性")
def s_mqtt():
    from wxbot.config import bot_config
    from wxbot.mqtt.adapter import MqttAdapter
    cfg = bot_config.get("mqtt_worker", {}) or {}
    if not cfg.get("enabled"):
        return "mqtt_worker.enabled=false，跳过"
    broker = cfg.get("broker", {})
    host, port = broker.get("host"), broker.get("port")
    adapter = MqttAdapter(cfg, "smoke_test")
    try:
        adapter.connect()
        time.sleep(3)
        ok = adapter.is_connected()
    finally:
        adapter.shutdown()
    if not ok:
        raise RuntimeError(f"3s 内未连上 {host}:{port}")
    return f"已连通 {host}:{port}"


@step("MinIO 上传器")
def s_minio():
    from wxbot.config import bot_config
    from wxbot.mqtt.common import MinioUploader
    cfg = bot_config.get("mqtt_worker", {}).get("minio", {}) or {}
    up = MinioUploader(cfg)
    return f"available={up.available} endpoint={cfg.get('endpoint')} bucket={cfg.get('bucket')}"


def main() -> int:
    print("=" * 60)
    print("wxbot_pyweixin 冒烟测试")
    print("=" * 60)
    results = []
    for name, fn in STEPS:
        try:
            detail = fn() or ""
            results.append((name, "PASS", detail))
            print(f"[PASS] {name} — {detail}")
        except Exception as e:
            results.append((name, "FAIL", str(e)))
            print(f"[FAIL] {name} — {e}")
    print("-" * 60)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    print(f"汇总: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
