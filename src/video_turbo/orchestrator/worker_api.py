"""机器接入接口：注册、心跳、领取任务、下载素材、传回结果。

全部需要接入密钥，路径列在 worker_gateway.WORKER_ROUTES 中（改动时按 protocol.py 的规则升级协议版本）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse, Response, StreamingResponse

from ..i18n import msg
from ..models import (
    AsrFailure,
    AsrResult,
    JobClaim,
    JobState,
    PluginStateReport,
    RestoreClaim,
    RestoreFailure,
    RestoreState,
    RestoreUploadComplete,
    WorkerHeartbeat,
    WorkerRegistration,
)
from ..plugins.base import STATE_MAX_BYTES, describe_error
from ..plugins.settings import PluginSettings
from . import chunked_upload, worker_service
from .context import NO_STORE_HEADERS, app_state, require_worker
from .rate_limit import ThrottledFileResponse
from .sources import SourceUnavailable

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/workers/register", dependencies=[Depends(require_worker)])
def register_worker(request: Request, worker: WorkerRegistration) -> dict:
    return worker_service.register(app_state(request), worker)


@router.post("/api/workers/{name}/heartbeat", dependencies=[Depends(require_worker)])
def heartbeat(request: Request, name: str, payload: WorkerHeartbeat) -> dict:
    return worker_service.heartbeat(app_state(request), name, payload)


@router.post(
    "/api/workers/{name}/claim",
    response_model=JobClaim | None,
    dependencies=[Depends(require_worker)],
)
def claim_job(request: Request, name: str) -> JobClaim | None:
    return worker_service.claim_asr_job(app_state(request), name)


@router.get("/api/worker/asr-settings", dependencies=[Depends(require_worker)])
def worker_asr_settings(request: Request) -> dict:
    """当前识别模式：桌面版识别机据此决定默认下载哪些模型、提示缺哪些模型。"""
    state = app_state(request)
    settings = state.database.asr_settings(state.config.asr)
    return {"mode": settings.mode, "whisperjav_ensemble": settings.mode == "whisperjav" and settings.whisperjav_ensemble}


@router.get("/api/worker/plugins/{plugin_sha256}", dependencies=[Depends(require_worker)])
def download_worker_plugin(request: Request, plugin_sha256: str) -> Response:
    """GPU 机器按文件指纹下载媒体库来源插件的源码（领取网盘影片的字幕任务时使用）。"""
    plugin = next(
        (
            item
            for item in app_state(request).database.installed_plugins().values()
            if "media_source" in (item.get("kinds") or ())
            and item.get("sha256") == plugin_sha256.lower()
        ),
        None,
    )
    if plugin is None or not plugin.get("source"):
        raise HTTPException(status_code=404, detail=msg("api.worker.pluginGone"))
    return Response(
        plugin["source"], media_type="text/x-python; charset=utf-8", headers=NO_STORE_HEADERS
    )


@router.post("/api/jobs/{job_id}/plugin-state", dependencies=[Depends(require_worker)])
def report_plugin_state(request: Request, job_id: str, report: PluginStateReport) -> dict:
    """GPU 机器回传网盘插件下载影片时保存的状态（例如刷新后的令牌），写入该插件的插件状态。"""
    job = app_state(request).database.get_job(job_id)
    if not job or not job["fetch_media"] or job["lease_owner"] != report.worker_name:
        raise HTTPException(status_code=409, detail=msg("common.job_not_owned"))
    library = app_state(request).config.library(str(job["library_id"]))
    if library is None or library.is_local:
        raise HTTPException(status_code=409, detail=msg("api.worker.libraryDeleted"))
    try:
        json_size = len(json.dumps(report.changes, ensure_ascii=False).encode())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=msg("api.worker.stateNotJson")) from exc
    if json_size > STATE_MAX_BYTES:
        raise HTTPException(status_code=413, detail=msg("api.worker.stateTooLarge"))
    key = app_state(request).orchestrator.sources.state_key(library.source)
    PluginSettings(app_state(request).database).save_state(key, report.changes)
    return {"saved": True}


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Range 请求头 → (起点, 终点)，含两端；没有或不是单段字节范围时返回 None（返回整个文件）。

    范围超出文件时抛出 416。
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[len("bytes=") :].strip().partition("-")
    try:
        if not first:
            start, end = max(size - int(last), 0), size - 1
        else:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
    except ValueError:
        return None
    if start < 0 or start > end:
        raise HTTPException(
            status_code=416, detail=msg("api.worker.badRange"), headers={"Content-Range": f"bytes */{size}"}
        )
    return start, end


@router.api_route(
    "/api/worker/media/{job_id}", methods=["GET", "HEAD"], dependencies=[Depends(require_worker)]
)
async def relay_media(
    request: Request,
    job_id: str,
    x_worker_name: str = Header(default=""),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """GPU 机器直连不了网盘时经主程序中转影片：主程序从媒体库来源读取，支持 Range 断点续传。

    所有连接共享主程序上行限速。
    """
    state = app_state(request)
    job = state.database.get_job(job_id)
    if not job or not job["fetch_media"] or job["lease_owner"] != x_worker_name:
        raise HTTPException(status_code=409, detail=msg("common.job_not_owned"))
    library_id, path = str(job["library_id"]), str(job["media_path"])
    sources = state.orchestrator.sources

    def open_source() -> tuple[Any, Any]:
        source = sources.for_library(library_id)
        try:
            return source, source.stat(path)
        except BaseException:
            source.close()
            raise

    try:
        source, entry = await asyncio.to_thread(open_source)
    except SourceUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=describe_error(exc)) from exc
    try:
        if entry is None or entry.is_dir:
            raise HTTPException(status_code=404, detail=msg("api.worker.sourceMissing"))
        size = entry.size
        ranged = _byte_range(range_header, size) if size else None
        start, end = ranged or (0, size - 1)
        headers = {**NO_STORE_HEADERS, "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        if ranged:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        status = 206 if ranged else 200
    except BaseException:
        await asyncio.to_thread(source.close)
        raise
    if request.method == "HEAD" or not size:
        await asyncio.to_thread(source.close)
        return Response(status_code=status, headers=headers)
    limiter = state.upload_limiter

    async def body() -> Any:
        chunks = source.read_range(path, start, end)
        try:
            while (chunk := await asyncio.to_thread(next, chunks, None)) is not None:
                await limiter.acquire(len(chunk))
                yield chunk
        finally:
            await asyncio.to_thread(source.close)

    return StreamingResponse(body(), status_code=status, headers=headers, media_type="application/octet-stream")


@router.get("/api/jobs/{job_id}/audio", dependencies=[Depends(require_worker)])
def download_audio(request: Request, job_id: str) -> FileResponse:
    job = app_state(request).database.get_job(job_id)
    if not job or job["lease_owner"] is None:
        raise HTTPException(status_code=404, detail=msg("api.worker.jobNotClaimed"))
    path = Path(job["audio_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail=msg("api.worker.audioMissing"))
    return ThrottledFileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        headers=NO_STORE_HEADERS,
        limiter=app_state(request).upload_limiter,
    )


@router.post("/api/jobs/{job_id}/asr-error", dependencies=[Depends(require_worker)])
def report_asr_error(request: Request, job_id: str, failure: AsrFailure) -> dict:
    accepted = app_state(request).database.record_asr_failure(
        job_id, failure.worker_name, failure.error
    )
    if not accepted:
        raise HTTPException(status_code=409, detail=msg("common.job_not_owned"))
    return {"accepted": True}


@router.post("/api/jobs/{job_id}/asr-result", dependencies=[Depends(require_worker)])
def submit_asr(request: Request, job_id: str, result: AsrResult) -> dict:
    state = app_state(request)
    job = state.database.get_job(job_id)
    if (
        not job
        or result.job_id != job_id
        or job["lease_owner"] != result.worker_name
        or job["state"] != JobState.TRANSCRIBING
    ):
        raise HTTPException(status_code=409, detail=msg("common.job_not_owned"))
    state.orchestrator.save_asr_result(job, result)
    return {"accepted": True, "cue_count": len(result.cues)}


RESTORE_UPLOAD_MAX_CHUNK = 256 * 1024 * 1024


@router.post(
    "/api/workers/{name}/restore/claim",
    response_model=RestoreClaim | None,
    dependencies=[Depends(require_worker)],
)
def claim_restore_job(
    request: Request, name: str, prefetch: bool = Query(default=False)
) -> RestoreClaim | None:
    return worker_service.claim_restore_job(app_state(request), name, prefetch=prefetch)


def _leased_restore_job(request: Request, job_id: str, worker_name: str) -> dict:
    job = app_state(request).database.is_restore_lease_holder(job_id, worker_name)
    if not job:
        raise HTTPException(status_code=409, detail=msg("api.worker.restoreNotOwned"))
    return job


@router.get("/api/restore/jobs/{job_id}/source", dependencies=[Depends(require_worker)])
def download_restore_source(
    request: Request, job_id: str, x_worker_name: str = Header(default="")
) -> FileResponse:
    job = _leased_restore_job(request, job_id, x_worker_name)
    path = Path(job["source_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail=msg("api.worker.sourceMissing"))
    stat = path.stat()
    if stat.st_size != job["source_size"] or stat.st_mtime_ns != job["source_mtime_ns"]:
        raise HTTPException(status_code=409, detail=msg("api.worker.sourceChanged"))
    # FileResponse 支持 Range 请求，Worker 中断后可从已下载位置继续；所有连接共享主控上行限速。
    return ThrottledFileResponse(
        path,
        filename=f"source{path.suffix.lower()}",
        media_type="application/octet-stream",
        headers=NO_STORE_HEADERS,
        limiter=app_state(request).upload_limiter,
    )


@router.get("/api/restore/jobs/{job_id}/upload", dependencies=[Depends(require_worker)])
async def restore_upload_offset(
    request: Request, job_id: str, x_worker_name: str = Header(default="")
) -> dict:
    job = _leased_restore_job(request, job_id, x_worker_name)
    path = app_state(request).restore.upload_path(job)
    # offset 供单连接顺序上传；chunked 表示支持多连接分块上传。
    return await asyncio.to_thread(chunked_upload.status, path)


@router.put("/api/restore/jobs/{job_id}/upload", dependencies=[Depends(require_worker)])
async def restore_upload_chunk(
    request: Request,
    job_id: str,
    offset: int = Query(ge=0),
    x_worker_name: str = Header(default=""),
) -> dict:
    job = _leased_restore_job(request, job_id, x_worker_name)
    path = app_state(request).restore.upload_path(job)
    if chunked_upload.state_path(path).exists():
        # 换回顺序上传（例如 Worker 降级）：丢弃分块数据从头开始。
        chunked_upload.discard(path)
    current = path.stat().st_size if path.is_file() else 0
    if offset != current:
        raise HTTPException(status_code=409, detail={"message": msg("api.worker.offsetMismatch"), "offset": current})
    received = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = bytearray()
    with path.open("ab") as handle:
        async for data in request.stream():
            received += len(data)
            if received > RESTORE_UPLOAD_MAX_CHUNK:
                handle.truncate(current)
                raise HTTPException(status_code=413, detail=msg("api.worker.chunkTooLarge"))
            buffer.extend(data)
            if len(buffer) >= 4 * 1024 * 1024:
                await asyncio.to_thread(handle.write, bytes(buffer))
                buffer.clear()
        if buffer:
            await asyncio.to_thread(handle.write, bytes(buffer))
    return {"offset": current + received}


@router.put("/api/restore/jobs/{job_id}/upload/chunk", dependencies=[Depends(require_worker)])
async def restore_upload_part(
    request: Request,
    job_id: str,
    index: int = Query(ge=0),
    chunk_size: int = Query(ge=1),
    total: int = Query(ge=1),
    x_worker_name: str = Header(default=""),
) -> dict:
    job = _leased_restore_job(request, job_id, x_worker_name)
    path = app_state(request).restore.upload_path(job)
    lock = app_state(request).upload_locks.get(job_id)
    try:
        async with lock:
            state = await asyncio.to_thread(chunked_upload.prepare, path, total=total, chunk_size=chunk_size)
        if index >= chunked_upload.chunk_count(total, chunk_size):
            raise chunked_upload.ChunkError(msg("api.worker.chunkIndex"))
        expected = chunked_upload.expected_length(index, total, chunk_size)
        written = await chunked_upload.write_chunk(path, index * chunk_size, expected, request.stream())
    except chunked_upload.ChunkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if written != expected:
        raise HTTPException(status_code=400, detail=msg("api.worker.chunkIncomplete", written=written, expected=expected))
    async with lock:
        state = chunked_upload.load_state(path) or state
        state["received"].add(index)
        await asyncio.to_thread(chunked_upload.save_state, path, state)
    return {"index": index, "received": len(state["received"])}


@router.post("/api/restore/jobs/{job_id}/complete", dependencies=[Depends(require_worker)])
def restore_upload_complete(
    request: Request, job_id: str, payload: RestoreUploadComplete
) -> dict:
    job = _leased_restore_job(request, job_id, payload.worker_name)
    path = app_state(request).restore.upload_path(job)
    chunk_state = chunked_upload.load_state(path)
    if chunk_state is not None:
        missing = chunked_upload.chunk_count(chunk_state["total"], chunk_state["chunk_size"]) - len(
            chunk_state["received"]
        )
        if missing or chunk_state["total"] != payload.size:
            raise HTTPException(
                status_code=409, detail={"message": msg("api.worker.chunksMissing", missing=missing), "offset": 0}
            )
    size = path.stat().st_size if path.is_file() else 0
    if size != payload.size:
        raise HTTPException(status_code=409, detail={"message": msg("api.worker.sizeMismatch"), "offset": size})
    accepted = app_state(request).database.update_restore_job(
        job_id,
        expected_states={RestoreState.DOWNLOADING, RestoreState.PROCESSING, RestoreState.UPLOADING},
        state=RestoreState.FINALIZING,
        lease_owner=None,
        lease_expires_at=None,
        upload_size=payload.size,
        upload_sha256=payload.sha256.lower(),
        upload_path=str(path),
        metadata_json=json.dumps(payload.metadata, ensure_ascii=False),
        progress_json=None,
    )
    if not accepted:
        raise HTTPException(status_code=409, detail=msg("api.worker.restoreChanged"))
    chunked_upload.state_path(path).unlink(missing_ok=True)
    return {"accepted": True}


@router.post("/api/restore/jobs/{job_id}/error", dependencies=[Depends(require_worker)])
def restore_report_error(request: Request, job_id: str, failure: RestoreFailure) -> dict:
    if not app_state(request).database.record_restore_failure(
        job_id, failure.worker_name, failure.error
    ):
        raise HTTPException(status_code=409, detail=msg("api.worker.restoreNotOwned"))
    return {"accepted": True}
