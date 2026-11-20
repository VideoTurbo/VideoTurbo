import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest
from conftest import PROTOCOL, prepare_server
from fastapi.testclient import TestClient

from video_turbo.i18n import render
from video_turbo.models import RestoreState
from video_turbo.orchestrator.app import app
from video_turbo.worker import restore_pipeline, restore_task
from video_turbo.worker.config import JasnaConfig, WorkerConfig
from video_turbo.worker.restore_pipeline import RestorePipeline

# 影片修复只在 Linux 显卡电脑上运行
pytestmark = pytest.mark.skipif(os.name == "nt", reason="影片修复只在 Linux 显卡电脑上运行")

HEADERS = {**PROTOCOL, "X-Worker-Token": "token"}


def _fake_encoder(executable: Path, *, works: bool = True) -> None:
    """Jasna 自带的 tools/ffmpeg：works=False 时模拟云 GPU 编码器不可用。"""
    ffmpeg = executable.parent / "tools" / "ffmpeg"
    ffmpeg.parent.mkdir(exist_ok=True)
    ffmpeg.write_text(
        "#!/bin/sh\nexit 0\n" if works
        else "#!/bin/sh\necho 'OpenEncodeSessionEx failed: unsupported device (2)' >&2\nexit 1\n"
    )
    ffmpeg.chmod(0o755)


def _start(tmp_path: Path, monkeypatch) -> Path:
    library = tmp_path / "library"
    library.mkdir()
    prepare_server(tmp_path, monkeypatch, {"media_libraries": [{"id": "lib-lib", "name": "lib", "path": str(library)}]}, worker_token="token", backup_dir=tmp_path / "backup")
    return library


def _register(client: TestClient, name: str, **extra) -> dict:
    payload = {
        "name": name, "version": "0.0.0", "gpu_name": "RTX", "memory_total_mb": 1, "model": "m", "compute_type": "c", "supported_asr_modes": ["faster_whisper"],
        "supports_restore": True, "restore_models": ["basicvsrpp"], **extra,
    }
    return client.post("/api/workers/register", json=payload, headers=HEADERS).json()


def _queue(client: TestClient, library: Path, names: list[str], size: int = 300_000) -> list[str]:
    database = app.state.database
    media_ids = []
    for index, name in enumerate(names):
        path = library / f"{name}.mp4"
        path.write_bytes(bytes([index + 1]) * size)
        stat = path.stat()
        media_id, _ = database.upsert_media(
            path=path, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            duration=100.0, probe=None, decision="done", reason="", file_key=None,
        )
        media_ids.append(media_id)
    job_ids = client.post("/api/restore/jobs", json={"media_ids": media_ids}).json()["created"]
    service = app.state.restore
    for _ in job_ids:
        asyncio.run(service._real_process_once())
    return job_ids


def _idle_service(monkeypatch) -> None:
    service = app.state.restore
    service._real_process_once = service.process_once

    async def idle() -> bool:
        return False

    monkeypatch.setattr(service, "process_once", idle)


