"""字幕识别与影片修复拆成独立服务后的角色划分与同机 GPU 互斥（字幕优先）。"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import raises_rendered

from video_turbo.i18n import render
from video_turbo.restore_store import machine_key
from video_turbo.worker import restore_pipeline
from video_turbo.worker.config import JasnaConfig, WorkerConfig, load_worker_config
from video_turbo.worker.gpu_lock import GpuLock
from video_turbo.worker.main import Worker
from video_turbo.worker.restore_pipeline import RestorePipeline

pytestmark = pytest.mark.skipif(os.name == "nt", reason="GPU 锁基于 flock")


def _config(tmp_path: Path, **extra) -> WorkerConfig:
    return WorkerConfig(
        name="rtx4070ti", worker_token="token", cache_dir=tmp_path / "work", gpu_lock_file=tmp_path / ".gpu.lock",
        idle={"enabled": False}, **extra,
    )


def test_gpu_lock_shared_for_asr_and_exclusive_for_restore(tmp_path: Path) -> None:
    path = tmp_path / ".gpu.lock"
    asr_1, asr_2, restore = GpuLock(path), GpuLock(path), GpuLock(path)
    assert asr_1.try_shared() and asr_2.try_shared()  # 多路字幕并行
    assert not restore.is_free() and not restore.try_exclusive()
    asr_1.release()
    assert not restore.try_exclusive()
    asr_2.release()
    assert restore.is_free() and not restore.held
    assert restore.try_exclusive()
    assert not asr_1.try_shared()  # 影片修复修复期间字幕暂停领取
    restore.release()
    assert asr_1.try_shared()
    asr_1.release()


def test_load_worker_config_defaults_gpu_lock_to_project_cache(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "worker.yaml").write_text("server_url: http://nas:8765\nworker_token: t\n")
    config = load_worker_config(config_dir / "worker.yaml")
    assert config.cache_dir == tmp_path / "cache"
    assert config.gpu_lock_file == tmp_path / "cache" / ".gpu.lock"


def test_machine_key_groups_restore_instance_with_asr_instances() -> None:
    assert machine_key("cloud5090-01") == machine_key("cloud5090-dc") == "cloud5090"
    assert machine_key("cloud5090-b-dc") == "cloud5090-b"
    assert machine_key("rtx4070ti") == "rtx4070ti"


def _capture_register(worker: Worker) -> list[dict]:
    payloads: list[dict] = []

    def server(request: httpx.Request) -> httpx.Response:
        import json

        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"restore_pipeline": True})

    worker.client = httpx.AsyncClient(base_url="http://nas", transport=httpx.MockTransport(server))
    return payloads


def test_roles_register_only_their_own_capability(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "jasna"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    jasna = JasnaConfig(enabled=True, executable=executable)
    monkeypatch.setattr(restore_pipeline, "probe_nvenc", lambda config: None)

    python = tmp_path / ".venv-whisperjav/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    (python.parent.parent / "video-turbo-whisperjav.json").write_text('{"version":"1.9.5","ensemble":true}')

    async def check() -> None:
        config = _config(tmp_path, jasna=jasna, whisperjav={"python_executable": python})
        asr = Worker(config, "asr")
        restore = Worker(config, "restore")
        try:
            for worker in (asr, restore):
                monkeypatch.setattr(
                    worker, "_query_hardware",
                    lambda: SimpleNamespace(name="RTX", driver_version="580", memory_total_mb=24000),
                )
                await worker.restore.check_encoder()
            asr_payloads, restore_payloads = _capture_register(asr), _capture_register(restore)
            await asr.register()
            await restore.register()
            assert asr_payloads[0]["supports_restore"] is False
            assert asr_payloads[0]["restore_models"] == []
            assert asr_payloads[0]["supported_asr_modes"] == ["whisperjav"]
            assert asr_payloads[0]["supports_whisperjav_ensemble"] is True
            assert restore_payloads[0]["supports_restore"] is True
            assert restore_payloads[0]["restore_models"] == ["basicvsrpp"]
            assert restore_payloads[0]["supported_asr_modes"] == []
            assert await asr.restore.run_once() is False  # 字幕服务从不领取影片修复任务
        finally:
            await asr.close()
            await restore.close()

    asyncio.run(check())


def test_unknown_role_is_rejected(tmp_path: Path) -> None:
    with raises_rendered(ValueError, "未知的服务类型"):
        Worker(_config(tmp_path), "both")


def test_asr_pauses_while_restore_holds_gpu_and_keeps_lock_while_jobs_remain(tmp_path: Path) -> None:
    async def check() -> None:
        worker = Worker(_config(tmp_path), "asr")
        claims: list[str] = []
        queue = [{"job_id": "a"}]

        def server(request: httpx.Request) -> httpx.Response:
            claims.append(request.url.path)
            if queue:
                return httpx.Response(200, json=queue.pop(0))
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})

        processed: list[str] = []

        async def process(claim: dict) -> None:
            processed.append(claim["job_id"])
            # 处理字幕期间持共享锁，影片修复无法开始修复。
            assert not GpuLock(tmp_path / ".gpu.lock").try_exclusive()

        await worker.client.aclose()
        worker.client = httpx.AsyncClient(base_url="http://nas", transport=httpx.MockTransport(server))
        worker.process_claim = process
        worker.config.poll_interval_seconds = 3
        restore = GpuLock(tmp_path / ".gpu.lock")
        assert restore.try_exclusive()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(worker._run_asr(once=True), 0.3)
            assert claims == []  # 影片修复修复期间不领取字幕任务
            restore.release()
            await worker._run_asr(once=True)
            assert processed == ["a"] and worker.gpu.mode == "shared"  # 领到任务后继续持锁
            await worker._run_asr(once=True)
            assert claims == ["/api/workers/rtx4070ti/claim"] * 2
            assert not worker.gpu.held  # 队列空了才让出 GPU
        finally:
            restore.release()
            await worker.close()

    asyncio.run(check())


def test_restore_waits_for_asr_before_claiming_and_processing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(restore_pipeline, "GPU_RETRY_SECONDS", 0.01)

    async def check() -> None:
        config = _config(tmp_path, jasna=JasnaConfig(enabled=True))
        gpu = GpuLock(config.gpu_lock_file)
        async with httpx.AsyncClient(base_url="http://nas") as client:
            async def report_error(job_id, exc):
                raise AssertionError(exc)

            pipeline = RestorePipeline(config, client, report_error=report_error, gpu=gpu)
            monkeypatch.setattr(restore_pipeline, "supported_models", lambda config: ["basicvsrpp"])
            pipeline.encoder_checked = True

            async def no_claim(*, prefetch):
                raise AssertionError("本机字幕任务进行中时不应领取影片修复任务")

            monkeypatch.setattr(pipeline, "_claim", no_claim)
            asr = GpuLock(config.gpu_lock_file)
            assert asr.try_shared()
            assert await pipeline.run_once() is False

            job = SimpleNamespace(id="j", cancel=asyncio.Event(), progress=None)
            job.cancel.is_set = lambda: False
            waiter = asyncio.create_task(pipeline._wait_for_gpu(job))
            await asyncio.sleep(0.05)
            assert not waiter.done()
            assert render(job.progress.detail) == "等待本机字幕任务结束"
            asr.release()
            await asyncio.wait_for(waiter, 1)
            assert gpu.mode == "exclusive"
            gpu.release()

    asyncio.run(check())
