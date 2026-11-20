"""影片修复任务的 SQLite 存取，作为 Database 的 mixin 使用。

影片修复任务与字幕任务互相独立：一部影片可以有多次影片修复记录（失败、取消后重新提交），
但同一时间最多只有一个进行中的任务。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .i18n import msg
from .models import (
    RESTORE_ACTIVE_STATES,
    RESTORE_WORKER_STATES,
    RestoreProgress,
    RestoreSettings,
    RestoreState,
    is_ltx_model,
    utc_now_iso,
)
from .settings_store import PIPELINE_PAUSED_KEY
from .source_paths import text as path_text
from .store_base import StoreBase

# 列表排序：接口参数 → SQL。
RESTORE_MEDIA_SORTS = {
    "updated_desc": "COALESCE(latest.updated_at, media_items.created_at) DESC, media_items.id DESC",
    "updated_asc": "COALESCE(latest.updated_at, media_items.created_at) ASC, media_items.id ASC",
    "name_asc": "media_items.path COLLATE NOCASE ASC",
    "name_desc": "media_items.path COLLATE NOCASE DESC",
    "size_asc": "media_items.size ASC, media_items.id ASC",
    "size_desc": "media_items.size DESC, media_items.id DESC",
}


def machine_key(worker_name: str) -> str:
    """同机多实例（字幕 cloud5090-01、cloud5090-02，影片修复 cloud5090-dc）属于同一台机器。"""
    return re.sub(r"[-_](?:\d{1,3}|dc)$", "", worker_name)


_ACTIVE_VALUES = tuple(str(state) for state in RESTORE_ACTIVE_STATES)
_WORKER_VALUES = tuple(str(state) for state in RESTORE_WORKER_STATES)
_STAGE_TO_STATE = {
    "downloading": RestoreState.DOWNLOADING,
    "preparing": RestoreState.PROCESSING,
    "queued": RestoreState.DOWNLOADING,
    "processing": RestoreState.PROCESSING,
    "uploading": RestoreState.UPLOADING,
}
RESTORE_VIEWS: dict[str, tuple[str, ...]] = {
    "active": _ACTIVE_VALUES,
    "error": (RestoreState.FAILED,),
    "done": (RestoreState.COMPLETED, RestoreState.CANCELLED),
}

RESTORE_KEY = "restore"


def _placeholders(values: tuple[Any, ...]) -> str:
    return ", ".join("?" for _ in values)


class RestoreStoreMixin(StoreBase):
    def restore_settings(self) -> RestoreSettings:
        return self.model_setting(RESTORE_KEY, RestoreSettings)

    def set_restore_settings(self, settings: RestoreSettings) -> None:
        self.set_model_setting(RESTORE_KEY, settings)

    def get_media(self, media_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM media_items WHERE id = ?", (media_id,)).fetchone()
            return dict(row) if row else None

    def iter_restore_media(
        self,
        *,
        query: str | None = None,
        library_id: str | None = None,
        sort: str = "updated_desc",
    ) -> Iterator[dict[str, Any]]:
        """媒体列表及每部影片最近一次影片修复任务。

        “更新时间”取最近一次任务的更新时间；还没有任务的影片取入库时间，新发现的影片排在前面。
        """
        clauses: list[str] = []
        parameters: list[Any] = []
        if query:
            clauses.append("media_items.path LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped}%")
        if library_id:
            clauses.append("media_items.library_id = ?")
            parameters.append(library_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT media_items.id AS media_id, media_items.path, media_items.library_id,
                       media_items.size, media_items.duration,
                       COALESCE(latest.updated_at, media_items.created_at) AS updated_at,
                       media_items.poster_path, media_items.poster_mtime_ns,
                       media_items.poster_media_path, media_items.poster_checked_at,
                       latest.id AS restore_job_id, latest.state AS restore_state,
                       latest.output_path AS restore_output_path,
                       latest.backup_path AS restore_backup_path,
                       latest.original_purged_at AS restore_original_purged_at
                FROM media_items
                LEFT JOIN restore_jobs AS latest ON latest.id = (
                    SELECT id FROM restore_jobs
                    WHERE restore_jobs.media_id = media_items.id
                    ORDER BY created_at DESC LIMIT 1
                )
                {where}
                ORDER BY {RESTORE_MEDIA_SORTS[sort]}
                """,
                parameters,
            ).fetchall()
        for row in rows:
            yield dict(row)

    def relocate_media(
        self,
        media_id: int,
        *,
        path: Path,
        size: int,
        mtime_ns: int,
        file_key: str | None,
        subtitle_path: Path | None = None,
    ) -> None:
        """影片修复发布后让媒体记录与字幕任务跟随新文件，避免下次扫描当作新影片。"""
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE media_items SET path = ?, size = ?, mtime_ns = ?, file_key = ?,
                    probe_json = '{}', updated_at = ?
                WHERE id = ?
                """,
                (path_text(path), size, mtime_ns, file_key, now, media_id),
            )
            if subtitle_path is not None:
                conn.execute(
                    "UPDATE jobs SET subtitle_path = ?, updated_at = ? WHERE media_id = ?",
                    (path_text(subtitle_path), now, media_id),
                )


    def latest_restore_job(self, media_id: int) -> dict[str, Any] | None:
        """影片最近一次影片修复任务；没有任务时返回 None。"""
        with self.connect() as conn:
            row = conn.execute(
                self._JOB_SELECT
                + " WHERE restore_jobs.media_id = ? ORDER BY restore_jobs.created_at DESC LIMIT 1",
                (media_id,),
            ).fetchone()
            return self._restore_job_dict(row) if row else None

    def create_restore_job(
        self,
        *,
        media_id: int,
        source_path: Path,
        source_size: int,
        source_mtime_ns: int,
        restoration_model: str,
        codec: str,
        output_extension: str,
        ltx_large_canvas: bool = False,
    ) -> str | None:
        """创建影片修复任务；该影片已有进行中的任务时返回 None。"""
        job_id = uuid.uuid4().hex
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                f"SELECT 1 FROM restore_jobs WHERE media_id = ? AND state IN ({_placeholders(_ACTIVE_VALUES)})",
                (media_id, *_ACTIVE_VALUES),
            ).fetchone()
            if active:
                return None
            conn.execute(
                """
                INSERT INTO restore_jobs
                    (id, media_id, state, source_path, source_size, source_mtime_ns,
                     restoration_model, codec, output_extension, ltx_large_canvas, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, media_id, RestoreState.PREPARING, path_text(source_path), source_size,
                    source_mtime_ns, restoration_model, codec, output_extension,
                    int(ltx_large_canvas and is_ltx_model(restoration_model)), now, now,
                ),
            )
        return job_id

    _JOB_SELECT = """
        SELECT restore_jobs.*, media_items.path AS media_path, media_items.library_id,
               media_items.duration,
               media_items.poster_path, media_items.poster_mtime_ns,
               media_items.poster_media_path, media_items.poster_checked_at,
               workers.restore_progress_json AS worker_progress_json_current,
               workers.current_job_id AS worker_current_job_id,
               workers.task_kind AS worker_task_kind
        FROM restore_jobs JOIN media_items ON media_items.id = restore_jobs.media_id
        LEFT JOIN workers ON workers.name = restore_jobs.lease_owner
    """

    @staticmethod
    def _restore_job_dict(row: sqlite3.Row) -> dict[str, Any]:
        job = dict(row)
        progress = None
        worker_raw = job.pop("worker_progress_json_current", None)
        own_raw = job.pop("worker_progress_json", None)
        raw = own_raw
        if (
            raw is None
            and worker_raw
            and job.get("worker_task_kind") == "restore"
            and job.get("worker_current_job_id") == job["id"]
        ):
            raw = worker_raw
        if raw and job["state"] in _WORKER_VALUES and job.get("lease_owner"):
            try:
                progress = RestoreProgress.model_validate_json(raw).model_dump()
            except ValueError:
                progress = None
        local = job.pop("progress_json", None)
        job["local_progress"] = json.loads(local) if local else None
        job["progress"] = progress
        metadata = job.pop("metadata_json", None)
        job["metadata"] = json.loads(metadata) if metadata else None
        job.pop("worker_current_job_id", None)
        job.pop("worker_task_kind", None)
        return job

    def get_restore_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(self._JOB_SELECT + " WHERE restore_jobs.id = ?", (job_id,)).fetchone()
            return self._restore_job_dict(row) if row else None

    def list_restore_jobs(
        self, *, view: str = "active", limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        states = RESTORE_VIEWS.get(view)
        where = f"WHERE restore_jobs.state IN ({_placeholders(states)})" if states else ""
        with self.connect() as conn:
            rows = conn.execute(
                self._JOB_SELECT
                + f"""
                {where}
                ORDER BY CASE WHEN restore_jobs.state IN ({_placeholders(_WORKER_VALUES)}, 'FINALIZING')
                    THEN 0 ELSE 1 END, restore_jobs.updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (*(states or ()), *_WORKER_VALUES, limit, offset),
            ).fetchall()
            return [self._restore_job_dict(row) for row in rows]

    def restore_job_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM restore_jobs GROUP BY state"
            ).fetchall()
            return {row["state"]: row["count"] for row in rows}

    def update_restore_job(
        self,
        job_id: str,
        *,
        expected_states: set[RestoreState] | None = None,
        **fields: Any,
    ) -> bool:
        if not fields:
            return False
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values: list[Any] = list(fields.values())
        sql = f"UPDATE restore_jobs SET {assignments} WHERE id = ?"
        values.append(job_id)
        if expected_states:
            states = tuple(str(state) for state in expected_states)
            sql += f" AND state IN ({_placeholders(states)})"
            values.extend(states)
        with self.connect() as conn:
            return conn.execute(sql, values).rowcount == 1

    def next_restore_job(self, state: RestoreState) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                self._JOB_SELECT
                + " WHERE restore_jobs.state = ? ORDER BY restore_jobs.created_at LIMIT 1",
                (state,),
            ).fetchone()
            return self._restore_job_dict(row) if row else None

    def claim_restore_job(
        self,
        *,
        worker_name: str,
        lease_seconds: int,
        prefetch: bool = False,
        heartbeat_timeout_seconds: int = 90,
    ) -> dict[str, Any] | None:
        """领取影片修复任务。prefetch=True 表示 Worker 正在修复别的影片、想提前下载下一部：
        只要其他机器上还有空闲的影片修复 Worker，就让给它们，避免任务在本机排队而别处闲着。"""
        now = datetime.now(UTC)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT supports_restore, restore_models_json FROM workers WHERE name = ?",
                (worker_name,),
            ).fetchone()
            if worker is None or not worker["supports_restore"]:
                return None
            # 与领取在同一事务中读取，避免暂停后仍派出任务。
            paused = conn.execute(
                "SELECT value FROM runtime_settings WHERE key = ?", (PIPELINE_PAUSED_KEY,)
            ).fetchone()
            if paused and paused["value"] == "1":
                return None
            if prefetch and self._other_machine_idle(conn, worker_name, now, heartbeat_timeout_seconds):
                return None
            models = set(json.loads(worker["restore_models_json"] or '["basicvsrpp"]'))
            machine = machine_key(worker_name)
            now_text = now.isoformat()
            rows = conn.execute(
                "SELECT * FROM restore_jobs WHERE state = ? ORDER BY created_at",
                (RestoreState.WAITING,),
            ).fetchall()

            def usable(candidate: sqlite3.Row) -> bool:
                if not (
                    candidate["restoration_model"] in models
                    or (is_ltx_model(candidate["restoration_model"]) and "ltx" in models)
                ):
                    return False
                reserved = candidate["reserved_machine"]
                # 为其他机器保留中的任务（那台机器上有续传所需的本地文件）暂不领取。
                return not reserved or reserved == machine or (candidate["reserved_until"] or "") <= now_text

            usable_rows = [candidate for candidate in rows if usable(candidate)]
            # 本机中断的任务优先，其余按提交顺序。
            row = next(
                (candidate for candidate in usable_rows if candidate["reserved_machine"] == machine),
                usable_rows[0] if usable_rows else None,
            )
            if row is None:
                return None
            # 换了 Worker 时，上一位 Worker 回传的半截文件不能续传。
            reset_upload = row["upload_owner"] not in (None, worker_name)
            conn.execute(
                """
                UPDATE restore_jobs SET state = ?, lease_owner = ?, lease_expires_at = ?,
                    last_worker = ?, attempts = attempts + 1, last_error = NULL,
                    started_at = COALESCE(started_at, ?), updated_at = ?,
                    upload_owner = ?, upload_size = NULL, upload_sha256 = NULL,
                    worker_progress_json = NULL, reserved_machine = NULL, reserved_until = NULL
                WHERE id = ?
                """,
                (
                    RestoreState.DOWNLOADING, worker_name, lease_until, worker_name,
                    utc_now_iso(), utc_now_iso(), worker_name, row["id"],
                ),
            )
            result = dict(row)
            result["reset_upload"] = reset_upload
            result["lease_owner"] = worker_name
            return result

    @staticmethod
    def _other_machine_idle(
        conn: sqlite3.Connection, worker_name: str, now: datetime, timeout_seconds: int
    ) -> bool:
        cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
        machine = machine_key(worker_name)
        rows = conn.execute(
            """
            SELECT name FROM workers
            WHERE supports_restore = 1 AND current_job_id IS NULL
                AND status IN ('online', 'busy') AND last_seen_at >= ?
            """,
            (cutoff,),
        ).fetchall()
        return any(machine_key(row["name"]) != machine for row in rows)

    def renew_restore_lease(
        self,
        job_id: str,
        worker_name: str,
        lease_seconds: int,
        progress: RestoreProgress | None,
    ) -> bool:
        lease_until = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        state = _STAGE_TO_STATE.get(progress.stage) if progress else None
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE restore_jobs SET lease_expires_at = ?, state = COALESCE(?, state), updated_at = ?,
                    worker_progress_json = COALESCE(?, worker_progress_json)
                WHERE id = ? AND lease_owner = ? AND state IN ({_placeholders(_WORKER_VALUES)})
                """,
                (
                    lease_until, state, utc_now_iso(),
                    progress.model_dump_json() if progress else None,
                    job_id, worker_name, *_WORKER_VALUES,
                ),
            )
            return cursor.rowcount == 1

    def restore_cancel_requested(self, job_id: str, worker_name: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state, lease_owner FROM restore_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return row is None or row["lease_owner"] != worker_name or row["state"] not in _WORKER_VALUES

    def is_restore_lease_holder(self, job_id: str, worker_name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM restore_jobs WHERE id = ? AND lease_owner = ? AND state IN ({_placeholders(_WORKER_VALUES)})",
                (job_id, worker_name, *_WORKER_VALUES),
            ).fetchone()
            return dict(row) if row else None

    def record_restore_failure(self, job_id: str, worker_name: str, error: str) -> bool:
        """Worker 明确报告失败：影片修复耗时很长，不自动重试，等待人工点“重试”。"""
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE restore_jobs SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?, finished_at = ?
                WHERE id = ? AND lease_owner = ? AND state IN ({_placeholders(_WORKER_VALUES)})
                """,
                (
                    RestoreState.FAILED, msg("restore.store.failed", worker_name=worker_name, error=error), now, now,
                    job_id, worker_name, *_WORKER_VALUES,
                ),
            )
            return cursor.rowcount == 1

    def _requeue_leased(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        reservation_minutes: int,
        reason: str,
    ) -> int:
        """把租约中的任务放回队列，并为原机器保留一段时间以便续传。"""
        now = datetime.now(UTC)
        until = (now + timedelta(minutes=reservation_minutes)).isoformat() if reservation_minutes else None
        for row in rows:
            owner = row["lease_owner"] or msg("jobs.store.unknownMachine")
            machine = machine_key(owner) if until else None
            note = msg("restore.store.reserved", reservation_minutes=reservation_minutes, machine=machine) if until else ""
            conn.execute(
                """
                UPDATE restore_jobs SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                    reserved_machine = ?, reserved_until = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    RestoreState.WAITING, machine, until,
                    msg("restore.store.requeued", owner=owner, reason=reason, note=note), now.isoformat(), row["id"],
                ),
            )
        return len(rows)

    def release_expired_restore_leases(self, reservation_minutes: int = 0) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT id, lease_owner FROM restore_jobs
                WHERE state IN ({_placeholders(_WORKER_VALUES)})
                    AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (*_WORKER_VALUES, now),
            ).fetchall()
            return self._requeue_leased(conn, rows, reservation_minutes, msg("restore.store.reason.silent"))

    def release_worker_restore_leases(self, worker_name: str, reservation_minutes: int = 0) -> int:
        """Worker 重新注册说明上一个进程已退出，它持有的任务不必等租约到期，立即放回并为本机保留。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT id, lease_owner FROM restore_jobs
                WHERE lease_owner = ? AND state IN ({_placeholders(_WORKER_VALUES)})
                """,
                (worker_name, *_WORKER_VALUES),
            ).fetchall()
            return self._requeue_leased(conn, rows, reservation_minutes, msg("restore.store.reason.restarted"))

    def recover_interrupted_restore_jobs(self) -> int:
        """主控重启时，发布中途中断的任务需要人工检查，不自动重复改名。"""
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE restore_jobs SET state = ?, last_error = ?, updated_at = ?
                WHERE state = ?
                """,
                (
                    RestoreState.FAILED,
                    msg("restore.store.recoverFinalizing"),
                    now,
                    RestoreState.FINALIZING,
                ),
            )
            return cursor.rowcount

    def cancel_restore_job(self, job_id: str) -> dict[str, Any] | None:
        """取消未发布的任务；返回取消前的任务记录，供调用方清理临时文件。"""
        cancellable = tuple(
            str(state) for state in RESTORE_ACTIVE_STATES if state != RestoreState.FINALIZING
        )
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM restore_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["state"] not in cancellable:
                return None
            conn.execute(
                """
                UPDATE restore_jobs SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (RestoreState.CANCELLED, msg("restore.store.cancelled"), now, now, job_id),
            )
            return dict(row)

    def delete_worker_restore_guard(self, name: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM restore_jobs WHERE lease_owner = ? AND state IN ({_placeholders(_WORKER_VALUES)}) LIMIT 1",
                (name, *_WORKER_VALUES),
            ).fetchone()
            return row is None