def test_prefetch_yields_to_idle_machines_and_heartbeat_renews_all_jobs(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _idle_service(monkeypatch)
        assert _register(client, "cloud5090-01")["registered"] is True
        _register(client, "cloud5090-02")
        first, second, third = _queue(client, library, ["A-001", "A-002", "A-003"])
        claim = client.post("/api/workers/cloud5090-01/restore/claim", headers=HEADERS).json()
        assert claim["job_id"] == first
        client.post(
            "/api/workers/cloud5090-01/heartbeat",
            json={"current_job_id": first, "status": "busy", "task_kind": "restore"},
            headers=HEADERS,
        )
        # 同机另一个实例空闲不算：它们共用一张卡。
        prefetched = client.post(
            "/api/workers/cloud5090-01/restore/claim", params={"prefetch": 1}, headers=HEADERS
        ).json()
        assert prefetched["job_id"] == second

        # 另一台机器的影片修复 Worker 空闲时，预取让给它。
        _register(client, "cloud4090-01")
        assert client.post(
            "/api/workers/cloud5090-01/restore/claim", params={"prefetch": 1}, headers=HEADERS
        ).json() is None
        assert client.post("/api/workers/cloud4090-01/restore/claim", headers=HEADERS).json()["job_id"] == third

        # 一次心跳续租全部任务，各自记录进度；取消的任务在响应中列出。
        assert client.post(f"/api/restore/jobs/{second}/actions/cancel").status_code == 200
        beat = client.post(
            "/api/workers/cloud5090-01/heartbeat",
            json={
                "current_job_id": first, "status": "busy", "task_kind": "restore",
                "restore_jobs": [
                    {"job_id": first, "progress": {"stage": "processing", "percent": 40, "detail": "修复视频"}},
                    {"job_id": second, "progress": {"stage": "queued", "detail": "原片已下载"}},
                ],
            },
            headers=HEADERS,
        ).json()
        assert beat["lease_renewed"] is True and beat["cancel_requested"] is False
        assert beat["restore_stopped_job_ids"] == [second]
        jobs = {job["id"]: job for job in client.get("/api/restore/jobs", params={"view": "all"}).json()["jobs"]}
        assert jobs[first]["state"] == RestoreState.PROCESSING and jobs[first]["progress"]["percent"] == 40
        worker = next(w for w in client.get("/api/status").json()["workers"] if w["name"] == "cloud5090-01")
        assert [job["job_id"] for job in worker["restore_jobs"]] == [first, second]


def test_pipeline_overlaps_download_processing_and_upload(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    monkeypatch.setattr(restore_pipeline, "PREFETCH_RETRY_SECONDS", 0.05)
    executable = tmp_path / "jasna-bin" / "jasna"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    _fake_encoder(executable)
    worker_config = WorkerConfig(
        worker_token="token", name="cloud5090-01", cache_dir=tmp_path / "work",
        jasna=JasnaConfig(enabled=True, executable=executable, upload_chunk_mb=1),
    )
    events: list[str] = []

    def wait_for(predicate, what: str) -> None:
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"等待超时：{what}")
            time.sleep(0.02)

    with TestClient(app) as client:
        _idle_service(monkeypatch)
        _register(client, "cloud5090-01")
        first, second = _queue(client, library, ["B-001", "B-002"], size=2_500_000)
        database = app.state.database

        def fake_jasna(config, *, source, output, working_directory, restoration_model, codec,
                       should_cancel, on_progress, on_stage, ltx_large_canvas=False):
            job_id = next(item for item in (first, second) if source.parent.name.endswith(item[:12]))
            events.append(f"process-start:{job_id}")
            if job_id == first:
                # 修复第一部期间，第二部的原片已经预取下载完成。
                wait_for(lambda: any(worker_config.restore_dir.glob(f"*-{second[:12]}/source.verified")), "预取第二部")
                events.append("prefetched-during-first")
            else:
                # 修复第二部期间，第一部的结果在后台回传完成。
                wait_for(lambda: database.get_restore_job(first)["state"] == RestoreState.FINALIZING, "回传第一部")
                events.append("uploaded-first-during-second")
            output.write_bytes(source.read_bytes()[::-1])
            return {"tool": "jasna"}

        monkeypatch.setattr(restore_task, "run_jasna", fake_jasna)

        async def run() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=HEADERS) as worker_client:
                errors: list[str] = []

                async def report_error(job_id: str, exc: Exception) -> None:
                    errors.append(f"{job_id}: {exc}")

                pipeline = RestorePipeline(worker_config, worker_client, report_error=report_error)

                assert await pipeline.check_encoder()
                assert await pipeline.run_once() is True
                assert pipeline.prefetched is not None and pipeline.prefetched.id == second
                assert pipeline.lock_held  # 持有预取任务时保留本机影片修复锁
                await pipeline.prefetched.download
                assert pipeline.prefetched.progress.stage == "queued"
                assert await pipeline.run_once() is True
                await pipeline.drain()
                assert errors == []
                assert pipeline.jobs == {} and not pipeline.lock_held

        asyncio.run(run())
        assert events == [
            f"process-start:{first}", "prefetched-during-first",
            f"process-start:{second}", "uploaded-first-during-second",
        ]
        for job_id in (first, second):
            job = database.get_restore_job(job_id)
            assert job["state"] == RestoreState.FINALIZING
            assert not list(worker_config.restore_dir.glob(f"*{job_id[:12]}"))


