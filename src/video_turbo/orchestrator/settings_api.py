"""控制台设置：语音识别、界面、传输限速和接入密钥。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from pydantic import BaseModel, Field

from .. import connection_string
from ..deployment import deployment
from ..models import (
    AsrSettings,
    TransferSettings,
    UiSettings,
)
from .context import app_state
from .network import lan_addresses
from .rate_limit import limit_bytes

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/settings/asr")
def get_asr_settings(request: Request) -> AsrSettings:
    return app_state(request).database.asr_settings(app_state(request).config.asr)


@router.put("/api/settings/asr")
def update_asr_settings(request: Request, settings: AsrSettings) -> AsrSettings:
    app_state(request).database.set_asr_settings(settings)
    return settings


@router.get("/api/settings/ui")
def get_ui_settings(request: Request) -> UiSettings:
    return app_state(request).database.ui_settings()


@router.put("/api/settings/ui")
def update_ui_settings(request: Request, settings: UiSettings) -> UiSettings:
    app_state(request).database.set_ui_settings(settings)
    return settings


@router.get("/api/settings/transfer")
def get_transfer_settings(request: Request) -> TransferSettings:
    return app_state(request).database.transfer_settings()


@router.put("/api/settings/transfer")
async def update_transfer_settings(request: Request, settings: TransferSettings) -> TransferSettings:
    await asyncio.to_thread(app_state(request).database.set_transfer_settings, settings)
    # 立即作用于正在进行的下载。
    app_state(request).upload_limiter.set_rate(limit_bytes(settings))
    return settings


@router.get("/api/settings/worker-token")
def get_worker_token(request: Request) -> dict:
    return {"token": app_state(request).worker_token}


@router.post("/api/settings/worker-token/reset")
def reset_worker_token(request: Request) -> dict:
    """生成新密钥并立即生效；使用旧密钥的 Worker 之后的请求都会被拒绝。"""
    token = app_state(request).database.reset_worker_token()
    app_state(request).worker_token = token
    logger.warning("接入密钥已在控制台重置，使用旧密钥的机器需要更新配置。")
    return {"token": token}


class ConnectionRequest(BaseModel):
    url: str = Field(min_length=1, max_length=300)


@router.get("/api/settings/worker-connection")
async def worker_connection_options(request: Request) -> dict:
    """“添加显卡电脑”的候选接入地址。Docker 里的网卡地址是容器内部的，不提供，由控制台按访问地址补上。"""
    kind = deployment()
    addresses = [] if kind == "docker" else await asyncio.to_thread(lan_addresses)
    return {
        "worker_port": app_state(request).config.server.worker_port,
        "addresses": addresses,
        "deployment": kind,
    }


@router.post("/api/settings/worker-connection")
def create_worker_connection(request: Request, payload: ConnectionRequest) -> dict:
    """按选定的接入地址生成接入串（含接入密钥）。"""
    try:
        code = connection_string.encode(payload.url, app_state(request).worker_token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"code": code}
