"""
追问微信联系人补充信息（交期/品牌规格等）
用法: python follow_up.py --chat <微信联系人> --message <追问内容> --instance <实例ID> [--context <base64上下文>] [--json]
      python follow_up.py <contact> <message> --instance <实例ID> [--json]  (兼容旧格式)
输出: JSON格式结果
"""
import sys, os, json
import argparse
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent
from _content_check import validate


def main():
    parser = argparse.ArgumentParser(description='追问微信联系人补充信息')
    parser.add_argument('contact', nargs='?', help='微信联系人微信名(兼容旧格式)')
    parser.add_argument('message', nargs='?', help='追问内容(兼容旧格式)')
    parser.add_argument('--json', action='store_true', help='输出JSON格式')
    parser.add_argument('--instance', required=True, help='Wbot 实例 ID（必须指定）')
    parser.add_argument('--context', help='消息上下文(base64编码的JSON)')
    parser.add_argument('--chat', help='聊天对象(新格式)')
    parser.add_argument('--message', dest='message_new', help='回复内容(新格式)')
    args = parser.parse_args()

    # 解析上下文
    context = {}
    if args.context:
        import base64
        try:
            context_json = base64.b64decode(args.context).decode('utf-8')
            context = json.loads(context_json)
        except Exception as e:
            result = {'ok': False, 'error': f'上下文解析失败: {e}'}
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            sys.exit(1)

    # 如果没传 --context，自动从本地缓存读取（兜底）
    if not context:
        import time
        chat_name = args.chat or args.contact or ''
        cache_dir = Path(__file__).parent.parent / 'logs' / 'context_cache'
        # 缓存文件名可能带后缀（如 李晓博TS-15129562650），尝试前缀匹配
        for f in cache_dir.glob(f'{args.instance}_{chat_name}*.json'):
            try:
                cache_data = json.loads(f.read_text(encoding='utf-8'))
                cached_ctx = cache_data.get('context', {})
                if int(time.time()) - cache_data.get('saved_at', 0) < 300:
                    context = cached_ctx
                    break
            except Exception:
                pass

    # 优先使用新格式参数，兼容旧格式
    chat = args.chat or args.contact or ''
    message = args.message_new or args.message or ''

    if not chat or not message:
        result = {'ok': False, 'error': '缺少 chat 和 message 参数'}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    # 合规检查
    if not validate(chat, message):
        sys.exit(1)

    agent = ProcurementAgent(instance_id=args.instance)
    if not agent.connect():
        result = {'ok': False, 'error': 'MQTT连接失败'}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    # ping 检查跳过：Wbot 不回复 ping 事件，不代表离线
    # 直接尝试发消息，以 send_text 回调结果为准

    # 检查实例一致性：context 中的 agentId/role 应与 --instance 匹配
    ctx_instance = context.get('agentId', '')
    if ctx_instance and ctx_instance != args.instance:
        err_msg = f'实例不匹配: --instance={args.instance} 但上下文属于 {ctx_instance}'
        result = {'ok': False, 'error': err_msg, 'contact': chat, 'instance': args.instance}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    # 从上下文中提取 targetId/targetName 和 correlationId
    target_id = context.get('targetId', '')
    target_name = context.get('targetName', '')
    correlation_id = context.get('correlationId', None)

    cid, callback = agent.send_text(
        target=chat,
        message=message,
        target_id=target_id,
        target_name=target_name or chat,
        correlation_id=correlation_id,
        context=context,
    )
    error = ''
    if callback:
        result = callback.get('result', {})
        if isinstance(result, dict):
            error = result.get('error', '')

    # publish 成功拿到 cid 即已发出，回调可能延迟或缺失（微信常见）
    # 有 cid 且无 error → 视为成功
    ok = bool(cid and not error)
    agent.disconnect()

    result = {'ok': ok, 'cid': cid, 'error': error, 'contact': chat, 'instance': args.instance}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif not ok and error:
        sys.exit(1)


if __name__ == '__main__':
    main()
