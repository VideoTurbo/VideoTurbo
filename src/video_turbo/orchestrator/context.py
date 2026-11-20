"""接口共用的依赖：带类型的运行状态、机器接入校验和通用常量。

运行时的服务对象在 ``app.lifespan`` 中创建并挂在 ``app.state`` 上（配置和接入密钥会在运行中被替换）。
``app_state(request)`` 返回同一个对象，只是带上类型，方便编辑器提示和类型检查；新增服务时在 ``AppState`` 中登记。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi import Header, HTTPException, Request

from ..i18n import msg
from ..protocol import PROTOCOL_MISMATCH_STATUS, WORKER_PROTOCOL_VERSION, mismatch_message
from ..security import secure_equals

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..config_store import ConfigStore
    from ..database import Database
    from .auth import AdminAuth
    from .chunked_upload import ChunkLocks
    from .cloud_launcher import CloudLauncher
    from .cloud_service import CloudService
    from .cloud_tunnel import TunnelManager
    from .integrations import MediaRefresher, NoticeService
    from .poster_service import PosterService
    from .processor import Orchestrator
    from .rate_limit import RateLimiter
    from .restore_service import RestoreService

logger = logging.getLogger(__name__)

LIST_SORT_PATTERN = "^(updated|name|size)_(asc|desc)$"
LIST_PAGE_SIZE = 20
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class AppState(Protocol):
    """``app.state`` 上的运行状态。"""

    config: AppConfig
    config_store: ConfigStore
    config_lock: asyncio.Lock
    database: Database
    orchestrator: Orchestrator
    worker_token: str
    admin_auth: AdminAuth
    admin_auth_lock: asyncio.Lock
    restore: RestoreService
    # 测试中替换为 httpx.MockTransport；为 None 时使用真实网络。
    plugin_transport: Any
    notices: NoticeService
    media_refresher: MediaRefresher
    cloud: CloudService
    cloud_tunnels: TunnelManager
    cloud_launcher: CloudLauncher
    upload_locks: ChunkLocks
    posters: PosterService
    upload_limiter: RateLimiter


def app_state(request: Request) -> AppState:
    return cast("AppState", request.app.state)


# 协议版本不一致的机器每次启动都会重新注册失败；控制台通知最多每 10 分钟一条。
PROTOCOL_NOTICE_SECONDS = 600
_protocol_notice_at = 0.0


def require_worker(
    request: Request,
    x_worker_token: str = Header(default=""),
    x_videoturbo_protocol: str = Header(default=""),
) -> None:
    if not secure_equals(x_worker_token, app_state(request).worker_token):
        raise HTTPException(status_code=401, detail=msg("common.worker_token_invalid"))
    if x_videoturbo_protocol.strip() != str(WORKER_PROTOCOL_VERSION):
        message = mismatch_message(WORKER_PROTOCOL_VERSION, x_videoturbo_protocol.strip())
        if request.url.path == "/api/workers/register":
            _report_protocol_mismatch(request, message)
        raise HTTPException(status_code=PROTOCOL_MISMATCH_STATUS, detail=message)


def _report_protocol_mismatch(request: Request, message: str) -> None:
    global _protocol_notice_at
    client = request.client.host if request.client else msg("api.context.unknownAddress")
    logger.warning("拒绝机器接入（%s）：%s", client, message)
    now = time.monotonic()
    notices = getattr(request.app.state, "notices", None)
    if notices is None or (_protocol_notice_at and now - _protocol_notice_at < PROTOCOL_NOTICE_SECONDS):
        return
    _protocol_notice_at = now
    notices.notify("warning", msg("api.context.versionMismatchTitle"), msg("api.context.versionMismatch", client=client, message=message))
