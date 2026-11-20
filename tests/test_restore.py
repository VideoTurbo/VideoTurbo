import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import PROTOCOL, prepare_server, raises_rendered
from fastapi.testclient import TestClient

from video_turbo.config import AppConfig, SubtitleConfig
from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.media import decide_without_probe
from video_turbo.models import RestoreState
from video_turbo.naming import job_dir_name
from video_turbo.orchestrator import restore_service
from video_turbo.orchestrator.app import app
from video_turbo.restore import (
    finalize_restore,
    ineligible_reason,
    marker_path,
    output_extension,
    restored_stem,
    sidecar_files,
    upload_temp_path,
)
from video_turbo.worker import restore_task
from video_turbo.worker.config import JasnaConfig, WorkerConfig
from video_turbo.worker.jasna_progress import OutputSplitter, parse_progress_line
from video_turbo.worker.restore_task import MachineLock, RestoreTask

PATTERNS = AppConfig().restore.skip_path_patterns
VIDEO_EXTENSIONS = ["mp4", "mkv"]


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("ABC-123", "ABC-123-U"),
        ("ABC-123-C", "ABC-123-UC"),
        ("abc-123-c", "abc-123-uc"),
        ("ABC-123_C", "ABC-123_UC"),
        ("ABC-123C", "ABC-123C-U"),
    ],
)
def test_restored_stem(stem: str, expected: str) -> None:
    assert restored_stem(stem) == expected


def test_output_extension_keeps_writable_containers() -> None:
    assert output_extension(".MKV") == "mkv"
    assert output_extension("mp4") == "mp4"
    assert output_extension(".avi") == "mp4"
    assert output_extension(".ts") == "mp4"


@pytest.mark.parametrize(
    ("relative", "eligible"),
    [
        ("ABC-123/ABC-123.mp4", True),
        ("ABC-123/ABC-123-C.mp4", True),
        ("x/ABC-123-U.mp4", False),
        ("x/ABC-123-UC.mp4", False),
        ("x/abc-123-uc.mkv", False),
        ("x/ABCU-123.mp4", True),
        ("x/JUFD-634_restored_wm.mp4", False),
        ("x/JUFD-634-restored.mkv", False),
        ("x/Unrestored-123.mp4", True),
    ],
)
def test_marked_names_hide_restore(tmp_path: Path, relative: str, eligible: bool) -> None:
    video = tmp_path / relative
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    assert (ineligible_reason(video, PATTERNS) is None) is eligible


def test_marker_file_hides_restore(tmp_path: Path) -> None:
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"x")
    marker_path(video).write_text("{}")
    assert "标记" in render(ineligible_reason(video, PATTERNS))


def test_uc_suffix_skips_subtitles_by_default(tmp_path: Path) -> None:
    config = SubtitleConfig()
    for name, skipped in [("ABC-123-UC.mp4", True), ("ABC-123-C.mp4", True), ("ABC-123-U.mp4", False)]:
        video = tmp_path / name
        video.write_bytes(b"x")
        decision = decide_without_probe(video, config, LanguagePair(), [])
        assert (decision is not None) is skipped, name


def test_sidecars_exclude_other_movies(tmp_path: Path) -> None:
    for name in [
        "ABC-123.mp4", "ABC-123.nfo", "ABC-123.zh.default.srt", "ABC-123-poster.jpg",
        "ABC-123-C.mp4", "ABC-123-C.nfo", "ABC-1234.nfo", "poster.jpg", ".ABC-123.hidden",
    ]:
        (tmp_path / name).write_bytes(b"x")
    names = [path.name for path in sidecar_files(tmp_path / "ABC-123.mp4", VIDEO_EXTENSIONS)]
    assert names == ["ABC-123-poster.jpg", "ABC-123.nfo", "ABC-123.zh.default.srt"]


def _library(tmp_path: Path, folder: str, stem: str) -> tuple[Path, Path]:
    library = tmp_path / "library"
    movie_dir = library / folder
    movie_dir.mkdir(parents=True)
    source = movie_dir / f"{stem}.mp4"
    source.write_bytes(b"original")
    for suffix in [".nfo", ".zh.default.srt", "-poster.jpg"]:
        (movie_dir / f"{stem}{suffix}").write_text(suffix)
    (movie_dir / "fanart.jpg").write_text("folder art")
    return library, source


