"""影片修复：状态、可选影片、任务列表与操作。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
)

from ..database import Database
from ..i18n import msg
from ..list_status import LIST_VIEW_PATTERN
from ..models import (
    RESTORATION_MODEL_LABELS,
    RESTORE_CODEC,
    RestoreBatchAction,
    RestoreRequest,
    RestoreSettings,
)
from .context import LIST_PAGE_SIZE, LIST_SORT_PATTERN, app_state
from .poster_service import attach_poster_urls
from .restore_service import RestoreError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/restore/status")
def restore_status(request: Request) -> dict:
    db: Database = app_state(request).database
    service = app_state(request).restore
    all_workers = db.list_workers()
    workers = [worker for worker in all_workers if worker.get("supports_restore")]
    return {
        # 启用了影片修复但本次启动检查未通过的 Worker（例如租到的机器没有可用的视频编码器）。
        "unavailable_workers": [
            {"name": worker["name"], "reason": worker["restore_unavailable_reason"],
             "last_seen_at": worker["last_seen_at"]}
            for worker in all_workers
            if worker.get("restore_unavailable_reason") and not worker.get("supports_restore")
        ],
        "enabled": service.enabled,
        "backup_dir": str(app_state(request).config.restore.backup_dir or ""),
        "settings": db.restore_settings().model_dump(),
        "models": RESTORATION_MODEL_LABELS,
        "job_counts": db.restore_job_counts(),
        "workers": workers,
        "heartbeat_timeout_seconds": app_state(request).config.workers.heartbeat_timeout_seconds,
    }


@router.put("/api/settings/restore")
def update_restore_settings(request: Request, settings: RestoreSettings) -> RestoreSettings:
    app_state(request).database.set_restore_settings(settings)
    return settings


@router.get("/api/restore/media")
async def restore_media(
    request: Request,
    query: str | None = Query(default=None, max_length=300),
    library: str | None = Query(default=None, max_length=64),
    status: str = Query(default="all", pattern=LIST_VIEW_PATTERN),
    sort: str = Query(default="updated_desc", pattern=LIST_SORT_PATTERN),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=LIST_PAGE_SIZE, ge=1, le=100),
) -> dict:
    return await asyncio.to_thread(
        app_state(request).restore.list_media,
        query=(query or "").strip() or None,
        library_id=library or None,
        status=status,
        sort=sort,
        limit=page_size,
        offset=(page - 1) * page_size,
    )


@router.get("/api/restore/jobs")
def restore_jobs(
    request: Request,
    view: str = Query(default="active", pattern="^(active|error|done|all)$"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return {
        "jobs": attach_poster_urls(app_state(request).database.list_restore_jobs(view=view, limit=limit))
    }


@router.post("/api/restore/jobs")
async def create_restore_jobs(request: Request, payload: RestoreRequest) -> dict:
    try:
        result = await asyncio.to_thread(
            app_state(request).restore.create_jobs,
            payload.media_ids,
            payload.restoration_model,
            RESTORE_CODEC,
            payload.ltx_large_canvas,
        )
    except RestoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    app_state(request).database.set_restore_settings(
        RestoreSettings(
            restoration_model=payload.restoration_model,
            ltx_large_canvas=payload.ltx_large_canvas,
        )
    )
    return result


@router.get("/api/restore/jobs/{job_id}")
def restore_job_detail(request: Request, job_id: str) -> dict:
    job = app_state(request).database.get_restore_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=msg("api.restore.jobNotFound"))
    return job


def _restore_action(request: Request, job_id: str, action: str) -> tuple[bool, str]:
    service = app_state(request).restore
    if action == "cancel":
        return service.cancel(job_id), msg("api.restore.cancelled")
    if action == "retry":
        return service.retry(job_id), msg("api.restore.requeued")
    if action == "purge_original":
        return service.purge_original(job_id)
    return False, msg("api.restore.unknownAction")


@router.post("/api/restore/jobs/{job_id}/actions/{action}")
async def restore_job_action(request: Request, job_id: str, action: str) -> dict:
    if action not in {"cancel", "retry", "purge_original"}:
        raise HTTPException(status_code=404, detail=msg("api.restore.unknownAction"))
    ok, message = await asyncio.to_thread(_restore_action, request, job_id, action)
    if not ok:
        raise HTTPException(status_code=409, detail=message if action == "purge_original" else msg("api.restore.actionNotAllowed"))
    return {"ok": True, "message": message}


@router.post("/api/restore/batch")
async def restore_batch(request: Request, payload: RestoreBatchAction) -> dict:
    succeeded: list[str] = []
    failed: list[str] = []
    for job_id in dict.fromkeys(payload.job_ids):
        ok, _ = await asyncio.to_thread(_restore_action, request, job_id, payload.action)
        (succeeded if ok else failed).append(job_id)
    return {"action": payload.action, "succeeded": succeeded, "failed": failed}
