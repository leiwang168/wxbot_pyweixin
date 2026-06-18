#!/usr/bin/env python
"""将回复转发到小程序 app/{agent_name}/out topic

用法: python tools/reply_to_app.py <correlationId> <回复内容>
"""
import sys
sys.path.insert(0, '.')

from tools.mqtt_client import ProcurementAgent
import json


def main():
    if len(sys.argv) < 3:
        print(json.dumps({'ok': False, 'error': '用法: reply_to_app.py <correlationId> <回复内容>'}))
        sys.exit(1)

    correlation_id = sys.argv[1]
    reply_text = sys.argv[2]

    agent = ProcurementAgent()
    agent.connect()
    agent._publish_app_out(correlation_id, {'ok': True, 'reply': reply_text})
    agent.disconnect()
    print(json.dumps({'ok': True, 'cid': correlation_id}, ensure_ascii=False))


if __name__ == '__main__':
    main()
