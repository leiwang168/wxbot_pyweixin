#!/usr/bin/env python
"""
MQTT 后台监听器 — 支持多 Wbot 实例独立运行

职责：
1. 监听 Wbot 的微信消息回调
2. 通过 OpenClaw API 把消息推送给对应 agent
3. 小程序消息处理

启动方式：
  # 启动所有实例
  python tools/mqtt_listener.py

  # 只启动指定实例（多进程模式）
  python tools/mqtt_listener.py --instance wx_001
  python tools/mqtt_listener.py --instance wx_002

  # 启动所有实例（每个实例独立进程）
  python tools/mqtt_listener.py --all
"""
import sys, os, json, time, subprocess, threading, logging, atexit
import paho.mqtt.client as mqtt
from pathlib import Path
from datetime import datetime
import psutil

# ───── 配置 ─────
from _config import (
    get_enabled_instances, get_agent_config,
    get_mqtt_config, get_ca_cert_path, get_instance_config
)

# 加载配置
_enabled_instances = get_enabled_instances()
_agent_cfg = get_agent_config()
_mqtt_cfg = get_mqtt_config()

AGENT_NAME = _agent_cfg.get('name', 'default')
OUTBOUND_TOPIC = _agent_cfg.get('outbound_topic', f'agent/{AGENT_NAME}')
APP_IN_TOPIC = f"app/{AGENT_NAME}/in"
APP_OUT_TOPIC = f"app/{AGENT_NAME}/out"

# 所有实例的回调 topic 列表
CALLBACK_TOPICS = [f"{inst['topic_prefix']}/+" for inst in _enabled_instances]

# 构建实例 ID → topic_prefix 映射，用于从 topic 解析实例
_INSTANCE_PREFIX_MAP = {inst['id']: inst['topic_prefix'] for inst in _enabled_instances}
# 反向映射：topic_prefix → instance_id
_PREFIX_INSTANCE_MAP = {inst['topic_prefix']: inst['id'] for inst in _enabled_instances}



