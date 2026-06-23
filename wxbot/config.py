# -*- coding: utf-8 -*-
"""BotConfig — 配置加载/保存/归一化/热重载。

字段精简自 SiverWXbot_plus 的 config.json，去掉所有 AI 平台相关字段（本期不做 AI）。
默认值参考 SiverWXbot `wxbot_core.py:273 create_new_config_file` 与 `464 update_global_config`。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from .logger import log

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.json")


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    # 通用 / 监听
    "admin": "文件传输助手",
    "AllListen_switch": False,        # False=白名单模式, True=全局监听模式
    "AllListen_filter_mute": True,    # 全局监听下过滤免打扰会话
    "black_list": [],                 # 全局监听黑名单（私聊+群聊均生效）
    "chat_listen_only": False,        # 私聊只监听不 AI 回复
    "listen_list": [],                # 白名单用户（AllListen_switch=False 时生效）
    "group": [],                      # 监听群聊（白名单模式，AllListen_switch=False 时生效）
    "group_switch": False,            # 群聊监听总开关
    "group_listen_only": False,
    "group_reply_at_msg": True,       # 群回复是否 @ 发言人
    "group_reply_quote": False,       # 群回复是否引用原消息（阶段一默认关闭，需运行时验证）
    "group_welcome": False,
    "group_welcome_random": 1.0,
    "group_welcome_msg": "欢迎新朋友！",
    "monitor_check_interval": 10,        # 消息监听轮询间隔（秒）
    "monitor_run_timeout": 30,           # 单轮 run_once 超时（秒），防 UI 操作死锁拖垮主循环
    "contacts_refresh_timeout": 300,     # 联系人全量缓存刷新超时（秒），config 可配
    # 关键词
    "chat_keyword_switch": False,
    "group_keyword_switch": False,
    "group_keyword_at_only": False,
    "keyword_dict": {},               # {关键词: 回复内容}
    # 自定义转发（阶段一骨架）
    "custom_forward_switch": False,
    "custom_forward_list": [],
    # 新好友
    "new_friend_switch": False,
    "new_friend_reply_switch": False,
    "new_friend_msg": [],             # 文字或图片绝对路径
    "new_friend_check_min": 60,       # 秒
    "new_friend_check_max": 300,      # 秒
    "new_friend_remark_use_nickname": True,
    "new_friend_remark_prefix": "",
    "new_friend_remark_prefix_timestamp": False,
    "new_friend_remark_suffix": "_机器人备注",
    "new_friend_remark_suffix_timestamp": False,
    "new_friend_tags": [],            # 已知 gap：pyweixin 无打标签接口，记日志跳过
    # 定时消息
    "scheduled_msg_switch": False,
    "scheduled_msg_list": [],
    "random_msg_switch": False,
    "random_msg_list": [],
    # 朋友圈
    "scheduled_moments_switch": False,
    "scheduled_moments_list": [],
    "moments_like_switch": False,
    "moments_like_min": 60,           # 分钟
    "moments_like_max": 120,
    "random_moments_switch": False,
    "random_moments_list": [],
    # 朋友圈导出(每日定时获取保存)
    "moments_export_switch": True,     # 每日朋友圈导出开关
    "moments_export": {
        "target_folder": r"E:\Desktop\朋友圈内容导出",  # 导出根目录(其下按 好友/日期/序号 结构)
        "number": None,                # None=取当天全部(只按 recent='Today' 过滤);填正整数则作条数上限
    },
    # 每日启停
    "everyday_start_stop_bot_switch": False,
    "everyday_start_bot_time": "08:00",
    "everyday_stop_bot_time": "23:00",
    # 记忆（阶段二落地，本期占位）
    "memory_switch": True,
    "memory_max_count": 3000,
    "memory_context_count": 1000,
    # 回复延时
    "reply_delay_switch": True,
    "reply_delay_min": 1,             # 秒
    "reply_delay_max": 5,
    # ---- 数字员工（业务核心）----
    "digital_employee_switch": True,  # 数字员工总开关（关闭则不调用 AI，仅关键词/转发）
    "api_configs": [                  # OpenAI 兼容接口（DusAPI/DeepSeek/通义/OpenAI 等通用）
        {"sdk": "openai", "key": "", "url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    ],
    "api_index": 0,                   # 当前使用的接口索引
    "default_persona": "默认客服",     # 全局默认岗位人设名（对应 config/persona/<名>.md）
    "chat_persona_map": {},           # {用户昵称: 岗位名} 私聊专属人设
    "group_persona_map": {},          # {群名: 岗位名} 群组专属人设
    "knowledge_switch": True,         # FAQ 知识库开关（精确/包含命中优先于 AI）
    "knowledge_threshold": 0.6,       # 知识库模糊匹配阈值（0~1，基于关键词重合度）
    "escalation_switch": True,        # 转人工开关
    "escalation_keywords": ["转人工", "人工客服", "找客服", "真人"],  # 命中即转人工
    "escalation_target": "",          # 转人工通知对象（空=通知 admin）
    "customer_crm_switch": True,      # 客户档案 CRM 开关
    # ---- 好友添加扩展（/添加好友 指令，主动加人）----
    "friend_add": {
        "enabled": False,
        "verify_text": "",            # 默认验证语（/添加好友 时附带）
        "rate_limit_seconds": 60,     # 同一目标限流间隔
        "daily_limit": 20,            # 每日添加上限
        "retry_count": 3,             # 失败重试次数
    },
    # ---- MQTT 数字员工（OpenClaw 通道）----
    "mqtt_worker": {
        "enabled": False,             # 默认关闭，需配合 MQTT broker 与 OpenClaw
        "broker": {
            "host": "localhost",
            "port": 1883,
            "username": "",
            "password": "",
            "vhost": "/",
            "tls": False,
        },
        "task_timeout": 60,
        "close_chat_window": True,
        "close_chat_window_delay": 1.0,
        "skip_local_reply_when_forwarded": True,  # 转发到上游后跳过本地 AI 回复
        "throttle": {
            "queue_max_size": 100,
            "rate_limit_per_second": 1.0,
            "rate_limit_burst": 3,
            "queue_alert_threshold": 80,
        },
        "minio": {                    # 富媒体消息上传（可选）
            "endpoint": "", "access_key": "", "secret_key": "",
            "bucket": "wbot", "secure": True, "public_url_prefix": "",
        },
        "download_dir": "",           # 媒体下载目录（空=临时目录）
        "workers": [
            {
                "enabled": True,
                "role": "default",
                "agent_id": "agent_001",
                "topics": {
                    "subscribe": "wxbot/{role}/tasks",
                    "callback_prefix": "wxbot/callback/{agent_id}",
                    "forward": "wxbot/{role}/events",
                },
                "forward_contacts": [],   # 空=兜底转发所有
            }
        ],
    },
}


def _coerce_int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """以 DEFAULTS 为准补全缺失字段，并对数值范围做归一化。"""
    cfg = dict(DEFAULTS)
    cfg.update(raw or {})

    # 范围校验：min <= max
    if cfg["new_friend_check_min"] > cfg["new_friend_check_max"]:
        cfg["new_friend_check_min"], cfg["new_friend_check_max"] = (
            cfg["new_friend_check_max"], cfg["new_friend_check_min"]
        )
    cfg["new_friend_check_min"] = _coerce_int(cfg["new_friend_check_min"], 60, 1, 3600)
    cfg["new_friend_check_max"] = _coerce_int(cfg["new_friend_check_max"], 300, 1, 3600)

    if cfg["reply_delay_min"] > cfg["reply_delay_max"]:
        cfg["reply_delay_min"], cfg["reply_delay_max"] = cfg["reply_delay_max"], cfg["reply_delay_min"]
    cfg["reply_delay_min"] = _coerce_int(cfg["reply_delay_min"], 1, 1, 600)
    cfg["reply_delay_max"] = _coerce_int(cfg["reply_delay_max"], 5, 1, 600)

    if cfg["moments_like_min"] > cfg["moments_like_max"]:
        cfg["moments_like_min"], cfg["moments_like_max"] = cfg["moments_like_max"], cfg["moments_like_min"]
    cfg["moments_like_min"] = _coerce_int(cfg["moments_like_min"], 60, 1, 1440)
    cfg["moments_like_max"] = _coerce_int(cfg["moments_like_max"], 120, 1, 1440)

    # 监听轮询间隔（秒）+ 单轮超时（秒）
    cfg["monitor_check_interval"] = _coerce_int(cfg.get("monitor_check_interval", 10), 10, 1, 3600)
    cfg["monitor_run_timeout"] = _coerce_int(cfg.get("monitor_run_timeout", 30), 30, 5, 600)

    # 列表/字典类型兜底
    for k in ("listen_list", "group", "new_friend_msg", "new_friend_tags",
              "scheduled_msg_list", "random_msg_list", "scheduled_moments_list",
              "random_moments_list", "custom_forward_list", "api_configs"):
        if not isinstance(cfg.get(k), list):
            cfg[k] = []
    for k in ("keyword_dict", "chat_persona_map", "group_persona_map"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    # api_index 边界
    n_api = len(cfg["api_configs"])
    if not isinstance(cfg.get("api_index"), int) or cfg["api_index"] < 0 or cfg["api_index"] >= max(n_api, 1):
        cfg["api_index"] = 0
    # knowledge_threshold 浮点范围
    try:
        th = float(cfg.get("knowledge_threshold", 0.6))
    except (TypeError, ValueError):
        th = 0.6
    cfg["knowledge_threshold"] = max(0.0, min(1.0, th))
    return cfg


class BotConfig:
    """线程安全的配置管理器。运行时通过 `BotConfig.cfg` 读取最新值。"""

    def __init__(self, path: str = _CONFIG_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._cfg: dict[str, Any] = dict(DEFAULTS)

    # ---- 生命周期 ----
    def load(self) -> dict[str, Any]:
        """加载配置；文件不存在则写入默认配置。"""
        with self._lock:
            if not os.path.exists(self._path):
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                self._cfg = dict(DEFAULTS)
                self._write()
                log.info(f"首次启动，已生成默认配置: {self._path}")
                return dict(self._cfg)
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                log.error(f"配置读取失败，使用默认值: {e}")
                raw = {}
            self._cfg = _normalize(raw)
            # 归一化后若有补全，回写一次
            self._write()
            return dict(self._cfg)

    def reload(self) -> dict[str, Any]:
        return self.load()

    def save(self) -> None:
        with self._lock:
            self._write()

    def _write(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cfg, f, ensure_ascii=False, indent=4)
        os.replace(tmp, self._path)

    # ---- 读写接口 ----
    @property
    def cfg(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._cfg)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._cfg.get(key, default)

    def set(self, key: str, value: Any, persist: bool = True) -> None:
        with self._lock:
            self._cfg[key] = value
            if persist:
                self._write()

    # ---- 监听列表便捷操作（供 /指令 调用）----
    def add_listen_user(self, name: str) -> bool:
        with self._lock:
            if name in self._cfg["listen_list"]:
                return False
            self._cfg["listen_list"].append(name)
            self._write()
            return True

    def remove_listen_user(self, name: str) -> bool:
        with self._lock:
            if name not in self._cfg["listen_list"]:
                return False
            self._cfg["listen_list"].remove(name)
            self._write()
            return True

    def add_group(self, name: str) -> bool:
        with self._lock:
            if name in self._cfg["group"]:
                return False
            self._cfg["group"].append(name)
            self._write()
            return True

    def remove_group(self, name: str) -> bool:
        with self._lock:
            if name not in self._cfg["group"]:
                return False
            self._cfg["group"].remove(name)
            self._write()
            return True


# 全局单例
bot_config = BotConfig()
