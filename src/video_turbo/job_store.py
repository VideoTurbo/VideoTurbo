"""字幕任务：创建、状态流转（开始、暂停、跳过、重新处理……）、列表、领取与租约。"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from . import languages
from .i18n import MARKER_CLOSE, MARKER_OPEN, msg
from .job_states import CANCELLABLE, PAUSABLE, pipeline_sources
from .languages import LanguagePair
from .list_status import SKIPPED_NOTE
from .models import (
    AsrMode,
    JobState,
    SourceKind,
    utc_now_iso,
)
from .naming import published_subtitle_path
from .store_base import StoreBase


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """“从头处理”时按新语言对重新选出的翻译来源。"""

    pair: LanguagePair
    source_kind: SourceKind
    stream_index: int | None
    source_path: str | None


# 列表排序：接口参数 → SQL。
JOB_SORTS = {
    "updated_desc": "jobs.updated_at DESC, jobs.id DESC",
    "updated_asc": "jobs.updated_at ASC, jobs.id ASC",
    "name_asc": "media_items.path COLLATE NOCASE ASC, jobs.id ASC",
    "name_desc": "media_items.path COLLATE NOCASE DESC, jobs.id DESC",
    "size_asc": "media_items.size ASC, jobs.id ASC",
    "size_desc": "media_items.size DESC, jobs.id DESC",
}

class JobStoreMixin(StoreBase):
    def recover_interrupted_jobs(self) -> int:
        """恢复因主控进程重启而停留在本地处理中间态的任务。"""
        now = utc_now_iso()
        recovered = 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT jobs.id, jobs.state, jobs.source_result_path, jobs.target_language,
                    media_items.path AS media_path, media_items.library_id
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                WHERE jobs.state IN (?, ?, ?, ?)
                """,
                (
                    JobState.EXTRACTING,
                    JobState.TRANSLATING,
                    JobState.VALIDATING,
                    JobState.PUBLISHING,
                ),
            ).fetchall()
            for row in rows:
                state = JobState(row["state"])
                output = published_subtitle_path(
                    Path(str(row["media_path"])), str(row["target_language"])
                )
                if state == JobState.PUBLISHING and self.files.exists(
                    str(row["library_id"]), str(output)
                ):
                    next_state = JobState.COMPLETED
                    subtitle_path: str | None = str(output)
                    error = None
                elif row["source_result_path"] and Path(str(row["source_result_path"])).is_file():
                    next_state = JobState.ASR_COMPLETED
                    subtitle_path = None
                    error = msg("jobs.store.recoveredTranslation")
                else:
                    next_state = JobState.DISCOVERED
                    subtitle_path = None
                    error = msg("jobs.store.recoveredPreparing")
                conn.execute(
                    """
                    UPDATE jobs SET state = ?, subtitle_path = ?, lease_owner = NULL,
                        lease_expires_at = NULL, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_state, subtitle_path, error, now, row["id"]),
                )
                recovered += 1
        return recovered

    def create_or_reset_job(
        self,
        *,
        media_id: int,
        source_kind: SourceKind,
        source_stream_index: int | None,
        source_path: str | None,
        changed: bool,
        pair: LanguagePair,
    ) -> tuple[str, bool]:
        """扫描发现需要字幕的影片时登记为“待处理”，由用户手动提交后才开始处理。

        语言对来自所属媒体库。尚未提交的任务（PENDING）每次扫描都按当前的语言对和来源刷新。
        """
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT jobs.*, media_items.library_id FROM jobs
                JOIN media_items ON media_items.id = jobs.media_id
                WHERE jobs.media_id = ?
                """,
                (media_id,),
            ).fetchone()
            if existing and existing["state"] == JobState.PENDING and not changed:
                # 取消后回到待处理的任务保留着进度；来源或语言对变了，旧进度就不能再用。
                same_source = (
                    existing["source_kind"], existing["source_stream_index"], existing["source_path"],
                    existing["source_language"], existing["target_language"],
                ) == (source_kind, source_stream_index, source_path, pair.source, pair.target)
                conn.execute(
                    """
                    UPDATE jobs SET source_kind = ?, source_stream_index = ?, source_path = ?,
                        source_language = ?, target_language = ?, updated_at = ?,
                        audio_path = CASE WHEN ? THEN audio_path ELSE NULL END,
                        audio_sha256 = CASE WHEN ? THEN audio_sha256 ELSE NULL END,
                        source_result_path = CASE WHEN ? THEN source_result_path ELSE NULL END,
                        target_result_path = CASE WHEN ? THEN target_result_path ELSE NULL END
                    WHERE id = ? AND state = ?
                    """,
                    (
                        source_kind,
                        source_stream_index,
                        source_path,
                        pair.source,
                        pair.target,
                        now,
                        same_source,
                        same_source,
                        same_source,
                        same_source,
                        existing["id"],
                        JobState.PENDING,
                    ),
                )
                return str(existing["id"]), False
            output_missing = False
            # 用户选了“不生成字幕”的任务没有字幕，不算输出丢失。
            if (
                existing
                and existing["state"] == JobState.COMPLETED
                and existing["last_error"] != SKIPPED_NOTE
            ):
                subtitle_path = existing["subtitle_path"]
                output_missing = not subtitle_path or not self.files.exists(
                    str(existing["library_id"]), str(subtitle_path)
                )
            # 扫描直接判定的任务没有识别来源，现在可以处理了就重建为待处理。
            scan_placeholder = bool(existing and existing["source_kind"] == SourceKind.NONE)

            # 媒体未变化时保留原任务；但已完成任务的输出被删除后应自动重建。
            if existing and not changed and not output_missing and not scan_placeholder:
                return str(existing["id"]), False
            if existing:
                conn.execute(
                    """
                    UPDATE jobs SET state = ?, source_kind = ?, source_stream_index = ?,
                        source_path = ?, source_language = ?, target_language = ?,
                        audio_path = NULL, audio_sha256 = NULL,
                        source_result_path = NULL, asr_duration_seconds = NULL,
                        target_result_path = NULL,
                        translation_stats_json = NULL,
                        reprocess_mode = NULL,
                        subtitle_path = NULL,
                        lease_owner = NULL, lease_expires_at = NULL, attempts = 0,
                        paused_from_state = NULL, last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        JobState.PENDING,
                        source_kind,
                        source_stream_index,
                        source_path,
                        pair.source,
                        pair.target,
                        now,
                        existing["id"],
                    ),
                )
                return str(existing["id"]), True

            job_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO jobs
                    (id, media_id, state, source_kind, source_stream_index, source_path,
                     source_language, target_language, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    media_id,
                    JobState.PENDING,
                    source_kind,
                    source_stream_index,
                    source_path,
                    pair.source,
                    pair.target,
                    now,
                    now,
                ),
            )
            return job_id, True

    def start_job(
        self,
        job_id: str,
        *,
        reusable_source_result_path: str | Path | None = None,
    ) -> JobState | None:
        """提交待处理的任务（含取消后回到待处理、带着进度的任务）或已跳过的任务。"""
        return self._continue_job(
            job_id,
            lambda job: job["state"] == JobState.PENDING
            or (
                job["state"] == JobState.COMPLETED
                and job["last_error"] == SKIPPED_NOTE
                and not job["subtitle_path"]
            ),
            reusable_source_result_path,
        )

    def skip_job(self, job_id: str) -> bool:
        """待处理的影片不生成字幕：直接记为已完成，之后仍可再提交。"""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET state = ?, last_error = ?, updated_at = ? WHERE id = ? AND state = ?",
                (JobState.COMPLETED, SKIPPED_NOTE, utc_now_iso(), job_id, JobState.PENDING),
            )
            return cursor.rowcount == 1

    def retry_job(
        self,
        job_id: str,
        *,
        reusable_source_result_path: str | Path | None = None,
    ) -> JobState | None:
        """重新提交任务；优先复用 ASR 结果，其次复用已提取音频。"""
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if (
                existing is None
                or existing["state"] != JobState.FAILED
                or existing["source_kind"] == SourceKind.NONE
            ):
                return None

            ja_candidates = [existing["source_result_path"], reusable_source_result_path]
            source_result_path = next(
                (
                    str(candidate)
                    for candidate in ja_candidates
                    if candidate and Path(str(candidate)).is_file()
                ),
                None,
            )
            can_reuse_asr = source_result_path is not None
            audio_path = existing["audio_path"]
            can_reuse_audio = bool(
                existing["source_kind"] == SourceKind.AUDIO
                and audio_path
                and Path(str(audio_path)).is_file()
                and existing["audio_sha256"]
            )
            if can_reuse_asr:
                next_state = JobState.ASR_COMPLETED
            elif can_reuse_audio:
                next_state = JobState.WAITING_ASR
            else:
                next_state = JobState.DISCOVERED
            conn.execute(
                """
                UPDATE jobs SET state = ?,
                    audio_path = CASE WHEN ? THEN audio_path ELSE NULL END,
                    audio_sha256 = CASE WHEN ? THEN audio_sha256 ELSE NULL END,
                    source_result_path = ?,
                    asr_duration_seconds =
                        CASE WHEN ? THEN asr_duration_seconds ELSE NULL END,
                    target_result_path = NULL, translation_stats_json = NULL,
                    subtitle_path = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, attempts = 0,
                    paused_from_state = NULL, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_state,
                    can_reuse_audio,
                    can_reuse_audio,
                    source_result_path,
                    can_reuse_asr,
                    now,
                    job_id,
                ),
            )
            return next_state

    def pause_job(self, job_id: str) -> bool:
        """暂停单个任务，并记录暂停前阶段供人工查看。"""
        pausable_states = sorted(PAUSABLE)
        now = utc_now_iso()
        with self.connect() as conn:
            placeholders = ", ".join("?" for _ in pausable_states)
            cursor = conn.execute(
                f"""
                UPDATE jobs SET paused_from_state = state, state = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND state IN ({placeholders})
                """,
                (JobState.PAUSED, msg("jobs.store.paused"), now, job_id, *pausable_states),
            )
            return cursor.rowcount == 1

    def resume_job(
        self,
        job_id: str,
        *,
        reusable_source_result_path: str | Path | None = None,
    ) -> JobState | None:
        """继续暂停任务；以磁盘上仍然有效的最近阶段为准恢复。"""
        return self._continue_job(
            job_id, lambda job: job["state"] == JobState.PAUSED, reusable_source_result_path
        )

    def _continue_job(
        self,
        job_id: str,
        accepts: Callable[[Any], bool],
        reusable_source_result_path: str | Path | None,
    ) -> JobState | None:
        """重新排队并保留磁盘上仍然有效的进度：有识别结果从翻译继续，有音频从识别继续。"""
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if existing is None or not accepts(existing):
                return None
            source_result_path = next(
                (
                    str(candidate)
                    for candidate in (existing["source_result_path"], reusable_source_result_path)
                    if candidate and Path(str(candidate)).is_file()
                ),
                None,
            )
            audio_path = existing["audio_path"]
            can_reuse_audio = bool(
                existing["source_kind"] == SourceKind.AUDIO
                and audio_path
                and Path(str(audio_path)).is_file()
                and existing["audio_sha256"]
            )
            if source_result_path:
                next_state = JobState.ASR_COMPLETED
            elif can_reuse_audio:
                next_state = JobState.WAITING_ASR
            else:
                next_state = JobState.DISCOVERED
            cursor = conn.execute(
                """
                UPDATE jobs SET state = ?,
                    audio_path = CASE WHEN ? THEN audio_path ELSE NULL END,
                    audio_sha256 = CASE WHEN ? THEN audio_sha256 ELSE NULL END,
                    source_result_path = ?,
                    asr_duration_seconds =
                        CASE WHEN ? THEN asr_duration_seconds ELSE NULL END,
                    lease_owner = NULL, lease_expires_at = NULL,
                    paused_from_state = NULL, last_error = NULL, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    next_state,
                    can_reuse_audio,
                    can_reuse_audio,
                    source_result_path,
                    source_result_path is not None,
                    now,
                    job_id,
                    existing["state"],
                ),
            )
            return next_state if cursor.rowcount == 1 else None

    def set_job_priority(self, job_id: str, priority: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ?",
                (priority, utc_now_iso(), job_id),
            )
            return cursor.rowcount == 1

    def save_paused_audio(self, job_id: str, audio_path: str | Path, audio_sha256: str) -> bool:
        """提取线程无法强制中断时，允许它把完整音频登记到已暂停或已取消（回到待处理）的任务。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET audio_path = ?, audio_sha256 = ?, updated_at = ?
                WHERE id = ? AND state IN (?, ?)
                """,
                (
                    str(audio_path), audio_sha256, utc_now_iso(), job_id,
                    JobState.PAUSED, JobState.PENDING,
                ),
            )
            return cursor.rowcount == 1

    def reprocess_job(
        self,
        job_id: str,
        mode: str,
        *,
        reusable_source_result_path: str | Path | None = None,
        reselected: SourceSelection | None = None,
    ) -> JobState | None:
        """按指定层级清除结果并重新排队；文件清理由 Orchestrator 完成。

        reselected 只用于“从头处理”：按媒体库当前语言对重新选择的来源和语言对。
        """
        if mode == "continue":
            existing = self.get_job(job_id)
            if existing and existing["state"] == JobState.PAUSED:
                return self.resume_job(job_id, reusable_source_result_path=reusable_source_result_path)
            return self.retry_job(job_id, reusable_source_result_path=reusable_source_result_path)
        if mode not in {"retranslate", "reasr", "full"}:
            return None

        allowed_states = {
            JobState.ASR_COMPLETED,
            JobState.WAITING_TRANSLATION,
            JobState.FAILED,
            JobState.PAUSED,
            JobState.COMPLETED,
        }
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT jobs.*, media_items.path AS media_path, media_items.library_id
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                WHERE jobs.id = ?
                """,
                (job_id,),
            ).fetchone()
            if (
                existing is None
                or existing["state"] not in allowed_states
                or existing["source_kind"] == SourceKind.NONE
            ):
                return None
            source_result_path = next(
                (
                    str(candidate)
                    for candidate in (existing["source_result_path"], reusable_source_result_path)
                    if candidate and Path(str(candidate)).is_file()
                ),
                None,
            )
            audio_path = existing["audio_path"]
            can_reuse_audio = bool(
                existing["source_kind"] == SourceKind.AUDIO
                and audio_path
                and Path(str(audio_path)).is_file()
                and existing["audio_sha256"]
            )
            # 已完成的任务重新处理时，保留旧字幕引用。
            # 发布阶段只允许原子替换这个任务明确接管的目标文件。
            expected_subtitle = published_subtitle_path(
                Path(str(existing["media_path"])), str(existing["target_language"])
            )
            managed_subtitle_candidates = [existing["subtitle_path"]]
            if existing["state"] == JobState.COMPLETED:
                managed_subtitle_candidates.append(expected_subtitle)
            managed_subtitle_path = next(
                (
                    str(candidate)
                    for candidate in managed_subtitle_candidates
                    if candidate and self.files.exists(str(existing["library_id"]), str(candidate))
                ),
                None,
            )
            if mode == "retranslate":
                if not source_result_path:
                    return None
                next_state = JobState.ASR_COMPLETED
            elif mode == "reasr":
                next_state = JobState.WAITING_ASR if can_reuse_audio else JobState.DISCOVERED
                source_result_path = None
            else:
                next_state = JobState.DISCOVERED
                source_result_path = None
                can_reuse_audio = False
            keep_previous_translation = mode == "retranslate"
            if mode == "full" and reselected is not None:
                conn.execute(
                    """
                    UPDATE jobs SET source_kind = ?, source_stream_index = ?, source_path = ?,
                        source_language = ?, target_language = ?
                    WHERE id = ?
                    """,
                    (
                        reselected.source_kind,
                        reselected.stream_index,
                        reselected.source_path,
                        reselected.pair.source,
                        reselected.pair.target,
                        job_id,
                    ),
                )

            conn.execute(
                """
                UPDATE jobs SET state = ?,
                    audio_path = CASE WHEN ? THEN audio_path ELSE NULL END,
                    audio_sha256 = CASE WHEN ? THEN audio_sha256 ELSE NULL END,
                    source_result_path = ?,
                    asr_duration_seconds =
                        CASE WHEN ? THEN asr_duration_seconds ELSE NULL END,
                    target_result_path = CASE WHEN ? THEN target_result_path ELSE NULL END,
                    translation_stats_json =
                        CASE WHEN ? THEN translation_stats_json ELSE NULL END,
                    reprocess_mode = ?, subtitle_path = ?,
                    lease_owner = NULL, lease_expires_at = NULL, attempts = 0,
                    paused_from_state = NULL, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_state,
                    can_reuse_audio,
                    can_reuse_audio,
                    source_result_path,
                    keep_previous_translation,
                    keep_previous_translation,
                    keep_previous_translation,
                    mode,
                    managed_subtitle_path,
                    now,
                    job_id,
                ),
            )
            return next_state

    def mark_reprocess_started(self, job_id: str, mode: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET reprocess_mode = ?, updated_at = ?
                WHERE id = ? AND state = ? AND reprocess_mode = ?
                """,
                (f"{mode}_running", utc_now_iso(), job_id, JobState.TRANSLATING, mode),
            )
            return cursor.rowcount == 1

    def cancel_job(self, job_id: str) -> bool:
        """取消已提交的任务：回到待处理并保留已有进度，再次提交时从可用进度继续。"""
        cancellable_states = sorted(CANCELLABLE)
        now = utc_now_iso()
        with self.connect() as conn:
            placeholders = ", ".join("?" for _ in cancellable_states)
            cursor = conn.execute(
                f"""
                UPDATE jobs SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                    reprocess_mode = NULL, paused_from_state = NULL, last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND state IN ({placeholders})
                """,
                (JobState.PENDING, now, job_id, *cancellable_states),
            )
            return cursor.rowcount == 1

    def record_scan_outcome(
        self,
        *,
        media_id: int,
        outcome: Literal["done", "unusable"],
        reason: str,
        pair: LanguagePair,
        subtitle_path: str | None = None,
    ) -> bool:
        """登记扫描判定不需要或无法生成字幕的影片，让它们也出现在任务列表里。

        已有目标字幕（done）记为已完成：没有任务就新建，未结束的任务同步结束。
        找不到可用识别来源（unusable）记为失败：只处理没有任务和尚未提交的任务，
        已提交的任务沿用当时选定的来源。扫描新建的任务来源为 NONE，不能重新处理；
        之后扫描判定可以处理时，create_or_reset_job 会把它改回待处理。
        返回任务是否新建或改变了状态。
        """
        if outcome == "done":
            target_state = JobState.COMPLETED
            open_states = {
                JobState.PENDING,
                JobState.DISCOVERED,
                JobState.EXTRACTING,
                JobState.WAITING_ASR,
                JobState.TRANSCRIBING,
                JobState.ASR_COMPLETED,
                JobState.WAITING_TRANSLATION,
                JobState.TRANSLATING,
                JobState.VALIDATING,
                JobState.FAILED,
                JobState.PAUSED,
            }
        else:
            target_state = JobState.FAILED
            subtitle_path = None
            open_states = {JobState.PENDING}
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, state, source_kind, reprocess_mode FROM jobs WHERE media_id = ?",
                (media_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO jobs
                        (id, media_id, state, source_kind, source_language, target_language,
                         subtitle_path, last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        media_id,
                        target_state,
                        SourceKind.NONE,
                        pair.source,
                        pair.target,
                        subtitle_path,
                        reason,
                        now,
                        now,
                    ),
                )
                return True
            if existing["source_kind"] == SourceKind.NONE:
                conn.execute(
                    """
                    UPDATE jobs SET state = ?, source_language = ?, target_language = ?,
                        subtitle_path = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target_state, pair.source, pair.target, subtitle_path, reason, now, existing["id"]),
                )
                return existing["state"] != target_state
            if existing["state"] not in open_states or existing["reprocess_mode"] is not None:
                return False
            conn.execute(
                """
                UPDATE jobs SET state = ?, subtitle_path = COALESCE(?, subtitle_path),
                    lease_owner = NULL, lease_expires_at = NULL, paused_from_state = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_state, subtitle_path, reason, now, existing["id"]),
            )
            return existing["state"] != target_state

    def complete_jobs_with_existing_subtitles(
        self, job_ids: list[str]
    ) -> tuple[list[str], dict[str, str]]:
        """仅在所有任务都有效且目标 SRT 存在时，原子地将整批任务标为完成。"""
        unique_ids = list(dict.fromkeys(job_ids))
        invalid: dict[str, str] = {}
        subtitle_paths: dict[str, str] = {}
        with self.connect() as conn:
            # 阻止资格校验与批量更新之间出现任务状态变化。
            conn.execute("BEGIN IMMEDIATE")
            rows: dict[str, sqlite3.Row] = {}
            # 避免依赖 SQLite 构建版本的 SQL 变量数量上限。
            for job_id in unique_ids:
                row = conn.execute(
                    """
                    SELECT jobs.id, jobs.state, jobs.target_language,
                        media_items.path AS media_path, media_items.library_id
                    FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                    WHERE jobs.id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if row is not None:
                    rows[job_id] = row

            for job_id in unique_ids:
                row = rows.get(job_id)
                if row is None:
                    invalid[job_id] = msg("api.jobNotFound")
                    continue
                if row["state"] == JobState.COMPLETED:
                    invalid[job_id] = msg("jobs.store.alreadyCompleted")
                    continue
                subtitle = published_subtitle_path(
                    Path(str(row["media_path"])), str(row["target_language"])
                )
                if not self.files.exists(str(row["library_id"]), str(subtitle)):
                    invalid[job_id] = msg("jobs.store.missingSrt", subtitle=subtitle)
                    continue
                subtitle_paths[job_id] = str(subtitle)

            if invalid:
                return [], invalid

            now = utc_now_iso()
            for job_id in unique_ids:
                conn.execute(
                    """
                    UPDATE jobs SET state = ?, subtitle_path = ?, lease_owner = NULL,
                        lease_expires_at = NULL, reprocess_mode = NULL,
                        paused_from_state = NULL,
                        last_error = ?, updated_at = ?
                    WHERE id = ? AND state <> ?
                    """,
                    (
                        JobState.COMPLETED,
                        subtitle_paths[job_id],
                        msg("jobs.store.completedExisting"),
                        now,
                        job_id,
                        JobState.COMPLETED,
                    ),
                )
            return unique_ids, {}

    def set_job_state(
        self,
        job_id: str,
        state: JobState,
        *,
        error: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> bool:
        allowed_fields = {
            "audio_path",
            "audio_sha256",
            "fetch_media",
            "source_result_path",
            "asr_duration_seconds",
            "target_result_path",
            "translation_stats_json",
            "reprocess_mode",
            "subtitle_path",
            "lease_owner",
            "lease_expires_at",
        }
        updates: dict[str, Any] = {
            "state": state,
            "last_error": error,
            "updated_at": utc_now_iso(),
        }
        for key, value in (fields or {}).items():
            if key not in allowed_fields:
                raise ValueError(f"不允许更新任务字段：{key}")
            updates[key] = value
        assignment = ", ".join(f"{key} = ?" for key in updates)
        # 只允许 job_states.PIPELINE_TRANSITIONS 中的变化；条件写在 WHERE 中，判断与写入是同一条语句。
        sources = sorted(pipeline_sources(state))
        if not sources:
            return False
        placeholders = ", ".join("?" for _ in sources)
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {assignment} WHERE id = ? AND state IN ({placeholders})",
                (*updates.values(), job_id, *sources),
            )
            return cursor.rowcount == 1

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT jobs.*, media_items.path AS media_path, media_items.duration,
                       media_items.library_id,
                       workers.asr_completed_chunks, workers.asr_total_chunks,
                       workers.asr_elapsed_seconds, workers.asr_eta_seconds, workers.asr_stage,
                       workers.asr_mode AS worker_asr_mode,
                       workers.current_job_id AS worker_current_job_id,
                       workers.asr_whisperjav_progress_json
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                LEFT JOIN workers ON workers.name = jobs.lease_owner
                WHERE jobs.id = ?
                """,
                (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_jobs(
        self,
        limit: int = 100,
        *,
        offset: int = 0,
        states: set[JobState] | None = None,
        query: str | None = None,
        library_id: str | None = None,
        sort: str = "updated_desc",
    ) -> list[dict[str, Any]]:
        where_clause, parameters = self._job_filter_sql(
            states=states, query=query, library_id=library_id
        )
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT jobs.*, media_items.path AS media_path, media_items.library_id,
                       media_items.size AS media_size,
                       media_items.poster_path, media_items.poster_mtime_ns,
                       media_items.poster_media_path, media_items.poster_checked_at,
                       workers.asr_completed_chunks, workers.asr_total_chunks,
                       workers.asr_elapsed_seconds, workers.asr_eta_seconds, workers.asr_stage,
                       workers.asr_mode AS worker_asr_mode,
                       workers.current_job_id AS worker_current_job_id,
                       workers.asr_whisperjav_progress_json
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                LEFT JOIN workers ON workers.name = jobs.lease_owner
                {where_clause}
                ORDER BY {JOB_SORTS[sort]}
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _job_filter_sql(
        *,
        states: set[JobState] | None = None,
        query: str | None = None,
        library_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if states:
            ordered_states = sorted(str(state) for state in states)
            placeholders = ", ".join("?" for _ in ordered_states)
            clauses.append(f"jobs.state IN ({placeholders})")
            parameters.extend(ordered_states)
        normalized_query = (query or "").strip().lower()
        if normalized_query:
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            clauses.append(
                "(LOWER(media_items.path) LIKE ? ESCAPE '\\' "
                "OR LOWER(jobs.id) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(jobs.last_error, '')) LIKE ? ESCAPE '\\')"
            )
            parameters.extend((pattern, pattern, pattern))
        if library_id:
            clauses.append("media_items.library_id = ?")
            parameters.append(library_id)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", parameters)

    def count_jobs(
        self,
        states: set[JobState] | None = None,
        *,
        query: str | None = None,
        library_id: str | None = None,
    ) -> int:
        where_clause, parameters = self._job_filter_sql(
            states=states, query=query, library_id=library_id
        )
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                {where_clause}
                """,
                parameters,
            ).fetchone()
            return int(row["count"])

    def job_counts(
        self, *, query: str | None = None, library_id: str | None = None
    ) -> dict[str, int]:
        """各内部状态的任务数，按列表的搜索和媒体库条件筛选。"""
        where_clause, parameters = self._job_filter_sql(query=query, library_id=library_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT jobs.state, COUNT(*) AS count
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                {where_clause}
                GROUP BY jobs.state
                """,
                parameters,
            ).fetchall()
            return {str(row["state"]): int(row["count"]) for row in rows}

    def next_job_in_state(self, state: JobState) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT jobs.*, media_items.path AS media_path, media_items.duration,
                    media_items.library_id
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                WHERE jobs.state = ? ORDER BY jobs.priority DESC, jobs.created_at LIMIT 1
                """,
                (state,),
            ).fetchone()
            return dict(row) if row else None

    def claim_asr_job(
        self,
        *,
        worker_name: str,
        lease_seconds: int,
        asr_mode: AsrMode | None = None,
        whisperjav_ensemble: bool = False,
    ) -> dict[str, Any] | None:
        """领取下一条待识别任务。

        网盘媒体库的任务（fetch_media）没有主程序抽好的音频，由 GPU 机器直接下载影片抽音频；
        不能这样做的 GPU 机器（没有 ffmpeg）不领取这些任务。

        whisperjav_ensemble 是控制台设置；只有源语言支持双遍（日语）的任务才真正双遍，
        结果里的 whisperjav_ensemble 是该任务实际使用的值。不支持双遍的 Worker
        仍可领取单遍任务（非日语任务，或控制台关闭了双遍）。
        """
        now = datetime.now(UTC)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        ensemble_languages = languages.ensemble_languages()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            single_pass_only = False
            worker = conn.execute("SELECT * FROM workers WHERE name = ?", (worker_name,)).fetchone()
            fetch_media = bool(worker and worker["supports_media_fetch"])
            if asr_mode is not None:
                if worker is None or asr_mode not in self._worker_dict(worker)["supported_asr_modes"]:
                    return None
                single_pass_only = bool(
                    asr_mode == "whisperjav"
                    and whisperjav_ensemble
                    and not worker["supports_whisperjav_ensemble"]
                )
            paused = conn.execute(
                "SELECT value FROM runtime_settings WHERE key = 'pipeline_paused'"
            ).fetchone()
            if paused and paused["value"] == "1":
                return None
            language_filter = ""
            parameters: list[Any] = [JobState.WAITING_ASR]
            if single_pass_only:
                placeholders = ", ".join("?" for _ in ensemble_languages)
                language_filter = f"AND jobs.source_language NOT IN ({placeholders})"
                parameters.extend(ensemble_languages)
            if not fetch_media:
                language_filter += " AND jobs.fetch_media = 0"
            row = conn.execute(
                f"""
                SELECT jobs.*, media_items.path AS media_path, media_items.library_id
                FROM jobs JOIN media_items ON media_items.id = jobs.media_id
                WHERE jobs.state = ? {language_filter}
                ORDER BY jobs.priority DESC, jobs.created_at LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE jobs SET state = ?, lease_owner = ?, lease_expires_at = ?,
                    attempts = attempts + 1, last_error = NULL, updated_at = ? WHERE id = ?
                """,
                (JobState.TRANSCRIBING, worker_name, lease_until, utc_now_iso(), row["id"]),
            )
            result = dict(row)
            result["lease_owner"] = worker_name
            result["lease_expires_at"] = lease_until
            result["whisperjav_ensemble"] = bool(
                asr_mode == "whisperjav"
                and whisperjav_ensemble
                and row["source_language"] in ensemble_languages
            )
            return result

    def renew_lease(self, job_id: str, worker_name: str, lease_seconds: int) -> bool:
        lease_until = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state = ?
                """,
                (lease_until, utc_now_iso(), job_id, worker_name, JobState.TRANSCRIBING),
            )
            return cursor.rowcount == 1

    def record_asr_failure(self, job_id: str, worker_name: str, error: str) -> bool:
        """Record this attempt's failure without changing the existing retry schedule."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET last_error = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state = ?
                """,
                (
                    msg("jobs.store.asrFailed", worker_name=worker_name, error=error), utc_now_iso(),
                    job_id, worker_name, JobState.TRANSCRIBING,
                ),
            )
            return cursor.rowcount == 1

    def release_expired_leases(self) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                    last_error = CASE WHEN last_error IS NOT NULL AND last_error <> ''
                        THEN last_error || char(10) || ?
                        -- 每行的机器名不同：在 SQL 里拼出 msg() 的标记（格式见 video_turbo.i18n）。
                        ELSE ? || json_object('k', 'jobs.store.timeout',
                            'p', json_object('machine', COALESCE(lease_owner, ?))) || ?
                        END, updated_at = ?
                WHERE state = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (
                    JobState.WAITING_ASR,
                    msg("jobs.store.timeoutAppend"),
                    MARKER_OPEN,
                    msg("jobs.store.unknownMachine"),
                    MARKER_CLOSE,
                    now,
                    JobState.TRANSCRIBING,
                    now,
                ),
            )
            return cursor.rowcount