def test_finalize_renames_files_folder_and_backs_up_original(tmp_path: Path) -> None:
    library, source = _library(tmp_path, "[ABC-123-C] 标题", "ABC-123-C")
    upload = upload_temp_path(source, "mp4")
    upload.write_bytes(b"restored")
    backup_dir = tmp_path / "backup"

    result = finalize_restore(
        source=source, upload=upload, output_ext="mp4", library_root=library, library_name="lib",
        backup_dir=backup_dir, video_extensions=VIDEO_EXTENSIONS, marker_payload={"job_id": "j1"},
    )

    folder = library / "[ABC-123-UC] 标题"
    assert result.output_path == folder / "ABC-123-UC.mp4"
    assert result.output_path.read_bytes() == b"restored"
    assert sorted(path.name for path in folder.iterdir()) == [
        "ABC-123-UC-poster.jpg", "ABC-123-UC.jasna.json", "ABC-123-UC.mp4", "ABC-123-UC.nfo",
        "ABC-123-UC.zh.default.srt", "fanart.jpg",
    ]
    assert not (library / "[ABC-123-C] 标题").exists()
    assert result.backup_path == backup_dir / "lib" / "[ABC-123-C] 标题" / "ABC-123-C.mp4"
    assert result.backup_path.read_bytes() == b"original"
    marker = json.loads(result.marker.read_text(encoding="utf-8"))
    assert marker["job_id"] == "j1" and marker["original_name"] == "ABC-123-C.mp4"


def test_finalize_keeps_shared_folder_name(tmp_path: Path) -> None:
    library, source = _library(tmp_path, "ABC-123", "ABC-123")
    (source.parent / "ABC-123-part2.mp4").write_bytes(b"other")
    upload = upload_temp_path(source, "mp4")
    upload.write_bytes(b"restored")
    result = finalize_restore(
        source=source, upload=upload, output_ext="mp4", library_root=library, library_name="lib",
        backup_dir=tmp_path / "backup", video_extensions=VIDEO_EXTENSIONS, marker_payload={},
    )
    assert result.folder_renamed_to is None
    assert result.output_path == library / "ABC-123" / "ABC-123-U.mp4"


def test_finalize_never_renames_library_root(tmp_path: Path) -> None:
    library = tmp_path / "ABC-123"
    library.mkdir()
    source = library / "ABC-123.mp4"
    source.write_bytes(b"original")
    upload = upload_temp_path(source, "mp4")
    upload.write_bytes(b"restored")
    result = finalize_restore(
        source=source, upload=upload, output_ext="mp4", library_root=library, library_name="lib",
        backup_dir=tmp_path / "backup", video_extensions=VIDEO_EXTENSIONS, marker_payload={},
    )
    assert result.output_path == library / "ABC-123-U.mp4"


def test_backup_dir_must_be_outside_libraries(tmp_path: Path) -> None:
    with raises_rendered(ValueError, "原片备份目录"):
        AppConfig.model_validate(
            {
                "media_libraries": [{"id": "lib-a", "name": "a", "path": str(tmp_path / "lib")}],
                "restore": {"backup_dir": str(tmp_path / "lib" / "backup")},
            }
        )


def test_progress_parser_reads_jasna_bars() -> None:
    video = parse_progress_line(
        "Processing video:  45%|████     |Processed: 1:23 (2848f) | Remaining: 1:02:10 (123f) | Speed: 35.0fps"
    )
    assert render(video.detail) == "修复视频" and video.percent == 45 and video.fps == 35.0 and video.eta_seconds == 3730
    ltx = parse_progress_line("LTX 3/4 denoise:  30%|██        | 12/40 [00:10<00:30,  1.20frame/s]")
    assert render(ltx.detail) == "LTX 第 3/4 遍 · 扩散修复" and ltx.eta_seconds == 30 and ltx.fps == 1.2
    download = parse_progress_line("LTX download:  12%|█ | 1.80G/15.0G [01:00<07:00, 31.2MB/s]")
    assert render(download.detail) == "下载 LTX 模型" and download.eta_seconds == 420
    assert parse_progress_line("installation verified") is None


def test_output_splitter_handles_carriage_returns() -> None:
    splitter = OutputSplitter()
    assert splitter.feed(b"a: 1%|x|\rb: 2%") == ["a: 1%|x|"]
    assert splitter.feed(b"|y|\nlog line\n") == ["b: 2%|y|", "log line"]
    assert splitter.flush() == []


