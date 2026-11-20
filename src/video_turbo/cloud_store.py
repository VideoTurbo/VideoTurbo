"""云平台凭据、实例绑定与控制台提醒的 SQLite 存取，作为 Database 的 mixin 使用。

凭据保存在 ``runtime_settings.cloud_credentials``，在控制台填写后立即生效，无需重启。
绑定按“机器”（Worker 名称去掉 -NN 编号）记录：同机多个 Worker 实例对应同一台云实例。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .cloud_models import (
    ACTIVE_LAUNCH_STATES,
    CloudBindingRequest,
    CloudSettings,
    LaunchRequest,
    NoticeLevel,
    normalize_services,
)
from .i18n import msg
from .models import utc_now_iso
from .store_base import StoreBase

logger = logging.getLogger(__name__)

# 数据库只留日志末尾，供弹窗显示；完整日志另写到数据库旁的 launch-logs/<任务 ID>.log 供下载。
LAUNCH_LOG_LIMIT = 60_000
LAUNCH_LOG_FILE_LIMIT = 20 * 1024 * 1024
LAUNCH_LOG_DIR = "launch-logs"
# 表示 SSH 主机密钥变化的部署错误（见 cloud_deploy）。
HOST_KEY_MESSAGE_KEYS = ("cloud.deploy.hostKeyChanged", "cloud.deploy.hostKeyUnreadable")


_BINDING_COLUMNS = (
    "machine",
    "provider",
    "instance_id",
    "instance_title",
    "shutdown_mode",
    "state",
    "idle_since",
    "drain_started_at",
    "stop_requested_at",
    "stopped_at",
    "last_error",
    "attempts",
    "next_attempt_at",
    "backstop_minutes",
    "bound_at",
    "updated_at",
)


class CloudStoreMixin(StoreBase):
    def cloud_settings(self) -> CloudSettings:
        stored = self.runtime_json("cloud")
        try:
            return CloudSettings.model_validate(stored or {})
        except ValueError:
            return CloudSettings()

    def set_cloud_settings(self, settings: CloudSettings) -> None:
        self.set_runtime_json("cloud", settings.model_dump())

    def cloud_credentials(self, provider: str) -> dict[str, str]:
        stored = self.runtime_json("cloud_credentials") or {}
        values = stored.get(provider) if isinstance(stored, dict) else None
        return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}

    def cloud_profiles(self) -> dict[str, dict[str, str]]:
        stored = self.runtime_json("cloud_profiles")
        return stored if isinstance(stored, dict) else {}

    def set_cloud_profile(self, profile_id: str, profile: dict[str, str] | None) -> None:
        profiles = self.cloud_profiles()
        if profile is None:
            profiles.pop(profile_id, None)
        else:
            profiles[profile_id] = profile
        self.set_runtime_json("cloud_profiles", profiles)

    def jasna_license(self) -> str:
        value = self.runtime_json("jasna_license")
        return value.strip() if isinstance(value, str) else ""

    def cloud_host_key(self, provider: str, instance_id: str) -> str | None:
        """云实例首次 SSH 连接时记录的主机公钥；没有记录时返回 None。"""
        stored = self.runtime_json("cloud_ssh_host_keys") or {}
        value = stored.get(f"{provider}:{instance_id}") if isinstance(stored, dict) else None
        return value if isinstance(value, str) and value else None

    def set_cloud_host_key(self, provider: str, instance_id: str, value: str | None) -> None:
        stored = self.runtime_json("cloud_ssh_host_keys")
        stored = stored if isinstance(stored, dict) else {}
        key = f"{provider}:{instance_id}"
        if value:
            stored[key] = value
        elif key in stored:
            del stored[key]
        else:
            return
        self.set_runtime_json("cloud_ssh_host_keys", stored)

    def set_jasna_license(self, value: str | None) -> None:
        value = (value or "").strip()
        if value:
            self.set_runtime_json("jasna_license", value)
        else:
            self.delete_setting("jasna_license")

    def set_cloud_credentials(self, provider: str, values: dict[str, str] | None) -> None:
        stored = self.runtime_json("cloud_credentials")
        stored = stored if isinstance(stored, dict) else {}
        if values:
            stored[provider] = values
        else:
            stored.pop(provider, None)
        self.set_runtime_json("cloud_credentials", stored)


    @staticmethod
    def _binding_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {column: row[column] for column in _BINDING_COLUMNS}

    def list_cloud_bindings(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM cloud_bindings ORDER BY machine").fetchall()
        return [self._binding_dict(row) for row in rows]

    def get_cloud_binding(self, machine: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM cloud_bindings WHERE machine = ?", (machine,)
            ).fetchone()
        return self._binding_dict(row) if row else None

    def cloud_binding_for_instance(self, provider: str, instance_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM cloud_bindings WHERE provider = ? AND instance_id = ?",
                (provider, instance_id),
            ).fetchone()
        return self._binding_dict(row) if row else None

    def bind_cloud_instance(self, machine: str, request: CloudBindingRequest) -> dict[str, Any]:
        """绑定或更换实例；同一实例已绑定到其他机器时抛出 ValueError。"""
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            other = conn.execute(
                "SELECT machine FROM cloud_bindings WHERE provider = ? AND instance_id = ? AND machine <> ?",
                (request.provider, request.instance_id, machine),
            ).fetchone()
            if other:
                raise ValueError(msg("cloud.store.alreadyBound", machine=other['machine']))
            conn.execute(
                """
                INSERT INTO cloud_bindings
                    (machine, provider, instance_id, instance_title, shutdown_mode, state, bound_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'watching', ?, ?)
                ON CONFLICT(machine) DO UPDATE SET
                    provider = excluded.provider, instance_id = excluded.instance_id,
                    instance_title = excluded.instance_title, shutdown_mode = excluded.shutdown_mode,
                    state = 'watching', idle_since = NULL, drain_started_at = NULL,
                    stop_requested_at = NULL, stopped_at = NULL, last_error = NULL, attempts = 0,
                    next_attempt_at = NULL, backstop_minutes = NULL,
                    bound_at = excluded.bound_at, updated_at = excluded.updated_at
                """,
                (
                    machine,
                    request.provider,
                    request.instance_id,
                    request.instance_title,
                    request.shutdown_mode,
                    now,
                    now,
                ),
            )
        binding = self.get_cloud_binding(machine)
        assert binding is not None
        return binding

    def unbind_cloud_instance(self, machine: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute("DELETE FROM cloud_bindings WHERE machine = ?", (machine,)).rowcount
                == 1
            )

    def update_cloud_binding(self, machine: str, **fields: Any) -> None:
        unknown = set(fields) - set(_BINDING_COLUMNS[3:])
        if unknown:
            raise ValueError(msg("cloud.store.unknownFields", fields=sorted(unknown)))
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE cloud_bindings SET {assignments} WHERE machine = ?",
                (*fields.values(), machine),
            )


    def machine_busy(self, worker_names: list[str]) -> bool:
        """这些 Worker 是否还持有字幕识别或影片修复任务的租约（含后台预取与回传）。"""
        if not worker_names:
            return False
        marks = ",".join("?" for _ in worker_names)
        with self.connect() as conn:
            asr = conn.execute(
                f"SELECT 1 FROM jobs WHERE state = 'TRANSCRIBING' AND lease_owner IN ({marks}) LIMIT 1",
                worker_names,
            ).fetchone()
            if asr:
                return True
            restore = conn.execute(
                f"""
                SELECT 1 FROM restore_jobs
                WHERE state IN ('DOWNLOADING', 'PROCESSING', 'UPLOADING') AND lease_owner IN ({marks})
                LIMIT 1
                """,
                worker_names,
            ).fetchone()
            return restore is not None

    def pending_asr_jobs(self) -> int:
        """还会需要语音识别的任务：已发现、提取中、等待 GPU。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE state IN ('DISCOVERED', 'EXTRACTING', 'WAITING_ASR')"
            ).fetchone()
        return int(row["count"])

    def pending_restore_models(self) -> set[str]:
        """排队中（计算校验值、等待 Worker）的影片修复任务所需的修复模型。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT restoration_model FROM restore_jobs WHERE state IN ('PREPARING', 'WAITING')"
            ).fetchall()
        return {str(row["restoration_model"]) for row in rows}


    def create_cloud_launch(self, request: LaunchRequest) -> str:
        launch_id = uuid.uuid4().hex[:12]
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cloud_launches
                    (id, provider, method, target_id, target_label, gpu_id, gpu_label, gpu_count,
                     interval_seconds, deploy, services, shutdown_mode, state, message,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'grabbing', ?, ?, ?)
                """,
                (
                    launch_id,
                    request.provider,
                    request.method,
                    request.target_id,
                    request.target_label or request.target_id,
                    request.gpu_id,
                    request.gpu_label or request.gpu_id,
                    request.gpu_count,
                    request.interval_seconds,
                    int(request.deploy),
                    ",".join(request.services),
                    request.shutdown_mode,
                    msg("cloud.store.launchStarting"),
                    now,
                    now,
                ),
            )
        return launch_id

    def get_cloud_launch(self, launch_id: str, *, with_log: bool = True) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cloud_launches WHERE id = ?", (launch_id,)).fetchone()
        if not row:
            return None
        launch = dict(row)
        launch["deploy"] = bool(launch["deploy"])
        launch["asr_only"] = bool(launch["asr_only"])
        launch["services"] = normalize_services(launch["services"].split(","))
        # 部署失败是因为 SSH 主机密钥和记录的不一致：控制台据此提供“信任新身份并重新安装”。
        launch["host_key_changed"] = launch["state"] == "deploy_failed" and any(
            f'"{key}"' in (launch["message"] or "") for key in HOST_KEY_MESSAGE_KEYS
        )
        if not with_log:
            launch.pop("log")
        return launch

    def list_cloud_launches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM cloud_launches ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            launch
            for row in rows
            if (launch := self.get_cloud_launch(row["id"], with_log=False)) is not None
        ]

    def active_cloud_launch_ids(self) -> list[str]:
        marks = ",".join("?" for _ in ACTIVE_LAUNCH_STATES)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM cloud_launches WHERE state IN ({marks}) ORDER BY created_at",
                ACTIVE_LAUNCH_STATES,
            ).fetchall()
        return [row["id"] for row in rows]

    def cloud_instance_deploying(self, provider: str, instance_id: str) -> bool:
        """按平台账号和实例查部署状态；主控重启、机器重新命名后仍能保护部署。"""
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM cloud_launches WHERE provider = ? AND instance_id = ? "
                "AND deploy = 1 AND state IN ('booting', 'deploying') LIMIT 1",
                (provider, instance_id),
            ).fetchone() is not None

    def update_cloud_launch(self, launch_id: str, **fields: Any) -> None:
        allowed = {
            "state",
            "attempts",
            "message",
            "instance_id",
            "machine",
            "grabbed_at",
            "finished_at",
            "asr_only",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(msg("cloud.store.unknownFields", fields=sorted(unknown)))
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE cloud_launches SET {assignments} WHERE id = ?",
                (*fields.values(), launch_id),
            )

    def cloud_launch_log_path(self, launch_id: str) -> Path:
        return self.path.parent / LAUNCH_LOG_DIR / f"{launch_id}.log"

    def append_cloud_launch_log(self, launch_id: str, text: str) -> None:
        if not text:
            return
        with self.connect() as conn:
            updated = conn.execute(
                "UPDATE cloud_launches SET log = substr(log || ?, -?) WHERE id = ?",
                (text, LAUNCH_LOG_LIMIT, launch_id),
            ).rowcount
        if updated:  # 任务已删除时不再生成日志文件
            self._append_launch_log_file(launch_id, text)

    def _append_launch_log_file(self, launch_id: str, text: str) -> None:
        path = self.cloud_launch_log_path(launch_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            size = path.stat().st_size if path.exists() else 0
            if size >= LAUNCH_LOG_FILE_LIMIT:
                return
            data = text.encode("utf-8")
            if size + len(data) > LAUNCH_LOG_FILE_LIMIT:
                limit_mb = LAUNCH_LOG_FILE_LIMIT // (1024 * 1024)
                data = f"\n（完整日志超过 {limit_mb}MB，之后的内容不再写入此文件，弹窗中仍显示最近部分）\n".encode()
            with path.open("ab") as handle:
                handle.write(data)
        except OSError:
            logger.warning("写入任务日志文件失败：%s", path, exc_info=True)

    def delete_cloud_launch(self, launch_id: str) -> bool:
        marks = ",".join("?" for _ in ACTIVE_LAUNCH_STATES)
        with self.connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM cloud_launches WHERE id = ? AND state NOT IN ({marks})",
                (launch_id, *ACTIVE_LAUNCH_STATES),
            )
            deleted = cursor.rowcount == 1
        if deleted:
            try:
                self.cloud_launch_log_path(launch_id).unlink(missing_ok=True)
            except OSError:
                logger.warning("删除任务日志文件失败：%s", launch_id, exc_info=True)
        return deleted


    def add_notice(self, level: NoticeLevel, title: str, message: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO notices (level, title, message, created_at) VALUES (?, ?, ?, ?)",
                (level, title, message, utc_now_iso()),
            )
            # 只保留最近 500 条。
            conn.execute("DELETE FROM notices WHERE id <= ?", ((cursor.lastrowid or 0) - 500,))
            return int(cursor.lastrowid or 0)

    def list_notices(self, *, after_id: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if after_id:
                rows = conn.execute(
                    "SELECT * FROM notices WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notices ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def notice_summary(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest_id, "
                "COALESCE(SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END), 0) AS unread FROM notices"
            ).fetchone()
        return {"latest_id": int(row["latest_id"]), "unread": int(row["unread"])}

    def mark_notices_read(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE notices SET read_at = ? WHERE read_at IS NULL", (utc_now_iso(),))
