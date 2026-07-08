# -*- coding: utf-8 -*-
"""验证主循环异常飞书推送（直接调 _alert_loop_exception，绕过夜间静默判断）。"""
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

from wxbot import webhook_send

# 先验证 webhook 配置 + 连通性
print(">>> 读取 webhook 配置...")
cfg = webhook_send.load_config()
print(f"    enabled={cfg['enabled']} url={cfg['url'][:60]}...")
if not cfg["enabled"]:
    print(">>> ❌ webhook 未启用，请检查 config/webhook.json")
    sys.exit(1)

# 模拟 monitor._alert_loop_exception 的推送逻辑（白天）
nickname = "测试昵称"
msg = f"测试主循环异常推送_{int(time.time())}"
print(f"\n>>> 推送飞书: title=【{nickname}】微信机器人异常")
ok, info = webhook_send.send_webhook(
    title=f"【{nickname}】微信机器人异常",
    content=f"异常: {msg}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
)
print(f">>> 结果: ok={ok} info={info}")
if ok:
    print(">>> ✅ 飞书推送成功，请检查飞书群")
else:
    print(">>> ❌ 推送失败")

# 验证节流逻辑：同一异常 1 小时内不应重复推
print("\n>>> 验证节流逻辑（用 monitor 实例）...")
from wxbot.monitor import monitor
monitor._last_loop_alert = ("重复异常文本", time.time())  # 模拟刚推过
# 静默调用：内部应因节流 return，不实际推送
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pass
# 直接判断：构造一个相同异常，应被节流
monitor._last_loop_alert = ("same_error", time.time())
before = monitor._last_loop_alert
monitor._alert_loop_exception(RuntimeError("same_error"))
after = monitor._last_loop_alert
if before == after:
    print(">>> ✅ 节流生效：相同异常 1 小时内被跳过")
else:
    print(">>> ⚠️ 节流未生效（注意：夜间时段23-6点会直接静默return，也算正常）")

# 验证夜间静默逻辑
hour = time.localtime().tm_hour
silent = hour >= 23 or hour < 6
print(f"\n>>> 当前小时: {hour}, 夜间静默期(23-6): {silent}")