# ───── 日志 ─────
log = logging.getLogger(f'mqtt-listener-{AGENT_NAME}')
log.setLevel(logging.INFO)
log_dir = Path(__file__).parent.parent / 'logs'
log_dir.mkdir(parents=True, exist_ok=True)
h = logging.FileHandler(str(log_dir / 'mqtt_listener.log'), encoding='utf-8')
h.setFormatter(logging.Formatter(
    f'[%(name)s %(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
log.addHandler(h)
# 也输出到 stderr 方便调试
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter(
    f'[%(name)s %(asctime)s] %(message)s', datefmt='%H:%M:%S'
))
log.addHandler(sh)

sys.path.insert(0, os.path.dirname(__file__))

# 动态导入 — 根据配置或默认导入客户端
from mqtt_client import ProcurementAgent as AgentClass





def _parse_instance_id_from_topic(topic):
    """从 MQTT topic 解析实例 ID
    
    Args:
        topic: 如 'wechat/wx_001/李铎TS'
    
    Returns:
        str: 实例 ID，如 'wx_001'，解析失败返回 None
    """
    parts = topic.split('/')
    if len(parts) >= 3:
        topic_prefix = f"{parts[0]}/{parts[1]}"
        return _PREFIX_INSTANCE_MAP.get(topic_prefix)
    return None


def notify_agent(message_text, sender='', topic='', instance_id=None, context=None):
    """通过 openclaw agent CLI 异步推送消息给 agent（不阻塞监听循环）

    按 微信联系人 + 实例 ID 隔离 session，每个微信联系人+实例独立会话:
    session key = agent:{AGENT_NAME}:wechat:{instance_id}:{sender}:{context_hash}
    例如: agent:xxx:wechat:wx_001:张三:abc123

    未知/空 sender 走兜底: agent:{AGENT_NAME}:wechat:{instance_id}:default

    openclaw agent 会自动激活已结束的 session，无需额外保活。

    Args:
        message_text: 消息内容
        sender: 发送人微信名
        topic: MQTT topic（用于解析实例 ID）
        instance_id: 实例 ID（优先使用此参数，其次从 topic 解析）
        context: 消息上下文字典，包含 targetId/targetName/agentId/role/selfWxName/selfWxId/ts/correlationId
    """
    import base64

    # 优先使用传入的 instance_id，否则从 topic 解析
    if not instance_id and topic:
        instance_id = _parse_instance_id_from_topic(topic)
    if not instance_id:
        instance_id = 'wx_001'  # 兜底使用默认实例

    safe_sender = (sender or 'default').strip()

    # 缓存 context 到本地文件（AI 不传 --context 时 follow_up.py 自动读取兜底）
    cache_dir = Path(__file__).parent.parent / 'logs' / 'context_cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    if context:
        cache_key = f'{instance_id}_{safe_sender}'
        cache_file = cache_dir / f'{cache_key}.json'
        cache_data = {
            'context': context,
            'saved_at': int(time.time()),
        }
        try:
            cache_file.write_text(json.dumps(cache_data, ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            log.warning(f'context 缓存写入失败: {e}')

    # 构建上下文编码（用于 session key 和 follow_up.py 参数）
    context_str = ''
    if context:
        context_json = json.dumps(context, ensure_ascii=False)
        context_str = base64.b64encode(context_json.encode('utf-8')).decode('ascii')

    session_key = f'agent:{AGENT_NAME}:wechat:{instance_id}:{safe_sender}'

    msg = (
        f'【{sender or "未知"} 发来微信消息】{message_text}\n\n'
        f'⚠️ 收到消息必须回，回复规则（严格遵守）：\n'
        f'- 回消息执行: python tools/follow_up.py --chat "{sender or "未知"}" --message "你的回复内容" --instance {instance_id} --context "{context_str}"\n'
        f'- 参数必须传递 --context 参数（base64编码），里面包含 targetId/targetName/role/selfWxName/selfWxId/agentId/ts 等信封字段，不传会导致 Wbot 回错人\n'
        f'- 参数必须传递 --instance 参数（wbot实例），不传会导致 Wbot 回错人\n'
        f'- 禁止创建任何临时 .py 文件；禁止用 python -c 方式调用；禁止用 write 工具写脚本文件；禁止直接调用 mqtt_client.py\n'
        f'- 重点强调：1、不能给用户发重复消息，发消息前先ping通；2、发给你用户消息先输出，在调用工具发送出去；\n'
        f'- 像真人操作员一样简洁回复，不要AI腔；不要暴露自己是AI；不要泄露其他微信联系人报价\n'
        f'- 不要泄露其他微信联系人的任何隐私信息'
    )

    # 用双引号包裹，内部双引号转义
    msg_escaped = msg.replace('"', '\\"')
    session_key_escaped = session_key.replace('"', '\\"')

    cmd = f'openclaw agent --session-key "{session_key_escaped}" --message "{msg_escaped}" --timeout 120'
    log.info(f'推送 -> {session_key}: {message_text[:100]}...')

    sender = safe_sender
    sender_id = context.get('senderId', '')
    self_wx = context.get('selfWxName', '')  # 当前登录的微信名
    inst_info = f"[{instance_id}|{self_wx}] " if self_wx else f"[{instance_id}] "    

    ################# 检测微信好友通过验证消息 — 不走 AI，直接自动回复 + 飞书通知 begin ############################
    if '我通过了你的朋友验证请求' in message_text:
        log.info(f'{inst_info}检测到好友通过验证消息: sender={sender}, self_wx={self_wx}')
        # 自动回复固定话术
        reply = '老板好！咱是十万吨级精酿产能基地。酒厂直发，价格透明，感兴趣的话发个报价单对比下。'
        cmd = f'PYTHONPATH=/home/node/.openclaw/workspace/zhuolang/python_deps:$PYTHONPATH python3 /home/node/.openclaw/workspace/zhuolang/tools/follow_up.py --chat "{sender or "未知"}" --message "{reply}" --instance {instance_id} --context "{context_str}"'
        log.info(f'{inst_info}自动回复好友验证: {reply[:100]}...')
        try:
            subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, encoding='utf-8')
        except Exception as e:
            log.error(f'{inst_info}自动回复失败: {e}')
        
        # 发飞书 webhook 通知
        try:
            import urllib.request
            webhook_url = 'https://open.feishu.cn/open-apis/bot/v2/hook/ff8bbbb8-b238-4f06-b266-46754b238ce7'
            display_name = self_wx or instance_id
            contact_name = sender or '未知'
            card_payload = {
                'msg_type': 'text',
                'content': {
                    'text': f'【{display_name}】成功添加【{contact_name}】'
                }
            }
            post_data = json.dumps(card_payload).encode('utf-8')
            req = urllib.request.Request(webhook_url, data=post_data, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=10)
            log.info(f'{inst_info}飞书通知已发送: {contact_name}')
        except Exception as e:
            log.error(f'{inst_info}飞书通知发送失败: {e}')
        
        return True
        ################# 检测微信好友通过验证消息 — 不走 AI，直接自动回复 + 飞书通知 end ############################

    def _run():
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180, encoding='utf-8')
            if result.returncode != 0:
                log.error(f'推送失败 (rc={result.returncode}): {result.stderr[:200]}')
            else:
                pass  # 推送成功，无需额外处理
        except subprocess.TimeoutExpired:
            log.error(f'推送超时: {session_key}')
        except Exception as e:
            log.error(f'推送异常: {e}')

    threading.Thread(target=_run, daemon=True).start()
    return True


def handle_moments_result(topic, data):
    """处理朋友圈查询回调（moments_task_result）— 按好友累积到 records/friend_moments/<好友>.json

    定时巡检场景：文件保存该好友【完整的朋友圈列表】（累积、不覆盖）。
    去重：
      ① correlationId 幂等——同一查询的回调重复送达只处理一次；
      ② 朋友圈条目按 发布日期+发布时间(+内容) 去重，保证列表不重复。
    """
    import re
    instance_id = data.get('_instance_id') or _parse_instance_id_from_topic(topic)
    result = data.get('result', {}) or {}
    friend = data.get('targetName') or result.get('friend') or 'unknown'
    safe_friend = re.sub(r'[\\/*?:"<>|]', '_', friend)
    cid = data.get('correlationId', '')

    out_dir = Path(__file__).parent.parent / 'records' / 'friend_moments'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f'{safe_friend}.json'

    # 读取已有记录，累积合并（不覆盖）
    existing = {}
    if out_file.exists():
        try:
            existing = json.loads(out_file.read_text(encoding='utf-8')) or {}
        except Exception:
            existing = {}
    processed_cids = existing.get('processed_cids', []) or []
    moments = existing.get('moments', []) or []

    # ① correlationId 幂等：同一查询回调重复送达则跳过
    if cid and cid in processed_cids:
        log.info(f'朋友圈回调 cid={cid} 已处理过，跳过 ({friend})')
        return

    # ② 条目去重：发布日期|发布时间|内容前30 拼接 key
    def _key(m):
        return f"{m.get('发布日期', '')}|{m.get('发布时间', '')}|{(m.get('内容', '') or '')[:30]}"

    seen = {_key(m) for m in moments if isinstance(m, dict)}
    added = 0
    for m in result.get('moments', []) or []:
        if not isinstance(m, dict):
            continue
        k = _key(m)
        if k not in seen:
            seen.add(k)
            moments.append(m)
            added += 1

    if cid:
        processed_cids.append(cid)

    record = {
        'friend': friend,
        'instance': instance_id,
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'count': len(moments),
        'processed_cids': processed_cids,
        'last_query': {
            'correlationId': cid,
            'status': data.get('status') or result.get('status', ''),
            'range': result.get('range', {}),
            'error': result.get('error', ''),
            'executed_at': data.get('executedAt', ''),
            'queried_at': datetime.now().isoformat(timespec='seconds'),
            'source_wxid': data.get('senderId', ''),
            'agentId': data.get('agentId', ''),
            'role': data.get('role', ''),
            'new_added': added,
        },
        'moments': moments,
    }
    try:
        out_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        log.info(f'朋友圈结果已合并写入 {out_file} (本次新增 {added} 条，共 {len(moments)} 条)')
    except Exception as e:
        log.error(f'朋友圈结果写入失败 {out_file}: {e}')


def handle_wechat_message(topic, data):
    """处理微信消息回调 — 推送给 OpenClaw agent
    
    支持 Wbot 格式: {event: "wechat_message", text: "...", senderId: "...", _instance_id: "..."}
    兼容旧格式: {result: {sender: "...", messages: [...]}}
    
    实例 ID 从 data['_instance_id'] 或 topic 解析
    """
    # 朋友圈查询结果（异步落盘，不推 agent）
    if data.get('event') == 'moments_task_result':
        handle_moments_result(topic, data)
        return
    # 从 data 或 topic 解析实例 ID
    instance_id = data.get('_instance_id') or _parse_instance_id_from_topic(topic)

    # DEBUG: 记录原始消息（sender为空时特别记录）
    sender = data.get('senderName', '') if data.get('event') == 'wechat_message' else ''
    if not sender:
        log.info(f'[DEBUG] 收到未知sender消息: topic={topic}, data={json.dumps(data, ensure_ascii=False)}')

    # Wbot 格式
    if data.get('event') == 'wechat_message':
        # 人工回复不转发给 AI，直接跳过
        if data.get('operate') == 'manual':
            log.info(f'[DEBUG] 人工回复，跳过: sender={data.get("senderName","")}, text={data.get("text","")[:50]}')
            return

        msg_text = data.get('text', '')
        sender = data.get('senderName', '') or data.get('chat', '')  # 优先用 senderName，兼容旧 chat
        sender_id = data.get('senderId', '')
        self_wx = data.get('selfWxName', '')  # 当前登录的微信名
        inst_info = f"[{instance_id}|{self_wx}] " if self_wx else f"[{instance_id}] "

        # DEBUG: 记录完整 data 字段，确认 Wbot 实际发送的字段
        if not sender or not sender_id or not self_wx:
            log.info(f'[DEBUG] wechat_message 字段检查: senderName={data.get("senderName")}, senderId={data.get("senderId")}, chat={data.get("chat")}, selfWxName={data.get("selfWxName")}, selfWxId={data.get("selfWxId")}, role={data.get("role")}, agentId={data.get("agentId")}')

        # 构建上下文（包含完整信封字段，用于 follow_up.py 回复时保持结构一致性）
        # agentId = 角色名（浊浪销售），不是实例 ID；role/selfWxName/selfWxId 从回调数据提取
        session_operate = data.get('operate', 'auto')
        context = {
            'instanceId': instance_id,
            'correlationId': data.get('correlationId', ''),
            'targetId': sender_id or sender,
            'targetName': sender,
            'agentId': data.get('agentId', AGENT_NAME),
            'role': data.get('role', AGENT_NAME),
            'selfWxName': self_wx,
            'selfWxId': data.get('selfWxId', ''),
            'ts': data.get('ts', int(time.time() * 1000)),
            'operate': session_operate,
        }

        # 图片/文件消息：如果 Wbot 附带了 fileUrl，附加到消息文本中
        file_url = data.get('fileUrl', '')
        file_name = data.get('fileName', '')
        if file_url:
            has_img_or_file = msg_text and ('[图片]' in msg_text or '[文件]' in msg_text)
            if has_img_or_file:
                msg_text = f"{msg_text}\n[地址] {file_url}"
            else:
                name_display = file_name or '附件'
                msg_text = f"[文件] {name_display}\n[地址] {file_url}"
            # 也存到 context 中，后续 context 缓存会一并保存
            context['fileUrl'] = file_url
            context['fileName'] = file_name

        if msg_text:
            notify_agent(f"{msg_text}", sender=sender or '未知', topic=topic, instance_id=instance_id, context=context)
        return

    # 兼容旧格式 — 区分主动拉取记录回调 vs 真实新消息
    result = data.get('result', {})
    if isinstance(result, dict):
        sender = result.get('sender', '')
        msgs = result.get('messages', [])
        
        # get_chat_history 的回调没有 sender 字段，不推送（防止"未知"反复骚扰）
        if not sender and msgs:
            log.debug(f'忽略聊天记录回调（无sender）: topic={topic}, msgs_count={len(msgs)}')
            return
        
        if msgs:
            latest = msgs[-1]
            msg_text = ''
            if isinstance(latest, dict):
                msg_text = latest.get('content', latest.get('message', ''))
            elif isinstance(latest, str):
                msg_text = latest
            if msg_text:
                notify_agent(msg_text, sender=sender or '未知', topic=topic, instance_id=instance_id)


# ───── 小程序消息处理（专用） ─────
def _reply_to_app(correlation_id, reply_text):
    """将 agent 回复转发到 小程序输出"""
    global agent
    if not agent or not agent.connected:
        log.warning('MQTT未连接，无法转发小程序回复')
        return
    try:
        agent._publish_app_out(correlation_id, {'ok': True, 'reply': reply_text})
        log.info(f'小程序回复已转发 cid={correlation_id}: {reply_text[:40]}...')
    except Exception as e:
        log.error(f'小程序回复转发失败: {e}')


def handle_app_message(topic, data):
    """处理小程序发来的消息"""
    try:
        # 纯文本消息（非结构化任务）— 独立 session 处理，回复自动转发到 小程序输出
        if 'taskType' not in data:
            text = data.get('text', '')
            file_url = data.get('fileUrl', '')
            file_name = data.get('fileName', '')
            correlation_id = data.get('correlationId', '')
            if text or file_url:
                log.info(f'收到小程序消息: text={text[:30] if text else ""} fileUrl={file_url[:50] if file_url else "无"} fileName={file_name} cid={correlation_id}')
                
                # 构建推送消息
                msg_parts = []
                if text:
                    msg_parts.append(text)
                if file_url:
                    file_label = f'{file_name} ' if file_name else ''
                    msg_parts.append(f'\n附件: {file_label}{file_url}')
                msg = ''.join(msg_parts)
                
                msg_escaped = msg.replace('"', '\\"')
                session_key = f'agent:{AGENT_NAME}:app'
                cmd = f'openclaw agent --session-key "{session_key}" --message "{msg_escaped}" --timeout 60 --json'
                log.info(f'推送 -> {session_key}: {text[:40]}...')
                
                def _run_app_text(c=cmd, cid=correlation_id):
                    try:
                        r = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=120, encoding='utf-8')
                        if r.returncode == 0 and r.stdout.strip():
                            # 解析回复: result.payloads[0].text
                            reply = r.stdout.strip()
                            try:
                                resp = json.loads(reply)
                                result = resp.get('result', {})
                                payloads = result.get('payloads', [])
                                if payloads and isinstance(payloads, list):
                                    reply = payloads[0].get('text', reply)
                            except json.JSONDecodeError:
                                pass
                            if cid:
                                _reply_to_app(cid, reply)
                                log.info(f'小程序回复已转发 cid={cid}: {str(reply)[:60]}...')
                            else:
                                log.info(f'小程序回复(无cid): {str(reply)[:60]}...')
                        else:
                            log.error(f'小程序处理失败 (rc={r.returncode}): {r.stderr[:200]}')
                    except subprocess.TimeoutExpired:
                        log.error(f'小程序文本推送超时')
                        if cid:
                            _reply_to_app(cid, '处理超时，请稍后再试')
                    except Exception as e:
                        log.error(f'小程序文本推送异常: {e}')
                
                threading.Thread(target=_run_app_text, daemon=True).start()
            return

        correlation_id = data.get('correlationId', '?')
        task_type = data.get('taskType', '')
        params = data.get('params', {})
        sender = data.get('senderId', 'mini_app')

        log.info(f'收到小程序任务: {task_type} cid={correlation_id}')

        result = {'ok': False, 'error': '未知任务类型'}

        if task_type == 'ping':
            result = {'ok': True, 'pong': True}

        elif task_type in ('create_inquiry', 'get_quotes', 'negotiate'):
            # 小程序任务 — 调用对应工具
            script_map = {
                'create_inquiry': ('send_inquiry.py', ['inquiryNo', 'material', 'quantity', 'contacts']),
                'get_quotes': ('fetch_quotes.py', ['inquiryNo', 'contacts']),
                'negotiate': ('follow_up.py', ['contact', 'message']),
            }
            script, required_params = script_map[task_type]
            missing = [p for p in required_params if not params.get(p)]
            if missing:
                result = {'ok': False, 'error': f'缺少参数: {", ".join(missing)}'}
            else:
                cmd = [
                    sys.executable,
                    str(Path(__file__).parent / script),
                ]
                for p in required_params:
                    val = params[p]
                    if isinstance(val, list):
                        cmd.append(','.join(val))
                    else:
                        cmd.append(str(val))
                cmd.append('--json')
                out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=60)
                data = json.loads(out.strip())
                result = {'ok': not data.get('error'), 'data': data}
        else:
            result = {'ok': False, 'error': f'不支持的任务类型: {task_type}'}

        # 发结果回 小程序输出
        agent._publish_app_out(correlation_id, result)
        log.info(f'小程序结果已发送 cid={correlation_id}')

    except Exception as e:
        log.error(f'小程序处理异常: {e}')


