"""
日志系统

文件 DEBUG + 控制台 INFO 双输出。
"""

import logging
import sys
from pathlib import Path
from datetime import datetime


def setup_logging(project_root: str, level: str = "DEBUG") -> logging.Logger:
    root_logger = logging.getLogger("knowledge-forge")
    if root_logger.handlers:
        return root_logger

    root_logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    log_dir = Path(project_root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"pipeline_{datetime.now().strftime('%Y%m%d')}.log"

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    root_logger.addHandler(fh)
    root_logger.addHandler(ch)
    root_logger.info(f"日志系统初始化 → {log_file}")
    return root_logger
