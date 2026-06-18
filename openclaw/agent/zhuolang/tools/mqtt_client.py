"""
微信助手 - 统一 MQTT 通信客户端
通过 Wbot 代理操作微信
支持 TLS 连接、双 topic 监听（微信回调 + 小程序入口）
支持多 Wbot 实例
"""
import paho.mqtt.client as mqtt
import json, uuid, time, sys, threading
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

# ========== 从 config/mqtt_instances.yml 加载配置 ==========
from _config import (
    get_mqtt_config, get_agent_config, get_instance_config,
    get_instance_outbound_topic, get_ca_cert_path, load_instances_config
)

_mqtt_cfg = get_mqtt_config()
_agent_cfg = get_agent_config()

# MQTT 连接参数
BROKER = _mqtt_cfg['broker']
PORT = _mqtt_cfg.get('port', 12403)
PORT_TLS = _mqtt_cfg.get('port_tls', 8883)
USERNAME = _mqtt_cfg['username']
PASSWORD = _mqtt_cfg['password']

# Agent 配置
AGENT_NAME = _agent_cfg.get('name', 'default')
OUTBOUND_TOPIC = _agent_cfg.get('outbound_topic', f'agent/{AGENT_NAME}')

# 默认实例（向后兼容）
DEFAULT_INSTANCE_ID = 'wx_001'


