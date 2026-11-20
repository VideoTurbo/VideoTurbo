import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from conftest import raises_rendered

from video_turbo.i18n import render
from video_turbo.worker.asr import TranscriptionCancelled
from video_turbo.worker.config import WorkerConfig
from video_turbo.worker.errors import describe_asr_failure
from video_turbo.worker.main import Worker


@pytest.mark.parametrize(("error", "expected"), [
    (RuntimeError("audio integrity: 5 gap(s) in the audio track, 10.0 s in all "
                  "(2.0 s at 0:38:52)"), "5 处缺口，合计 10.0 秒"),
    (RuntimeError("audio integrity: the audio track's time runs backwards"), "音频不完整"),
    (RuntimeError("CUDA out of memory"), "识别内存不足"),
    (RuntimeError("WhisperJAV 识别超时。"), "识别太久没完成"),
    (httpx.ReadTimeout("read timed out"), "与主程序通信超时"),
    (httpx.RemoteProtocolError("Server disconnected"), "与主程序通信失败"),
    (ValueError("临时音频 SHA-256 校验失败。"), "语音识别处理失败"),
])
def test_failure_explains_category_and_retains_backend_detail(error, expected):
    description = render(describe_asr_failure(error, "提取音频"))
    assert expected in description
    assert "出错阶段：提取音频" in render(description)
    assert str(error) in description
    assert type(error).__name__ in description


def test_large_error_retains_beginning_and_final_cause_within_report_limit():
    raw = describe_asr_failure(RuntimeError("first cause " + "x" * 20000 + " final cause"), "s" * 1000)
    # 传给主程序的是带多语言标记的原文，受接口 8000 字符的限制。
    assert len(raw) <= 8000
    description = render(raw)
    assert "first cause" in description and "final cause" in description


@pytest.mark.parametrize("report_status", [200, 404, "disconnected"])
def test_worker_reports_original_error_before_clearing_stage(tmp_path, monkeypatch, report_status):
    async def check():
        worker = Worker(WorkerConfig(cache_dir=tmp_path, worker_token="token"))
        requests = []

        def select(_mode):
            worker._update_asr_stage("加载识别模型")
            raise RuntimeError("CUDA out of memory")

        def server(request):
            requests.append((request.url.path, json.loads(request.content)))
            if report_status == "disconnected":
                raise httpx.RemoteProtocolError("Server disconnected")
            return httpx.Response(report_status, json={"accepted": True})

        monkeypatch.setattr(worker, "_select_transcriber", select)
        await worker.client.aclose()
        worker.client = httpx.AsyncClient(base_url="http://nas", transport=httpx.MockTransport(server))
        try:
            with raises_rendered(RuntimeError, "CUDA out of memory"):
                await worker.process_claim({"job_id": "job"})
            assert requests[0][0] == "/api/jobs/job/asr-error"
            error = requests[0][1]["error"]
            assert "加载识别模型" in render(error) and "识别内存不足" in render(error)
            assert "CUDA out of memory" in error
            assert worker.current_job_id is None
            assert worker.asr_stage is None
        finally:
            await worker.close()

    asyncio.run(check())


def test_worker_cancellation_is_not_reported_as_failure(tmp_path, monkeypatch):
    async def check():
        worker = Worker(WorkerConfig(cache_dir=tmp_path, worker_token="token"))

        def select(_mode):
            raise TranscriptionCancelled("已取消")

        async def unexpected_report(*_args):
            pytest.fail("Cancellation must not be reported as an ASR error")

        monkeypatch.setattr(worker, "_select_transcriber", select)
        monkeypatch.setattr(worker, "report_failure", unexpected_report)
        try:
            with pytest.raises(TranscriptionCancelled):
                await worker.process_claim({"job_id": "job"})
        finally:
            await worker.close()

    asyncio.run(check())


def test_backend_error_is_not_masked_by_hardware_failure(tmp_path, monkeypatch):
    async def check():
        worker = Worker(WorkerConfig(cache_dir=tmp_path, worker_token="token"))
        reports = []
        probes = 0

        def hardware():
            nonlocal probes
            probes += 1
            if probes > 1:
                raise RuntimeError("nvidia-smi unavailable")
            return SimpleNamespace(memory_free_mb=10000)

        def transcribe(*_args):
            raise RuntimeError("audio integrity: 5 gap(s) in the audio track, 10.0 s in all")

        def server(request):
            if request.method == "GET":
                return httpx.Response(200, content=b"audio")
            reports.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True})

        monkeypatch.setattr(worker, "_select_transcriber", lambda _: None)
        monkeypatch.setattr(worker, "_query_hardware", hardware)
        monkeypatch.setattr(worker.transcriber, "transcribe", transcribe)
        await worker.client.aclose()
        worker.client = httpx.AsyncClient(base_url="http://nas", transport=httpx.MockTransport(server))
        try:
            with raises_rendered(RuntimeError, "audio integrity"):
                await worker.process_claim({"job_id": "job", "audio_url": "/audio", "language": "ja",
                                            "audio_sha256": sha256(b"audio").hexdigest()})
            assert "5 处缺口" in render(reports[0]["error"])
            assert "nvidia-smi unavailable" not in reports[0]["error"]
        finally:
            await worker.close()

    asyncio.run(check())


@pytest.mark.parametrize("registered", [True, False])
def test_worker_reregisters_when_master_dropped_it(tmp_path, monkeypatch, registered):
    async def check():
        worker = Worker(WorkerConfig(cache_dir=tmp_path, worker_token="token"))
        registrations = []

        async def register():
            registrations.append(True)

        def server(request):
            return httpx.Response(200, json={"ok": True, "registered": registered, "lease_renewed": False})

        monkeypatch.setattr(worker, "register", register)
        monkeypatch.setattr(worker, "_query_hardware", lambda: SimpleNamespace(utilization=0, memory_free_mb=1))
        await worker.client.aclose()
        worker.client = httpx.AsyncClient(base_url="http://nas", transport=httpx.MockTransport(server))
        try:
            await worker.heartbeat()
            assert registrations == ([] if registered else [True])
        finally:
            await worker.close()

    asyncio.run(check())
