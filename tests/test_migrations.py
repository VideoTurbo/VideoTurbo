import json
import sqlite3
from pathlib import Path

import pytest
from conftest import raises_rendered

from video_turbo import migrations
from video_turbo.database import Database
from video_turbo.migrations import SCHEMA_VERSION, SchemaError, migrate, schema_version


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def test_new_database_is_created_at_latest_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    database.initialize()  # 重启：已是最新版本，不重复执行迁移。
    with database.connect() as conn:
        assert schema_version(conn) == SCHEMA_VERSION
        assert {"media_items", "jobs", "workers", "runtime_settings", "restore_jobs",
                "cloud_bindings", "cloud_launches", "notices"} <= _tables(conn)


def test_pending_migrations_run_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(tmp_path / "app.db")
    migrate(conn)

    def rewrite_setting(c: sqlite3.Connection) -> None:
        c.execute("UPDATE runtime_settings SET value = '\"new\"' WHERE key = 'demo'")

    conn.execute("INSERT INTO runtime_settings VALUES ('demo', '\"old\"', 'now')")
    conn.commit()
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (*migrations.MIGRATIONS, "ALTER TABLE notices ADD COLUMN extra TEXT;", rewrite_setting),
    )
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", len(migrations.MIGRATIONS))

    base = len(migrations.MIGRATIONS) - 2
    assert migrate(conn) == base
    assert schema_version(conn) == base + 2
    assert "extra" in {row[1] for row in conn.execute("PRAGMA table_info(notices)")}
    assert conn.execute("SELECT value FROM runtime_settings WHERE key = 'demo'").fetchone()[0] == '"new"'


def test_failed_migration_rolls_back_to_previous_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(tmp_path / "app.db")
    migrate(conn)
    broken = "ALTER TABLE notices ADD COLUMN extra TEXT;\nSELECT * FROM missing_table;"
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, broken))
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", len(migrations.MIGRATIONS))

    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert schema_version(conn) == len(migrations.MIGRATIONS) - 1
    assert "extra" not in {row[1] for row in conn.execute("PRAGMA table_info(notices)")}


def test_newer_database_is_rejected(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with raises_rendered(SchemaError, "请升级主程序"):
        migrate(conn)


def test_unversioned_database_is_rejected(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY)")
    conn.commit()
    with raises_rendered(SchemaError, "没有结构版本号"):
        migrate(conn)


def test_skipped_note_migration_matches_the_current_marker(tmp_path) -> None:
    import sqlite3

    from video_turbo.list_status import SKIPPED_NOTE
    from video_turbo.migrations import V1, V2_SKIPPED_NOTE, _apply

    # 迁移里写死的标记必须与程序现在生成的一致，否则旧的“已跳过”任务认不出来。
    assert "'" + SKIPPED_NOTE + "'" in V2_SKIPPED_NOTE
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    _apply(conn, V1)
    conn.execute(
        "INSERT INTO media_items (path, library_id, size, mtime_ns, probe_json, created_at, updated_at)"
        " VALUES ('/m.mkv', 'lib', 1, 1, '{}', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO jobs (id, media_id, state, source_kind, source_language, target_language, last_error,"
        " created_at, updated_at) VALUES ('j', 1, 'COMPLETED', 'none', 'ja', 'zh-Hans', '已跳过，不生成字幕。', 'now', 'now')"
    )
    _apply(conn, V2_SKIPPED_NOTE)
    assert conn.execute("SELECT last_error FROM jobs").fetchone()[0] == SKIPPED_NOTE


@pytest.mark.parametrize(
    ("mode", "ensemble", "expected"),
    [("faster_whisper", True, False), ("whisperjav", True, True), ("whisperjav", False, False)],
)
def test_subtitle_upgrade_preserves_pass_setting(tmp_path, mode, ensemble, expected):
    from video_turbo.config import AppConfig

    old = {"mode": mode, "whisperjav_ensemble": ensemble}
    settings = {"mode": "whisperjav", "whisperjav_ensemble": expected}
    assert AppConfig.model_validate({"asr": old}).asr.model_dump() == settings

    conn = sqlite3.connect(tmp_path / "upgrade.db")
    migrations._apply(conn, migrations.V1)
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO runtime_settings VALUES ('asr', ?, 'now')", (json.dumps(old),))
    conn.executemany(
        "INSERT INTO workers(name, version, gpu_name, memory_total_mb, model, compute_type, status, "
        "registered_at, last_seen_at, supported_asr_modes_json, asr_mode) "
        "VALUES (?, '1', 'RTX', 8000, 'model', 'float16', 'idle', 'now', 'now', ?, 'faster_whisper')",
        [("fw", '["faster_whisper"]'), ("both", '["faster_whisper","whisperjav"]')],
    )
    conn.commit()
    migrate(conn)
    assert json.loads(conn.execute("SELECT value FROM runtime_settings WHERE key='asr'").fetchone()[0]) == settings
    assert conn.execute("SELECT name, supported_asr_modes_json, asr_mode FROM workers ORDER BY name").fetchall() == [
        ("both", '["whisperjav"]', None), ("fw", "[]", None),
    ]
    conn.close()
