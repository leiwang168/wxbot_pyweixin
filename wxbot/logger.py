# -*- coding: utf-8 -*-
"""日志：控制台 + 滚动文件，统一 UTF-8。"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("wxbot")
    if logger.handlers:  # 已初始化
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    # 控制台（强制 UTF-8，避免 Windows emoji/中文报错）
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # 文件
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(_LOG_DIR, f"wxbot_{datetime.now():%Y%m%d}.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        logger.warning(f"文件日志初始化失败，仅控制台输出: {e}")
    return logger


log = _build_logger()
