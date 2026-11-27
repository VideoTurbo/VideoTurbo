"""数据库结构版本与迁移。

数据库用 SQLite 的 ``PRAGMA user_version`` 记录结构版本：版本号等于已执行的迁移条数。
``MIGRATIONS[n]`` 把数据库从第 n 版升到第 n+1 版；``MIGRATIONS[0]`` 创建首版公开发布时的全部表。
主程序启动时按顺序执行尚未执行的迁移，每一条迁移与版本号的更新在同一个事务中完成，
中途失败时整条回滚，数据库停在上一版。

修改表结构或 ``runtime_settings`` 中保存的设置格式时：

- 在 ``MIGRATIONS`` 末尾追加一条，不修改、不删除已发布的迁移；
- 迁移可以是 SQL 脚本，也可以是接收连接的函数（改写设置 JSON 等需要 Python 的场景）；
- 迁移运行在事务中，不能执行 ``PRAGMA foreign_keys``。需要重建表时按 SQLite 官方的
  “先建新表、复制数据、删旧表、改名”顺序完成；外键在事务中仍然生效，复制顺序要满足约束。

数据库版本高于本程序支持的版本（降级了主程序）时拒绝启动，避免旧程序误改新结构。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

Migration = str | Callable[[sqlite3.Connection], None]


class SchemaError(RuntimeError):
    """数据库结构版本与本程序不匹配；message 直接展示给用户。"""


V1 = """
    CREATE TABLE media_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        -- 来源内的路径；本地来源是主程序所在系统中的路径。
        path TEXT NOT NULL,
        library_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        -- 改名、移动后不变的文件标识（本地是“设备号:inode”，网盘是文件 id）。
        file_key TEXT,
        duration REAL,
        probe_json TEXT NOT NULL,
        decision TEXT,
        decision_reason TEXT,
        -- 海报缓存：poster_media_path 记录查找时的影片路径，影片改名或移动后自动失效。
        poster_path TEXT,
        poster_size INTEGER,
        poster_mtime_ns INTEGER,
        poster_media_path TEXT,
        poster_checked_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(library_id, path)
    );

    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
        state TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_stream_index INTEGER,
        source_path TEXT,
        -- 语言对在创建任务时从所属媒体库写入，之后流水线只读这里。
        source_language TEXT NOT NULL,
        target_language TEXT NOT NULL,
        audio_path TEXT,
        audio_sha256 TEXT,
        -- 网盘影片：GPU 机器直接下载影片抽音频，没有主程序抽好的音频。
        fetch_media INTEGER NOT NULL DEFAULT 0,
        source_result_path TEXT,
        asr_duration_seconds REAL,
        target_result_path TEXT,
        translation_stats_json TEXT,
        reprocess_mode TEXT,
        subtitle_path TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL DEFAULT 0,
        paused_from_state TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(media_id)
    );

    CREATE TABLE workers (
        name TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        gpu_name TEXT NOT NULL,
        driver_version TEXT,
        memory_total_mb INTEGER NOT NULL,
        model TEXT NOT NULL,
        compute_type TEXT NOT NULL,
        gpu_utilization INTEGER,
        memory_free_mb INTEGER,
        current_job_id TEXT,
        status TEXT NOT NULL,
        asr_completed_chunks INTEGER,
        asr_total_chunks INTEGER,
        asr_elapsed_seconds REAL,
        asr_eta_seconds REAL,
        supported_asr_modes_json TEXT,
        supports_whisperjav_ensemble INTEGER NOT NULL DEFAULT 0,
        asr_mode TEXT,
        asr_stage TEXT,
        asr_whisperjav_progress_json TEXT,
        device_type TEXT NOT NULL DEFAULT 'cuda',
        asr_device TEXT,
        supports_restore INTEGER NOT NULL DEFAULT 0,
        -- 有 ffmpeg，可以直接从网盘下载影片抽音频。
        supports_media_fetch INTEGER NOT NULL DEFAULT 0,
        restore_models_json TEXT,
        restore_unavailable_reason TEXT,
        task_kind TEXT,
        restore_progress_json TEXT,
        restore_jobs_json TEXT,
        registered_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );

    CREATE TABLE runtime_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX jobs_state_idx ON jobs(state, created_at);
    CREATE INDEX jobs_state_priority_idx
        ON jobs(state, priority DESC, created_at);
    CREATE INDEX workers_seen_idx ON workers(last_seen_at);
    CREATE INDEX media_items_file_idx ON media_items(library_id, file_key);

    CREATE TABLE restore_jobs (
        id TEXT PRIMARY KEY,
        media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
        state TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_size INTEGER NOT NULL,
        source_mtime_ns INTEGER NOT NULL,
        source_sha256 TEXT,
        restoration_model TEXT NOT NULL,
        codec TEXT NOT NULL,
        output_extension TEXT NOT NULL,
        upload_path TEXT,
        upload_owner TEXT,
        upload_size INTEGER,
        upload_sha256 TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        last_worker TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        progress_json TEXT,
        worker_progress_json TEXT,
        ltx_large_canvas INTEGER NOT NULL DEFAULT 0,
        reserved_machine TEXT,
        reserved_until TEXT,
        metadata_json TEXT,
        output_path TEXT,
        backup_path TEXT,
        original_purged_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );
    CREATE INDEX restore_jobs_state_idx
        ON restore_jobs(state, created_at);
    CREATE INDEX restore_jobs_media_idx
        ON restore_jobs(media_id, created_at);

    CREATE TABLE cloud_bindings (
        machine TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        instance_title TEXT,
        shutdown_mode TEXT NOT NULL DEFAULT 'off',
        state TEXT NOT NULL DEFAULT 'watching',
        idle_since TEXT,
        drain_started_at TEXT,
        stop_requested_at TEXT,
        stopped_at TEXT,
        last_error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        backstop_minutes INTEGER,
        bound_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX cloud_bindings_instance_idx
        ON cloud_bindings(provider, instance_id);

    CREATE TABLE cloud_launches (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        method TEXT NOT NULL,
        target_id TEXT NOT NULL,
        target_label TEXT NOT NULL,
        gpu_id TEXT NOT NULL,
        gpu_label TEXT NOT NULL,
        gpu_count INTEGER NOT NULL,
        interval_seconds REAL NOT NULL,
        deploy INTEGER NOT NULL,
        -- 逗号分隔的服务列表。
        services TEXT NOT NULL,
        -- NVENC 检查失败后，用户明确选择仅继续字幕；保留最初的服务选择供重新部署。
        asr_only INTEGER NOT NULL DEFAULT 0,
        shutdown_mode TEXT NOT NULL,
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        message TEXT,
        instance_id TEXT,
        machine TEXT,
        log TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        grabbed_at TEXT,
        finished_at TEXT
    );

    CREATE TABLE notices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        read_at TEXT
    );
