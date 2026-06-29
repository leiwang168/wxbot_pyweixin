"""
配置加载器 — 从 config/mqtt_instances.yml 和 config/minio.yaml 读取配置
支持多 Wbot 实例配置
"""
import os
import yaml
from pathlib import Path


def _find_config_dir():
    """找到项目根目录下的 config/ 目录"""
    # 从当前文件位置向上找 tools/... -> agent_dir
    path = Path(__file__).resolve().parent  # tools/
    # 再往上一级就是 agent 工作区根目录
    return path.parent / 'config'


CONFIG_DIR = _find_config_dir()


# 缓存配置
_instances_config_cache = None


def load_instances_config():
    """加载多实例 MQTT 配置

    Returns:
        dict: {
            broker, port, username, password, ...,
            agent_name, outbound_topic, app_in_topic, app_out_topic,
            instances: [{id, topic_prefix, enabled, contacts}, ...]
        }
    """
    global _instances_config_cache
    if _instances_config_cache is not None:
        return _instances_config_cache

    # 优先读取 mqtt.yaml（合并版），如果不存在则回退到 mqtt_instances.yml
    path = CONFIG_DIR / 'mqtt.yaml'
    if not path.exists():
        path = CONFIG_DIR / 'mqtt_instances.yml'

    if not path.exists():
        raise FileNotFoundError(f'MQTT 实例配置文件不存在: {path}')
    with open(path, encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    # 加载每个实例的独立配置
    loaded_instances = []
    for inst_ref in cfg.get('instances', []):
        if isinstance(inst_ref, str):
            # 引用外部文件
            inst_path = Path(inst_ref)
            if not inst_path.is_absolute():
                inst_path = CONFIG_DIR / inst_path
        elif isinstance(inst_ref, dict):
            # 内联配置（新格式）
            loaded_instances.append(inst_ref)
            continue
        else:
            continue

        if inst_path.exists():
            with open(inst_path, encoding='utf-8') as f:
                inst_cfg = yaml.safe_load(f)
            loaded_instances.append(inst_cfg)

    cfg['instances'] = loaded_instances
    _instances_config_cache = cfg
    return _instances_config_cache


def get_enabled_instances():
    """获取所有已启用的 Wbot 实例列表

    Returns:
        list: [{id, topic_prefix, enabled}, ...] 只返回 enabled=true 的
    """
    cfg = load_instances_config()
    instances = cfg.get('instances', [])
    return [inst for inst in instances if inst.get('enabled', True)]


def get_instance_config(instance_id):
    """获取指定实例的配置

    Args:
        instance_id: 实例 ID，如 'wx_001'

    Returns:
        dict: {id, topic_prefix, enabled} 或 None
    """
    instances = get_enabled_instances()
    for inst in instances:
        if inst.get('id') == instance_id:
            return inst
    return None


def get_instance_outbound_topic(instance_id):
    """获取指定实例的下行任务 topic（per-instance 隔离的关键）。

    优先取 instance 自身的 outbound_topic；未配置时回退到全局
    agent.outbound_topic，保证旧配置行为不变。

    Returns:
        str: 该实例的下行 topic；实例不存在时回退到全局默认。
    """
    inst = get_instance_config(instance_id)
    if inst and inst.get('outbound_topic'):
        return inst['outbound_topic']
    return get_agent_config().get('outbound_topic', f'agent/default')


def get_mqtt_config():
    """获取 MQTT 连接配置（向后兼容）

    Returns:
        dict: {broker, port, username, password, connect_timeout, ping_timeout, message_timeout, reconnect_min, reconnect_max}
    """
    cfg = load_instances_config()
    return {
        'broker': cfg.get('broker', '192.168.10.101'),
        'port': cfg.get('port', 1883),
        'username': cfg.get('username', ''),
        'password': cfg.get('password', ''),
        'connect_timeout': cfg.get('connect_timeout', 20),
        'ping_timeout': cfg.get('ping_timeout', 10),
        'message_timeout': cfg.get('message_timeout', 20),
        'reconnect_min': cfg.get('reconnect_min', 3),
        'reconnect_max': cfg.get('reconnect_max', 60),
    }


def get_agent_config():
    """获取 OpenClaw Agent 配置

    Returns:
        dict: {name, outbound_topic, app_in_topic, app_out_topic}
    """
    cfg = load_instances_config()
    # 支持新格式（agent.name）和旧格式（agent_name）
    agent_cfg = cfg.get('agent', {})
    agent_name = agent_cfg.get('name') or cfg.get('agent_name', 'default')
    return {
        'name': agent_name,
        'outbound_topic': agent_cfg.get('outbound_topic') or cfg.get('outbound_topic', f'agent/{agent_name}'),
        'app_in_topic': agent_cfg.get('in_topic') or cfg.get('app_in_topic', f'app/{agent_name}/in'),
        'app_out_topic': agent_cfg.get('out_topic') or cfg.get('app_out_topic', f'app/{agent_name}/out'),
    }


def load_mqtt_config():
    """加载 MQTT 配置（向后兼容）

    Returns:
        dict: {broker, port, username, password, connect_timeout, ping_timeout, message_timeout, reconnect_min, reconnect_max}
    """
    return get_mqtt_config()


def load_minio_config():
    """加载 MinIO 配置

    Returns:
        dict: {endpoint, access_key, secret_key, bucket_name, region, secure}
    """
    path = CONFIG_DIR / 'minio.yaml'
    if not path.exists():
        raise FileNotFoundError(f'MinIO 配置文件不存在: {path}')
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def get_ca_cert_path():
    """获取 CA 证书路径（绝对路径）

    Returns:
        Path: CA 证书文件路径，如果不存在返回 None
    """
    mqtt_cfg = get_mqtt_config()
    cert_file = mqtt_cfg.get('ca_cert', 'mqtt_ca.pem')
    cert_path = CONFIG_DIR / cert_file
    return cert_path if cert_path.exists() else None
