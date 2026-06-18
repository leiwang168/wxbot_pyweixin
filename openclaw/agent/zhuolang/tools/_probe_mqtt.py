"""探测MQTT Broker信息"""
import socket, struct, time

def probe_mqtt(host, port, timeout=5):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))

        # Build MQTT CONNECT packet
        payload = bytearray()
        payload.extend(b'\x00\x04MQTT')
        payload.append(4)  # protocol level
        payload.append(0x02)  # clean session
        payload.extend(struct.pack('>H', 60))  # keepalive
        client_id = b'probe'
        payload.extend(struct.pack('>H', len(client_id)))
        payload.extend(client_id)

        rem_len = len(payload)
        packet = bytearray([0x10])  # CONNECT
        if rem_len < 128:
            packet.append(rem_len)
        else:
            packet.append(rem_len % 128 + 128)
            packet.append(rem_len // 128)
        packet.extend(payload)

        s.sendall(bytes(packet))
        time.sleep(1.5)
        resp = s.recv(4096)
        s.close()

        if resp and len(resp) >= 4:
            reason = resp[3] if len(resp) > 3 else resp[2]
            reasons = {0: '成功', 1: '协议拒绝', 2: 'ID拒绝', 3: '服务器不可用', 4: '认证错误', 5: '未授权'}
            return f'CONNACK code={reason} ({reasons.get(reason, "未知")})'
        return '无响应'
    except Exception as e:
        return f'连接失败: {e}'

if __name__ == '__main__':
    print(f'12403: {probe_mqtt("192.168.10.101", 12403)}')
    print(f'1883:  {probe_mqtt("192.168.10.101", 1883)}')
    print(f'8883:  {probe_mqtt("192.168.10.101", 8883)}')
