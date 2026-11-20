import asyncio
from pathlib import Path

import pytest

from video_turbo.config import AppConfig, MediaLibraryConfig
from video_turbo.database import Database
from video_turbo.models import JobState, SourceKind
from video_turbo.naming import title_key
from video_turbo.orchestrator.processor import Orchestrator


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("ABC-123", "abc-123"),
        ("ABC-123-C", "abc-123"),
        ("ABC-123-U", "abc-123"),
        ("abc-123-uc", "abc-123"),
        ("ABC-123_U", "abc-123"),
        ("ABC-123-U-C", "abc-123"),
        ("ABCU-123", "abcu-123"),
    ],
)
def test_title_key_strips_edition_suffixes(stem: str, expected: str) -> None:
    assert title_key(stem) == expected


def _orchestrator(tmp_path: Path, monkeypatch) -> tuple[Orchestrator, Database, Path]:
    media_root = tmp_path / "media"
    media_root.mkdir()
    config = AppConfig(
        media_libraries=[MediaLibraryConfig(id="lib-115", name="115", path=media_root)],
        scan={"settle_minutes": 0},
        wake_on_lan={"enabled": False},
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    monkeypatch.setattr(
        "video_turbo.orchestrator.processor.run_ffprobe",
        lambda _path: {
            "format": {"duration": "60"},
            "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
        },
    )
    return Orchestrator(config, database, tmp_path / "cache"), database, media_root


def _media_rows(database: Database) -> list[dict]:
    with database.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM media_items ORDER BY id")]


def _jobs(database: Database) -> list[dict]:
    with database.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM jobs")]


def _complete_job(database: Database, media_id: int, subtitle: Path) -> str:
    subtitle.write_text("字幕", encoding="utf-8")
    with database.connect() as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, subtitle_path = ? WHERE media_id = ?",
            (JobState.COMPLETED, str(subtitle), media_id),
        )
        return str(conn.execute("SELECT id FROM jobs WHERE media_id = ?", (media_id,)).fetchone()[0])


def test_externally_restored_and_renamed_movie_keeps_original_record(
    tmp_path: Path, monkeypatch
) -> None:
    orchestrator, database, root = _orchestrator(tmp_path, monkeypatch)
    old_folder = root / "ABC-123"
    old_folder.mkdir()
    old_video = old_folder / "ABC-123.mp4"
    old_video.write_bytes(b"original")
    assert asyncio.run(orchestrator.scan())["pending"] == 1
    (old,) = _media_rows(database)
    job_id = _complete_job(database, old["id"], old_folder / "ABC-123.zh.default.srt")
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO restore_jobs (id, media_id, state, source_path, source_size,
                source_mtime_ns, restoration_model, codec, output_extension, created_at, updated_at)
            VALUES ('d1', ?, 'FAILED', ?, 8, 0, 'm', 'c', 'mp4', 'now', 'now')
            """,
            (old["id"], str(old_video)),
        )

    # 外部影片修复：内容和大小都变了，文件夹与影片都加了 -U，字幕随文件夹移动但仍是旧片名。
    new_folder = root / "ABC-123-U"
    old_folder.rename(new_folder)
    new_video = new_folder / "ABC-123-U.mp4"
    # 先写新文件再删旧文件：Linux 会复用刚释放的 inode，让新文件被当成旧文件直接改名。
    new_video.write_bytes(b"restored, larger")
    (new_folder / "ABC-123.mp4").unlink()

    result = asyncio.run(orchestrator.scan())

    assert result["relocated"] == 1
    (media,) = _media_rows(database)
    assert media["id"] == old["id"]
    assert media["path"] == str(new_video)
    assert media["size"] == new_video.stat().st_size
    (job,) = _jobs(database)
    assert job["id"] == job_id
    assert job["state"] == JobState.COMPLETED
    assert job["subtitle_path"] == str(new_folder / "ABC-123-U.zh.default.srt")
    assert (new_folder / "ABC-123-U.zh.default.srt").read_text(encoding="utf-8") == "字幕"
    assert not (new_folder / "ABC-123.zh.default.srt").exists()
    with database.connect() as conn:
        assert conn.execute("SELECT media_id FROM restore_jobs").fetchone()[0] == old["id"]

    repeated = asyncio.run(orchestrator.scan())
    assert repeated["relocated"] == 0
    assert repeated["pending"] == 0
    assert len(_media_rows(database)) == 1


def test_scan_merges_duplicate_record_left_by_earlier_scan(tmp_path: Path, monkeypatch) -> None:
    orchestrator, database, root = _orchestrator(tmp_path, monkeypatch)
    old_video = root / "ABC-123-C.mp4"
    old_video.write_bytes(b"original")
    asyncio.run(orchestrator.scan())
    new_video = root / "ABC-123-UC.mp4"
    new_video.write_bytes(b"restored")
    asyncio.run(orchestrator.scan())
    old_video.unlink()
    # 旧版本扫描后已经留下两条记录。
    assert len(_media_rows(database)) == 2
    old_id = _media_rows(database)[0]["id"]

    result = asyncio.run(orchestrator.scan())

    assert result["relocated"] == 1
    (media,) = _media_rows(database)
    assert media["id"] == old_id
    assert media["path"] == str(new_video)
    # -UC 属于硬字幕：扫描直接记为已完成，合并后只留一条任务。
    assert media["decision"] == "done"
    (job,) = _jobs(database)
    assert job["state"] == JobState.COMPLETED
    assert job["source_kind"] == SourceKind.NONE


def test_completed_job_of_new_file_wins_over_old_queued_job(tmp_path: Path, monkeypatch) -> None:
    orchestrator, database, root = _orchestrator(tmp_path, monkeypatch)
    old_video = root / "ABC-123.mp4"
    old_video.write_bytes(b"original")
    asyncio.run(orchestrator.scan())
    new_video = root / "ABC-123-U.mp4"
    new_video.write_bytes(b"restored")
    asyncio.run(orchestrator.scan())
    old_id, new_id = (row["id"] for row in _media_rows(database))
    new_job_id = _complete_job(database, new_id, root / "ABC-123-U.zh.default.srt")
    old_video.unlink()

    asyncio.run(orchestrator.scan())

    (media,) = _media_rows(database)
    assert media["id"] == old_id
    (job,) = _jobs(database)
    assert job["id"] == new_job_id
    assert job["state"] == JobState.COMPLETED


def test_ambiguous_rename_candidates_are_left_alone(tmp_path: Path, monkeypatch) -> None:
    orchestrator, database, root = _orchestrator(tmp_path, monkeypatch)
    old_video = root / "ABC-123.mp4"
    old_video.write_bytes(b"original")
    asyncio.run(orchestrator.scan())
    # 先写新文件再删旧文件：Linux 会复用刚释放的 inode，让新文件被当成旧文件直接改名。
    (root / "ABC-123-C.mp4").write_bytes(b"chinese")
    (root / "ABC-123-U.mp4").write_bytes(b"restored")
    old_video.unlink()

    result = asyncio.run(orchestrator.scan())

    assert result["relocated"] == 0
    paths = [row["path"] for row in _media_rows(database)]
    assert str(old_video) in paths
    assert len(paths) == 3
