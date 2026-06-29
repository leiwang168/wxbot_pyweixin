#!/usr/bin/env python
"""多实例架构模拟测试 — 不依赖微信端，纯本地验证"""
import sys, os, json

sys.path.insert(0, os.path.dirname(__file__))

passed = 0
failed = 0

def test(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f'  [PASS] {name}')
    except AssertionError as e:
        failed += 1
        print(f'  [FAIL] {name}: {e}')
    except Exception as e:
        failed += 1
        print(f'  [FAIL] {name}: {e}')


# ====================
# 测试 1: 配置文件加载
# ====================
print('\n=== 测试 1: 配置文件加载 ===')

def t1_load_config():
    from _config import load_instances_config, get_enabled_instances
    cfg = load_instances_config()
    assert cfg is not None, '配置为空'
    instances = get_enabled_instances()
    assert len(instances) == 1, f'期望1个实例，实际{len(instances)}'
    assert instances[0]['id'] == 'wx_001'
    assert instances[0]['topic_prefix'] == 'wechat/wx_001'
    assert '李铎TS' in instances[0]['contacts']
    print(f'     实例: {instances[0]["id"]}, 微信联系人: {instances[0]["contacts"]}')

test('配置加载', t1_load_config)


# ====================
# 测试 2: 微信联系人-实例映射
# ====================
print('\n=== 测试 2: 微信联系人-实例映射 ===')

def t2_contact_mapping():
    from _config import get_instance_for_contact
    assert get_instance_for_contact('李铎TS') == 'wx_001'
    assert get_instance_for_contact('李晓博TS') == 'wx_001'
    assert get_instance_for_contact('俱文东') == 'wx_001'
    assert get_instance_for_contact('不存在的人') is None
    assert get_instance_for_contact('') is None

test('微信联系人映射', t2_contact_mapping)


# ====================
# 测试 3: 公共参数加载
# ====================
print('\n=== 测试 3: 公共参数加载 ===')

def t3_common_config():
    from _config import get_mqtt_config, get_agent_config

    mqtt = get_mqtt_config()
    assert mqtt['broker'] == '192.168.10.101'
    assert mqtt['port'] == 1883
    assert mqtt['username'] == 'mqtt_client'
    print(f'     MQTT: {mqtt["broker"]}:{mqtt["port"]}')

    agent = get_agent_config()
    assert agent['name'] == 'test_agent'
    assert agent['outbound_topic'] == 'agent/test_agent'
    print(f'     Agent: {agent["name"]} -> {agent["outbound_topic"]}')

test('公共参数', t3_common_config)


# ====================
# 测试 4: ProcurementAgent 实例创建
# ====================
print('\n=== 测试 4: ProcurementAgent 实例创建 ===')

def t4_agent_creation():
    from mqtt_client import ProcurementAgent

    # wx_001 应该创建成功
    a1 = ProcurementAgent(instance_id='wx_001')
    assert a1.callback_topic == 'wechat/wx_001/+'
    assert a1.app_in_topic == 'app/test_agent/in'
    assert a1.instance_id == 'wx_001'
    print(f'     wx_001 OK: callback={a1.callback_topic}')

    # wx_002 应该抛错（enabled: false）
    try:
        a2 = ProcurementAgent(instance_id='wx_002')
        assert False, 'wx_002不应该创建成功'
    except ValueError as e:
        assert '不存在或未启用' in str(e)
        print(f'     wx_002 正确拒绝: {e}')

    # wx_999 不存在
    try:
        a3 = ProcurementAgent(instance_id='wx_999')
        assert False, 'wx_999不应该创建成功'
    except ValueError:
        print(f'     wx_999 正确拒绝')

test('Agent创建', t4_agent_creation)


# ====================
# 测试 5: Session Key 格式
# ====================
print('\n=== 测试 5: Session Key 格式 ===')

def t5_session_key():
    def build_key(instance_id, sender):
        safe = (sender or 'default').strip()
        return f'agent:test_agent:wechat:{instance_id}:{safe}'

    assert build_key('wx_001', '李铎TS') == 'agent:test_agent:wechat:wx_001:李铎TS'
    assert build_key('wx_001', '') == 'agent:test_agent:wechat:wx_001:default'
    assert build_key('wx_002', '微信联系人A') == 'agent:test_agent:wechat:wx_002:微信联系人A'

    # 不同实例的同一微信联系人应有不同 session
    k1 = build_key('wx_001', '李铎TS')
    k2 = build_key('wx_002', '李铎TS')
    assert k1 != k2, '不同实例同名微信联系人应隔离'
    print(f'     wx_001: {k1}')
    print(f'     wx_002: {k2}')
    print(f'     隔离验证: k1 != k2 -> {k1 != k2}')

test('Session Key', t5_session_key)


# ====================
# 测试 6: Topic 解析
# ====================
print('\n=== 测试 6: Topic 解析实例 ID ===')

