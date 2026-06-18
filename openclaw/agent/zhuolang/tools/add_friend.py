#!/usr/bin/env python
"""通过 MQTT 添加微信好友

用法: python tools/add_friend.py <手机号或微信号> [--remark 好友备注] [--verify-text 验证消息] [--tags 标签1,标签2] [--permission 朋友圈|仅聊天] [--instance wx_001]
示例: python tools/add_friend.py 17729292473 --remark "微信联系人A-张三" --verify-text "你好，询价" --tags "微信联系人,啤酒" --permission 朋友圈
"""
import sys, os, json, time, argparse
sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent


def main():
    parser = argparse.ArgumentParser(description='通过 MQTT 添加微信好友')
    parser.add_argument('target', help='手机号或微信号')
    parser.add_argument('--verify-text', default='', help='验证消息')
    parser.add_argument('--remark', default='', help='添加后的备注名')
    parser.add_argument('--tags', default='', help='标签列表，逗号分隔')
    parser.add_argument('--permission', default='朋友圈', choices=['朋友圈', '仅聊天'], help='权限设置')
    parser.add_argument('--instance', default='wx_001', help='Wbot 实例 ID（默认 wx_001）')
    args = parser.parse_args()

    instance_id = args.instance

    agent = ProcurementAgent(instance_id=instance_id)
    if not agent.connect():
        print(json.dumps({'ok': False, 'error': 'MQTT连接失败'}))
        sys.exit(1)

    ping = agent.ping(timeout=10)
    if not ping:
        print(json.dumps({'ok': False, 'error': 'Wbot 未在线'}))
        agent.disconnect()
        sys.exit(1)

    # 新格式：扁平化字段，event=add_friend
    fields = {
        'targetId': args.target,
        'targetName': args.target,
    }
    if args.verify_text:
        fields['verifyText'] = args.verify_text
    if args.remark:
        fields['remark'] = args.remark
    if args.tags:
        fields['tags'] = [t.strip() for t in args.tags.split(',') if t.strip()]
    if args.permission:
        fields['permission'] = args.permission

    cid = agent._publish_task('add_friend', fields)
    print(f'发送添加好友请求: {args.target}, 备注: {args.remark or "无"}, cid={cid}')

    result = agent._wait_callback(cid, timeout=30)
    agent.disconnect()

    if result:
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({'ok': False, 'error': '无回调响应，Wbot 可能不支持 add_friend'}))


if __name__ == '__main__':
    main()