def test_pipeline_disabled_runs_serially(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    executable = tmp_path / "jasna"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    _fake_encoder(executable)
    worker_config = WorkerConfig(
        worker_token="token", name="w1", cache_dir=tmp_path / "work",
        jasna=JasnaConfig(enabled=True, executable=executable, pipeline=False),
    )

    def fake_jasna(config, *, source, output, **_):
        output.write_bytes(b"out")
        return {}

    monkeypatch.setattr(restore_task, "run_jasna", fake_jasna)
    with TestClient(app) as client:
        _idle_service(monkeypatch)
        _register(client, "w1")
        first, _second = _queue(client, library, ["C-001", "C-002"])

        async def run() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=HEADERS) as worker_client:
                async def report_error(job_id: str, exc: Exception) -> None:
                    raise AssertionError(exc)

                pipeline = RestorePipeline(worker_config, worker_client, report_error=report_error)

                assert await pipeline.check_encoder()
                assert not pipeline.pipelined
                assert await pipeline.run_once() is True
                # 关闭流水线：不预取，回传在 run_once 内完成。
                assert pipeline.jobs == {} and pipeline.prefetched is None

        asyncio.run(run())
        assert app.state.database.get_restore_job(first)["state"] == RestoreState.FINALIZING


def test_cancelled_prefetch_is_not_processed(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    monkeypatch.setattr(restore_pipeline, "PREFETCH_RETRY_SECONDS", 0.05)
    executable = tmp_path / "jasna"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    _fake_encoder(executable)
    worker_config = WorkerConfig(
        worker_token="token", name="w1", cache_dir=tmp_path / "work",
        jasna=JasnaConfig(enabled=True, executable=executable),
    )
    processed: list[str] = []

    def fake_jasna(config, *, source, output, **_):
        processed.append(source.parent.name)
        deadline = time.monotonic() + 10
        while len(list((tmp_path / "work" / "restore").glob("*/source.verified"))) < 2:
            if time.monotonic() > deadline:
                raise AssertionError("预取超时")
            time.sleep(0.02)
        output.write_bytes(b"out")
        return {}

    monkeypatch.setattr(restore_task, "run_jasna", fake_jasna)
    with TestClient(app) as client:
        _idle_service(monkeypatch)
        _register(client, "w1")
        first, second = _queue(client, library, ["D-001", "D-002"])

        async def run() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=HEADERS) as worker_client:
                async def report_error(job_id: str, exc: Exception) -> None:
                    raise AssertionError(exc)

                pipeline = RestorePipeline(worker_config, worker_client, report_error=report_error)

                assert await pipeline.check_encoder()
                await pipeline.run_once()
                await pipeline.prefetched.download
                pipeline.stop([second])
                assert await pipeline.run_once() is True
                await pipeline.drain()
                assert pipeline.jobs == {} and not pipeline.lock_held

        asyncio.run(run())
        assert processed == [f"D-001-{first[:12]}"]


def test_worker_without_encoder_does_not_claim_restore(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    executable = tmp_path / "jasna"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    _fake_encoder(executable, works=False)
    worker_config = WorkerConfig(
        worker_token="token", name="w1", cache_dir=tmp_path / "work",
        jasna=JasnaConfig(enabled=True, ltx=True, executable=executable),
    )
    with TestClient(app) as client:
        _idle_service(monkeypatch)
        _register(client, "w1")
        _queue(client, library, ["F-001"])

        async def run() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=HEADERS) as worker_client:
                async def report_error(job_id: str, exc: Exception) -> None:
                    raise AssertionError(exc)

                pipeline = RestorePipeline(worker_config, worker_client, report_error=report_error)
                # 检查前不领取任务。
                assert pipeline.models == [] and await pipeline.run_once() is False
                assert await pipeline.check_encoder() is False
                assert "unsupported device" in pipeline.encoder_error
                assert render(pipeline.unavailable_reason).startswith("显卡的视频编码功能（NVENC）用不了")
                assert pipeline.models == [] and not pipeline.enabled
                assert await pipeline.run_once() is False
                # 换到编码器可用的机器（Worker 重启后重新检查）即可启用。
                _fake_encoder(executable, works=True)
                assert await pipeline.check_encoder() is True
                assert pipeline.models == ["basicvsrpp", "ltx"]
                assert pipeline.unavailable_reason is None

        asyncio.run(run())
        assert client.get("/api/restore/jobs").json()["jobs"][0]["state"] == "WAITING"


def test_unavailable_reason_reported_and_cleared(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch)
    with TestClient(app) as client:
        reason = "显卡的视频编码功能（NVENC）用不了：unsupported device (2)"
        _register(client, "cloud5090-01", supports_restore=False, restore_models=[],
                  restore_unavailable_reason=reason)
        status = client.get("/api/restore/status").json()
        assert status["workers"] == []
        assert [(w["name"], w["reason"]) for w in status["unavailable_workers"]] == [("cloud5090-01", reason)]
        worker = next(w for w in client.get("/api/status").json()["workers"] if w["name"] == "cloud5090-01")
        assert worker["restore_unavailable_reason"] == reason
        # 换到编码器可用的机器后重新注册，原因被清除。
        _register(client, "cloud5090-01")
        status = client.get("/api/restore/status").json()
        assert status["unavailable_workers"] == [] and [w["name"] for w in status["workers"]] == ["cloud5090-01"]


