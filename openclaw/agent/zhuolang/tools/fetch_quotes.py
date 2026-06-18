"""
拉取微信联系人聊天记录并输出结构化摘要（原始记录存本地文件，不进上下文）
用法: python fetch_quotes.py <询价单号> <微信联系人1,微信联系人2,...> [--limit=100] [--json] [--instance wx_001]
示例: python fetch_quotes.py CG-20260529-01 "李晓博TS,俱文东"
"""
import sys, os, json, time, argparse
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent


def save_raw_records(batch_no, contact, messages):
    """原始聊天记录存到本地 records/{batch_no}/{contact}/raw.json"""
    path = Path(__file__).parent.parent / 'records' / batch_no / contact
    path.mkdir(parents=True, exist_ok=True)
    (path / 'raw.json').write_text(
        json.dumps(messages, ensure_ascii=False, default=str, indent=2),
        encoding='utf-8'
    )
    return path / 'raw.json'


def extract_summary(messages):
    """从消息列表中提取报价相关信息"""
    if not messages:
        return {'count': 0, 'has_price': False, 'latest_price': None, 'summary': '无消息记录'}

    price_keywords = ['价', '¥', '￥', '元', 'usd', 'rmb', '报价']
    delivery_keywords = ['交期', '交货', '到货', '发货', '货期']
    brand_keywords = ['品牌', '规格', '型号', '参数']

    latest_price = None
    has_price = False
    latest_delivery = None
    latest_brand = None
    latest_msg = None

    for m in reversed(messages):
        content = m.get('content', m.get('message', ''))
        if not isinstance(content, str):
            continue
        content_lower = content.lower()
        if not latest_msg:
            latest_msg = content[:100]

        if any(kw in content for kw in price_keywords):
            if not has_price:
                has_price = True
                latest_price = content[:120]
        if any(kw in content for kw in delivery_keywords):
            if not latest_delivery:
                latest_delivery = content[:120]
        if any(kw in content for kw in brand_keywords):
            if not latest_brand:
                latest_brand = content[:120]

    return {
        'count': len(messages),
        'has_price': has_price,
        'latest_price': latest_price,
        'latest_delivery': latest_delivery,
        'latest_brand': latest_brand,
        'latest_msg': latest_msg,
    }


def main():
    parser = argparse.ArgumentParser(description='拉取微信联系人聊天记录')
    parser.add_argument('batch_no', help='询价单号')
    parser.add_argument('contacts', help='微信联系人列表，逗号分隔')
    parser.add_argument('--limit', type=int, default=50, help='拉取消息条数 (默认: 50)')
    parser.add_argument('--json', action='store_true', help='输出JSON格式')
    parser.add_argument('--instance', required=True, help='Wbot 实例 ID（必须指定）')
    args = parser.parse_args()

    contacts = [s.strip() for s in args.contacts.split(',') if s.strip()]

    agent = ProcurementAgent(instance_id=args.instance)
    if not agent.connect():
        print('[错误] MQTT连接失败')
        sys.exit(1)

    ping = agent.ping(timeout=8)
    if not ping:
        print('[错误] Wbot未在线')
        agent.disconnect()
        sys.exit(1)

    results = {}

    for contact in contacts:
        cid, callback = agent.get_chat_history(contact, limit=args.limit)
        msgs, err = agent.parse_messages(callback)
        raw_path = None

        if err or not msgs:
            summary = {'count': 0, 'has_price': False, 'latest_price': None, 'summary': f'错误: {err or "无消息记录"}'}
        else:
            formatted = []
            for msg in msgs:
                formatted.append({
                    'sender': msg.get('sender', msg.get('talker', '?')),
                    'type': msg.get('type', 'text'),
                    'content': msg.get('content', msg.get('message', '')),
                })
            # 原始记录存本地
            raw_path = save_raw_records(args.batch_no, contact, formatted)
            summary = extract_summary(formatted)

        summary['raw_path'] = str(raw_path) if raw_path else None
        results[contact] = summary

    agent.disconnect()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for contact, s in results.items():
            print(f'【{contact}】')
            print(f'  消息数: {s["count"]}')
            if s.get('summary', '').startswith('错误') or s.get('summary') == '无消息记录':
                print(f'  {s["summary"]}')
            else:
                if s['has_price']:
                    print(f'  💰 有报价: {s["latest_price"]}')
                else:
                    print(f'  💰 暂无报价')
                if s['latest_delivery']:
                    print(f'  📅 交期: {s["latest_delivery"]}')
                if s['latest_brand']:
                    print(f'  🏷️ 品牌: {s["latest_brand"]}')
            if s['raw_path']:
                print(f'  📄 原始记录: {s["raw_path"]}')


if __name__ == '__main__':
    main()