class ProxyListenerClient:
    """MQTT 代理客户端 - 仅用于订阅实例的回调 topic
    不发送消息，只接收并路由到处理函数
    断连期间自动缓存消息，重连后补推
    """

    def __init__(self, target_topics=None, target_instances=None, use_tls=False):
        # 确保在当前线程导入 mqtt 模块
        import paho.mqtt.client as mqtt_client
        self._mqtt_client = mqtt_client
        self.use_tls = use_tls
        self.client = None
        self.connected = False
        self._lock = threading.Lock()
        self._callbacks = {}
        self._wechat_msg_handler = None
        self._app_msg_handler = None
        self.target_topics = target_topics or CALLBACK_TOPICS
        # 构建实例映射
        if target_instances:
            self._inst_prefix_map = {inst['id']: inst['topic_prefix'] for inst in target_instances}
            self._prefix_inst_map = {inst['topic_prefix']: inst['id'] for inst in target_instances}
        else:
            self._inst_prefix_map = _INSTANCE_PREFIX_MAP
            self._prefix_inst_map = _PREFIX_INSTANCE_MAP

    def set_wechat_msg_handler(self, handler):
        self._wechat_msg_handler = handler

    def set_app_msg_handler(self, handler):
        self._app_msg_handler = handler

    def _on_connect(self, c, u, f, rc, props=None):
        if rc == 0:
            # 订阅指定的回调 topic + 小程序 topic
            subs = [(t, 1) for t in self.target_topics] + [(APP_IN_TOPIC, 1)]
            c.subscribe(subs)
            self.connected = True

    def _on_message(self, c, u, m):
        try:
            raw = m.payload.decode('utf-8')
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {'text': raw}
            topic = m.topic

            # 判断消息类型并注入 instance_id
            instance_id = None
            for inst_id, topic_prefix in self._inst_prefix_map.items():
                if topic.startswith(f"{topic_prefix}/"):
                    instance_id = inst_id
                    data['_instance_id'] = inst_id
                    break

            # 微信回调
            if instance_id and self._wechat_msg_handler:
                self._wechat_msg_handler(topic, data)

            # 小程序入站
            elif topic == APP_IN_TOPIC and self._app_msg_handler:
                self._app_msg_handler(topic, data)

        except Exception as e:
            log.error(f'消息处理异常: {e}')

    def connect(self):
        # 如果已有 client，先彻底清理（防止 client_id 冲突）
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except:
                pass
            self.client = None
        self.connected = False

        # 每次连接使用唯一 client_id，避免与旧残留 client 冲突
        client_id = f'{AGENT_NAME}-proxy-{int(time.time() * 1000)}'
        self.client = self._mqtt_client.Client(client_id=client_id, callback_api_version=self._mqtt_client.CallbackAPIVersion.VERSION2)
        self.client.username_pw_set(_mqtt_cfg['username'], _mqtt_cfg['password'])

        if self.use_tls:
            ca_path = get_ca_cert_path()
            if ca_path and ca_path.exists():
                self.client.tls_set(ca_certs=str(ca_path))
            else:
                log.warning('CA证书不存在，TLS可能失败')
                self.client.tls_set()

        # keepalive 60s，适配 broker 端约 90s 的超时
        KEEPALIVE = 60

        def _on_disconnect(c, u, flags, reason_code, props=None):
            # paho-mqtt v2 回调签名: (client, userdata, flags, reason_code, properties)
            if self.connected:
                self.connected = False
                log.warning(f'MQTT 连接断开 (reason_code={reason_code})')
        self.client.on_disconnect = _on_disconnect
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        port = _mqtt_cfg.get('port_tls', 8883) if self.use_tls else _mqtt_cfg.get('port', 12403)
        try:
            self.client.connect(_mqtt_cfg['broker'], port, KEEPALIVE)
        except Exception as e:
            log.error(f'MQTT connect 异常: {e}')
            # connect 失败也要清理 client，防止 loop 线程泄漏
            self.client = None
            return False
        self.client.loop_start()
        time.sleep(1.5)
        return self.connected

    def disconnect(self):
        if self.client:
            try:
                self.client.loop_stop()
            except:
                pass
            try:
                self.client.disconnect()
            except:
                pass
        self.client = None
        self.connected = False


