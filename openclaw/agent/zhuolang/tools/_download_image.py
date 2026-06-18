"""尝试通过MQTT获取图片 - 尝试不同的task type"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent

agent = ProcurementAgent(timeout=15)
if not agent.connect():
    print('[错误] MQTT连接失败')
    sys.exit(1)

ping = agent.ping(timeout=8)
if not ping:
    print('[错误] Wbot未在线')
    agent.disconnect()
    sys.exit(1)
print('[在线] ✓')

# Try different task types
task_types = ['get_media', 'download_media', 'get_image', 'get_file', 'fetch_media']

for task_type in task_types:
    cid = agent._publish_task(task_type, {
        'contact': '静静',
        'filename': 'wxauto_image_20260528212540712514.jpg',
    })
    callback = agent._wait_callback(cid, timeout=10)
    if callback:
        err = callback.get('result', {}).get('error', '')
        print(f'{task_type}: {err[:100] if err else "OK"}')
        if not err:
            print(json.dumps(callback, ensure_ascii=False, indent=2, default=str)[:500])
    else:
        print(f'{task_type}: timeout')

agent.disconnect()