def test_large_canvas_option_reaches_worker_claim(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _idle_service(monkeypatch)
        _register(client, "w1", restore_models=["basicvsrpp", "ltx"])
        database = app.state.database
        paths = []
        for name in ("G-001", "G-002"):
            path = library / f"{name}.mp4"
            path.write_bytes(b"x" * 1000)
            stat = path.stat()
            paths.append(database.upsert_media(
                path=path, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                duration=None, probe=None, decision="done", reason="", file_key=None,
            )[0])
        created = client.post("/api/restore/jobs", json={
            "media_ids": [paths[0]], "restoration_model": "ltx", "ltx_large_canvas": True}).json()["created"]
        # BasicVSR++ 任务即使勾选也不会带上 LTX 选项。
        client.post("/api/restore/jobs", json={
            "media_ids": [paths[1]], "restoration_model": "basicvsrpp", "ltx_large_canvas": True})
        assert client.get("/api/restore/status").json()["settings"]["ltx_large_canvas"] is True
        service = app.state.restore
        for _ in range(2):
            asyncio.run(service._real_process_once())
        claims = [client.post("/api/workers/w1/restore/claim", headers=HEADERS).json() for _ in range(2)]
        by_model = {claim["restoration_model"]: claim for claim in claims}
        assert by_model["ltx"]["job_id"] == created[0] and by_model["ltx"]["ltx_large_canvas"] is True
        assert by_model["basicvsrpp"]["ltx_large_canvas"] is False
        listed = {job["restoration_model"]: job for job in client.get("/api/restore/jobs").json()["jobs"]}
        assert listed["ltx"]["ltx_large_canvas"] == 1 and listed["basicvsrpp"]["ltx_large_canvas"] == 0


def test_interrupted_job_is_reserved_for_its_machine(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _idle_service(monkeypatch)
        database = app.state.database
        for name in ("cloud5090-01", "cloud5090-02", "cloud4090-01"):
            _register(client, name)
        (job_id,) = _queue(client, library, ["H-001"])
        assert client.post("/api/workers/cloud5090-01/restore/claim", headers=HEADERS).json()["job_id"] == job_id

        # Worker 重启后重新注册：不必等租约到期，任务立即放回并为本机保留。
        _register(client, "cloud5090-01")
        job = database.get_restore_job(job_id)
        assert job["state"] == "WAITING" and job["reserved_machine"] == "cloud5090"
        assert "已重启" in render(job["last_error"]) and "优先派回 cloud5090" in render(job["last_error"])
        # 其他机器领不到；同机另一个实例（共用本地目录）可以领。
        assert client.post("/api/workers/cloud4090-01/restore/claim", headers=HEADERS).json() is None
        assert client.post("/api/workers/cloud5090-02/restore/claim", headers=HEADERS).json()["job_id"] == job_id
        assert database.get_restore_job(job_id)["reserved_machine"] is None

        # 租约到期同样保留；保留期过后任何机器都能领。
        database.update_restore_job(job_id, lease_expires_at="2000-01-01T00:00:00+00:00")
        assert database.release_expired_restore_leases(30) == 1
        assert client.post("/api/workers/cloud4090-01/restore/claim", headers=HEADERS).json() is None
        database.update_restore_job(job_id, reserved_until="2000-01-01T00:00:00+00:00")
        assert client.post("/api/workers/cloud4090-01/restore/claim", headers=HEADERS).json()["job_id"] == job_id


def test_own_machine_reserved_job_is_claimed_first(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _idle_service(monkeypatch)
        database = app.state.database
        _register(client, "cloud5090-01")
        older, newer = _queue(client, library, ["I-001", "I-002"])
        database.update_restore_job(newer, reserved_machine="cloud5090", reserved_until="2999-01-01T00:00:00+00:00")
        assert client.post("/api/workers/cloud5090-01/restore/claim", headers=HEADERS).json()["job_id"] == newer
        assert client.post("/api/workers/cloud5090-01/restore/claim", headers=HEADERS).json()["job_id"] == older