class ProcurementAgent:
    """微信助手 MQTT 通信代理（支持多实例）"""

    def __init__(self, instance_id='wx_001', client_id=None, timeout=20, use_tls=False):
        """
        Args:
            instance_id: Wbot 实例 ID，如 'wx_001'、'wx_002'
            client_id: MQTT client ID，默认自动生成
            timeout: 等待回调超时（秒）
            use_tls: 是否使用 TLS 连接
        """
        self.instance_id = instance_id
        self.client_id = client_id or f'{AGENT_NAME}-{instance_id}-{uuid.uuid4().hex[:4]}'
        self.timeout = timeout
        self.client = None
        self.callbacks = {}
        self.connected = False
        self._lock = threading.Lock()
        self.use_tls = use_tls
        self._app_msg_handler = None
        self._wechat_msg_handler = None

        # 实例配置
        self.instance_config = get_instance_config(instance_id)
        if not self.instance_config:
            raise ValueError(f'实例 ID {instance_id} 不存在或未启用')

        # Topic 配置
        self.callback_topic = f"{self.instance_config['topic_prefix']}/+"
        self.app_in_topic = f"app/{AGENT_NAME}/in"
        self.app_out_topic = f"app/{AGENT_NAME}/out"
        # 该实例专属的下行任务 topic（per-instance 隔离）：
        # 优先取 instance 配置，未配置则回退到全局 agent.outbound_topic
        self.outbound_topic = get_instance_outbound_topic(instance_id)

    def set_app_msg_handler(self, handler):
        """设置小程序消息处理函数 handler(topic, payload_dict)"""
        self._app_msg_handler = handler

    def set_wechat_msg_handler(self, handler):
        """设置微信消息处理函数 handler(topic, payload_dict)
        监听器模式用：把微信群聊/联系人消息推送给 OpenClaw agent
        """
        self._wechat_msg_handler = handler

    def _on_connect(self, c, u, f, rc, props=None):
        if rc == 0:
            # 订阅实例回调 topic + 小程序 topic
            subs = [(self.callback_topic, 1), (self.app_in_topic, 1)]
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

            # 微信回调 topic: wechat/wx_001/xxx
            if topic.startswith(f"{self.instance_config['topic_prefix']}/"):
                cid = data.get('correlationId', '?')
                with self._lock:
                    self.callbacks[cid] = data
                # 如果有 wechat handler（后台监听器模式），也推送
                if self._wechat_msg_handler:
                    # 在 data 中注入 instance_id，方便路由
                    data_with_instance = {**data, '_instance_id': self.instance_id}
                    self._wechat_msg_handler(topic, data_with_instance)

            # 小程序入站 topic
            elif topic == self.app_in_topic and self._app_msg_handler:
                self._app_msg_handler(topic, data)

        except Exception as e:
            pass

    def connect(self):
        self.client = mqtt.Client(client_id=self.client_id, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.client.username_pw_set(USERNAME, PASSWORD)

        if self.use_tls:
            ca_path = get_ca_cert_path()
            if ca_path and ca_path.exists():
                self.client.tls_set(ca_certs=str(ca_path))
            else:
                print(f'[警告] CA证书不存在，TLS可能失败')
                self.client.tls_set()

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        port = PORT_TLS if self.use_tls else PORT
        self.client.connect(BROKER, port, 60)
        self.client.loop_start()
        time.sleep(1.5)
        return self.connected

    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def _publish_task(self, event, fields, correlation_id=None, use_wrapper=False):
        """发送统一格式消息到 Wbot。

        发布到本实例专属的 outbound_topic（per-instance 物理隔离），
        仅订阅该 topic 的 Wbot 会收到任务，避免多实例重复执行。

        Args:
            event: 事件类型 (wechat_message, add_friend, ping, ...)
            fields: 业务字段字典(全部平铺在顶层)
            correlation_id: 可选，不传则自动生成
            use_wrapper: 是否使用 OpenClaw wrapper 格式（兼容旧 wbot）
        """
        cid = correlation_id or uuid.uuid4().hex[:8]
        payload_data = {
            'event': event,
            'correlationId': cid,
            'agentId': self.instance_id,  # 默认用实例 ID
            'ts': int(time.time() * 1000),
            **fields,
        }
        # 允许 fields 覆盖 agentId（用于传递角色名）
        if 'agentId' in fields:
            payload_data['agentId'] = fields['agentId']

        if use_wrapper:
            inner = json.dumps(payload_data, ensure_ascii=False)
            payload = json.dumps({
                'senderId': 'openclaw',
                'text': inner,
                'kind': 'final',
                'ts': int(time.time() * 1000),
                'correlationId': cid,
            }, ensure_ascii=False)
        else:
            payload = json.dumps(payload_data, ensure_ascii=False)

        self.client.publish(self.outbound_topic, payload, qos=1)
        return cid

    def _publish_app_out(self, correlation_id, data):
        """发送结果到小程序（app out topic）"""
        payload = json.dumps({
            'senderId': 'openclaw',
            'correlationId': correlation_id,
            'ts': int(time.time() * 1000),
            'data': data,
        }, ensure_ascii=False)
        self.client.publish(self.app_out_topic, payload, qos=1)

    def _wait_callback(self, cid, timeout=None):
        """等待指定 cid 的回调"""
        timeout = timeout or self.timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if cid in self.callbacks:
                    return self.callbacks.pop(cid)
            time.sleep(0.3)
        return None

    def ping(self, timeout=10):
        """测试 Wbot 连通性"""
        cid = self._publish_task('ping', {})
        return self._wait_callback(cid, timeout)

    def send_text(self, target, message, timeout=20, file_url=None, target_id='', target_name='', correlation_id=None, context=None):
        """发送文本消息到微信联系人（wechat_message 反向）

        Args:
            target: 目标联系人（兼容旧接口，同时作为 targetId 和 targetName）
            message: 消息内容
            timeout: 等待回调超时（秒）
            file_url: 远程文件 URL
            target_id: 目标微信号（新格式优先）
            target_name: 目标备注名（新格式优先）
            correlation_id: 关联 ID（用于回复原始消息时保持一致）
            context: 消息上下文字典，包含 targetId/targetName/role/selfWxName/selfWxId 等
        """
        # 优先从 context 中取字段
        ctx = context or {}
        # targetId 从 context 取（微信ID），没有则用 target 参数（兼容旧调用者传微信号）
        target_id_val = ctx.get('targetId') or target_id or target
        target_name_val = ctx.get('targetName') or target_name or target

        fields = {
            'targetName': target_name_val,
            'text': message,
        }

        # Wbot 要求：targetId 必须传微信号，不是昵称。
        # 当 targetId 和 targetName 相同时（说明不是真实微信号），不传 targetId，
        # 让 Wbot 按 targetName 搜索联系人。
        if target_id_val != target_name_val:
            fields['targetId'] = target_id_val

        # 仅发送文件时（无文字），Wbot 要求 text 为空字符串且 type='fileUrl'
        # 有文字内容时 type='text'，Wbot 按文本+附件处理
        if file_url and not message.strip():
            fields['type'] = 'fileUrl'
            fields['text'] = ''
        else:
            fields['type'] = 'text'

        # 保持信封字段一致性
        if ctx.get('agentId'):
            fields['agentId'] = ctx['agentId']
        if ctx.get('role'):
            fields['role'] = ctx['role']
        if ctx.get('selfWxName'):
            fields['selfWxName'] = ctx['selfWxName']
        if ctx.get('selfWxId'):
            fields['selfWxId'] = ctx['selfWxId']
        # operate：优先用显式参数，其次 context
        op = ctx.get('operate', '') or ''
        if op:
            fields['operate'] = op
        if file_url:
            fields['fileUrl'] = file_url
        cid = self._publish_task('wechat_message', fields, correlation_id=correlation_id or ctx.get('correlationId'))
        return cid, self._wait_callback(cid, timeout)

    def get_chat_history(self, contact, limit=50, timeout=20, target_id='', target_name=''):
        """拉取聊天记录"""
        fields = {
            'targetId': target_id or contact,
            'targetName': target_name or contact,
            'limit': limit,
        }
        cid = self._publish_task('get_chat_history', fields)
        return cid, self._wait_callback(cid, timeout)

    def post_moments(self, text='', media_files=None, privacy='public', tags=None, timeout=30):
        """发朋友圈（支持图片和视频）

        :param text: 朋友圈文字内容（文字和媒体文件至少有其一）
        :param media_files: 媒体文件 URL 列表（图片/视频均可，最多 9 个）
        :param privacy: 隐私设置 'public'(默认) / 'whitelist' / 'blacklist'
        :param tags: 隐私标签列表（whitelist/blacklist 模式下生效）
        :param timeout: 等待回调超时（秒），发朋友圈操作较慢建议 30s+
        :return: (correlationId, callback_data)
        """
        fields = {
            'privacy': privacy,
        }
        if text:
            fields['text'] = text
        if media_files:
            fields['media_files'] = media_files
        if tags:
            fields['tags'] = tags

        cid = self._publish_task('post_moments', fields)
        return cid, self._wait_callback(cid, timeout)

    def get_friend_details(self, n=None, name_prefix=None, timeout=30):
        """获取微信好友详情列表

        注意：该方法耗时较长（约 0.3~0.5s/人），好友多时建议用 n 限制数量。

        :param n: 获取前 n 个好友（None=全部）
        :param name_prefix: 昵称前缀筛选（如 "张" 只返回张姓好友）
        :param timeout: 等待回调超时（秒），好友多时建议 30s+
        :return: (correlationId, callback_data) callback_data 包含 friends 列表
        """
        fields = {}
        if n is not None:
            fields['n'] = int(n)
        if name_prefix:
            fields['name_prefix'] = name_prefix
        cid = self._publish_task('get_friend_details', fields)
        return cid, self._wait_callback(cid, timeout)

    def parse_messages(self, callback_data):
        """从回调数据中提取消息列表"""
        if not callback_data:
            return [], None
        result = callback_data.get('result', {})
        if isinstance(result, dict):
            err = result.get('error')
            msgs = result.get('messages', [])
            return msgs, err
        return [], str(result)


# ========== 向后兼容：默认实例的快捷函数 ==========
# 如果不传 instance_id，使用默认实例 wx_001

def _get_default_agent(**kwargs):
    """获取默认实例的 agent（用于向后兼容）"""
    if 'instance_id' not in kwargs:
        kwargs['instance_id'] = DEFAULT_INSTANCE_ID
    return ProcurementAgent(**kwargs)


# 导出默认构造函数，保持向后兼容
def create_agent(instance_id=None, client_id=None, timeout=20, use_tls=False):
    """创建 agent 实例（向后兼容）"""
    inst_id = instance_id or DEFAULT_INSTANCE_ID
    return ProcurementAgent(instance_id=inst_id, client_id=client_id, timeout=timeout, use_tls=use_tls)