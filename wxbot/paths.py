# -*- coding: utf-8 -*-
"""运行时路径管理。

开发时：基于 __file__ 推导项目根目录。
PyInstaller 打包后：基于 sys.executable 所在目录（exe 旁边）。

所有运行时可写目录（config/、logs/、memory/、customer/）统一经此模块获取，
确保打包为 exe 后数据文件不在临时解压目录中。
"""
from __future__ import annotations

import os
import sys


def get_app_dir() -> str:
    """应用根目录。

    - 开发时：项目根目录（wxbot/ 的上级）
    - 打包后：exe 所在目录
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_config_dir() -> str:
    """config/ 目录（config.json、webhook.json、contacts_cache.json 等）。"""
    return os.path.join(get_app_dir(), "config")


def get_images_dir() -> str:
    """config/images/ 模板图目录（收款/红包按钮等静态资源）。

    打包后从 _MEIPASS（exe 临时解压目录）读取；开发时从项目根目录读取。
    """
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'config', 'images')
    return os.path.join(get_app_dir(), 'config', 'images')


def get_config_path() -> str:
    """config/config.json 主配置文件路径。"""
    return os.path.join(get_config_dir(), "config.json")


def get_logs_dir() -> str:
    """logs/ 日志目录。"""
    return os.path.join(get_app_dir(), "logs")


def get_memory_dir() -> str:
    """memory/ 记忆存储目录。"""
    return os.path.join(get_app_dir(), "memory")


def get_customer_dir() -> str:
    """customer/ 客户数据目录。"""
    return os.path.join(get_app_dir(), "customer")


def ensure_dirs() -> None:
    """确保所有运行时目录存在。"""
    for d in (get_config_dir(), get_logs_dir(), get_memory_dir(), get_customer_dir()):
        os.makedirs(d, exist_ok=True)
