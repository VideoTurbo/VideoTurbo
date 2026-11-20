"""媒体库中的影片：扫描结果、ffprobe 缓存、海报缓存、改名跟踪和各媒体库的统计。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from .i18n import msg
from .models import (
    JobState,
    SourceKind,
    utc_now_iso,
)
from .naming import published_subtitle_path, title_key
from .settings_store import LIBRARY_SCANS_KEY
from .source_paths import text as path_text
from .store_base import StoreBase


class MediaFiles(Protocol):
    """数据库判断媒体库里的文件（影片、已发布字幕）时使用的来源；主程序启动时换成按媒体库来源分发的实现。"""

    def exists(self, library_id: str, path: str) -> bool: ...

    def rename(self, library_id: str, source: str, target: str) -> None: ...


class LocalMediaFiles:
    def exists(self, library_id: str, path: str) -> bool:
        return Path(path).is_file()

    def rename(self, library_id: str, source: str, target: str) -> None:
        if Path(target).exists():
            raise FileExistsError(target)
        Path(source).rename(target)


class MediaStoreMixin(StoreBase):
    def set_poster_cache(
        self,
        media_id: int,
        *,
        media_path: str,
        poster: Path | None,
        size: int | None,
        mtime_ns: int | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE media_items SET poster_path = ?, poster_size = ?, poster_mtime_ns = ?,
                    poster_media_path = ?, poster_checked_at = ?
                WHERE id = ?
                """,
                (path_text(poster) if poster else None, size, mtime_ns, media_path, utc_now_iso(), media_id),
            )

    def cached_probe(
        self, library_id: str, path: str | Path, size: int, mtime_ns: int
    ) -> dict[str, Any] | None:
        """返回与当前文件大小、修改时间一致的上次 ffprobe 结果。

        空结果表示上次扫描未探测或探测结果已作废，必须重新运行 ffprobe。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT size, mtime_ns, probe_json FROM media_items WHERE library_id = ? AND path = ?",
                (library_id, path_text(path)),
            ).fetchone()
        if row is None or row["size"] != size or row["mtime_ns"] != mtime_ns:
            return None
        try:
            probe = json.loads(row["probe_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return probe if isinstance(probe, dict) and probe else None

    def upsert_media(
        self,
        *,
        path: str | Path,
        library_id: str,
        size: int,
        mtime_ns: int,
        duration: float | None,
        probe: dict[str, Any] | None,
        decision: str,
        reason: str,
        file_key: str | None = None,
    ) -> tuple[int, bool]:
        """写入媒体记录。

        probe 为 None 表示本次扫描未运行 ffprobe：文件未变化时保留原探测结果；
        文件已变化或是新文件时清空探测结果，防止之后复用与当前文件不符的旧数据。
        """
        now = utc_now_iso()
        with self.connect() as conn:
            relocated = False
            existing = conn.execute(
                "SELECT id, path, size, mtime_ns FROM media_items WHERE library_id = ? AND path = ?",
                (library_id, path_text(path)),
            ).fetchone()
            if existing is None and file_key is not None:
                # 改名或移动文件时文件标识保持不变（本地是设备号和 inode）。
                # 只接管唯一且旧路径已经消失的记录，避免把仍然存在的硬链接误判为改名。
                relocation_candidates = conn.execute(
                    """
                    SELECT id, path, size, mtime_ns FROM media_items
                    WHERE library_id = ? AND file_key = ?
                    """,
                    (library_id, file_key),
                ).fetchall()
                missing_candidates = [
                    row
                    for row in relocation_candidates
                    if not self.files.exists(library_id, str(row["path"]))
                ]
                if len(missing_candidates) == 1:
                    existing = missing_candidates[0]
                    relocated = True
            changed = (
                existing is None or existing["size"] != size or existing["mtime_ns"] != mtime_ns
            )
            keep_probe = probe is None and not changed
            probe_json = json.dumps(probe or {}, ensure_ascii=False)
            if probe is None:
                duration = None
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO media_items
                        (path, library_id, size, mtime_ns, file_key, duration, probe_json,
                         decision, decision_reason, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        path_text(path),
                        library_id,
                        size,
                        mtime_ns,
                        file_key,
                        duration,
                        probe_json,
                        decision,
                        reason,
                        now,
                        now,
                    ),
                )
                return int(cursor.lastrowid), True

            conn.execute(
                """
                UPDATE media_items SET
                    path = ?, library_id = ?, size = ?, mtime_ns = ?, file_key = ?,
                    duration = CASE WHEN ? THEN duration ELSE ? END,
                    probe_json = CASE WHEN ? THEN probe_json ELSE ? END,
                    decision = ?, decision_reason = ?, updated_at = ?,
                    -- 每次扫描让“没找到海报”的记录重新查找一次。
                    poster_checked_at = CASE WHEN poster_path IS NULL THEN NULL ELSE poster_checked_at END
                WHERE id = ?
                """,
                (
                    path_text(path),
                    library_id,
                    size,
                    mtime_ns,
                    file_key,
                    keep_probe,
                    duration,
                    keep_probe,
                    probe_json,
                    decision,
                    reason,
                    now,
                    existing["id"],
                ),
            )
            if relocated:
                job = conn.execute(
                    "SELECT target_language FROM jobs WHERE media_id = ?", (existing["id"],)
                ).fetchone()
                relocated_subtitle = (
                    published_subtitle_path(Path(str(path)), str(job["target_language"]))
                    if job
                    else None
                )
                if relocated_subtitle is not None and self.files.exists(
                    library_id, path_text(relocated_subtitle)
                ):
                    conn.execute(
                        """
                        UPDATE jobs SET subtitle_path = ?, updated_at = ?
                        WHERE media_id = ?
                        """,
                        (path_text(relocated_subtitle), now, existing["id"]),
                    )
            return int(existing["id"]), changed

    def reconcile_renamed_media(
        self, library_id: str, present_ids: set[int]
    ) -> list[dict[str, Any]]:
        """整库扫描后，把旧路径已消失的记录合并到改名后的新文件上。

        影片修复会改变文件内容，inode、大小和修改时间都对不上，只能按片名匹配：
        去掉 -U / -UC / -C 后片名相同、同一媒体库、双方都只有唯一一个候选时才合并。
        合并保留旧记录（字幕任务、影片修复历史），文件信息换成新文件，再删除新文件的重复记录。
        present_ids 是本次扫描确认文件存在的媒体记录。
        """
        now = utc_now_iso()
        merges: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM media_items WHERE library_id = ?", (library_id,)
            ).fetchall()
            missing: dict[str, list[sqlite3.Row]] = {}
            present: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                path = Path(str(row["path"]))
                key = title_key(path.stem)
                if int(row["id"]) in present_ids:
                    present.setdefault(key, []).append(row)
                elif not self.files.exists(library_id, path_text(path)):
                    missing.setdefault(key, []).append(row)
            for key, old_rows in missing.items():
                new_rows = present.get(key, [])
                if len(old_rows) != 1 or len(new_rows) != 1:
                    continue
                old, new = old_rows[0], new_rows[0]
                old_id, new_id = int(old["id"]), int(new["id"])
                old_job = conn.execute(
                    "SELECT id, state FROM jobs WHERE media_id = ?", (old_id,)
                ).fetchone()
                new_job = conn.execute(
                    "SELECT id, state, source_kind FROM jobs WHERE media_id = ?", (new_id,)
                ).fetchone()
                # 两边都有字幕任务时只能留一个：新文件的任务只是待处理、刚排队、失败，
                # 或是扫描直接判定的结果，就保留旧任务的历史；新文件已完成或正在处理，
                # 则以新文件的任务为准。
                keep_new_job = new_job is not None and (
                    old_job is None
                    or (
                        new_job["source_kind"] != SourceKind.NONE
                        and new_job["state"]
                        not in {
                            JobState.PENDING,
                            JobState.DISCOVERED,
                            JobState.FAILED,
                        }
                    )
                )
                if keep_new_job and old_job is not None:
                    conn.execute("DELETE FROM jobs WHERE id = ?", (old_job["id"],))
                if new_job is not None:
                    if keep_new_job:
                        conn.execute(
                            "UPDATE jobs SET media_id = ? WHERE id = ?", (old_id, new_job["id"])
                        )
                    else:
                        conn.execute("DELETE FROM jobs WHERE id = ?", (new_job["id"],))
                conn.execute(
                    "UPDATE restore_jobs SET media_id = ? WHERE media_id = ?", (old_id, new_id)
                )
                conn.execute("DELETE FROM media_items WHERE id = ?", (new_id,))
                conn.execute(
                    """
                    UPDATE media_items SET
                        path = ?, size = ?, mtime_ns = ?, file_key = ?,
                        duration = ?, probe_json = ?, decision = ?, decision_reason = ?,
                        poster_path = NULL, poster_size = NULL, poster_mtime_ns = NULL,
                        poster_media_path = NULL, poster_checked_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new["path"],
                        new["size"],
                        new["mtime_ns"],
                        new["file_key"],
                        new["duration"],
                        new["probe_json"],
                        new["decision"],
                        new["decision_reason"],
                        now,
                        old_id,
                    ),
                )
                kept_job_id = (
                    new_job["id"] if keep_new_job else old_job["id"] if old_job else None
                )
                merges.append(
                    {
                        "media_id": old_id,
                        "merged_media_id": new_id,
                        "old_path": str(old["path"]),
                        "new_path": str(new["path"]),
                        "job_id": kept_job_id,
                        "job_from_old": bool(old_job is not None and not keep_new_job),
                    }
                )

        for merge in merges:
            if merge["job_from_old"]:
                merge["subtitle_renamed"] = self._follow_renamed_subtitle(
                    library_id, merge["job_id"], Path(merge["old_path"]), Path(merge["new_path"])
                )
        return merges

    def _follow_renamed_subtitle(
        self, library_id: str, job_id: str, old_video: Path, new_video: Path
    ) -> str | None:
        """旧任务发布的字幕按新片名改名，Emby 才能把它识别为新片的字幕。"""
        with self.connect() as conn:
            job = conn.execute(
                "SELECT subtitle_path, target_language FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if job is None:
            return None
        language = str(job["target_language"])
        target = published_subtitle_path(new_video, language)
        renamed: str | None = None
        if not self.files.exists(library_id, path_text(target)):
            old_name = published_subtitle_path(old_video, language).name
            candidates = [
                job["subtitle_path"],
                path_text(published_subtitle_path(old_video, language)),
                # 文件夹改名后，旧字幕跟着进了新文件夹，但仍是旧片名。
                path_text(new_video.with_name(old_name)),
            ]
            source = next(
                (
                    Path(str(candidate))
                    for candidate in candidates
                    if candidate and self.files.exists(library_id, path_text(candidate))
                ),
                None,
            )
            if source is None:
                return None
            try:
                self.files.rename(library_id, path_text(source), path_text(target))
            except OSError:
                return None
            renamed = path_text(source)
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET subtitle_path = ?, updated_at = ? WHERE id = ?",
                (path_text(target), utc_now_iso(), job_id),
            )
        return renamed

    def library_stats(self) -> dict[str, dict[str, Any]]:
        """各媒体库的影片数和最近一次扫描完成的时间。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT library_id, COUNT(*) AS count FROM media_items GROUP BY library_id"
            ).fetchall()
        scans = self.runtime_json(LIBRARY_SCANS_KEY) or {}
        stats: dict[str, dict[str, Any]] = {
            str(library_id): {"media_count": 0, "scanned_at": scanned_at}
            for library_id, scanned_at in scans.items()
        }
        for row in rows:
            stats.setdefault(str(row["library_id"]), {"scanned_at": None})["media_count"] = int(
                row["count"]
            )
        return stats

    def record_library_scan(self, library_id: str) -> None:
        scans = dict(self.runtime_json(LIBRARY_SCANS_KEY) or {})
        scans[library_id] = utc_now_iso()
        self.set_runtime_json(LIBRARY_SCANS_KEY, scans)

    def library_busy_reason(self, library_id: str) -> str | None:
        """媒体库还有正在处理的字幕或影片修复任务时返回原因，这时不能删除媒体库。"""
        from .models import RESTORE_ACTIVE_STATES

        idle_states = (
            JobState.PENDING, JobState.COMPLETED, JobState.FAILED, JobState.PAUSED,
        )
        with self.connect() as conn:
            subtitle = conn.execute(
                f"""
                SELECT COUNT(*) AS count FROM jobs
                JOIN media_items ON media_items.id = jobs.media_id
                WHERE media_items.library_id = ?
                    AND jobs.state NOT IN ({", ".join("?" for _ in idle_states)})
                """,
                (library_id, *idle_states),
            ).fetchone()["count"]
            restore = conn.execute(
                f"""
                SELECT COUNT(*) AS count FROM restore_jobs
                JOIN media_items ON media_items.id = restore_jobs.media_id
                WHERE media_items.library_id = ?
                    AND restore_jobs.state IN ({", ".join("?" for _ in RESTORE_ACTIVE_STATES)})
                """,
                (library_id, *RESTORE_ACTIVE_STATES),
            ).fetchone()["count"]
        parts = []
        if subtitle:
            parts.append(msg("libraries.busy.subtitleJobs", subtitle=subtitle))
        if restore:
            parts.append(msg("libraries.busy.restoreJobs", restore=restore))
        return msg("libraries.busy.message", jobs=parts) if parts else None

    def delete_library_media(self, library_id: str) -> int:
        """删除媒体库的影片记录（字幕任务和影片修复记录随之删除），不动任何文件。"""
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM media_items WHERE library_id = ?", (library_id,))
        scans = dict(self.runtime_json(LIBRARY_SCANS_KEY) or {})
        if scans.pop(library_id, None) is not None:
            self.set_runtime_json(LIBRARY_SCANS_KEY, scans)
        return int(cursor.rowcount)

    def media_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT decision, COUNT(*) AS count FROM media_items GROUP BY decision"
            ).fetchall()
            return {str(row["decision"]): int(row["count"]) for row in rows}
