"""主程序日志文件：写到 ``VIDEO_TURBO_LOG_DIR``（默认 /app/logs，docker-compose 中挂载到 ./logs）下的 server.log。

单个文件超过 ``MAX_BYTES`` 时轮转，最多保留 ``BACKUP_COUNT`` 个旧文件（server.log.1 …），总占用有上限。
目录无法创建或写入时只记一条警告，主程序照常运行；``VIDEO_TURBO_LOG_DIR`` 设为空字符串时不写文件。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..environment import getenv
from ..i18n import LocalizedFormatter
from .diagnostics import LOG_FORMAT

logger = logging.getLogger(__name__)

LOG_DIR_ENV = "VIDEO_TURBO_LOG_DIR"
DEFAULT_LOG_DIR = "/app/logs"
LOG_FILE = "server.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
# 与诊断包的日志缓冲一致：根日志器和 uvicorn 日志器（不向根日志器传递），不含访问日志。
_TARGETS = ("", "uvicorn")

_handler: RotatingFileHandler | None = None


def log_dir() -> Path | None:
    value = getenv(LOG_DIR_ENV, DEFAULT_LOG_DIR)
    return Path(value) if value else None


def _detach() -> None:
    global _handler
    if _handler is None:
        return
    for name in _TARGETS:
        logging.getLogger(name).removeHandler(_handler)
    _handler.close()
    _handler = None


def install_log_file() -> Path | None:
    """挂载日志文件，返回文件路径；重复调用不会重复挂载，目录变化时换到新目录。"""
    global _handler
    directory = log_dir()
    if directory is None:
        _detach()
        return None
    path = (directory / LOG_FILE).resolve()
    if _handler is not None and Path(_handler.baseFilename) == path:
        return path
    _detach()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("无法写入日志文件 %s（%s），日志只输出到终端。", path, exc)
        return None
    handler.setFormatter(LocalizedFormatter(LOG_FORMAT))
    for name in _TARGETS:
        logging.getLogger(name).addHandler(handler)
    _handler = handler
    return path


def close_log_file() -> None:
    _detach()
