"""已接入的机器：注册、心跳、列表与清理。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .models import (
    JobState,
    RestoreProgress,
    WhisperJavProgress,
    WorkerHeartbeat,
    WorkerRegistration,
    utc_now_iso,
)
from .store_base import StoreBase


class WorkerStoreMixin(StoreBase):
    def register_worker(self, worker: WorkerRegistration) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workers
                    (name, version, gpu_name, driver_version, memory_total_mb, model, compute_type,
                     status, registered_at, last_seen_at, supported_asr_modes_json,
                     device_type, supports_whisperjav_ensemble, supports_restore, restore_models_json,
                     restore_unavailable_reason, supports_media_fetch)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    version = excluded.version,
                    gpu_name = excluded.gpu_name,
                    driver_version = excluded.driver_version,
                    memory_total_mb = excluded.memory_total_mb,
                    model = excluded.model,
                    compute_type = excluded.compute_type,
                    supported_asr_modes_json = excluded.supported_asr_modes_json,
                    supports_whisperjav_ensemble = excluded.supports_whisperjav_ensemble,
                    supports_restore = excluded.supports_restore,
                    restore_models_json = excluded.restore_models_json,
                    restore_unavailable_reason = excluded.restore_unavailable_reason,
                    supports_media_fetch = excluded.supports_media_fetch,
                    task_kind = NULL, restore_progress_json = NULL, restore_jobs_json = NULL,
                    device_type = excluded.device_type, asr_device = NULL,
                    asr_mode = NULL, asr_stage = NULL,
                    asr_whisperjav_progress_json = NULL,
                    status = 'online',
                    current_job_id = NULL,
                    asr_completed_chunks = NULL,
                    asr_total_chunks = NULL,
                    asr_elapsed_seconds = NULL,
                    asr_eta_seconds = NULL,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    worker.name,
                    worker.version,
                    worker.gpu_name,
                    worker.driver_version,
                    worker.memory_total_mb,
                    worker.model,
                    worker.compute_type,
                    now,
                    now,
                    json.dumps(worker.supported_asr_modes),
                    worker.device_type,
                    int(worker.supports_whisperjav_ensemble),
                    int(worker.supports_restore and bool(worker.restore_models)),
                    json.dumps(worker.restore_models),
                    worker.restore_unavailable_reason,
                    int(worker.supports_media_fetch),
                ),
            )

    def heartbeat_worker(self, name: str, heartbeat: WorkerHeartbeat) -> bool:
        """更新心跳；Worker 未注册（例如离线过久已被删除）时返回 False。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workers SET gpu_utilization = ?, memory_free_mb = ?,
                    current_job_id = ?, status = ?, asr_completed_chunks = ?,
                    asr_total_chunks = ?, asr_elapsed_seconds = ?, asr_eta_seconds = ?,
                    asr_mode = ?, asr_stage = ?, device_type = COALESCE(?, device_type),
                    asr_device = ?, asr_whisperjav_progress_json = ?, task_kind = ?,
                    restore_progress_json = ?, restore_jobs_json = ?, last_seen_at = ?
                WHERE name = ?
                """,
                (
                    heartbeat.gpu_utilization,
                    heartbeat.memory_free_mb,
                    heartbeat.current_job_id,
                    heartbeat.status,
                    heartbeat.asr_completed_chunks,
                    heartbeat.asr_total_chunks,
                    heartbeat.asr_elapsed_seconds,
                    heartbeat.asr_eta_seconds,
                    heartbeat.asr_mode,
                    heartbeat.asr_stage,
                    heartbeat.device_type,
                    heartbeat.asr_device,
                    heartbeat.asr_whisperjav_progress.model_dump_json()
                    if heartbeat.asr_whisperjav_progress is not None
                    and heartbeat.asr_mode == "whisperjav" and heartbeat.current_job_id
                    else None,
                    (heartbeat.task_kind or "asr") if heartbeat.current_job_id else None,
                    heartbeat.restore_progress.model_dump_json()
                    if heartbeat.restore_progress is not None
                    and heartbeat.task_kind == "restore"
                    and heartbeat.current_job_id
                    else None,
                    json.dumps([job.model_dump() for job in heartbeat.restore_jobs], ensure_ascii=False)
                    if heartbeat.restore_jobs
                    else None,
                    utc_now_iso(),
                    name,
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _worker_dict(row: sqlite3.Row) -> dict[str, Any]:
        worker = dict(row)
        worker["supported_asr_modes"] = json.loads(worker.pop("supported_asr_modes_json") or "[]")
        worker["supports_whisperjav_ensemble"] = bool(worker["supports_whisperjav_ensemble"])
        worker["supports_restore"] = bool(worker.get("supports_restore"))
        worker["supports_media_fetch"] = bool(worker.get("supports_media_fetch"))
        worker["restore_models"] = json.loads(worker.pop("restore_models_json", None) or "[]")
        try:
            worker["restore_jobs"] = json.loads(worker.pop("restore_jobs_json", None) or "[]")
        except ValueError:
            worker["restore_jobs"] = []
        restore_progress = worker.pop("restore_progress_json", None)
        worker["restore_progress"] = None
        if restore_progress and worker.get("task_kind") == "restore":
            try:
                worker["restore_progress"] = RestoreProgress.model_validate_json(
                    restore_progress
                ).model_dump()
            except ValueError:
                pass
        progress = worker.pop("asr_whisperjav_progress_json", None)
        worker["asr_whisperjav_progress"] = None
        if progress:
            try:
                worker["asr_whisperjav_progress"] = WhisperJavProgress.model_validate_json(
                    progress
                ).model_dump()
            except ValueError:
                pass  # Optional progress must not prevent listing Workers.
        return worker

    def list_workers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
            return [self._worker_dict(row) for row in rows]

    def get_worker(self, name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workers WHERE name = ?", (name,)).fetchone()
            return self._worker_dict(row) if row else None

    def delete_worker(self, name: str, *, last_seen_at: str | None = None) -> bool:
        """删除没有正在执行 ASR 任务的 Worker；历史任务仍保留原 Worker 名称。

        传入 last_seen_at 时，只有心跳时间仍是该值才删除，避免误删刚恢复心跳的 Worker。
        """
        with self.connect() as conn:
            active_job = conn.execute(
                "SELECT 1 FROM jobs WHERE lease_owner = ? AND state = ? LIMIT 1",
                (name, JobState.TRANSCRIBING),
            ).fetchone()
            if active_job:
                return False
        if not self.delete_worker_restore_guard(name):
            return False
        with self.connect() as conn:
            if last_seen_at is None:
                cursor = conn.execute("DELETE FROM workers WHERE name = ?", (name,))
            else:
                cursor = conn.execute(
                    "DELETE FROM workers WHERE name = ? AND last_seen_at = ?", (name, last_seen_at)
                )
            return cursor.rowcount == 1

    def delete_offline_workers(self, max_offline_seconds: float) -> list[str]:
        """删除心跳停止超过指定秒数的 Worker；仍持有任务租约的会保留。"""
        now = datetime.now(UTC)
        deleted: list[str] = []
        for worker in self.list_workers():
            last_seen_at = str(worker["last_seen_at"])
            try:
                age = (now - datetime.fromisoformat(last_seen_at)).total_seconds()
            except (TypeError, ValueError):
                continue
            if age > max_offline_seconds and self.delete_worker(worker["name"], last_seen_at=last_seen_at):
                deleted.append(worker["name"])
        return deleted
