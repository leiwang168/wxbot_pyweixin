#!/usr/bin/env python
"""
实例/联系人管理工具 - 查询联系人所在的 Wbot 实例

用法:
  python tools/contact_instance.py --instance wx_001 "联系人名"    # 查询联系人所在实例
  python tools/contact_instance.py --instance wx_001 --list        # 列出指定实例的已知联系人
  python tools/contact_instance.py --list                         # 列出所有实例
"""
import sys, os, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / 'tools'))
from _config import get_enabled_instances


def list_all_instances():
    """列出所有实例"""
    instances = get_enabled_instances()
    print(f"已启用实例数: {len(instances)}\n")
    for inst in instances:
        inst_id = inst.get('id')
        print(f"[+] 实例: {inst_id}")
        print(f"    Topic: {inst.get('topic_prefix')}")
        print()


def search_contact(instance_id, name):
    """在指定实例的 context_cache 中查找联系人"""
    cache_dir = Path(__file__).parent.parent / 'logs' / 'context_cache'
    if not cache_dir.exists():
        return []
    results = []
    for cache_file in cache_dir.glob(f'{instance_id}_{name}*.json'):
        try:
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            ctx = data.get('context', {})
            results.append({
                'instance': instance_id,
                'targetName': ctx.get('targetName', name),
                'targetId': ctx.get('targetId', ''),
            })
        except Exception:
            pass
    return results


def list_contacts(instance_id):
    """列出指定实例的已知联系人"""
    cache_dir = Path(__file__).parent.parent / 'logs' / 'context_cache'
    contacts = []
    if cache_dir.exists():
        for cache_file in cache_dir.glob(f'{instance_id}_*.json'):
            try:
                data = json.loads(cache_file.read_text(encoding='utf-8'))
                ctx = data.get('context', {})
                contacts.append({
                    'name': ctx.get('targetName', cache_file.stem.split('_', 1)[1]),
                    'targetId': ctx.get('targetId', ''),
                })
            except Exception:
                pass
    return contacts


def main():
    parser = argparse.ArgumentParser(description='实例/联系人管理工具')
    parser.add_argument('contact_name', nargs='?', help='联系人名称（查询时需要）')
    parser.add_argument('--list', action='store_true', help='列出所有实例或指定实例的联系人')
    parser.add_argument('--instance', required=True, help='指定实例 ID（必须指定）')
    parser.add_argument('--json', action='store_true', help='输出JSON格式')
    args = parser.parse_args()

    if args.list:
        if args.contact_name:
            # 查询联系人
            results = search_contact(args.instance, args.contact_name)
            if not results:
                out = {'error': f'实例 {args.instance} 中未找到联系人 "{args.contact_name}"', 'results': []}
            else:
                out = {'error': None, 'results': results}
        else:
            # 列出指定实例的联系人
            contacts = list_contacts(args.instance)
            out = {'error': None, 'instance': args.instance, 'contacts': contacts}
    else:
        # 列出所有实例
        if args.json:
            out = {'error': None, 'instances': get_enabled_instances()}
        else:
            list_all_instances()
            return

    if args.json or args.list:
        if not args.list and not args.json:
            return
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()