def run_listener(instance_id=None):
    """启动 MQTT 监听（永不退出，一直重试直到连接上）

    Args:
        instance_id: 可选，只监听指定实例。传 None 则监听所有已启用实例。
    """
    global agent
    _retry_intervals = [10, 30, 60, 120]
    _retry_idx = 0

    # 确定要监听的实例和 topic
    if instance_id:
        inst = get_instance_config(instance_id)
        if not inst:
            log.error(f'实例 {instance_id} 不存在或未启用')
            return
        target_instances = [inst]
        target_topics = [f"{inst['topic_prefix']}/+"]
    else:
        target_instances = _enabled_instances
        target_topics = CALLBACK_TOPICS

    # 用于 finally 清理，防止 client 泄漏
    proxy_client = None

    while True:
        try:
            # 创建新的 proxy client 之前，先彻底销毁旧实例（防止 client_id 冲突和线程泄漏）
            if proxy_client:
                try:
                    proxy_client.disconnect()
                except Exception as e:
                    log.warning(f'清理旧客户端异常: {e}')
                proxy_client = None

            proxy_client = ProxyListenerClient(
                target_topics=target_topics,
                target_instances=target_instances,
                use_tls=False
            )
            proxy_client.set_wechat_msg_handler(handle_wechat_message)
            proxy_client.set_app_msg_handler(handle_app_message)

            if not proxy_client.connected:
                if not proxy_client.connect():
                    delay = _retry_intervals[min(_retry_idx, len(_retry_intervals)-1)]
                    log.warning(f'MQTT连接失败，{delay}s后重试...')
                    # 失败后清理当前 client，下次循环会重新创建
                    try:
                        proxy_client.disconnect()
                    except:
                        pass
                    time.sleep(delay)
                    _retry_idx += 1
                    continue

            # 创建 AgentClass 实例（用于发送小程序回复等），使用唯一 client_id
            sender_id = f'{AGENT_NAME}-sender-{int(time.time())}'
            agent = AgentClass(instance_id=target_instances[0]['id'], client_id=sender_id, use_tls=False)
            if not agent.connected:
                agent.connect()

            _retry_idx = 0
            topics_str = ', '.join(target_topics)
            cur_outbound = target_instances[0].get('outbound_topic') or OUTBOUND_TOPIC
            log.info(f'已连接！监听中: 出站={cur_outbound}, 回调=[{topics_str}], 小程序={APP_IN_TOPIC}')

            while True:
                time.sleep(5)
                if not proxy_client.connected:
                    log.warning('MQTT连接断开，重新连接...')
                    # 必须先清理 proxy_client，否则旧的 loop 线程残留会导致 client_id 冲突
                    try:
                        proxy_client.disconnect()
                    except Exception as e:
                        log.warning(f'断开 proxy_client 异常: {e}')
                    try:
                        agent.disconnect()
                    except:
                        pass
                    agent = None
                    break

        except KeyboardInterrupt:
            log.info('收到停止信号')
            break
        except Exception as e:
            delay = _retry_intervals[min(_retry_idx, len(_retry_intervals)-1)]
            log.error(f'监听异常: {e}，{delay}s后重试...')
            # 异常后清理，避免带着脏状态重试
            if proxy_client:
                try:
                    proxy_client.disconnect()
                except:
                    pass
            time.sleep(delay)
            _retry_idx += 1

    # 最终清理
    if agent:
        try:
            agent.disconnect()
        except:
            pass
    if proxy_client:
        try:
            proxy_client.disconnect()
        except:
            pass


