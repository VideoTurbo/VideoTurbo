"""诊断包：用户反馈问题时从控制台下载一个 zip 附在 Issue 里。

内容包括版本与运行环境、控制台设置（去掉所有密钥）、GPU 机器、插件、任务数量、最近的通知，
以及主程序最近的日志。日志只保存在内存里（最近 ``LOG_LINES`` 行），不写磁盘，重启后清空。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import platform
import re
import sys
import zipfile
from collections import deque
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import __version__
from ..deployment import deployment
from ..environment import getenv
from ..i18n import LocalizedFormatter
from ..migrations import SCHEMA_VERSION, schema_version
from ..plugins import installer as plugin_installer
from ..protocol import WORKER_PROTOCOL_VERSION

router = APIRouter()

LOG_LINES = 2000
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
NOTICE_LIMIT = 50
REDACTED = "***"
# 键名含这些词、值为非空字符串时一律替换为 ***；布尔值（例如 key_configured）保留。
_SECRET_KEY = re.compile(
    r"key|token|secret|password|passwd|credential|license|cookie|authorization|masked",
    re.IGNORECASE,
)
# 插件源码可能很大且不利于排查，诊断包只保留插件的标识信息。
_PLUGIN_FIELDS = ("kind", "kinds", "url", "sha256", "version", "installed_at", "updated_at")


class LogBuffer(logging.Handler):
    """在内存中保留最近的日志行。deque 的 append 是线程安全的，多线程写日志不需要额外加锁。"""

    def __init__(self, capacity: int = LOG_LINES) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(LocalizedFormatter(LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - 与 logging.Handler 的约定一致：交给 handleError 处理
            self.handleError(record)

    def text(self) -> str:
        return "\n".join(list(self.lines))


log_buffer = LogBuffer()
started_at = datetime.now(UTC)


def install_log_buffer() -> None:
    """挂到根日志器和 uvicorn 日志器上（uvicorn 的日志不向根日志器传递）；重复调用不会重复挂载。

    访问日志（uvicorn.access）每次轮询都会产生，量大且无助于排查，不收集。
    """
    for name in ("", "uvicorn"):
        target = logging.getLogger(name)
        if log_buffer not in target.handlers:
            target.addHandler(log_buffer)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """递归去掉密钥：按键名替换，并把已知的密钥原文（例如接入密钥）从所有字符串中抹掉。"""
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(item, str) and item and _SECRET_KEY.search(str(key))
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        return scrub(value, secrets)
    return value


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def _plugins(installed: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        plugin_id: {field: item[field] for field in _PLUGIN_FIELDS if field in item}
        for plugin_id, item in installed.items()
    }


def collect(app: Any) -> dict[str, Any]:
    """汇总诊断信息（同步读取数据库，调用方放到线程中执行）。"""
    state = app.state
    database = state.database
    config = state.config
    store = state.config_store
    with database.connect() as conn:
        database_version = schema_version(conn)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "version": __version__,
        "worker_protocol": WORKER_PROTOCOL_VERSION,
        "database_schema": {"current": database_version, "supported": SCHEMA_VERSION},
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "deployment": deployment(),
            "local_preview": getenv("VIDEO_TURBO_LOCAL_PREVIEW") == "1",
            "plugin_safe_mode": plugin_installer.safe_mode(),
        },
        "deployment": {
            "port": config.server.port,
            "worker_port": config.server.worker_port,
            "trusted_proxies_set": bool(config.server.trusted_proxies),
            "restore_backup_dir_set": bool(config.restore.backup_dir),
        },
        "warnings": config.warnings(),
        "settings": store.public_settings(),
        "llm": store.llm().public(),
        "asr": database.asr_settings(config.asr).model_dump(mode="json"),
        "transfer": database.transfer_settings().model_dump(mode="json"),
        "pipeline_paused": state.orchestrator.paused,
        "scan_status": state.orchestrator.scan_status,
        "translation": state.orchestrator.translation_status(),
        "media_counts": database.media_counts(),
        "job_counts": database.job_counts(),
        "restore_job_counts": database.restore_job_counts(),
        "workers": database.list_workers(),
        "cloud": state.cloud.status(),
        "plugins": _plugins(database.installed_plugins()),
        "notices": database.list_notices(limit=NOTICE_LIMIT),
    }


def build_archive(app: Any) -> bytes:
    secrets = (str(app.state.worker_token or ""),)
    info = redact(collect(app), secrets)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "diagnostics.json", json.dumps(info, ensure_ascii=False, indent=2, default=str)
        )
        archive.writestr("server.log", scrub(log_buffer.text(), secrets))
    return buffer.getvalue()


@router.get("/api/diagnostics")
async def download_diagnostics(request: Request) -> Response:
    content = await asyncio.to_thread(build_archive, request.app)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Response(
        content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="videoturbo-diagnostics-{stamp}.zip"',
            "Cache-Control": "no-store",
        },
    )
