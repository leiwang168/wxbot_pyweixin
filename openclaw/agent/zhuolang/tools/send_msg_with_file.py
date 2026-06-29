"""
发送文件（含文字消息）到微信联系人
流程：
  本地文件 → 上传到MinIO → MQTT发送文字+fileUrl
  网络地址(http/https开头) → 直接使用URL发送，跳过上传

用法: python send_msg_with_file.py "联系人" "消息内容" "文件路径/URL" [--json] [--instance wx_001]
"""
import sys, os, json, argparse, uuid
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent
from upload_to_minio import upload_file
from _content_check import validate


def _lookup_target_id(agent, name, timeout=15):
    """通过 get_friend_details 查询联系人微信号，返回 (target_id, target_name)"""
    cid, cb = agent.get_friend_details(name_prefix=name, timeout=timeout)
    if cb:
        result = cb.get('result', {})
        if isinstance(result, dict):
            friends = result.get('friends', [])
            # 精确匹配
            for f in friends:
                remark = f.get('remark', '') or ''
                nickname = f.get('nickname', '') or ''
                if remark == name or nickname == name:
                    wid = f.get('wxid', '') or f.get('id', '') or ''
                    if wid and wid != name:
                        return wid, remark or nickname
    return None, None


def main():
    parser = argparse.ArgumentParser(description='发送文件到微信联系人')
    parser.add_argument('contacts', help='微信联系人（支持逗号分隔多个）')
    parser.add_argument('message', help='消息内容')
    parser.add_argument('file_path', help='附件文件路径或网络URL(http/https开头则跳过上传）')
    parser.add_argument('--json', action='store_true', help='输出JSON格式')
    parser.add_argument('--instance', required=True, help='Wbot 实例 ID（必须指定）')
    parser.add_argument('--target-id', help='目标微信号（targetId），显式指定后不自动查询）')
    parser.add_argument('--target-name', help='目标微信备注名（targetName），不传则用联系人名称）')
    parser.add_argument('--correlation-id', help='关联ID，不传时自动生成')
    args = parser.parse_args()

    contacts = [s.strip() for s in args.contacts.split(',') if s.strip()]

    if not contacts:
        out = {'error': '未指定微信联系人', 'results': []}
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        sys.exit(1)

    # 判断是本地文件还是网络URL
    is_url = args.file_path.lower().startswith(('http://', 'https://'))

    if is_url:
        file_url = args.file_path
        filename = os.path.basename(args.file_path.split('?')[0])
    else:
        if not os.path.isfile(args.file_path):
            out = {'error': f'附件不存在: {args.file_path}', 'results': []}
            if args.json:
                print(json.dumps(out, ensure_ascii=False))
            sys.exit(1)

        filename = os.path.basename(args.file_path)
        remote_path = f'files/{filename}'
        try:
            file_url = upload_file(args.file_path, remote_path)
        except Exception as e:
            out = {'error': f'上传失败: {e}', 'results': []}
            if args.json:
                print(json.dumps(out, ensure_ascii=False))
            sys.exit(1)

    message = args.message

    # 合规检查
    for s in contacts:
        if not validate(s, message):
            sys.exit(1)

    # MQTT发送，参考 follow_up.py 处理 targetId/targetName
    all_results = []
    agent = ProcurementAgent(instance_id=args.instance)
    if not agent.connect():
        all_results.append({'instance': args.instance, 'contacts': contacts, 'error': 'MQTT连接失败'})
        print(json.dumps({'error': 'MQTT连接失败'}) if args.json else '[错误] MQTT连接失败')
        sys.exit(1)

    # 对每个联系人：优先从 --target-id，其次 context_cache，最后自动查好友列表
    # 先查 context_cache
    cache_dir = Path(__file__).parent.parent / 'logs' / 'context_cache'
    need_lookup = []  # 需要查好友列表的
    name_to_target = {}  # name -> (target_id, target_name)
    identity_from_cache = {}  # 缓存中找到的身份信息

    for s in contacts:
        if args.target_id:
            name_to_target[s] = (args.target_id, args.target_name or s)
            continue
        # 从 context_cache 找（优先，减少一次好友列表请求）
        found = False
        if cache_dir.exists():
            for f in cache_dir.glob(f'{args.instance}_{s}*.json'):
                try:
                    cache_data = json.loads(f.read_text(encoding='utf-8'))
                    cached_ctx = cache_data.get('context', {})
                    target_id = cached_ctx.get('targetId', '')
                    target_name = cached_ctx.get('targetName', '')
                    if target_id and target_id != target_name:
                        name_to_target[s] = (target_id, target_name)
                        # 缓存中的自身微信身份
                        if not identity_from_cache:
                            identity_from_cache['selfWxName'] = cached_ctx.get('selfWxName', '')
                            identity_from_cache['selfWxId'] = cached_ctx.get('selfWxId', '')
                        found = True
                        break
                except Exception:
                    pass
        if not found:
            need_lookup.append(s)

    # 查好友列表（同时获取 selfWxName/selfWxId）
    self_wx_name = ''
    self_wx_id = ''
    if need_lookup:
        cid, cb = agent.get_friend_details(timeout=15)
        if cb:
            result = cb.get('result', {})
            if isinstance(result, dict):
                # 从回调中提取自身微信身份
                self_wx_name = result.get('selfWxName', '')
                self_wx_id = result.get('selfWxId', '')
                friends = result.get('friends', [])
                for f in friends:
                    remark = f.get('remark', '') or ''
                    nickname = f.get('nickname', '') or ''
                    wid = f.get('wxid', '') or f.get('id', '') or ''
                    for name in (remark, nickname):
                        if name and name in need_lookup and wid and wid != name:
                            name_to_target[name] = (wid, remark or nickname)
    # 如果没拉好友列表，从缓存取身份；缓存也没有才 ping
    if not need_lookup:
        if identity_from_cache:
            self_wx_name = identity_from_cache.get('selfWxName', '')
            self_wx_id = identity_from_cache.get('selfWxId', '')
        else:
            # 发一条 ping 拿身份信息
            cb = agent.ping(timeout=8)
            if cb:
                self_wx_name = cb.get('selfWxName', '') or cb.get('result', {}).get('selfWxName', '')
                self_wx_id = cb.get('selfWxId', '') or cb.get('result', {}).get('selfWxId', '')

    for s in contacts:
        if s in name_to_target:
            target_id, tn = name_to_target[s]
            target_name = args.target_name or tn
        else:
            target_id = s
            target_name = args.target_name or s

        # 构建 context 传递信封字段
        ctx = {
            'targetId': target_id,
            'targetName': target_name,
        }
        if self_wx_name:
            ctx['selfWxName'] = self_wx_name
        if self_wx_id:
            ctx['selfWxId'] = self_wx_id

        # 优先用手动传入的 correlation_id，没有则自动生成
        corr_id = args.correlation_id or uuid.uuid4().hex[:8]

        cid, callback = agent.send_text(
            target=s,
            message=message,
            file_url=file_url,
            target_id=target_id,
            target_name=target_name,
            correlation_id=corr_id,
            context=ctx,
        )
        status = '?'
        error = ''
        if callback:
            status = callback.get('status', '?')
            result_data = callback.get('result', {})
            if isinstance(result_data, dict):
                status = result_data.get('status', status)
                error = result_data.get('error', '')
        ok = status in ('ok', 'success')
        all_results.append({'contact': s, 'instance': args.instance, 'cid': cid, 'ok': ok, 'error': error})

    agent.disconnect()

    out = {'error': None, 'file_url': file_url, 'results': all_results}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f'已发送: {message[:30]}... 文件: {filename}')


if __name__ == '__main__':
    main()
