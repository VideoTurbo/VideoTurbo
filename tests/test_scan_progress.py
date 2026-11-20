import asyncio
import json
from pathlib import Path

import pytest
from conftest import force_job_state

from video_turbo.config import AppConfig, MediaLibraryConfig
from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.media import output_subtitle_path
from video_turbo.media_sources.local import LocalSource
from video_turbo.models import Cue, JobState, SourceKind
from video_turbo.orchestrator.processor import Orchestrator
from video_turbo.source_paths import text as path_text


def test_scan_publishes_live_result_object(tmp_path: Path, monkeypatch) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    video = media_root / "movie.mkv"
    video.write_bytes(b"movie")
    config = AppConfig(
        media_libraries=[MediaLibraryConfig(id="lib-1", name="电影", path=media_root)],
        scan={"settle_minutes": 0},
        wake_on_lan={"enabled": False},
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    orchestrator = Orchestrator(config, database, tmp_path / "cache")
    monkeypatch.setattr(
        "video_turbo.orchestrator.processor.run_ffprobe",
        lambda _path: {
            "format": {"duration": "60"},
            "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
        },
    )

    orchestrator.set_paused(True)
    paused = asyncio.run(orchestrator.scan())
    assert paused["status"] == "paused"
    assert database.is_pipeline_paused() is True
    orchestrator.set_paused(False)

    result = asyncio.run(orchestrator.scan())

    assert result is orchestrator.last_scan
    assert result["status"] == "completed"
    assert result["scanned"] == 1
    assert result["pending"] == 1
    assert result["started_at"]
    assert result["finished_at"]
    assert result["current_path"] is None

    repeated = asyncio.run(orchestrator.scan())
    assert repeated["scanned"] == 1
    assert repeated["pending"] == 0
    assert repeated["completed"] == 0


def test_manual_scan_uses_selected_enabled_libraries(tmp_path: Path, monkeypatch) -> None:
    libraries = []
    for name in ("first", "second", "disabled"):
        root = tmp_path / name
        root.mkdir()
        (root / f"{name}.mkv").write_bytes(b"movie")
        libraries.append(MediaLibraryConfig(id=f"lib-{name}", name=name, path=root, enabled=name != "disabled"))
    config = AppConfig(
        media_libraries=libraries,
        scan={"settle_minutes": 0},
        wake_on_lan={"enabled": False},
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    orchestrator = Orchestrator(config, database, tmp_path / "cache")
    monkeypatch.setattr(
        "video_turbo.orchestrator.processor.run_ffprobe",
        lambda _path: {
            "format": {"duration": "60"},
            "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
        },
    )

    selected = asyncio.run(orchestrator.scan(("lib-second",)))
    assert selected["libraries"] == ["second"]
    assert selected["scanned"] == 1
    assert selected["pending"] == 1

    scheduled = asyncio.run(orchestrator.scan())
    assert scheduled["libraries"] == ["first", "second"]
    assert scheduled["scanned"] == 2
    assert scheduled["pending"] == 1


def _scan_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Database, Orchestrator, list[Path]]:
    media_root = tmp_path / "media"
    media_root.mkdir()
    config = AppConfig(
        media_libraries=[MediaLibraryConfig(id="lib-1", name="电影", path=media_root)],
        scan={"settle_minutes": 0},
        wake_on_lan={"enabled": False},
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    orchestrator = Orchestrator(config, database, tmp_path / "cache")
    probed: list[Path] = []

    def fake_ffprobe(path: Path) -> dict:
        probed.append(path)
        return {
            "format": {"duration": str(path.stat().st_size)},
            "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
        }

    monkeypatch.setattr("video_turbo.orchestrator.processor.run_ffprobe", fake_ffprobe)
    return media_root, database, orchestrator, probed


def _media_row(database: Database, video: Path):
    with database.connect() as conn:
        return conn.execute(
            "SELECT duration, probe_json FROM media_items WHERE path = ?", (str(video),)
        ).fetchone()


def test_scan_reuses_probe_until_file_changes(tmp_path: Path, monkeypatch) -> None:
    media_root, database, orchestrator, probed = _scan_fixture(tmp_path, monkeypatch)
    video = media_root / "movie.mkv"
    video.write_bytes(b"movie")

    assert asyncio.run(orchestrator.scan())["pending"] == 1
    repeated = asyncio.run(orchestrator.scan())
    assert repeated["pending"] == 0
    assert probed == [video]

    video.write_bytes(b"replaced movie")
    changed = asyncio.run(orchestrator.scan())
    assert changed["pending"] == 1
    assert probed == [video, video]
    assert _media_row(database, video)["duration"] == len(b"replaced movie")


def test_scan_skips_ffprobe_when_subtitle_already_exists(tmp_path: Path, monkeypatch) -> None:
    media_root, database, orchestrator, probed = _scan_fixture(tmp_path, monkeypatch)
    video = media_root / "movie.mkv"
    video.write_bytes(b"movie")
    output_subtitle_path(video, "zh-CN", orchestrator.config.subtitles).write_text("字幕", encoding="utf-8")

    result = asyncio.run(orchestrator.scan())

    assert result["completed"] == 1
    assert result["errors"] == []
    assert probed == []
    row = _media_row(database, video)
    assert row["duration"] is None
    assert json.loads(row["probe_json"]) == {}


def test_early_skip_discards_probe_of_replaced_file(tmp_path: Path, monkeypatch) -> None:
    media_root, database, orchestrator, probed = _scan_fixture(tmp_path, monkeypatch)
    video = media_root / "movie.mkv"
    video.write_bytes(b"movie")
    assert asyncio.run(orchestrator.scan())["pending"] == 1

    # 已有字幕时替换影片：本次跳过不探测，但旧探测结果必须作废。
    subtitle = output_subtitle_path(video, "zh-CN", orchestrator.config.subtitles)
    subtitle.write_text("字幕", encoding="utf-8")
    video.write_bytes(b"new version")
    assert asyncio.run(orchestrator.scan())["completed"] == 1
    assert probed == [video]
    assert json.loads(_media_row(database, video)["probe_json"]) == {}

    subtitle.unlink()
    rescanned = asyncio.run(orchestrator.scan())
    assert rescanned["pending"] == 1
    assert probed == [video, video]
    assert _media_row(database, video)["duration"] == len(b"new version")


def test_early_skip_keeps_probe_of_unchanged_file(tmp_path: Path, monkeypatch) -> None:
    media_root, database, orchestrator, probed = _scan_fixture(tmp_path, monkeypatch)
    video = media_root / "movie.mkv"
    video.write_bytes(b"movie")
    assert asyncio.run(orchestrator.scan())["pending"] == 1

    subtitle = output_subtitle_path(video, "zh-CN", orchestrator.config.subtitles)
    subtitle.write_text("字幕", encoding="utf-8")
    assert asyncio.run(orchestrator.scan())["completed"] == 1
    assert _media_row(database, video)["duration"] == len(b"movie")

    subtitle.unlink()
    assert asyncio.run(orchestrator.scan())["pending"] == 1
    assert probed == [video]


def test_scan_lists_each_directory_once(tmp_path: Path, monkeypatch) -> None:
    media_root, _database, orchestrator, probed = _scan_fixture(tmp_path, monkeypatch)
    nested = media_root / "nested"
    nested.mkdir()
    for directory in (media_root, nested):
        for name in ("first", "second", "third"):
            (directory / f"{name}.mkv").write_bytes(b"movie")
    (media_root / "first.zh-CN.srt").write_text("字幕", encoding="utf-8")
    (nested / "third.chs.ass").write_text("字幕", encoding="utf-8")
    listed: list[Path] = []
    original = LocalSource.list_dir

    def counting_list(self: LocalSource, directory: str) -> list:
        listed.append(Path(directory))
        return original(self, directory)

    monkeypatch.setattr(LocalSource, "list_dir", counting_list)

    result = asyncio.run(orchestrator.scan())

    assert sorted(listed) == sorted([media_root, nested])
    assert result["pending"] == 4
    assert result["completed"] == 2
    assert sorted(path.name for path in probed) == [
        "first.mkv",
        "second.mkv",
        "second.mkv",
        "third.mkv",
    ]


def test_pipeline_prioritizes_asr_completed_before_new_audio_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    requested_states: list[JobState] = []

    class FakeDatabase:
        def is_pipeline_paused(self) -> bool:
            return False

        def next_job_in_state(self, state: JobState):
            requested_states.append(state)
            if state == JobState.ASR_COMPLETED:
                return {"id": "translated-first"}
            if state == JobState.DISCOVERED:
                return {"id": "extract-later"}
            return None

        def set_job_state(self, _job_id: str, _state: JobState) -> bool:
            return True

    orchestrator = Orchestrator(
        AppConfig(translation={"api_key": "token"}), FakeDatabase(), tmp_path / "cache"
    )
    translated: list[str] = []

    async def fake_translate(job: dict, *, state_claimed: bool = False) -> None:
        assert state_claimed is True
        translated.append(job["id"])

    monkeypatch.setattr(orchestrator, "_translate_job", fake_translate)

    async def run() -> None:
        assert await orchestrator.process_once() is True
        await asyncio.sleep(0)
        await orchestrator.close()

    asyncio.run(run())
    assert translated == ["translated-first"]
    assert requested_states == [JobState.ASR_COMPLETED]


def test_translation_job_waits_when_only_fallback_nodes_are_available(tmp_path: Path) -> None:
    state_updates: list[tuple[str, JobState, str | None]] = []

    class FakeDatabase:
        def is_pipeline_paused(self) -> bool:
            return False

        def translation_upstream_states(self) -> dict:
            return {"primary-1": {"enabled": False, "consecutive_failures": 8}}

        def set_translation_upstream_states(self, _states: dict) -> None:
            pass

        def set_job_state(
            self, job_id: str, state: JobState, error: str | None = None
        ) -> bool:
            state_updates.append((job_id, state, error))
            return True

    config = AppConfig(
        translation={
            "api_key": None,
            "model": "grok-4.5",
            "upstreams": [
                {
                    "name": "primary-1",
                    "base_url": "http://primary-1/v1",
                    "api_key": "primary-token",
                }
            ],
            "fallback_model": "deepseek-v4-flash",
            "fallback_api_key": "fallback-token",
        }
    )
    orchestrator = Orchestrator(config, FakeDatabase(), tmp_path / "cache")

    started = orchestrator._start_translation_job(
        {"id": "job-1", "state": JobState.ASR_COMPLETED}
    )

    assert started is False
    assert orchestrator.translation_tasks == {}
    assert orchestrator.translation_status()["primary_available"] is False
    assert [(job_id, state, render(error)) for job_id, state, error in state_updates] == [
        (
            "job-1",
            JobState.WAITING_TRANSLATION,
            "主力模型当前全部不可用；备用模型不会顶替主力模型，已暂停翻译。",
        )
    ]


def test_pipeline_can_run_two_translation_jobs_at_once(tmp_path: Path, monkeypatch) -> None:
    jobs = [{"id": "movie-1"}, {"id": "movie-2"}]

    class FakeDatabase:
        def is_pipeline_paused(self) -> bool:
            return False

        def next_job_in_state(self, state: JobState):
            if state == JobState.ASR_COMPLETED and jobs:
                return jobs.pop(0)
            return None

        def set_job_state(self, _job_id: str, _state: JobState) -> bool:
            return True

    orchestrator = Orchestrator(
        AppConfig(translation={"api_key": "token", "job_concurrency": 2}),
        FakeDatabase(),
        tmp_path / "cache",
    )
    started: list[str] = []
    release = asyncio.Event()

    async def fake_translate(job: dict, *, state_claimed: bool = False) -> None:
        assert state_claimed is True
        started.append(job["id"])
        await release.wait()

    monkeypatch.setattr(orchestrator, "_translate_job", fake_translate)

    async def run() -> None:
        assert await orchestrator.process_once() is True
        assert await orchestrator.process_once() is True
        await asyncio.sleep(0)
        assert set(started) == {"movie-1", "movie-2"}
        assert len(orchestrator.translation_tasks) == 2
        release.set()
        await asyncio.gather(*orchestrator.translation_tasks.values())
        orchestrator._reap_translation_tasks()
        assert orchestrator.translation_tasks == {}
        await orchestrator.close()

    asyncio.run(run())


def test_translation_job_concurrency_limits_started_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeDatabase:
        def is_pipeline_paused(self) -> bool:
            return False

        def set_job_state(self, _job_id: str, _state: JobState, error=None) -> bool:
            return True

    orchestrator = Orchestrator(
        AppConfig(
            translation={"api_key": "translation-token", "concurrency": 8, "job_concurrency": 2},
        ),
        FakeDatabase(),
        tmp_path / "cache",
    )
    release = asyncio.Event()

    async def fake_translate(job: dict, *, state_claimed: bool = False) -> None:
        assert state_claimed is True
        await release.wait()

    monkeypatch.setattr(orchestrator, "_translate_job", fake_translate)

    async def run() -> None:
        assert orchestrator._start_translation_job({"id": "translation-1"}) is True
        assert orchestrator._start_translation_job({"id": "translation-2"}) is True
        assert orchestrator._start_translation_job({"id": "translation-3"}) is False
        assert set(orchestrator.translation_tasks) == {"translation-1", "translation-2"}
        assert orchestrator.translation_request_semaphore._value == 8
        release.set()
        await asyncio.gather(*orchestrator.translation_tasks.values())
        orchestrator._reap_translation_tasks()
        await orchestrator.close()

    asyncio.run(run())


# owns_subtitle=False：任务记为已完成时字幕是影片原有的（扫描判定或发布时目标已存在）。
@pytest.mark.parametrize("owns_subtitle", [True, False])
def test_existing_subtitle_retranslation_replaces_its_own_subtitle(
    tmp_path: Path, monkeypatch, owns_subtitle: bool
) -> None:
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    config = AppConfig(
        translation={"api_key": "token"},
        media_libraries=[MediaLibraryConfig(id="lib-1", name="电影", path=tmp_path)],
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=60,
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
    orchestrator = Orchestrator(config, database, tmp_path / "cache")

    class FakeClient:
        async def aclose(self) -> None:
            return None

    orchestrator.translation_http_client = FakeClient()
    ja_path = orchestrator._job_dir(job_id) / "result.source.json"
    ja_path.parent.mkdir(parents=True)
    cue = Cue(cue_id=1, start=1, end=3, source="こんにちは")
    ja_path.write_text(cue.model_dump_json().join(("[", "]")), encoding="utf-8")
    zh_path = orchestrator._job_dir(job_id) / "result.target.json"
    zh_path.write_text("旧完整翻译\n", encoding="utf-8")
    checkpoint_path = orchestrator._job_dir(job_id) / "result.target.partial.json"
    checkpoint_path.write_text("旧翻译检查点\n", encoding="utf-8")
    subtitle = output_subtitle_path(movie, "zh-CN", config.subtitles)
    subtitle.write_text("旧字幕\n", encoding="utf-8")
    fields = {"source_result_path": str(ja_path), "target_result_path": str(zh_path)}
    if owns_subtitle:
        fields["subtitle_path"] = str(subtitle)
    database.start_job(job_id)
    force_job_state(database, job_id, JobState.COMPLETED, fields=fields)

    async def fake_translate(cues, _config, **_kwargs):
        assert not checkpoint_path.exists()
        assert database.get_job(job_id)["reprocess_mode"] == "retranslate_running"
        assert render(zh_path.read_text(encoding="utf-8")) == "旧完整翻译\n"
        translated = [item.model_copy(deep=True) for item in cues]
        translated[0].target = "你好"
        metrics = _kwargs["metrics"]
        metrics.initialize_batches(1, 0)
        metrics.batch_completed(len(translated), "grok-4.5")
        metrics.finish()
        return translated

    monkeypatch.setattr("video_turbo.orchestrator.processor.translate_cues", fake_translate)

    assert orchestrator.reprocess_job(job_id, "retranslate") == JobState.ASR_COMPLETED
    assert render(subtitle.read_text(encoding="utf-8")) == "旧字幕\n"
    assert render(zh_path.read_text(encoding="utf-8")) == "旧完整翻译\n"
    assert render(checkpoint_path.read_text(encoding="utf-8")) == "旧翻译检查点\n"
    queued = database.get_job(job_id)
    assert queued is not None
    assert queued["target_result_path"] == path_text(zh_path)
    assert queued["reprocess_mode"] == "retranslate"
    assert asyncio.run(orchestrator.pause_job(job_id)) is True
    assert render(zh_path.read_text(encoding="utf-8")) == "旧完整翻译\n"
    assert render(checkpoint_path.read_text(encoding="utf-8")) == "旧翻译检查点\n"
    assert orchestrator.resume_job(job_id) == JobState.ASR_COMPLETED

    async def run() -> None:
        job = database.get_job(job_id)
        assert job is not None
        await orchestrator._translate_job(job)
        await orchestrator.close()

    asyncio.run(run())

    completed = database.get_job(job_id)
    assert completed is not None
    assert completed["state"] == JobState.COMPLETED
    assert completed["reprocess_mode"] is None
    assert "你好" in render(zh_path.read_text(encoding="utf-8"))
    assert "你好" in render(subtitle.read_text(encoding="utf-8"))
    detail = orchestrator.job_detail(job_id)
    assert detail is not None
    assert detail["translation_stats"]["provider_cue_counts"] == {"grok": 1}
    assert detail["translation_stats"]["provider_percentages"] == {"grok": 100.0}
    assert detail["translation_stats"]["upstream_cue_counts"] == {"primary": 1}


def test_audio_duration_allows_valid_asr_cues_past_bad_container_duration(
    tmp_path: Path, monkeypatch
) -> None:
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"movie")
    config = AppConfig(
        translation={"api_key": "token"},
        media_libraries=[MediaLibraryConfig(id="lib-1", name="电影", path=tmp_path)],
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    media_id, changed = database.upsert_media(
        path=movie,
        library_id="lib-1",
        size=movie.stat().st_size,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=60,
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
    orchestrator = Orchestrator(config, database, tmp_path / "cache")
    job_dir = orchestrator._job_dir(job_id)
    job_dir.mkdir(parents=True)
    audio_path = job_dir / "audio.opus"
    audio_path.write_bytes(b"audio")
    ja_path = job_dir / "result.source.json"
    cue = Cue(cue_id=1, start=70, end=75, source="最後の台詞")
    ja_path.write_text(cue.model_dump_json().join(("[", "]")), encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, 
        job_id,
        JobState.ASR_COMPLETED,
        fields={
            "audio_path": str(audio_path),
            "audio_sha256": "hash",
            "source_result_path": str(ja_path),
            "asr_duration_seconds": 75.0,
        },
    )

    async def fake_translate(cues, _config, **kwargs):
        translated = [item.model_copy(update={"target": "最后一句"}) for item in cues]
        metrics = kwargs["metrics"]
        metrics.initialize_batches(1, 0)
        metrics.batch_completed(1, "grok-4.5")
        metrics.finish()
        return translated

    monkeypatch.setattr(
        "video_turbo.orchestrator.processor.run_ffprobe",
        lambda _path: {"format": {"duration": "60"}},
    )
    monkeypatch.setattr("video_turbo.orchestrator.processor.translate_cues", fake_translate)

    async def run() -> None:
        job = database.get_job(job_id)
        assert job is not None
        await orchestrator._translate_job(job)
        await orchestrator.close()

    asyncio.run(run())

    completed = database.get_job(job_id)
    assert completed is not None
    assert completed["state"] == JobState.COMPLETED



def test_scan_can_be_cancelled_midway(tmp_path: Path, monkeypatch) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    # 文件名不能命中内嵌字幕规则：例如「c.mkv」会按带中字的影片直接跳过、不调用 ffprobe。Linux 上目录遍历
    # 顺序不固定，它排在第一个时本测试会多扫一部影片。
    for name in ("first.mkv", "second.mkv", "third.mkv"):
        (media_root / name).write_bytes(b"movie")
    config = AppConfig(
        media_libraries=[MediaLibraryConfig(id="lib-1", name="电影", path=media_root)],
        scan={"settle_minutes": 0},
        wake_on_lan={"enabled": False},
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    orchestrator = Orchestrator(config, database, tmp_path / "cache")
    assert orchestrator.cancel_scan() is False  # 没有扫描在进行

    def probe_then_cancel(_path: Path) -> dict:
        assert orchestrator.cancel_scan() is True
        return {
            "format": {"duration": "60"},
            "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
        }

    monkeypatch.setattr("video_turbo.orchestrator.processor.run_ffprobe", probe_then_cancel)
    result = asyncio.run(orchestrator.scan())

    assert result["status"] == "cancelled"
    assert result["scanned"] == 1 and result["pending"] == 1
    assert orchestrator.scan_status == "idle"

    # 停止标记不会带到下一次扫描。
    monkeypatch.setattr(
        "video_turbo.orchestrator.processor.run_ffprobe",
        lambda _path: {
            "format": {"duration": "60"},
            "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
        },
    )
    again = asyncio.run(orchestrator.scan())
    assert again["status"] == "completed" and again["scanned"] == 3
