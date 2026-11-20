"""主控侧的影片修复任务调度：计算原片校验值、校验回传文件、备份原片并发布新片。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import AppConfig, MediaLibraryConfig
from ..database import Database
from ..i18n import msg
from ..list_status import (
    RESTORE_STATUS,
    RESTORE_STEPS,
    VIEW_STATUSES,
    ListStatus,
    view_counts,
)
from ..media import duration_from_probe, run_ffprobe
from ..models import (
    RESTORE_ACTIVE_STATES,
    JobState,
    RestoreState,
    is_ltx_model,
    utc_now_iso,
)
from ..restore import (
    finalize_restore,
    ineligible_reason,
    output_extension,
    relocated_path,
    upload_temp_path,
)
from .chunked_upload import discard as discard_upload
from .poster_service import attach_poster_urls

logger = logging.getLogger(__name__)

# 字幕流水线在这两个状态会读写影片文件，发布影片修复结果需等它们结束。
SUBTITLE_STATES_USING_VIDEO = {JobState.EXTRACTING, JobState.PUBLISHING}
# 影片修复需要主程序备份原片并替换文件，目前只支持本地文件夹。
REMOTE_UNSUPPORTED = msg("restore.service.remoteUnsupported")


class RestoreError(RuntimeError):
    pass


def _hash_file(path: Path, report: Any, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    total = max(1, path.stat().st_size)
    done = 0
    last = 0.0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last >= 2:
                report(done, total)
                last = now
    report(total, total)
    return digest.hexdigest()


class RestoreService:
    def __init__(self, config: AppConfig, database: Database):
        self.config = config
        self.database = database
        # 影片修复发布后调用，通知媒体服务器刷新（integrations.MediaRefresher.request），由应用启动时设置。
        self.on_published: Callable[[], None] = lambda: None
        # 列表每 5 秒刷新一次；标记文件检查要访问磁盘，结果缓存一分钟。
        self._ineligible_cache: dict[str, tuple[float, str | None]] = {}


    @property
    def enabled(self) -> bool:
        return self.config.restore.backup_dir is not None

    def _library(self, library_id: str) -> MediaLibraryConfig:
        library = self.config.library(library_id)
        if library is None:
            raise RestoreError(msg("restore.service.libraryGone"))
        if not library.is_local:
            raise RestoreError(REMOTE_UNSUPPORTED)
        return library

    def _is_remote(self, library_id: str | None) -> bool:
        library = self.config.library(library_id)
        return bool(library and not library.is_local)

    def _set_local_progress(self, job_id: str, stage: str, **values: Any) -> None:
        payload = {"stage": stage, "updated_at": utc_now_iso(), **values}
        self.database.update_restore_job(job_id, progress_json=json.dumps(payload, ensure_ascii=False))

    def upload_path(self, job: dict[str, Any]) -> Path:
        return upload_temp_path(Path(job["source_path"]), job["output_extension"])


    def _ineligible_reason(self, path: Path) -> str | None:
        key = str(path)
        now = time.monotonic()
        cached = self._ineligible_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        reason = ineligible_reason(path, self.config.restore.skip_path_patterns)
        if len(self._ineligible_cache) > 50_000:
            self._ineligible_cache.clear()
        self._ineligible_cache[key] = (now + 60, reason)
        return reason

    def media_status(self, row: dict[str, Any]) -> tuple[ListStatus, str | None]:
        """影片在列表里的状态与说明。已修复、不需要修复、不支持修复都算已完成。"""
        state = row.get("restore_state")
        if state in {str(value) for value in RESTORE_ACTIVE_STATES}:
            return RESTORE_STATUS[RestoreState(state)], None
        if state == RestoreState.COMPLETED:
            return "completed", None
        if self._is_remote(row.get("library_id")):
            return "completed", REMOTE_UNSUPPORTED
        reason = self._ineligible_reason(Path(row["path"]))
        if reason:
            return "completed", reason
        if state == RestoreState.FAILED:
            return "failed", None
        if state == RestoreState.CANCELLED:
            return "pending", msg("restore.service.cancelledBefore")
        return "pending", None

    @staticmethod
    def media_actions(item: dict[str, Any]) -> list[str]:
        """界面上这部影片能做的操作；界面只按这份列表显示按钮。"""
        job = item.get("job") or {}
        state = job.get("state")
        actions = ["restore"] if item["can_restore"] else []
        if state in RESTORE_ACTIVE_STATES and state != RestoreState.FINALIZING:
            actions.append("cancel")
        if state == RestoreState.FAILED and item["status"] == "failed":
            actions.append("retry")
        if state == RestoreState.COMPLETED and job.get("backup_path") and not job.get("original_purged_at"):
            actions.append("purge_original")
        return actions

    def list_media(
        self,
        *,
        query: str | None,
        library_id: str | None,
        status: str,
        sort: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        status_counts: dict[str, int] = {}
        wanted = VIEW_STATUSES.get(status)
        for row in self.database.iter_restore_media(
            query=query, library_id=library_id, sort=sort
        ):
            media_status, reason = self.media_status(row)
            status_counts[media_status] = status_counts.get(media_status, 0) + 1
            if wanted is not None and media_status not in wanted:
                continue
            items.append({**row, "status": media_status, "status_reason": reason})
        # 筛选后总数变少时退回最后一页，避免停在空页上。
        pages = max(1, (len(items) + limit - 1) // limit)
        offset = min(offset, (pages - 1) * limit)
        page = items[offset : offset + limit]
        attach_poster_urls(page)
        for item in page:
            item["can_restore"] = item["status"] in {"pending", "failed"}
            # 一部影片一行：附上最近一次影片修复任务，列表直接显示进度和可用操作。
            item["job"] = (
                self.database.get_restore_job(item["restore_job_id"])
                if item.get("restore_job_id")
                else None
            )
            job_state = (item["job"] or {}).get("state")
            item["step"] = (
                RESTORE_STEPS.get(RestoreState(job_state))
                if job_state and item["status"] in {"queued", "running"}
                else None
            )
            library = self.config.library(item.get("library_id"))
            item["library_name"] = library.name if library else msg("jobs.deletedLibrary")
            # 网盘影片不能修复，也不为它逐个请求网盘。
            item["exists"] = (
                True if library is not None and not library.is_local else Path(item["path"]).is_file()
            )
            if not item["exists"] and item["can_restore"]:
                item["can_restore"] = False
                item["status_reason"] = msg("restore.service.fileMissing")
            item["actions"] = self.media_actions(item)
        return {
            "items": page,
            "total": len(items),
            "page": offset // limit + 1,
            "pages": pages,
            "counts": view_counts(status_counts),
        }

    def create_jobs(
        self, media_ids: list[int], model: str, codec: str, ltx_large_canvas: bool = False
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RestoreError(msg("restore.service.noBackupDir"))
        created: list[str] = []
        skipped: list[dict[str, Any]] = []
        for media_id in dict.fromkeys(media_ids):
            media = self.database.get_media(media_id)
            if media is None:
                skipped.append({"media_id": media_id, "reason": msg("restore.service.mediaMissing")})
                continue
            path = Path(media["path"])
            reason = (
                REMOTE_UNSUPPORTED
                if self._is_remote(media.get("library_id"))
                else ineligible_reason(path, self.config.restore.skip_path_patterns)
            )
            if reason is None and not path.is_file():
                reason = msg("restore.service.rescan")
            if reason:
                skipped.append({"media_id": media_id, "path": str(path), "reason": reason})
                continue
            stat = path.stat()
            # 一部影片只保留一条影片修复记录：上次失败或已取消的任务直接复用。
            previous = self.database.latest_restore_job(media_id)
            if previous and previous["state"] in {RestoreState.FAILED, RestoreState.CANCELLED}:
                large_canvas = bool(ltx_large_canvas and is_ltx_model(model))
                same = (
                    previous["restoration_model"] == model
                    and bool(previous.get("ltx_large_canvas")) == large_canvas
                )
                reused = (
                    self.retry(previous["id"])
                    if same
                    else self._resubmit(previous, path, stat, model, codec, large_canvas)
                )
                if reused:
                    created.append(previous["id"])
                else:
                    skipped.append(
                        {"media_id": media_id, "path": str(path), "reason": msg("restore.service.stateChanged")}
                    )
                continue
            job_id = self.database.create_restore_job(
                media_id=media_id,
                source_path=path,
                source_size=stat.st_size,
                source_mtime_ns=stat.st_mtime_ns,
                restoration_model=model,
                codec=codec,
                output_extension=output_extension(path.suffix),
                ltx_large_canvas=ltx_large_canvas,
            )
            if job_id is None:
                skipped.append({"media_id": media_id, "path": str(path), "reason": msg("restore.service.alreadyRunning")})
            else:
                created.append(job_id)
        return {"created": created, "skipped": skipped}

    def _resubmit(
        self, job: dict[str, Any], path: Path, stat: Any, model: str, codec: str, large_canvas: bool
    ) -> bool:
        """换模型重新提交：沿用原记录，但旧模型的回传文件和进度全部作废。"""
        try:
            discard_upload(self.upload_path(job))
        except OSError:
            logger.warning("清理影片修复传回的临时文件失败：%s", job["id"], exc_info=True)
        return self.database.update_restore_job(
            job["id"],
            expected_states={RestoreState.FAILED, RestoreState.CANCELLED},
            state=RestoreState.PREPARING,
            source_path=str(path),
            source_size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
            source_sha256=None,
            restoration_model=model,
            codec=codec,
            output_extension=output_extension(path.suffix),
            ltx_large_canvas=int(large_canvas),
            upload_path=None,
            upload_owner=None,
            upload_size=None,
            upload_sha256=None,
            lease_owner=None,
            lease_expires_at=None,
            reserved_machine=None,
            reserved_until=None,
            progress_json=None,
            worker_progress_json=None,
            last_error=None,
            started_at=None,
            finished_at=None,
        )


    def cancel(self, job_id: str) -> bool:
        job = self.database.cancel_restore_job(job_id)
        if job is None:
            return False
        try:
            discard_upload(self.upload_path(job))
        except OSError:
            logger.warning("清理影片修复传回的临时文件失败：%s", job_id, exc_info=True)
        return True

    def retry(self, job_id: str) -> bool:
        job = self.database.get_restore_job(job_id)
        if job is None or job["state"] not in {RestoreState.FAILED, RestoreState.CANCELLED}:
            return False
        source = Path(job["source_path"])
        upload = self.upload_path(job)
        # 已完整回传但发布失败：直接重新发布，不再占用 GPU。
        if (
            job.get("upload_sha256")
            and job.get("upload_size")
            and upload.is_file()
            and upload.stat().st_size == job["upload_size"]
            and source.is_file()
        ):
            return self.database.update_restore_job(
                job_id,
                expected_states={RestoreState.FAILED, RestoreState.CANCELLED},
                state=RestoreState.FINALIZING,
                last_error=None,
                finished_at=None,
            )
        if not source.is_file():
            self.database.update_restore_job(job_id, last_error=msg("restore.service.originalGone"))
            return False
        stat = source.stat()
        unchanged = (
            job.get("source_sha256")
            and stat.st_size == job["source_size"]
            and stat.st_mtime_ns == job["source_mtime_ns"]
        )
        return self.database.update_restore_job(
            job_id,
            expected_states={RestoreState.FAILED, RestoreState.CANCELLED},
            state=RestoreState.WAITING if unchanged else RestoreState.PREPARING,
            source_size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
            source_sha256=job["source_sha256"] if unchanged else None,
            last_error=None,
            finished_at=None,
            progress_json=None,
        )

    def purge_original(self, job_id: str) -> tuple[bool, str]:
        job = self.database.get_restore_job(job_id)
        if job is None or job["state"] != RestoreState.COMPLETED:
            return False, msg("restore.service.purgeCompletedOnly")
        if job.get("original_purged_at"):
            return False, msg("restore.service.alreadyPurged")
        backup = Path(job["backup_path"] or "")
        backup_dir = self.config.restore.backup_dir
        if not job.get("backup_path") or backup_dir is None:
            return False, msg("restore.service.noBackup")
        resolved = Path(os.path.abspath(backup))
        if Path(os.path.abspath(backup_dir)) not in resolved.parents:
            return False, msg("restore.service.backupOutside")
        resolved.unlink(missing_ok=True)
        # 删除空的上级目录，直到备份根目录为止。
        parent = resolved.parent
        root = Path(os.path.abspath(backup_dir))
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        self.database.update_restore_job(job_id, original_purged_at=utc_now_iso())
        return True, msg("restore.service.purged")


    async def process_once(self) -> bool:
        job = self.database.next_restore_job(RestoreState.FINALIZING)
        if job and await self._finalize(job):
            return True
        job = self.database.next_restore_job(RestoreState.PREPARING)
        if job:
            await asyncio.to_thread(self._prepare, job)
            return True
        return False

    def _prepare(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        source = Path(job["source_path"])
        try:
            if not source.is_file():
                raise RestoreError(msg("api.worker.sourceMissing"))
            stat = source.stat()

            def report(done: int, total: int) -> None:
                self._set_local_progress(job_id, "hashing", done_bytes=done, total_bytes=total)

            digest = _hash_file(source, report)
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise RestoreError(msg("restore.service.sourceChanged"))
            self.database.update_restore_job(
                job_id,
                expected_states={RestoreState.PREPARING},
                state=RestoreState.WAITING,
                source_sha256=digest,
                source_size=after.st_size,
                source_mtime_ns=after.st_mtime_ns,
                progress_json=None,
            )
        except Exception as exc:
            logger.exception("影片修复任务 %s 准备失败。", job_id)
            self.database.update_restore_job(
                job_id,
                expected_states={RestoreState.PREPARING},
                state=RestoreState.FAILED,
                last_error=msg("restore.service.prepareFailed", exc=exc),
                finished_at=utc_now_iso(),
                progress_json=None,
            )

    async def _finalize(self, job: dict[str, Any]) -> bool:
        subtitle_job = self._subtitle_job(job["media_id"])
        if subtitle_job and subtitle_job["state"] in SUBTITLE_STATES_USING_VIDEO:
            self._set_local_progress(job["id"], "waiting_subtitle", detail=msg("restore.service.waitingSubtitle"))
            return False
        try:
            result = await asyncio.to_thread(self._finalize_sync, job, subtitle_job)
        except Exception as exc:
            logger.exception("影片修复任务 %s 发布失败。", job["id"])
            self.database.update_restore_job(
                job["id"],
                expected_states={RestoreState.FINALIZING},
                state=RestoreState.FAILED,
                last_error=msg("restore.service.publishFailed", exc=exc),
                finished_at=utc_now_iso(),
                progress_json=None,
            )
            return True
        if result:
            self.on_published()
        return True

    def _subtitle_job(self, media_id: int) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id, state, subtitle_path FROM jobs WHERE media_id = ?", (media_id,)
            ).fetchone()
            return dict(row) if row else None

    def _finalize_sync(self, job: dict[str, Any], subtitle_job: dict[str, Any] | None) -> bool:
        job_id = job["id"]
        source = Path(job["source_path"])
        upload = self.upload_path(job)
        backup_dir = self.config.restore.backup_dir
        if backup_dir is None:
            raise RestoreError(msg("restore.service.noBackupDirShort"))
        if not upload.is_file():
            raise RestoreError(msg("restore.service.uploadMissing"))
        size = upload.stat().st_size
        if size != job.get("upload_size"):
            raise RestoreError(msg("restore.service.sizeMismatch", size=size, expected=job.get('upload_size')))

        def report(done: int, total: int) -> None:
            self._set_local_progress(job_id, "verifying", done_bytes=done, total_bytes=total)

        digest = _hash_file(upload, report)
        if digest != job.get("upload_sha256"):
            raise RestoreError(msg("restore.service.shaMismatch"))
        self._set_local_progress(job_id, "probing", detail=msg("restore.service.probing"))
        new_probe = run_ffprobe(upload)
        new_duration = duration_from_probe(new_probe)
        old_duration = job.get("duration")
        if old_duration is None and source.is_file():
            old_duration = duration_from_probe(run_ffprobe(source))
        if new_duration is None:
            raise RestoreError(msg("restore.service.probeFailed"))
        tolerance = self.config.restore.max_duration_difference_seconds
        if old_duration is not None and abs(new_duration - old_duration) > tolerance:
            raise RestoreError(
                msg("restore.service.durationMismatch", new_duration=f"{new_duration:.1f}", old_duration=f"{old_duration:.1f}", tolerance=f"{tolerance:g}")
            )
        if not any(stream.get("codec_type") == "video" for stream in new_probe.get("streams", [])):
            raise RestoreError(msg("restore.service.noVideo"))
        old_audio = 0
        if source.is_file():
            try:
                old_audio = sum(
                    1 for stream in run_ffprobe(source).get("streams", []) if stream.get("codec_type") == "audio"
                )
            except Exception:  # noqa: BLE001 - 原片探测失败不阻止发布
                old_audio = 0
        new_audio = sum(1 for stream in new_probe.get("streams", []) if stream.get("codec_type") == "audio")
        if old_audio and not new_audio:
            raise RestoreError(msg("restore.service.noAudio"))

        def stage(name: str) -> None:
            self._set_local_progress(job_id, "publishing", detail=name)

        library = self._library(str(job["library_id"]))
        result = finalize_restore(
            source=source,
            upload=upload,
            output_ext=job["output_extension"],
            library_root=library.local_path,
            library_name=library.name,
            backup_dir=backup_dir,
            video_extensions=self.config.scan.video_extensions,
            marker_payload={
                "tool": "jasna",
                "job_id": job_id,
                "worker": job.get("last_worker"),
                "restoration_model": job["restoration_model"],
                "codec": job["codec"],
                "source_sha256": job.get("source_sha256"),
                "output_sha256": digest,
                "worker_metadata": job.get("metadata") or {},
            },
            progress=stage,
        )
        stat = result.output_path.stat()
        subtitle_path = None
        if subtitle_job and subtitle_job.get("subtitle_path"):
            subtitle_path = relocated_path(subtitle_job["subtitle_path"], result, source)
        self.database.relocate_media(
            job["media_id"],
            path=result.output_path,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            file_key=f"{stat.st_dev}:{stat.st_ino}",
            subtitle_path=subtitle_path,
        )
        self.database.update_restore_job(
            job_id,
            expected_states={RestoreState.FINALIZING},
            state=RestoreState.COMPLETED,
            output_path=str(result.output_path),
            backup_path=str(result.backup_path),
            finished_at=utc_now_iso(),
            last_error=None,
            progress_json=json.dumps(
                {
                    "stage": "done",
                    "renamed": result.renamed,
                    "folder_renamed_to": result.folder_renamed_to,
                },
                ensure_ascii=False,
            ),
        )
        logger.info("影片修复任务 %s 已发布：%s", job_id, result.output_path)
        return True