_PID_FILE = None  # 全局变量，用于存储 pid 文件路径

def ensure_single_instance():
    """单实例锁机制 — 基于 PID 文件的锁
    
    将当前进程的 PID 写入一个文件，后续启动的进程读到这个文件时
    检查对应 PID 是否仍在运行且是监听器进程。
    如果 PID 文件存在且对应进程还在运行，则本进程退出（不杀别人）。
    如果 PID 文件中的进程已死，则覆盖写入本进程 PID。
    
    彻底避免多进程互相残杀的问题。
    """
    global _PID_FILE
    current_pid = os.getpid()
    pid_dir = Path(__file__).parent.parent / 'logs'
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = pid_dir / 'mqtt_listener.pid'
    _PID_FILE = str(pid_file)
    listener_script = os.path.basename(__file__)
    
    # 检查 PID 文件
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid != current_pid:
                # 检查旧 PID 是否仍在运行且是监听器
                try:
                    old_proc = psutil.Process(old_pid)
                    cmdline = old_proc.cmdline()
                    cmd_str = ' '.join(cmdline)
                    if 'python' in (old_proc.name().lower() or '') and listener_script in cmd_str:
                        log.warning(f'PID锁文件中找到运行中的监听器 (PID={old_pid})，本进程退出')
                        return False
                    else:
                        # PID 文件中的进程不是监听器，可以覆盖
                        pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # 旧进程已死，可以覆盖
                    pass
        except (ValueError, OSError):
            pass
    
    # 写本进程 PID 到锁文件
    pid_file.write_text(str(current_pid), encoding='utf-8')
    log.info(f'PID锁已写入: {pid_file} (PID={current_pid})')
    return True


