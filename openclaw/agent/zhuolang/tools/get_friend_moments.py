# -*- coding: utf-8 -*-
"""获取好友朋友圈 — 定时巡检下发入口（异步，不等回调）。

朋友圈获取是耗时操作：本工具只负责下发 get_friend_moments 指令并立即返回 cid，
不同步等待。wxbot 完成后异步发 moments_task_result 回调，由常驻 mqtt_listener
收到并写入 records/friend_moments/<好友>.json。

用法：
  python get_friend_moments.py --instance wx_001 --friend "浊浪精酿全国业务" --json
  python get_friend_moments.py --instance wx_001 --friend "..." --start 2026-06-20 --end 2026-06-29
"""
import sys
import os
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent


def request_friend_moments(friend, start, end, instance='wx_001', limit=50):
    """异步下发获取好友朋友圈指令，立即返回 cid，不等回调。

    Returns:
        dict: {'ok', 'cid', 'friend', 'instance', 'range', 'message'} 或 {'ok': False, 'error'}
    """
    agent = ProcurementAgent(instance_id=instance)
    if not agent.connect():
        return {'ok': False, 'error': 'MQTT连接失败', 'friend': friend, 'instance': instance}
    try:
        cid = agent.get_friend_moments(friend, start, end, limit)
        import time as _t
        _t.sleep(1)  # 确保 qos=1 publish 送达 broker 后再断开
    finally:
        agent.disconnect()
    return {
        'ok': True, 'cid': cid, 'friend': friend, 'instance': instance,
        'range': {'start': start, 'end': end, 'limit': limit},
        'message': f'已发起查询，结果将异步写入 records/friend_moments/{friend}.json',
    }


def main():
    parser = argparse.ArgumentParser(description='获取好友朋友圈（异步下发，结果由 listener 落盘）')
    parser.add_argument('--instance', required=True, help='Wbot 实例 ID')
    parser.add_argument('--friend', required=True, help='好友备注/昵称')
    parser.add_argument('--start', default='', help='开始日期 YYYY-MM-DD（缺省=当天）')
    parser.add_argument('--end', default='', help='结束日期 YYYY-MM-DD（缺省=当天）')
    parser.add_argument('--limit', type=int, default=50, help='最多获取条数（默认 50）')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    args = parser.parse_args()

    today = datetime.now().strftime('%Y-%m-%d')
    start = args.start or today
    end = args.end or today

    result = request_friend_moments(args.friend, start, end, instance=args.instance, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result.get('ok'):
        print(f"✅ 已发起查询 cid={result['cid']}")
        print(f"   好友: {args.friend} | 范围: {start} ~ {end} | 实例: {args.instance}")
        print(f"   结果稍后由 listener 写入 records/friend_moments/{args.friend}.json")
    else:
        print(f"❌ 下发失败: {result.get('error', '')}")
        sys.exit(1)


if __name__ == '__main__':
    main()
