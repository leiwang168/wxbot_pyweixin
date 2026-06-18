"""
小程序 MQTT 消息监听器
作为独立进程运行，监听 app/{agent_name}/in 并处理微信任务
"""
import sys, os, json, time, subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from mqtt_client import ProcurementAgent


def handle_app_message(topic, data):
    """处理小程序发来的消息"""
    try:
        correlation_id = data.get('correlationId', '?')
        task_type = data.get('taskType', '')
        params = data.get('params', {})
        sender = data.get('senderId', 'mini_app')

        print(f'[APP] 收到任务: {task_type} cid={correlation_id} sender={sender}')

        result = {'ok': False, 'error': '未知任务类型'}

        if task_type == 'ping':
            result = {'ok': True, 'pong': True}

        elif task_type == 'create_inquiry':
            result = handle_create_inquiry(params)

        elif task_type == 'get_quotes':
            result = handle_get_quotes(params)

        elif task_type == 'negotiate':
            result = handle_negotiate(params)

        else:
            result = {'ok': False, 'error': f'不支持的任务类型: {task_type}'}

        # 发结果回 app/{agent_name}/out
        agent._publish_app_out(correlation_id, result)
        print(f'[APP] 结果已发送 cid={correlation_id} ok={result["ok"]}')

    except Exception as e:
        print(f'[APP] 处理异常: {e}')


def handle_create_inquiry(params):
    """创建询价单"""
    inquiry_no = params.get('inquiryNo', '')
    material = params.get('material', '')
    quantity = params.get('quantity', '')
    contacts = params.get('contacts', [])

    if not all([inquiry_no, material, quantity, contacts]):
        return {'ok': False, 'error': '参数不完整: 需要 inquiryNo, material, quantity, contacts'}

    # 调用发送工具
    cmd = [
        sys.executable,
        str(Path(__file__).parent / 'send_inquiry.py'),
        inquiry_no, material, str(quantity),
        ','.join(contacts),
        '--json'
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=60)
        data = json.loads(out.strip())
        if data.get('error'):
            return {'ok': False, 'error': data['error']}
        return {'ok': True, 'results': data['results']}
    except subprocess.CalledProcessError as e:
        return {'ok': False, 'error': f'发送失败: {e.output}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def handle_get_quotes(params):
    """拉取报价"""
    inquiry_no = params.get('inquiryNo', '')
    contacts = params.get('contacts', [])
    if not inquiry_no or not contacts:
        return {'ok': False, 'error': '需要 inquiryNo 和 contacts'}

    cmd = [
        sys.executable,
        str(Path(__file__).parent / 'fetch_quotes.py'),
        inquiry_no,
        ','.join(contacts),
        '--json'
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=60)
        data = json.loads(out.strip())
        return {'ok': True, 'quotes': data}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def handle_negotiate(params):
    """议价"""
    contact = params.get('contact', '')
    message = params.get('message', '')
    if not contact or not message:
        return {'ok': False, 'error': '需要 contact 和 message'}

    cmd = [
        sys.executable,
        str(Path(__file__).parent / 'follow_up.py'),
        contact, message,
        '--json'
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=60)
        data = json.loads(out.strip())
        return {'ok': data.get('ok', False), 'cid': data.get('cid', ''), 'error': data.get('error', '')}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def main():
    print('[APP-LISTENER] 启动小程序 MQTT 监听器...')

    global agent
    agent = ProcurementAgent(client_id='default-app', use_tls=False)
    agent.set_app_msg_handler(handle_app_message)

    if not agent.connect():
        print('[APP-LISTENER] MQTT连接失败')
        sys.exit(1)

    print(f'[APP-LISTENER] 监听中: {agent._app_msg_handler.__name__ if agent._app_msg_handler else "none"}')

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print('\n[APP-LISTENER] 停止')
    finally:
        agent.disconnect()


if __name__ == '__main__':
    agent = None
    main()