def remove_pid_file():
    """进程退出时清理PID文件"""
    global _PID_FILE
    if _PID_FILE and Path(_PID_FILE).exists():
        try:
            current = int(Path(_PID_FILE).read_text().strip())
            if current == os.getpid():
                Path(_PID_FILE).unlink(missing_ok=True)
        except (ValueError, OSError):
            pass


def cleanup_old_instance():
    """从 PID 文件读取旧进程ID，只杀那一个进程并清理 PID 文件"""
    pid_file = Path(__file__).parent.parent / 'logs' / 'mqtt_listener.pid'
    old_pid = None
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            log.info(f'从PID文件读到旧进程: {old_pid}')
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)

    if old_pid and old_pid != os.getpid():
        try:
            p = psutil.Process(old_pid)
            cmd_str = ' '.join(p.cmdline())
            if 'mqtt_listener' in cmd_str:
                log.warning(f'终止旧监听器进程 (PID={old_pid})')
                p.terminate()
                time.sleep(1)
                if p.is_running():
                    p.kill()
                    time.sleep(0.5)
                log.info(f'旧进程已终止')
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            log.info(f'旧进程 (PID={old_pid}) 已不存在，无需清理')


def main():
    cleanup_old_instance()
    # 解析命令行参数
    single_instance = None
    start_all = False
    for arg in sys.argv[1:]:
        if arg.startswith('--instance='):
            single_instance = arg.split('=', 1)[1]
        elif arg == '--all':
            start_all = True

    if start_all:
        # 为每个已启用实例启动独立进程
        for inst in _enabled_instances:
            inst_id = inst.get('id')
            log.info(f'启动子进程: mqtt-{inst_id}')
            subprocess.Popen(
                [sys.executable, __file__, f'--instance={inst_id}'],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0
            )
        log.info(f'已启动 {len(_enabled_instances)} 个监听器进程')
        return

    if single_instance:
        # 只监听指定实例
        ensure_single_instance_custom(single_instance)
        log.info(f'═══ MQTT 监听器启动 [{AGENT_NAME}:{single_instance}] ═══')
        log.info(f'  回调: wechat/{single_instance}/+')
        run_listener(instance_id=single_instance)
        return

    if not ensure_single_instance():
        sys.exit(1)

    log.info(f'═══ MQTT 监听器启动 [{AGENT_NAME}] ═══')
    # 每个 instance 的下行 topic（per-instance 隔离）；未配置时回退全局
    outbound_map = [
        f"{inst['id']} -> {inst.get('outbound_topic') or OUTBOUND_TOPIC}"
        for inst in _enabled_instances
    ]
    log.info(f'  出站(按实例): {outbound_map}')
    log.info(f'  回调: {len(CALLBACK_TOPICS)} 个实例: {CALLBACK_TOPICS}')
    log.info(f'  小程序: {APP_IN_TOPIC} -> {APP_OUT_TOPIC}')
    log.info(f'  已启用实例: {[inst["id"] for inst in _enabled_instances]}')
    run_listener()


if __name__ == '__main__':
    agent = None
    atexit.register(remove_pid_file)
    main()
