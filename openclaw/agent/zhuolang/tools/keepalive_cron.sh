#!/bin/bash
# 宿主机cron脚本 - 浊浪MQTT监听器保活
# 每分钟检测，掉线自动重启
# crontab: * * * * * /root/keepalive_zhuolang.sh

WORKDIR="/home/node/.openclaw/workspace/zhuolang"
CONTAINER="973a2a407c66"
LOG="/var/log/keepalive_zhuolang.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测开始" >> "$LOG"

# 检测监听器进程
LISTENER_OK=$(docker exec $CONTAINER pgrep -f "mqtt_listener.py" 2>/dev/null | wc -l)

if [ "$LISTENER_OK" -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 监听器不在线，重启中..." >> "$LOG"
    docker exec -d $CONTAINER bash -c "cd $WORKDIR && PYTHONPATH=$WORKDIR/python_deps:\$PYTHONPATH nohup python3 tools/mqtt_listener.py > /dev/null 2>&1 &"
    sleep 2
    # 确认重启结果
    RETRY_OK=$(docker exec $CONTAINER pgrep -f "mqtt_listener.py" 2>/dev/null | wc -l)
    if [ "$RETRY_OK" -ge 1 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 重启成功" >> "$LOG"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 重启失败，请手动检查" >> "$LOG"
    fi
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 监听器在线，无需操作" >> "$LOG"
fi