"""

# 多语言：“已跳过”的任务用 last_error 中的固定文字识别（list_status.SKIPPED_NOTE），
# 改成多语言标记后，把旧数据库里的中文原文换成同样的标记。
V2_SKIPPED_NOTE = """
    UPDATE jobs SET last_error = '⟦i18n:{"k": "status.skipped", "p": {}}⟧'
    WHERE last_error = '已跳过，不生成字幕。';
"""

V3_WHISPERJAV = """
UPDATE runtime_settings
   SET value = json_set(value, '$.mode', 'whisperjav', '$.whisperjav_ensemble', json('false'))
 WHERE key = 'asr' AND json_valid(value) AND json_extract(value, '$.mode') = 'faster_whisper';
UPDATE workers SET supported_asr_modes_json = (
    SELECT json_group_array(value) FROM json_each(workers.supported_asr_modes_json)
     WHERE value = 'whisperjav'
) WHERE json_valid(supported_asr_modes_json) AND supported_asr_modes_json LIKE '%faster_whisper%';
UPDATE workers SET asr_mode = NULL WHERE asr_mode = 'faster_whisper';
"""

MIGRATIONS: tuple[Migration, ...] = (V1, V2_SKIPPED_NOTE, V3_WHISPERJAV)

SCHEMA_VERSION = len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _statements(script: str) -> Iterator[str]:
    """把 SQL 脚本拆成单条语句，以便在同一个事务中逐条执行（executescript 会先提交事务）。"""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip():
        raise ValueError(f"迁移脚本末尾有不完整的语句：{buffer.strip()[:80]}")


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    if callable(migration):
        migration(conn)
        return
    for statement in _statements(migration):
        conn.execute(statement)


def migrate(conn: sqlite3.Connection, path: Path | str = "数据库") -> int:
    """把数据库升级到 ``SCHEMA_VERSION``，返回升级前的版本。"""
    conn.commit()
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"{path} 的结构版本（{current}）比本程序支持的版本（{SCHEMA_VERSION}）新，"
            "请升级主程序后再启动。"
        )
    if current == 0 and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone():
        raise SchemaError(
            f"{path} 没有结构版本号，是首版公开发布之前的测试版本创建的，无法升级。"
            "请删除该文件后重新启动主程序。"
        )
    for version in range(current, SCHEMA_VERSION):
        # IMMEDIATE：迁移开始时就拿到写锁，避免另一个进程在中途写入。
        conn.execute("BEGIN IMMEDIATE")
        try:
            _apply(conn, MIGRATIONS[version])
            conn.execute(f"PRAGMA user_version = {version + 1}")
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    return current
