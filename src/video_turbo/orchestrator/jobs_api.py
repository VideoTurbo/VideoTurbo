"""字幕任务：列表、扫描、自动处理开关、翻译模型和任务操作。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse

from ..database import Database
from ..i18n import msg
from ..list_status import LIST_VIEW_PATTERN, job_states_for, job_view_counts
from ..models import (
    BatchJobAction,
    JobPriorityUpdate,
    JobReprocessRequest,
    JobState,
    ManualScanRequest,
)
from .context import LIST_PAGE_SIZE, LIST_SORT_PATTERN, NO_STORE_HEADERS, app_state
from .poster_service import attach_poster_urls
from .processor import Orchestrator, ReprocessError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/media/{media_id}/poster")
async def media_poster(request: Request, media_id: int) -> FileResponse:
    path = await app_state(request).posters.thumbnail(media_id)
    if path is None:
        raise HTTPException(status_code=404, detail=msg("api.jobs.noPoster"))
    media_type = {".png": "image/png", ".bmp": "image/bmp"}.get(path.suffix.lower(), "image/jpeg")
    # 地址里带海报修改时间，海报变化后地址随之变化，可以放心让浏览器缓存。
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/jobs")
async def list_jobs(
    request: Request,
    view: str = Query(default="all", pattern=LIST_VIEW_PATTERN),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=LIST_PAGE_SIZE, ge=1, le=100),
    q: str | None = Query(default=None, max_length=300),
    library: str | None = Query(default=None, max_length=64),
    sort: str = Query(default="updated_desc", pattern=LIST_SORT_PATTERN),
) -> dict:
    db: Database = app_state(request).database
    orchestrator: Orchestrator = app_state(request).orchestrator
    filter_args = {"states": job_states_for(view), "query": q, "library_id": library}

    def read_database() -> tuple[int, int, int, list[dict], dict[str, int]]:
        total = db.count_jobs(**filter_args)
        pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(page, pages)
        rows = db.list_jobs(
            page_size, offset=(current_page - 1) * page_size, **filter_args, sort=sort
        )
        return total, pages, current_page, rows, db.job_counts(query=q, library_id=library)

    total, pages, current_page, rows, state_counts = await asyncio.to_thread(read_database)
    return {
        "items": attach_poster_urls(orchestrator.enrich_jobs(rows)),
        "total": total,
        "page": current_page,
        "page_size": page_size,
        "pages": pages,
        "view": view,
        "counts": job_view_counts(state_counts),
    }


@router.post("/api/scan")
async def start_scan(
    request: Request,
    background_tasks: BackgroundTasks,
    selection: ManualScanRequest | None = None,
) -> dict:
    if not app_state(request).config.scan.manual_scan_enabled:
        raise HTTPException(status_code=403, detail=msg("api.jobs.manualScanDisabled"))
    orchestrator: Orchestrator = app_state(request).orchestrator
    if orchestrator.paused:
        raise HTTPException(status_code=409, detail=msg("api.jobs.paused"))
    library_ids = tuple(selection.library_ids) if selection else None
    if library_ids is not None:
        config = app_state(request).config
        if len(set(library_ids)) != len(library_ids) or any(
            (library := config.library(library_id)) is None or not library.enabled
            for library_id in library_ids
        ):
            raise HTTPException(status_code=400, detail=msg("api.jobs.badLibraries"))
    background_tasks.add_task(orchestrator.scan, library_ids)
    return {"accepted": True}


@router.post("/api/scan/cancel")
def cancel_scan(request: Request) -> dict:
    orchestrator: Orchestrator = app_state(request).orchestrator
    return {"cancelled": orchestrator.cancel_scan()}


@router.post("/api/pipeline/pause")
def pause_pipeline(request: Request) -> dict:
    orchestrator: Orchestrator = app_state(request).orchestrator
    orchestrator.set_paused(True)
    return {"paused": True}


@router.post("/api/pipeline/resume")
def resume_pipeline(request: Request) -> dict:
    orchestrator: Orchestrator = app_state(request).orchestrator
    orchestrator.set_paused(False)
    return {"paused": False}


@router.post("/api/translation/upstreams/{name}/enable")
async def enable_translation_upstream(request: Request, name: str) -> dict:
    orchestrator: Orchestrator = app_state(request).orchestrator
    if not await orchestrator.enable_translation_upstream(name):
        raise HTTPException(
            status_code=409,
            detail=msg("api.jobs.upstreamNotReady"),
        )
    return {"enabled": True, "name": name}


@router.post("/api/translation/upstreams/{name}/probe")
async def probe_translation_upstream(request: Request, name: str) -> dict:
    result = await app_state(request).orchestrator.probe_translation_upstream(name)
    if result is None:
        raise HTTPException(
            status_code=409,
            detail=msg("api.jobs.probeUnavailable"),
        )
    return result


@router.post("/api/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: str) -> dict:
    cached_asr = app_state(request).orchestrator._job_dir(job_id) / "result.source.json"
    next_state = app_state(request).database.retry_job(
        job_id,
        reusable_source_result_path=cached_asr,
    )
    if next_state is None:
        raise HTTPException(
            status_code=409, detail=msg("api.jobs.retryFailedOnly")
        )
    return {"accepted": True, "state": next_state}


@router.get("/api/jobs/{job_id}")
async def job_detail(request: Request, job_id: str) -> dict:
    detail = app_state(request).orchestrator.job_detail(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=msg("api.jobNotFound"))
    return detail


@router.get("/api/jobs/{job_id}/files/{kind}")
async def job_file(request: Request, job_id: str, kind: str) -> FileResponse:
    path = app_state(request).orchestrator.resolve_job_file(job_id, kind)
    if path is None:
        raise HTTPException(status_code=404, detail=msg("api.jobs.fileNotFound"))
    media_type = "application/json" if path.suffix.lower() == ".json" else "text/plain"
    if kind == "audio":
        media_type = "application/octet-stream"
    return FileResponse(
        path,
        filename=path.name,
        media_type=media_type,
        headers=NO_STORE_HEADERS,
    )


@router.post("/api/jobs/{job_id}/start")
def start_job(request: Request, job_id: str) -> dict:
    """把待处理的影片提交到字幕队列。"""
    if app_state(request).orchestrator.start_job(job_id) is None:
        raise HTTPException(status_code=409, detail=msg("api.jobs.startPendingOnly"))
    return {"started": True}


@router.post("/api/jobs/{job_id}/skip")
def skip_job(request: Request, job_id: str) -> dict:
    """待处理的影片不生成字幕，记为已完成。"""
    if not app_state(request).database.skip_job(job_id):
        raise HTTPException(status_code=409, detail=msg("api.jobs.skipPendingOnly"))
    return {"accepted": True, "state": JobState.COMPLETED}


@router.post("/api/jobs/{job_id}/pause")
async def pause_job(request: Request, job_id: str) -> dict:
    if not await app_state(request).orchestrator.pause_job(job_id):
        raise HTTPException(status_code=409, detail=msg("api.jobs.pauseNotAllowed"))
    return {"accepted": True, "state": JobState.PAUSED}


@router.post("/api/jobs/{job_id}/resume")
def resume_job(request: Request, job_id: str) -> dict:
    next_state = app_state(request).orchestrator.resume_job(job_id)
    if next_state is None:
        raise HTTPException(status_code=409, detail=msg("api.jobs.resumePausedOnly"))
    return {"accepted": True, "state": next_state}


@router.post("/api/jobs/{job_id}/priority")
def update_job_priority(
    request: Request, job_id: str, payload: JobPriorityUpdate
) -> dict:
    if not app_state(request).database.set_job_priority(job_id, payload.priority):
        raise HTTPException(status_code=404, detail=msg("api.jobNotFound"))
    return {"accepted": True, "priority": payload.priority}


@router.post("/api/jobs/{job_id}/reprocess")
def reprocess_job(
    request: Request, job_id: str, payload: JobReprocessRequest
) -> dict:
    try:
        next_state = app_state(request).orchestrator.reprocess_job(job_id, payload.mode)
    except ReprocessError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if next_state is None:
        raise HTTPException(
            status_code=409,
            detail=msg("api.jobs.reprocessNotAllowed"),
        )
    return {"accepted": True, "state": next_state, "mode": payload.mode}


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> dict:
    if not await app_state(request).orchestrator.cancel_job(job_id):
        raise HTTPException(status_code=409, detail=msg("api.jobs.cancelNotAllowed"))
    return {"accepted": True, "state": JobState.PENDING}


def _completion_validation_message(invalid: dict[str, str], *, batch: bool) -> str:
    examples = [msg("api.jobs.invalidItem", job_id=job_id, reason=reason) for job_id, reason in list(invalid.items())[:5]]
    remainder = len(invalid) - len(examples)
    suffix = msg("api.jobs.invalidMore", remainder=remainder) if remainder else ""
    prefix = msg("api.jobs.batchNotRun") if batch else msg("api.jobs.cannotComplete")
    return prefix + msg("common.listJoin").join(examples) + suffix


@router.post("/api/jobs/{job_id}/complete")
async def complete_job(request: Request, job_id: str) -> dict:
    succeeded, invalid = await app_state(request).orchestrator.complete_jobs_with_existing_subtitles(
        [job_id]
    )
    if invalid:
        status_code = 404 if invalid.get(job_id) == "任务不存在" else 409
        raise HTTPException(
            status_code=status_code,
            detail=_completion_validation_message(invalid, batch=False),
        )
    job = app_state(request).database.get_job(succeeded[0])
    return {
        "accepted": True,
        "state": JobState.COMPLETED,
        "subtitle_path": job["subtitle_path"] if job else None,
    }


@router.post("/api/jobs/batch/action")
async def batch_job_action(request: Request, payload: BatchJobAction) -> dict:
    orchestrator: Orchestrator = app_state(request).orchestrator
    if payload.action == "complete":
        succeeded, invalid = await orchestrator.complete_jobs_with_existing_subtitles(
            payload.job_ids
        )
        if invalid:
            raise HTTPException(
                status_code=409,
                detail=_completion_validation_message(invalid, batch=True),
            )
        return {"action": payload.action, "succeeded": succeeded, "failed": []}
    succeeded: list[str] = []
    failed: list[str] = []
    for job_id in dict.fromkeys(payload.job_ids):
        ok = await orchestrator.apply_job_action(job_id, payload.action)
        (succeeded if ok else failed).append(job_id)
    return {"action": payload.action, "succeeded": succeeded, "failed": failed}
