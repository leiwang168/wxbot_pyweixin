# Keep this file empty (or with only comments) to skip heartbeat API calls.

# ═════ 定期检查任务 ═════

# 1. 检查 MQTT 监听器是否在线
# 2. 检查 MQTT broker 连接状态
# 3. 如果离线则自动启动

- 检查监听器在线状态
  - 获取 python 进程中命令行含 mqtt_listener 的进程列表
  - 如果找不到，检测如果为0，则启动: cd tools && python mqtt_listener.py
  - 如果是 cron job 触发且发现监听器离线，自动先重启再report
- 检查 MQTT broker 连接状态
  - 通过 ping 测试 broker 是否可达
  - 如果不可达则记录日志