def t6_topic_parsing():
    from _config import get_enabled_instances

    def parse(topic):
        for inst in get_enabled_instances():
            prefix = inst['topic_prefix']
            if topic.startswith(f'{prefix}/'):
                return inst['id']
        return None

    assert parse('wechat/wx_001/李铎TS') == 'wx_001'
    assert parse('wechat/wx_001/某某') == 'wx_001'
    assert parse('wechat/wx_999/未知') is None
    assert parse('app/test_agent/in') is None
    print(f'     wechat/wx_001/李铎TS -> wx_001')
    print(f'     wechat/wx_999/未知 -> None')
    print(f'     app/test_agent/in -> None')

test('Topic解析', t6_topic_parsing)


# ====================
# 测试 7: 工具自动路由 (follow_up.py)
# ====================
print('\n=== 测试 7: 工具自动路由 ===')

def t7_tool_routing():
    from _config import get_instance_for_contact
    from contact_instance import group_contacts_by_instance

    # 同一实例的微信联系人
    g1 = group_contacts_by_instance(['李铎TS', '李晓博TS'])
    assert len(g1) == 1
    assert 'wx_001' in g1
    assert sorted(g1['wx_001']) == sorted(['李铎TS', '李晓博TS'])
    print(f'     同实例分组: {g1}')

    # 单个微信联系人
    inst = get_instance_for_contact('俱文东')
    assert inst == 'wx_001'
    print(f'     俱文东 -> {inst}')

test('工具路由', t7_tool_routing)


# ====================
# 测试 8: 多实例独立配置互不干扰
# ====================
print('\n=== 测试 8: 多实例配置互不干扰 ===')

def t8_instance_isolation():
    from _config import get_instance_config

    wx001 = get_instance_config('wx_001')
    assert wx001 is not None
    assert wx001['contacts'] == ['李铎TS', '李晓博TS', '俱文东']
    assert wx001['enabled'] is True

    # wx_002 存在但未启用
    from _config import load_instances_config
    cfg = load_instances_config()
    all_instances = cfg.get('instances', [])
    wx002_cfg = next((i for i in all_instances if i.get('id') == 'wx_002'), None)
    assert wx002_cfg is not None, 'wx_002 配置文件应存在'
    assert wx002_cfg['enabled'] is False, 'wx_002 应为 disabled'
    print(f'     wx_001: enabled=True, contacts=3')
    print(f'     wx_002: enabled=False (待激活)')

test('实例隔离', t8_instance_isolation)


# ====================
# 测试 9: ProxyListenerClient 多实例订阅
# ====================
print('\n=== 测试 9: ProxyListenerClient 订阅逻辑 ===')

def t9_proxy_subscription():
    from _config import get_enabled_instances

    # 单实例模式
    inst = get_enabled_instances()[0]
    topics = [f"{inst['topic_prefix']}/+"]
    assert topics == ['wechat/wx_001/+']
    print(f'     单实例订阅: {topics}')

    # 多实例模式（模拟存在 wx_001 和 wx_002）
    mock_instances = [
        {'id': 'wx_001', 'topic_prefix': 'wechat/wx_001'},
        {'id': 'wx_002', 'topic_prefix': 'wechat/wx_002'},
    ]
    all_topics = [f"{i['topic_prefix']}/+" for i in mock_instances]
    assert all_topics == ['wechat/wx_001/+', 'wechat/wx_002/+']
    print(f'     多实例订阅: {all_topics}')

    # 实例映射
    inst_map = {i['id']: i['topic_prefix'] for i in mock_instances}
    assert inst_map == {'wx_001': 'wechat/wx_001', 'wx_002': 'wechat/wx_002'}
    print(f'     实例映射: {inst_map}')

test('Proxy订阅', t9_proxy_subscription)


# ====================
# 测试 10: 内容安全检查
# ====================
print('\n=== 测试 10: 内容安全检查 ===')

def t10_content_check():
    from _content_check import validate

    # 正常消息
    assert validate('李铎TS', '交期多少天？') is True
    print('     正常消息: 通过')

    # 泄露报价
    assert validate('李铎TS', '李晓博TS报了200块，你的呢？') is False
    print('     泄露报价: 拦截')

    # 暴露AI
    assert validate('李铎TS', '我是AI助手') is False
    print('     暴露AI: 拦截')

    # 提及内部流程
    assert validate('李铎TS', '等我汇报给老板再回复你') is False
    print('     提及内部: 拦截')

test('内容安全', t10_content_check)


# ========== 汇总 ==========
print(f'\n{"="*50}')
print(f'测试完成: {passed} 通过, {failed} 失败, {passed+failed} 总计')
if failed == 0:
    print('所有测试通过!')
else:
    print(f'有 {failed} 个测试失败，请检查')
    sys.exit(1)
