"""机器接入的业务逻辑：注册、心跳续租、领取字幕识别和影片修复任务。接口层见 worker_api。"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import languages
from ..models import (
    JobClaim,
    JobState,
    MediaFetch,
    RestoreClaim,
    WorkerHeartbeat,
    WorkerRegistration,
)
from . import chunked_upload
from .context import AppState
from .sources import SourceUnavailable

logger = logging.getLogger(__name__)


def register(state: AppState, worker: WorkerRegistration) -> dict:
    database = state.database
    database.register_worker(worker)
    # 重新注册说明上一个进程已退出：它持有的影片修复任务立即放回，并为本机保留以便续传。
    released = database.release_worker_restore_leases(
        worker.name, state.config.restore.resume_reservation_minutes
    )
    if released:
        logger.info("机器 %s 重启，%d 个影片修复任务已重新排队，并为这台机器保留。", worker.name, released)
    return {"registered": True, "lease_seconds": state.config.workers.lease_seconds}


def heartbeat(state: AppState, name: str, payload: WorkerHeartbeat) -> dict:
    """记录心跳并为机器正在处理的任务续租；被取消或已不属于它的任务告诉机器停下。"""
    database = state.database
    registered = database.heartbeat_worker(name, payload)
    renewed = False
    cancel_requested = False
    restore_lease = state.config.restore.lease_seconds
    restore_stopped: list[str] = []
    current_reported = False
    for report in payload.restore_jobs:
        if database.restore_cancel_requested(report.job_id, name) or not database.renew_restore_lease(
            report.job_id, name, restore_lease, report.progress
        ):
            restore_stopped.append(report.job_id)
        if report.job_id == payload.current_job_id:
            current_reported = True
            cancel_requested = report.job_id in restore_stopped
            renewed = not cancel_requested
    if payload.current_job_id and payload.task_kind == "restore" and not current_reported:
        cancel_requested = database.restore_cancel_requested(payload.current_job_id, name)
        if not cancel_requested:
            renewed = database.renew_restore_lease(
                payload.current_job_id, name, restore_lease, payload.restore_progress
            )
    elif payload.current_job_id and payload.task_kind != "restore":
        job = database.get_job(payload.current_job_id)
        cancel_requested = bool(
            # 取消的任务回到待处理，暂停的任务停在已暂停：都要让机器停下。
            job and job["state"] in {JobState.PENDING, JobState.PAUSED}
        )
        if not cancel_requested:
            renewed = database.renew_lease(
                payload.current_job_id, name, state.config.workers.lease_seconds
            )
    return {
        "ok": True,
        "registered": registered,
        "lease_renewed": renewed,
        "cancel_requested": cancel_requested,
        "restore_stopped_job_ids": restore_stopped,
    }


def claim_asr_job(state: AppState, name: str) -> JobClaim | None:
    config = state.config
    if state.orchestrator.paused or state.cloud.claims_blocked(name):
        return None
    settings = state.database.asr_settings(config.asr)
    mode = settings.mode
    ensemble = mode == "whisperjav" and settings.whisperjav_ensemble
    job = state.database.claim_asr_job(
        worker_name=name,
        lease_seconds=config.workers.lease_seconds,
        asr_mode=mode,
        whisperjav_ensemble=ensemble,
    )
    if not job:
        return None
    media = None
    if job["fetch_media"]:
        # 网盘影片：机器用插件直接下载并抽音频，影片不经过主程序。
        try:
            transfer = state.orchestrator.sources.transfer(
                str(job["library_id"]), str(job["media_path"])
            )
        except SourceUnavailable as exc:
            state.database.set_job_state(
                job["id"],
                JobState.FAILED,
                error=exc.message,
                fields={"lease_owner": None, "lease_expires_at": None},
            )
            return None
        media = MediaFetch(
            **transfer,
            relay_path=f"/api/worker/media/{job['id']}",
            stream_index=int(job["source_stream_index"]),
            channels=config.audio.channels,
            sample_rate=config.audio.sample_rate,
            opus_bitrate=config.audio.opus_bitrate,
        )
    return JobClaim(
        job_id=job["id"],
        audio_url=None if media else f"/api/jobs/{job['id']}/audio",
        audio_sha256=None if media else job["audio_sha256"],
        media=media,
        title=Path(job["media_path"]).stem,
        language=str(languages.get(job["source_language"]).asr_code),
        asr_mode=mode,
        whisperjav_ensemble=job["whisperjav_ensemble"],
    )


def claim_restore_job(state: AppState, name: str, *, prefetch: bool) -> RestoreClaim | None:
    if state.orchestrator.paused or state.cloud.claims_blocked(name):
        return None
    job = state.database.claim_restore_job(
        worker_name=name,
        lease_seconds=state.config.restore.lease_seconds,
        prefetch=prefetch,
        heartbeat_timeout_seconds=state.config.workers.heartbeat_timeout_seconds,
    )
    if not job:
        return None
    if job.get("reset_upload"):
        chunked_upload.discard(state.restore.upload_path(job))
    return RestoreClaim(
        job_id=job["id"],
        source_url=f"/api/restore/jobs/{job['id']}/source",
        upload_url=f"/api/restore/jobs/{job['id']}/upload",
        source_size=job["source_size"],
        source_sha256=job["source_sha256"],
        title=Path(job["source_path"]).stem,
        source_extension=Path(job["source_path"]).suffix.lstrip(".").lower(),
        output_extension=job["output_extension"],
        restoration_model=job["restoration_model"],
        codec=job["codec"],
        ltx_large_canvas=bool(job.get("ltx_large_canvas")),
    )
