import json
import sqlite3
import threading
from pathlib import Path

import pytest
from conftest import force_job_state, same_path

from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.list_status import SKIPPED_NOTE
from video_turbo.models import AsrSettings, JobState, SourceKind, WorkerRegistration


@pytest.mark.parametrize(
    "yaml_default,saved,expected",
    [
        (True, {"mode": "whisperjav"}, True),
        (False, {"mode": "whisperjav"}, False),
        (True, {"mode": "whisperjav", "whisperjav_ensemble": False}, False),
        (False, {"mode": "whisperjav", "whisperjav_ensemble": True}, True),
    ],
)
def test_saved_asr_settings_inherit_only_missing_yaml_fields(
    tmp_path: Path, yaml_default: bool, saved: dict, expected: bool
) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    defaults = AsrSettings(mode="faster_whisper", whisperjav_ensemble=yaml_default)
    assert database.asr_settings(defaults) == defaults
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) VALUES ('asr', ?, CURRENT_TIMESTAMP)",
            (json.dumps(saved),),
        )
    settings = database.asr_settings(defaults)
    assert settings.mode == "whisperjav"
    assert settings.whisperjav_ensemble is expected


def test_media_rename_preserves_job_and_intermediate_results(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    old_movie = tmp_path / "download-prefix@wrong-name.mp4"
    old_movie.write_bytes(b"same movie")
    old_stat = old_movie.stat()
    media_id, changed = database.upsert_media(
        path=old_movie,
        library_id="lib-115",
        size=old_stat.st_size,
        mtime_ns=old_stat.st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
        file_key=f"{old_stat.st_dev}:{old_stat.st_ino}",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    ja_result = tmp_path / "result.source.json"
    ja_result.write_text("[]", encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.WAITING_ASR,
        fields={
            "audio_path": str(audio),
            "audio_sha256": "hash",
            "source_result_path": str(ja_result),
        },
    )

    renamed_movie = tmp_path / "correct-name.mp4"
    old_movie.rename(renamed_movie)
    renamed_subtitle = tmp_path / "correct-name.zh.default.srt"
    renamed_subtitle.write_text("字幕", encoding="utf-8")
    renamed_stat = renamed_movie.stat()
    renamed_media_id, renamed_changed = database.upsert_media(
        path=renamed_movie,
        library_id="lib-115",
        size=renamed_stat.st_size,
        mtime_ns=renamed_stat.st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
        file_key=f"{renamed_stat.st_dev}:{renamed_stat.st_ino}",
    )

    assert renamed_media_id == media_id
    assert renamed_changed is False
    job = database.get_job(job_id)
    assert job is not None
    assert job["media_path"] == same_path(renamed_movie)
    assert job["state"] == JobState.WAITING_ASR
    assert job["audio_path"] == same_path(audio)
    assert job["source_result_path"] == same_path(ja_result)
    assert job["subtitle_path"] == same_path(renamed_subtitle)
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 1


def test_delete_worker_preserves_history_and_blocks_active_asr(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    database.register_worker(
        WorkerRegistration(
            name="old-gpu",
            version="0.0.0",
            gpu_name="RTX 4090",
            memory_total_mb=24564,
            model="large-v3",
            compute_type="float16",
            supported_asr_modes=["faster_whisper"],
        )
    )
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    database.start_job(job_id)
    force_job_state(database, job_id, JobState.WAITING_ASR)
    claimed = database.claim_asr_job(
        worker_name="old-gpu",
        lease_seconds=60,
    )
    assert claimed is not None

    assert database.delete_worker("old-gpu") is False
    assert database.cancel_job(job_id) is True
    assert database.delete_worker("old-gpu") is True
    assert database.get_worker("old-gpu") is None
    assert database.get_job(job_id)["state"] == JobState.PENDING


def test_delete_offline_workers_only_removes_long_offline_idle_workers(tmp_path: Path) -> None:
    from video_turbo.models import WorkerHeartbeat

    database = Database(tmp_path / "app.db")
    database.initialize()
    for name in ("fresh", "stale", "stale-busy"):
        database.register_worker(
            WorkerRegistration(
                name=name, version="0.0.0", gpu_name="RTX 4090", memory_total_mb=24564, model="large-v3", compute_type="float16", supported_asr_modes=["faster_whisper"]
            )
        )
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie, library_id="lib-1", size=5, mtime_ns=movie.stat().st_mtime_ns,
        duration=100, probe={}, decision="process", reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id, source_kind=SourceKind.AUDIO, source_stream_index=1,
        source_path=None, changed=changed,
        pair=LanguagePair(),
    )
    database.start_job(job_id)
    force_job_state(database, job_id, JobState.WAITING_ASR)
    assert database.claim_asr_job(
        worker_name="stale-busy", lease_seconds=60
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE workers SET last_seen_at = ? WHERE name IN ('stale', 'stale-busy')",
            ("2000-01-01T00:00:00+00:00",),
        )

    assert database.delete_offline_workers(3600) == ["stale"]
    assert {worker["name"] for worker in database.list_workers()} == {"fresh", "stale-busy"}
    # 心跳在读取后刷新过，就不能按旧时间删除。
    assert database.delete_worker("fresh", last_seen_at="2000-01-01T00:00:00+00:00") is False
    assert database.heartbeat_worker("fresh", WorkerHeartbeat(status="online")) is True
    assert database.heartbeat_worker("stale", WorkerHeartbeat(status="online")) is False


def test_job_claim_and_expired_lease(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, created = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    assert created is True
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.WAITING_ASR,
        fields={"audio_path": str(audio), "audio_sha256": "hash"},
    )
    database.set_pipeline_paused(True)
    assert Database(tmp_path / "app.db").is_pipeline_paused() is True
    assert (
        database.claim_asr_job(
            worker_name="rtx4070ti",
            lease_seconds=-1,
        )
        is None
    )
    database.set_pipeline_paused(False)
    claimed = database.claim_asr_job(
        worker_name="rtx4070ti",
        lease_seconds=-1,
    )
    assert claimed and claimed["id"] == job_id
    assert database.release_expired_leases() == 1
    assert database.get_job(job_id)["state"] == JobState.WAITING_ASR
    assert Database(tmp_path / "app.db").is_pipeline_paused() is False


def test_completed_job_is_reset_when_published_subtitle_was_deleted(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, created = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    assert created is True
    subtitle = tmp_path / "movie.srt"
    subtitle.write_text("字幕", encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.COMPLETED,
        fields={"subtitle_path": str(subtitle)},
    )

    # 输出还在时，重复扫描不应重置已完成任务。
    same_job_id, queued = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=False,
        pair=LanguagePair(),
    )
    assert same_job_id == job_id
    assert queued is False
    assert database.get_job(job_id)["state"] == JobState.COMPLETED

    # 输出被删除后，下次扫描应把同一任务放回处理队列。
    subtitle.unlink()
    reset_job_id, queued = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=False,
        pair=LanguagePair(),
    )
    reset_job = database.get_job(job_id)
    assert reset_job_id == job_id
    assert queued is True
    assert reset_job["state"] == JobState.PENDING
    assert reset_job["subtitle_path"] is None


def test_complete_jobs_with_existing_subtitles_is_atomic(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    job_ids: list[str] = []
    subtitles: list[Path] = []
    for index in range(2):
        movie = tmp_path / f"movie-{index}.mkv"
        movie.write_bytes(b"movie")
        media_id, changed = database.upsert_media(
            path=movie,
            library_id="lib-1",
            size=movie.stat().st_size,
            mtime_ns=movie.stat().st_mtime_ns,
            duration=100,
            probe={},
            decision="process",
            reason="test",
        )
        job_id, _ = database.create_or_reset_job(
            media_id=media_id,
            source_kind=SourceKind.AUDIO,
            source_stream_index=1,
            source_path=None,
            changed=changed,
            pair=LanguagePair(),
        )
        database.start_job(job_id)
        force_job_state(database, job_id, JobState.FAILED, error="字幕校验失败")
        job_ids.append(job_id)
        subtitles.append(tmp_path / f"movie-{index}.zh.default.srt")

    subtitles[0].write_text("已有字幕", encoding="utf-8")
    succeeded, invalid = database.complete_jobs_with_existing_subtitles(job_ids)

    assert succeeded == []
    assert list(invalid) == [job_ids[1]]
    assert database.get_job(job_ids[0])["state"] == JobState.FAILED
    assert database.get_job(job_ids[1])["state"] == JobState.FAILED

    subtitles[1].write_text("已有字幕", encoding="utf-8")
    succeeded, invalid = database.complete_jobs_with_existing_subtitles(job_ids)

    assert succeeded == job_ids
    assert invalid == {}
    for job_id, subtitle in zip(job_ids, subtitles, strict=True):
        completed = database.get_job(job_id)
        assert completed["state"] == JobState.COMPLETED
        assert completed["subtitle_path"] == same_path(subtitle)
        # 晚到的后台阶段结果不能把人工确认完成的任务改回处理中。
        assert database.set_job_state(job_id, JobState.FAILED, error="晚到结果") is False
        assert database.get_job(job_id)["state"] == JobState.COMPLETED

    succeeded, invalid = database.complete_jobs_with_existing_subtitles([job_ids[0]])
    assert succeeded == []
    assert {key: render(value) for key, value in invalid.items()} == {job_ids[0]: "任务已经是已完成状态"}


def test_failed_job_can_retry_with_existing_audio(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.FAILED,
        error="字幕显示时间过长。",
        fields={"audio_path": str(audio), "audio_sha256": "hash"},
    )

    next_state = database.retry_job(job_id)

    retried = database.get_job(job_id)
    assert next_state == JobState.WAITING_ASR
    assert retried["state"] == JobState.WAITING_ASR
    assert retried["last_error"] is None
    assert retried["audio_path"] == same_path(audio)


def test_cancelled_job_returns_to_pending_and_resumes_from_progress(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    assert database.cancel_job(job_id) is False  # 尚未提交的任务没有可取消的
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.WAITING_ASR,
        fields={"audio_path": str(audio), "audio_sha256": "hash"},
    )

    assert database.cancel_job(job_id) is True
    # 取消前已经在跑的步骤不能再把任务推进下去。
    assert database.set_job_state(job_id, JobState.ASR_COMPLETED) is False
    cancelled = database.get_job(job_id)
    assert cancelled["state"] == JobState.PENDING
    assert cancelled["last_error"] is None
    assert cancelled["audio_path"] == same_path(audio)

    assert database.start_job(job_id) == JobState.WAITING_ASR
    assert database.get_job(job_id)["state"] == JobState.WAITING_ASR


def test_cancelled_translation_reuses_existing_asr_result(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    ja_result = tmp_path / "result.source.json"
    ja_result.write_text("[]", encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.WAITING_TRANSLATION,
        fields={
            "audio_path": str(audio),
            "audio_sha256": "hash",
            "source_result_path": str(ja_result),
        },
    )
    assert database.cancel_job(job_id) is True

    assert database.start_job(job_id) == JobState.ASR_COMPLETED

    retried = database.get_job(job_id)
    assert retried["state"] == JobState.ASR_COMPLETED
    assert retried["source_result_path"] == same_path(ja_result)
    assert retried["audio_path"] == same_path(audio)


def test_retry_recovers_asr_file_when_old_version_cleared_database_path(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    database.start_job(job_id)
    assert database.cancel_job(job_id) is True
    cached_asr = tmp_path / "cache" / "subtitles" / job_id / "result.source.json"
    cached_asr.parent.mkdir(parents=True)
    cached_asr.write_text("[]", encoding="utf-8")

    next_state = database.start_job(job_id, reusable_source_result_path=cached_asr)

    assert next_state == JobState.ASR_COMPLETED
    assert database.get_job(job_id)["source_result_path"] == same_path(cached_asr)


def test_cancelled_job_is_completed_when_scan_finds_existing_subtitle(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "ATID-637-C.mp4"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="旧规则",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    database.start_job(job_id)
    assert database.cancel_job(job_id) is True

    changed_state = database.record_scan_outcome(
        media_id=media_id,
        outcome="done",
        reason="文件名或目录名符合硬编码中文字幕规则。",
        pair=LanguagePair(),
    )

    job = database.get_job(job_id)
    assert changed_state is True
    assert job["state"] == JobState.COMPLETED
    assert job["source_kind"] == SourceKind.AUDIO
    assert render(job["last_error"]) == "文件名或目录名符合硬编码中文字幕规则。"


def _scan_media(database: Database, tmp_path: Path, name: str = "movie.mkv") -> int:
    movie = tmp_path / name
    movie.write_bytes(b"movie")
    media_id, _ = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="done",
        reason="",
    )
    return media_id


def test_scan_outcome_creates_listed_jobs_that_cannot_be_reprocessed(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    done_id = _scan_media(database, tmp_path, "done.mkv")
    unusable_id = _scan_media(database, tmp_path, "unusable.mkv")
    subtitle = tmp_path / "done.zh.default.srt"

    assert database.record_scan_outcome(
        media_id=done_id, outcome="done", reason="已存在简体中文字幕。",
        pair=LanguagePair(), subtitle_path=str(subtitle),
    ) is True
    assert database.record_scan_outcome(
        media_id=unusable_id, outcome="unusable", reason="无法可靠确定日语主音轨。",
        pair=LanguagePair(),
    ) is True
    # 再扫一次状态不变，不重复计数。
    assert database.record_scan_outcome(
        media_id=unusable_id, outcome="unusable", reason="无法可靠确定日语主音轨。",
        pair=LanguagePair(),
    ) is False

    jobs = {job["media_id"]: job for job in database.list_jobs()}
    done, unusable = jobs[done_id], jobs[unusable_id]
    assert done["state"] == JobState.COMPLETED and done["subtitle_path"] == same_path(subtitle)
    assert unusable["state"] == JobState.FAILED
    assert render(unusable["last_error"]) == "无法可靠确定日语主音轨。"
    assert {done["source_kind"], unusable["source_kind"]} == {SourceKind.NONE}
    assert database.retry_job(unusable["id"]) is None
    for mode in ("continue", "retranslate", "reasr", "full"):
        assert database.reprocess_job(unusable["id"], mode) is None
        assert database.reprocess_job(done["id"], mode) is None


def test_scan_outcome_job_returns_to_pending_once_processable(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    media_id = _scan_media(database, tmp_path)
    database.record_scan_outcome(
        media_id=media_id, outcome="unusable", reason="无法可靠确定日语主音轨。",
        pair=LanguagePair(),
    )

    job_id, created = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=False,
        pair=LanguagePair(),
    )

    job = database.get_job(job_id)
    assert created is True
    assert job["state"] == JobState.PENDING
    assert job["source_kind"] == SourceKind.AUDIO
    assert job["last_error"] is None


def test_unusable_scan_outcome_only_fails_unsubmitted_jobs(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    media_id = _scan_media(database, tmp_path)
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=True,
        pair=LanguagePair(),
    )
    assert database.start_job(job_id) == JobState.DISCOVERED

    assert database.record_scan_outcome(
        media_id=media_id, outcome="unusable", reason="无法可靠确定日语主音轨。",
        pair=LanguagePair(),
    ) is False
    assert database.get_job(job_id)["state"] == JobState.DISCOVERED


def test_dashboard_jobs_prioritize_running_and_support_server_filtering(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()

    running_movie = tmp_path / "running.mkv"
    running_movie.write_bytes(b"running")
    running_media_id, running_changed = database.upsert_media(
        path=running_movie,
        library_id="lib-1",
        size=running_movie.stat().st_size,
        mtime_ns=running_movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    running_job_id, _ = database.create_or_reset_job(
        media_id=running_media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=running_changed,
        pair=LanguagePair(),
    )
    database.start_job(running_job_id)
    force_job_state(database, 
        running_job_id,
        JobState.TRANSLATING,
        error="上游偶发超时",
        fields={"lease_owner": "rtx4090-1"},
    )
    database.set_job_priority(running_job_id, 100)

    completed_movie = tmp_path / "completed.mkv"
    completed_movie.write_bytes(b"completed")
    completed_media_id, completed_changed = database.upsert_media(
        path=completed_movie,
        library_id="lib-1",
        size=completed_movie.stat().st_size,
        mtime_ns=completed_movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    completed_job_id, _ = database.create_or_reset_job(
        media_id=completed_media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=completed_changed,
        pair=LanguagePair(),
    )
    force_job_state(database, completed_job_id, JobState.COMPLETED)

    active_jobs = database.list_jobs(50, states={JobState.TRANSLATING})

    assert [job["id"] for job in active_jobs] == [running_job_id]
    assert database.count_jobs({JobState.TRANSLATING}) == 1
    assert database.count_jobs() == 2
    assert [job["id"] for job in database.list_jobs(query="RUNNING.MKV")] == [
        running_job_id
    ]
    assert [job["id"] for job in database.list_jobs(query="偶发超时")] == [running_job_id]
    assert database.count_jobs(library_id="lib-1") == 2
    assert database.count_jobs(library_id="lib-tv") == 0
    # 默认最近更新的在前；可按影片名和大小排序，并分页。
    assert [job["id"] for job in database.list_jobs()] == [completed_job_id, running_job_id]
    assert [job["id"] for job in database.list_jobs(sort="name_desc")] == [
        running_job_id,
        completed_job_id,
    ]
    assert [job["id"] for job in database.list_jobs(sort="size_asc")] == [
        running_job_id,
        completed_job_id,
    ]
    assert [job["id"] for job in database.list_jobs(1, offset=1)] == [running_job_id]
    assert database.list_jobs()[0]["media_size"] == completed_movie.stat().st_size


def test_scanned_job_waits_as_pending_until_started(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, created = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )

    assert created is True
    assert database.get_job(job_id)["state"] == JobState.PENDING
    # 待处理的任务不会被流水线领取，手动提交后才排队。
    assert database.next_job_in_state(JobState.DISCOVERED) is None
    assert database.start_job(job_id) == JobState.DISCOVERED
    assert database.get_job(job_id)["state"] == JobState.DISCOVERED
    assert database.start_job(job_id) is None


def test_skipped_job_counts_as_completed_and_can_be_started_again(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    media_id = _scan_media(database, tmp_path)
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=True,
        pair=LanguagePair(),
    )
    assert database.skip_job(job_id) is True
    assert database.skip_job(job_id) is False
    skipped = database.get_job(job_id)
    assert skipped["state"] == JobState.COMPLETED
    assert skipped["last_error"] == SKIPPED_NOTE

    # 再次扫描不会因为“没有字幕”把跳过的任务改回待处理。
    _, reset = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=False,
        pair=LanguagePair(),
    )
    assert reset is False
    assert database.get_job(job_id)["state"] == JobState.COMPLETED

    assert database.start_job(job_id) == JobState.DISCOVERED
    assert database.get_job(job_id)["last_error"] is None


def test_pending_job_drops_kept_progress_when_language_pair_changes(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    media_id = _scan_media(database, tmp_path)
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=True,
        pair=LanguagePair(),
    )
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    database.start_job(job_id)
    force_job_state(database, 
        job_id, JobState.WAITING_ASR, fields={"audio_path": str(audio), "audio_sha256": "hash"}
    )
    assert database.cancel_job(job_id) is True

    database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=False,
        pair=LanguagePair(),
    )
    assert database.get_job(job_id)["audio_path"] == same_path(audio)

    database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=False,
        pair=LanguagePair(source="en", target="zh"),
    )
    assert database.get_job(job_id)["audio_path"] is None


def test_worker_token_is_generated_once_and_can_be_reset(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()

    token = database.worker_token()
    assert len(token) >= 32
    assert database.worker_token() == token
    assert database.reset_worker_token() != token
    assert database.worker_token() != token


def test_interrupted_translation_is_recovered_after_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    ja_result = tmp_path / "result.source.json"
    ja_result.write_text("[]", encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.ASR_COMPLETED,
        fields={"source_result_path": str(ja_result)},
    )
    force_job_state(database, job_id, JobState.TRANSLATING)

    assert database.recover_interrupted_jobs() == 1

    recovered = database.get_job(job_id)
    assert recovered["state"] == JobState.ASR_COMPLETED
    assert render(recovered["last_error"]) == "主程序重启，已从翻译中间进度恢复。"


def test_interrupted_publish_recovers_new_output_name(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    database.start_job(job_id)
    force_job_state(database, job_id, JobState.PUBLISHING)
    subtitle = tmp_path / "movie.zh.default.srt"
    subtitle.write_text("字幕", encoding="utf-8")

    assert database.recover_interrupted_jobs() == 1

    recovered = database.get_job(job_id)
    assert recovered["state"] == JobState.COMPLETED
    assert recovered["subtitle_path"] == same_path(subtitle)


def test_job_pause_resume_and_priority_order(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()

    job_ids = []
    for index in range(2):
        movie = tmp_path / f"movie-{index}.mkv"
        movie.write_bytes(b"movie")
        media_id, changed = database.upsert_media(
            path=movie,
            library_id="lib-1",
            size=movie.stat().st_size,
            mtime_ns=movie.stat().st_mtime_ns,
            duration=100,
            probe={},
            decision="process",
            reason="test",
        )
        job_id, _ = database.create_or_reset_job(
            media_id=media_id,
            source_kind=SourceKind.AUDIO,
            source_stream_index=1,
            source_path=None,
            changed=changed,
            pair=LanguagePair(),
        )
        audio = tmp_path / f"audio-{index}.opus"
        audio.write_bytes(b"audio")
        database.start_job(job_id)
        force_job_state(database, 
            job_id,
            JobState.WAITING_ASR,
            fields={"audio_path": str(audio), "audio_sha256": f"hash-{index}"},
        )
        job_ids.append(job_id)

    assert database.pause_job(job_ids[0]) is True
    paused = database.get_job(job_ids[0])
    assert paused["state"] == JobState.PAUSED
    assert paused["paused_from_state"] == JobState.WAITING_ASR
    assert database.set_job_state(job_ids[0], JobState.COMPLETED) is False
    assert database.resume_job(job_ids[0]) == JobState.WAITING_ASR

    assert database.set_job_priority(job_ids[1], 100) is True
    claimed = database.claim_asr_job(
        worker_name="4090",
        lease_seconds=60,
    )
    assert claimed and claimed["id"] == job_ids[1]


def test_reprocess_levels_keep_only_reusable_stage_files(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    audio = tmp_path / "audio.opus"
    ja_result = tmp_path / "result.source.json"
    zh_result = tmp_path / "result.target.json"
    for path in (audio, ja_result, zh_result):
        path.write_text("[]", encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.FAILED,
        fields={
            "audio_path": str(audio),
            "audio_sha256": "hash",
            "source_result_path": str(ja_result),
            "asr_duration_seconds": 123.5,
            "target_result_path": str(zh_result),
            "translation_stats_json": '{"provider_cue_counts":{"grok":1}}',
        },
    )

    assert database.reprocess_job(job_id, "retranslate") == JobState.ASR_COMPLETED
    retranslate = database.get_job(job_id)
    assert retranslate["source_result_path"] == same_path(ja_result)
    assert retranslate["asr_duration_seconds"] == 123.5
    assert retranslate["target_result_path"] == same_path(zh_result)
    assert retranslate["translation_stats_json"] == '{"provider_cue_counts":{"grok":1}}'
    assert retranslate["reprocess_mode"] == "retranslate"

    assert database.pause_job(job_id) is True
    assert database.reprocess_job(job_id, "reasr") == JobState.WAITING_ASR
    reasr = database.get_job(job_id)
    assert reasr["audio_path"] == same_path(audio)
    assert reasr["source_result_path"] is None
    assert reasr["asr_duration_seconds"] is None
    assert reasr["target_result_path"] is None
    assert reasr["reprocess_mode"] == "reasr"

    assert database.pause_job(job_id) is True
    assert database.reprocess_job(job_id, "full") == JobState.DISCOVERED
    full = database.get_job(job_id)
    assert full["audio_path"] is None
    assert full["audio_sha256"] is None
    assert full["reprocess_mode"] == "full"


def test_completed_job_can_reprocess_and_claim_existing_target_subtitle(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    subtitle = tmp_path / "movie.zh.default.srt"
    subtitle.write_text("旧字幕", encoding="utf-8")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="done",
        reason="目标字幕文件已存在：movie.zh.default.srt",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.COMPLETED,
        error="目标字幕文件已存在：movie.zh.default.srt",
    )

    assert database.reprocess_job(job_id, "full") == JobState.DISCOVERED
    queued = database.get_job(job_id)
    assert queued["subtitle_path"] == same_path(subtitle)
    assert queued["reprocess_mode"] == "full"
    assert database.record_scan_outcome(
        media_id=media_id, outcome="done", reason="目标字幕文件已存在", pair=LanguagePair()
    ) is False
    assert database.get_job(job_id)["state"] == JobState.DISCOVERED


def test_completed_job_can_retranslate_and_keeps_published_subtitle(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    ja_result = tmp_path / "result.source.json"
    ja_result.write_text("[]", encoding="utf-8")
    subtitle = tmp_path / "movie.zh.default.srt"
    subtitle.write_text("旧字幕", encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.COMPLETED,
        fields={"source_result_path": str(ja_result), "subtitle_path": str(subtitle)},
    )

    assert database.reprocess_job(job_id, "retranslate") == JobState.ASR_COMPLETED

    retranslate = database.get_job(job_id)
    assert retranslate["source_result_path"] == same_path(ja_result)
    assert retranslate["target_result_path"] is None
    assert retranslate["subtitle_path"] == same_path(subtitle)
    assert render(subtitle.read_text(encoding="utf-8")) == "旧字幕"


def test_review_reprocess_mode_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=100,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=LanguagePair(),
    )
    zh_result = tmp_path / "result.target.json"
    zh_result.write_text("[]", encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, job_id, JobState.COMPLETED, fields={"target_result_path": str(zh_result)})

    assert database.reprocess_job(job_id, "review") is None
    assert database.get_job(job_id)["state"] == JobState.COMPLETED


def test_translation_upstream_states_are_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    states = {
        "节点-1": {
            "enabled": False,
            "consecutive_failures": 8,
            "last_error": "余额不足",
        }
    }

    database.set_translation_upstream_states(states)
    database.set_translation_upstream_states(
        {"DeepSeek-1": {"enabled": True, "consecutive_failures": 0}}
    )

    assert Database(tmp_path / "app.db").translation_upstream_states() == {
        **states,
        "DeepSeek-1": {"enabled": True, "consecutive_failures": 0},
    }




def test_close_leaves_connections_in_use_to_their_thread(tmp_path: Path) -> None:
    # 关机时后台线程可能仍在查询：close() 不能从主线程关掉它正在用的连接（SQLite 会崩溃）。
    database = Database(tmp_path / "app.db")
    database.initialize()
    entered, closed = threading.Event(), threading.Event()
    result: dict[str, object] = {}

    def background_query() -> None:
        with database.connect() as conn:
            entered.set()
            closed.wait(5)
            result["value"] = conn.execute("SELECT 1").fetchone()[0]
            result["connection"] = conn

    thread = threading.Thread(target=background_query)
    thread.start()
    assert entered.wait(5)
    database.close()
    closed.set()
    thread.join(5)

    assert result["value"] == 1
    # 线程用完后自行关闭了被移出连接池的连接。
    with pytest.raises(sqlite3.ProgrammingError):
        result["connection"].execute("SELECT 1")
    # 之后再次使用会重新打开连接。
    assert database.ui_settings() is not None


def test_model_setting_fills_missing_fields_and_ignores_bad_data(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    defaults = AsrSettings(mode="faster_whisper")
    assert database.asr_settings(defaults) == defaults

    # 只保存了部分字段：其余字段用调用方给的默认值补齐。
    database.set_runtime_json("asr", {"whisperjav_ensemble": False})
    assert database.asr_settings(defaults).mode == "faster_whisper"
    assert database.asr_settings(defaults).whisperjav_ensemble is False

    # 坏数据（不是 JSON、类型不对、不符合模型）都回到默认值，不阻止主程序启动。
    for broken in ("not json", "[]", '{"mode": "unknown"}'):
        database._set_setting_text("asr", broken)
        assert database.asr_settings(defaults) == defaults

    database.delete_setting("asr")
    assert database.runtime_json("asr") is None