def test_configure_jasna_updates_existing_block(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from configure_jasna import update

    original = "name: w\njasna:\n  enabled: false  # 注释\n  ltx: false\nother: 1\n"
    updated = update(original, enabled=True, ltx=True)
    assert yaml.safe_load(updated) == {"name": "w", "jasna": {"enabled": True, "ltx": True}, "other": 1}
    assert "# 注释" in render(updated)
    appended = yaml.safe_load(update("name: w\n", enabled=True, ltx=None))
    assert appended["jasna"]["enabled"] is True and appended["jasna"]["ltx"] is False


def test_machine_lock_is_exclusive(tmp_path: Path) -> None:
    first, second = MachineLock(tmp_path), MachineLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    assert not MachineLock(None).acquire()


# ---------------------------------------------------------------------------
# 端到端：主控 API + Worker 断点续传
# ---------------------------------------------------------------------------


def _start_app(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    library = tmp_path / "library"
    backup = tmp_path / "backup"
    prepare_server(tmp_path, monkeypatch, {"media_libraries": [{"id": "lib-lib", "name": "lib", "path": str(library)}]}, worker_token="token", backup_dir=backup)
    return library, backup


def _fake_probe(path: Path) -> dict:
    return {"format": {"duration": "100.0"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}


def test_restore_end_to_end_with_resumed_transfers(tmp_path: Path, monkeypatch) -> None:
    library, backup = _start_app(tmp_path, monkeypatch)
    movie_dir = library / "ABC-123"
    movie_dir.mkdir(parents=True)
    source = movie_dir / "ABC-123.mp4"
    source_bytes = bytes(range(256)) * 12_000  # ~3 MB
    source.write_bytes(source_bytes)
    (movie_dir / "ABC-123.zh.default.srt").write_text("1\n")
    (tmp_path / "library" / "XYZ-999-U.mp4").write_bytes(b"u")
    monkeypatch.setattr(restore_service, "run_ffprobe", _fake_probe)
    restored = b"R" * 2_600_000

    def fake_jasna(config, *, source, output, working_directory, restoration_model, codec, should_cancel, on_progress, on_stage, ltx_large_canvas=False):
        assert source.read_bytes() == source_bytes
        on_progress(parse_progress_line("Processing video: 100%|█|Processed: 0:01 (30f) | Remaining: 0:00 (0f) | Speed: 30.0fps"))
        output.write_bytes(restored)
        return {"tool": "jasna", "restoration_model": restoration_model}

    monkeypatch.setattr(restore_task, "run_jasna", fake_jasna)
    headers = {**PROTOCOL, "X-Worker-Token": "token"}

    with TestClient(app) as client:
        database = app.state.database
        service = app.state.restore
        real_process_once = service.process_once

        async def idle() -> bool:
            return False

        monkeypatch.setattr(service, "process_once", idle)
        stat = source.stat()
        media_id, _ = database.upsert_media(
            path=source, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            duration=100.0, probe=None, decision="done", reason="已有字幕", file_key=f"{stat.st_dev}:{stat.st_ino}",
        )
        other = tmp_path / "library" / "XYZ-999-U.mp4"
        other_id, _ = database.upsert_media(
            path=other, library_id="lib-lib", size=1, mtime_ns=other.stat().st_mtime_ns,
            duration=None, probe=None, decision="process", reason="", file_key=None,
        )

        media = client.get("/api/restore/media", params={"status": "all"}).json()
        by_id = {item["media_id"]: item for item in media["items"]}
        assert by_id[media_id]["can_restore"] is True
        assert by_id[media_id]["actions"] == ["restore"]
        # 不需要修复的影片算已完成，原因放在说明里。
        assert by_id[other_id]["can_restore"] is False and by_id[other_id]["status"] == "completed"
        assert by_id[other_id]["status_reason"]
        assert media["counts"] == {"all": 2, "pending": 1, "active": 0, "failed": 0, "completed": 1}

        created = client.post(
            "/api/restore/jobs", json={"media_ids": [media_id, other_id], "restoration_model": "basicvsrpp", "codec": "hevc"}
        ).json()
        assert len(created["created"]) == 1 and created["skipped"][0]["media_id"] == other_id
        job_id = created["created"][0]
        # 同一影片不能重复提交。
        again = client.post("/api/restore/jobs", json={"media_ids": [media_id]}).json()
        assert again["created"] == [] and "进行中" in render(again["skipped"][0]["reason"])

        asyncio.run(real_process_once())
        job = database.get_restore_job(job_id)
        assert job["state"] == RestoreState.WAITING
        assert job["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()

        # 旧 Worker（未声明影片修复能力）领不到任务；LTX 任务只给声明 ltx 的 Worker。
        registration = {
            "name": "w1", "version": "0.0.0", "gpu_name": "RTX", "memory_total_mb": 1, "model": "m", "compute_type": "c", "supported_asr_modes": ["faster_whisper"],
        }
        client.post("/api/workers/register", json=registration, headers=headers)
        assert client.post("/api/workers/w1/restore/claim", headers=headers).json() is None
        client.post(
            "/api/workers/register",
            json={**registration, "supports_restore": True, "restore_models": ["basicvsrpp"]},
            headers=headers,
        )
        claim = client.post("/api/workers/w1/restore/claim", headers=headers).json()
        assert claim["job_id"] == job_id and claim["output_extension"] == "mp4"

        # 下载只能由租约持有者发起，且支持 Range。
        assert client.get(claim["source_url"], headers={**headers, "X-Worker-Name": "w2"}).status_code == 409
        ranged = client.get(claim["source_url"], headers={**headers, "X-Worker-Name": "w1", "Range": "bytes=10-19"})
        assert ranged.status_code == 206 and ranged.content == source_bytes[10:20]

        worker_config = WorkerConfig(
            worker_token="token", name="w1", cache_dir=tmp_path / "worker-cache",
            jasna=JasnaConfig(enabled=True, upload_chunk_mb=1),
        )
        job_dir = worker_config.restore_dir / job_dir_name(claim["title"], job_id, ascii_only=True)
        job_dir.mkdir(parents=True)
        # 模拟上次中断：已下载前 1 MB。
        (job_dir / "source.mp4").write_bytes(source_bytes[: 1024 * 1024])
        # 模拟上次回传中断：主控已收到前 1 MB。
        first = client.put(
            claim["upload_url"], params={"offset": 0}, content=restored[: 1024 * 1024],
            headers={**headers, "X-Worker-Name": "w1"},
        )
        assert first.json() == {"offset": 1024 * 1024}
        wrong = client.put(
            claim["upload_url"], params={"offset": 0}, content=b"x", headers={**headers, "X-Worker-Name": "w1"}
        )
        assert wrong.status_code == 409

        reports = []

        async def run_worker() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as worker_client:
                task = RestoreTask(worker_config, worker_client, claim, report=reports.append, should_cancel=lambda: False)
                await task.run()

        asyncio.run(run_worker())
        stages = {report.stage for report in reports}
        assert {"downloading", "processing", "uploading"} <= stages
        download_reports = [r for r in reports if r.stage == "downloading" and r.transferred_bytes is not None]
        assert download_reports[0].transferred_bytes == 1024 * 1024  # 从断点开始计数
        assert not job_dir.exists()  # 完成后清理本地临时文件
        assert database.get_restore_job(job_id)["state"] == RestoreState.FINALIZING

        asyncio.run(real_process_once())
        job = database.get_restore_job(job_id)
        assert job["state"] == RestoreState.COMPLETED, job["last_error"]
        new_dir = library / "ABC-123-U"
        assert (new_dir / "ABC-123-U.mp4").read_bytes() == restored
        assert (new_dir / "ABC-123-U.zh.default.srt").is_file()
        assert (new_dir / "ABC-123-U.jasna.json").is_file()
        assert Path(job["backup_path"]) == backup / "lib" / "ABC-123" / "ABC-123.mp4"
        assert database.get_media(media_id)["path"] == str(new_dir / "ABC-123-U.mp4")
        media = client.get("/api/restore/media", params={"status": "all"}).json()
        assert {item["media_id"]: item for item in media["items"]}[media_id]["can_restore"] is False

        purged = client.post(f"/api/restore/jobs/{job_id}/actions/purge_original")
        assert purged.status_code == 200
        assert not Path(job["backup_path"]).exists() and not (backup / "lib").exists()
        assert client.post(f"/api/restore/jobs/{job_id}/actions/purge_original").status_code == 409


def test_worker_failure_and_retry(tmp_path: Path, monkeypatch) -> None:
    library, _ = _start_app(tmp_path, monkeypatch)
    library.mkdir(parents=True)
    source = library / "ABC-001.mkv"
    source.write_bytes(b"video")
    headers = {**PROTOCOL, "X-Worker-Token": "token"}
    with TestClient(app) as client:
        database = app.state.database
        service = app.state.restore
        real_process_once = service.process_once

        async def idle() -> bool:
            return False

        monkeypatch.setattr(service, "process_once", idle)
        stat = source.stat()
        media_id, _ = database.upsert_media(
            path=source, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            duration=None, probe=None, decision="done", reason="", file_key=None,
        )
        job_id = client.post(
            "/api/restore/jobs", json={"media_ids": [media_id], "restoration_model": "ltx", "codec": "h264"}
        ).json()["created"][0]
        asyncio.run(real_process_once())
        registration = {
            "name": "w1", "version": "0.0.0", "gpu_name": "RTX", "memory_total_mb": 1, "model": "m", "compute_type": "c", "supported_asr_modes": ["faster_whisper"],
            "supports_restore": True, "restore_models": ["basicvsrpp"],
        }
        client.post("/api/workers/register", json=registration, headers=headers)
        assert client.post("/api/workers/w1/restore/claim", headers=headers).json() is None
        client.post("/api/workers/register", json={**registration, "restore_models": ["basicvsrpp", "ltx"]}, headers=headers)
        claim = client.post("/api/workers/w1/restore/claim", headers=headers).json()
        assert claim["restoration_model"] == "ltx" and claim["output_extension"] == "mkv"
        assert claim["codec"] == "hevc"  # 旧客户端传入的编码被忽略，固定 HEVC

        beat = client.post(
            "/api/workers/w1/heartbeat",
            json={
                "current_job_id": job_id, "status": "busy", "task_kind": "restore",
                "restore_progress": {"stage": "processing", "detail": "修复视频", "percent": 50, "fps": 20},
            },
            headers=headers,
        ).json()
        assert beat["lease_renewed"] is True and beat["cancel_requested"] is False
        listed = client.get("/api/restore/jobs").json()["jobs"][0]
        assert listed["state"] == RestoreState.PROCESSING and listed["progress"]["percent"] == 50
        worker = next(w for w in client.get("/api/status").json()["workers"] if w["name"] == "w1")
        assert worker["task_kind"] == "restore" and worker["restore_progress"]["fps"] == 20

        failed = client.post(f"/api/restore/jobs/{job_id}/error", json={"worker_name": "w1", "error": "显存不足"}, headers=headers)
        assert failed.status_code == 200
        job = database.get_restore_job(job_id)
        assert job["state"] == RestoreState.FAILED and "显存不足" in render(job["last_error"])
        # 失败不会被自动重新派发。
        assert client.post("/api/workers/w1/restore/claim", headers=headers).json() is None

        assert client.post(f"/api/restore/jobs/{job_id}/actions/retry").status_code == 200
        assert database.get_restore_job(job_id)["state"] == RestoreState.WAITING  # 原片未变，复用校验值
        claim = client.post("/api/workers/w1/restore/claim", headers=headers).json()
        assert claim["job_id"] == job_id

        assert client.post(f"/api/restore/jobs/{job_id}/actions/cancel").status_code == 200
        beat = client.post(
            "/api/workers/w1/heartbeat",
            json={"current_job_id": job_id, "status": "busy", "task_kind": "restore"},
            headers=headers,
        ).json()
        assert beat["cancel_requested"] is True


def test_resubmitting_a_cancelled_movie_reuses_its_job(tmp_path: Path, monkeypatch) -> None:
    library, _ = _start_app(tmp_path, monkeypatch)
    library.mkdir(parents=True)
    source = library / "ABC-002.mkv"
    source.write_bytes(b"video")
    with TestClient(app) as client:
        database = app.state.database

        async def idle() -> bool:
            return False

        monkeypatch.setattr(app.state.restore, "process_once", idle)
        stat = source.stat()
        media_id, _ = database.upsert_media(
            path=source, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            duration=None, probe=None, decision="done", reason="", file_key=None,
        )

        def submit(model: str) -> dict:
            return client.post(
                "/api/restore/jobs", json={"media_ids": [media_id], "restoration_model": model}
            ).json()

        job_id = submit("basicvsrpp")["created"][0]
        assert render(submit("basicvsrpp")["skipped"][0]["reason"]) == "已有进行中的影片修复任务。"

        # 取消后用同一模型重新提交：沿用原记录。
        assert client.post(f"/api/restore/jobs/{job_id}/actions/cancel").status_code == 200
        assert submit("basicvsrpp")["created"] == [job_id]

        # 取消后换模型重新提交：仍是同一条记录，模型更新、回到准备阶段。
        assert client.post(f"/api/restore/jobs/{job_id}/actions/cancel").status_code == 200
        assert submit("ltx")["created"] == [job_id]
        jobs = client.get("/api/restore/jobs", params={"view": "all"}).json()["jobs"]
        assert [(job["id"], job["restoration_model"], job["state"]) for job in jobs] == [
            (job_id, "ltx", RestoreState.PREPARING)
        ]

        # 影片列表一部一行，并带上这条任务。
        media = client.get("/api/restore/media", params={"status": "active"}).json()
        assert media["counts"] == {"all": 1, "pending": 0, "active": 1, "failed": 0, "completed": 0}
        assert media["items"][0]["status"] == "running"
        assert render(media["items"][0]["step"]) == "检查原片完整性"
        assert media["items"][0]["job"]["id"] == job_id
