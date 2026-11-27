import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from conftest import PROTOCOL, prepare_server, raises_rendered
from fastapi.testclient import TestClient

from video_turbo.config import AppConfig
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.worker_gateway import WorkerGateway, is_worker_route
from video_turbo.worker import restore_task
from video_turbo.worker.config import JasnaConfig, WorkerConfig
from video_turbo.worker.restore_task import RestoreTask

HEADERS = {**PROTOCOL, "X-Worker-Token": "token"}


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("GET", "/health", False),
        ("POST", "/api/wake", False),
        ("GET", "/openapi.json", False),
        ("GET", "/redoc", False),
        ("POST", "/api/workers/register", True),
        ("POST", "/api/workers/cloud5090-01/heartbeat", True),
        ("POST", "/api/workers/w/restore/claim", True),
        ("GET", "/api/jobs/abc/audio", True),
        ("POST", "/api/jobs/abc/asr-result", True),
        ("GET", "/api/restore/jobs/abc/source", True),
        ("PUT", "/api/restore/jobs/abc/upload/chunk", True),
        ("POST", "/api/restore/jobs/abc/complete", True),
        ("GET", "/", False),
        ("GET", "/docs", False),
        ("GET", "/api/status", False),
        ("DELETE", "/api/workers/w", False),
        ("POST", "/api/jobs/abc/complete", False),
        ("POST", "/api/restore/jobs/abc/actions/purge_original", False),
        ("GET", "/api/restore/jobs/abc", False),
        ("PUT", "/api/settings/ui", False),
        ("GET", "/api/media/1/poster", False),
    ],
)
def test_worker_gateway_routes(method: str, path: str, allowed: bool) -> None:
    assert is_worker_route(method, path) is allowed


def test_worker_port_must_differ() -> None:
    with raises_rendered(ValueError, "机器接入端口"):
        AppConfig.model_validate({"server": {"port": 8765, "worker_port": 8765}})
    assert AppConfig().server.worker_port == 8766


def _start(tmp_path: Path, monkeypatch) -> Path:
    library = tmp_path / "library"
    library.mkdir()
    prepare_server(tmp_path, monkeypatch, {"media_libraries": [{"id": "lib-lib", "name": "lib", "path": str(library)}]}, worker_token="token", backup_dir=tmp_path / "backup")
    return library


def test_gateway_serves_worker_api_only(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch)
    with TestClient(app):
        gateway = TestClient(WorkerGateway(app))
        not_found = {"detail": "Not Found"}
        for method, path in [("GET", "/"), ("GET", "/docs"), ("GET", "/health"), ("GET", "/api/status"),
                             ("POST", "/api/restore/jobs/x/actions/purge_original")]:
            for headers in ({}, HEADERS):
                response = gateway.request(method, path, headers=headers)
                assert (response.status_code, response.json()) == (404, not_found), path
        # 密钥缺失或错误时与不存在的接口返回相同的 404，并且不读取请求体。
        for headers in ({}, {**PROTOCOL, "X-Worker-Token": "wrong"}):
            response = gateway.post("/api/workers/register", content=b"x" * 1_000_000, headers=headers)
            assert (response.status_code, response.json()) == (404, not_found)
        # 带正确密钥的 Worker 接口照常校验参数。
        assert gateway.post("/api/workers/register", json={}, headers=HEADERS).status_code == 422
        registered = gateway.post(
            "/api/workers/register",
            json={"name": "w", "version": "0.0.0", "gpu_name": "g", "memory_total_mb": 1, "model": "m", "compute_type": "c", "supported_asr_modes": ["whisperjav"]},
            headers=HEADERS,
        )
        assert registered.status_code == 200


def test_chunked_upload_out_of_order(tmp_path: Path, monkeypatch) -> None:
    library = _start(tmp_path, monkeypatch)
    source = library / "E-001.mp4"
    source.write_bytes(b"s" * 1000)
    mb = 1024 * 1024
    result = bytes(range(256)) * (10 * 1024 + 7)  # 约 2.6 MB，3 个 1 MB 分块
    with TestClient(app) as client:
        database = app.state.database
        stat = source.stat()
        media_id, _ = database.upsert_media(
            path=source, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            duration=None, probe=None, decision="done", reason="", file_key=None,
        )
        job_id = client.post("/api/restore/jobs", json={"media_ids": [media_id]}).json()["created"][0]
        database.update_restore_job(job_id, state="WAITING", source_sha256="a" * 64)
        client.post(
            "/api/workers/register",
            json={"name": "w", "version": "0.0.0", "gpu_name": "g", "memory_total_mb": 1, "model": "m", "compute_type": "c", "supported_asr_modes": ["whisperjav"],
                  "supports_restore": True, "restore_models": ["basicvsrpp"]},
            headers=HEADERS,
        )
        claim = client.post("/api/workers/w/restore/claim", headers=HEADERS).json()
        worker_headers = {**HEADERS, "X-Worker-Name": "w"}
        url = claim["upload_url"] + "/chunk"
        params = {"chunk_size": mb, "total": len(result)}

        def put(index: int, data: bytes):
            return client.put(url, params={**params, "index": index}, content=data, headers=worker_headers)

        assert put(2, result[2 * mb :]).status_code == 200
        # 不完整的分块不会记为已收到。
        assert put(0, result[: mb // 2]).status_code == 400
        assert put(0, result[: mb] + b"extra").status_code == 400
        status = client.get(claim["upload_url"], headers=worker_headers).json()
        assert status["chunked"] is True and status["received"] == [2]
        complete = {"worker_name": "w", "size": len(result), "sha256": hashlib.sha256(result).hexdigest()}
        assert client.post(f"/api/restore/jobs/{job_id}/complete", json=complete, headers=HEADERS).status_code == 409
        assert put(0, result[:mb]).status_code == 200
        assert put(1, result[mb : 2 * mb]).status_code == 200
        assert put(3, b"x").status_code == 400
        accepted = client.post(f"/api/restore/jobs/{job_id}/complete", json=complete, headers=HEADERS)
        assert accepted.status_code == 200
        upload = app.state.restore.upload_path(database.get_restore_job(job_id))
        assert upload.read_bytes() == result
        assert not upload.with_name(upload.name + ".chunks.json").exists()


class FakeServer:
    """模拟主控的下载与分块上传接口，可注入网络中断。"""

    def __init__(self, source: bytes) -> None:
        self.source = source
        self.ranges: list[str] = []
        self.fail_ranges: set[str] = set()
        self.ignore_range = False
        self.chunks: dict[int, bytes] = {}
        self.chunk_meta: tuple[int, int] | None = None
        self.fail_chunks: set[int] = set()
        self.completed: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/source"):
            header = request.headers.get("range")
            if header is None or self.ignore_range:
                return httpx.Response(200, content=self.source)
            self.ranges.append(header)
            if header in self.fail_ranges:
                self.fail_ranges.discard(header)
                raise httpx.ReadError("连接被重置")
            start, end = (int(x) for x in re.match(r"bytes=(\d+)-(\d+)", header).groups())
            return httpx.Response(206, content=self.source[start : end + 1])
        if path.endswith("/upload/chunk"):
            index = int(request.url.params["index"])
            meta = (int(request.url.params["chunk_size"]), int(request.url.params["total"]))
            if self.chunk_meta != meta:
                self.chunk_meta, self.chunks = meta, {}
            if index in self.fail_chunks:
                self.fail_chunks.discard(index)
                raise httpx.WriteError("上传中断")
            self.chunks[index] = request.read()
            return httpx.Response(200, json={"index": index})
        if path.endswith("/upload"):
            size, total = self.chunk_meta or (None, None)
            return httpx.Response(200, json={
                "offset": 0, "chunked": True, "chunk_size": size, "total": total, "received": sorted(self.chunks),
            })
        if path.endswith("/complete"):
            self.completed = json.loads(request.content)
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(404)


def _task(tmp_path: Path, server: FakeServer, *, connections: int = 4) -> tuple[RestoreTask, httpx.AsyncClient]:
    config = WorkerConfig(
        worker_token="t", name="w", cache_dir=tmp_path / "work",
        jasna=JasnaConfig(download_connections=connections, upload_connections=connections, upload_chunk_mb=1),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler), base_url="http://nas")
    claim = {
        "job_id": "job1", "source_url": "/api/restore/jobs/job1/source", "upload_url": "/api/restore/jobs/job1/upload",
        "source_size": len(server.source), "source_sha256": hashlib.sha256(server.source).hexdigest(),
        "source_extension": "mp4", "output_extension": "mp4", "restoration_model": "basicvsrpp", "codec": "hevc",
    }
    return RestoreTask(config, client, claim, report=lambda _p: None, should_cancel=lambda: False), client


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(restore_task, "DOWNLOAD_CHUNK", 256 * 1024)
    real_sleep = asyncio.sleep

    async def no_wait(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(restore_task.asyncio, "sleep", no_wait)


@pytest.mark.skipif(os.name == "nt", reason="影片修复只在 Linux 显卡电脑上运行，分段写入用 os.pwrite")
def test_parallel_download_resumes_and_retries(tmp_path: Path, fast_retries) -> None:
    source = bytes(range(256)) * 12_000  # 约 3 MB，12 个 256 KB 分块
    server = FakeServer(source)
    task, client = _task(tmp_path, server)
    task.job_dir.mkdir(parents=True)
    # 上次中断：分块 0、5 已完成，其余位置是垃圾数据。
    chunk = 256 * 1024
    partial = bytearray(b"\0" * len(source))
    for index in (0, 5):
        partial[index * chunk : (index + 1) * chunk] = source[index * chunk : (index + 1) * chunk]
    task.source.write_bytes(bytes(partial))
    (task.job_dir / "source.chunks.json").write_text("[0, 5]")
    server.fail_ranges.add(f"bytes={3 * chunk}-{4 * chunk - 1}")

    asyncio.run(task.download())
    asyncio.run(client.aclose())
    assert task.source.read_bytes() == source
    requested = {int(re.match(r"bytes=(\d+)", r).group(1)) // chunk for r in server.ranges}
    assert requested == set(range(12)) - {0, 5}
    assert server.ranges.count(f"bytes={3 * chunk}-{4 * chunk - 1}") == 2  # 中断后重试
    assert not (task.job_dir / "source.chunks.json").exists()
    assert (task.job_dir / "source.verified").is_file()


def test_download_falls_back_when_range_ignored(tmp_path: Path, fast_retries) -> None:
    server = FakeServer(b"z" * 1_000_000)
    server.ignore_range = True
    task, client = _task(tmp_path, server)
    asyncio.run(task.download())
    asyncio.run(client.aclose())
    assert task.source.read_bytes() == server.source


def test_parallel_upload_retries_and_skips_received(tmp_path: Path, fast_retries) -> None:
    server = FakeServer(b"")
    task, client = _task(tmp_path, server)
    task.job_dir.mkdir(parents=True)
    result = bytes(range(251)) * 14_000  # 约 3.5 MB，4 个 1 MB 分块
    task.output.write_bytes(result)
    mb = 1024 * 1024
    server.chunk_meta = (mb, len(result))
    server.chunks = {1: result[mb : 2 * mb]}  # 上次已传完分块 1
    server.fail_chunks = {2}

    asyncio.run(task.upload({"tool": "jasna"}))
    asyncio.run(client.aclose())
    assert b"".join(server.chunks[i] for i in range(4)) == result
    assert server.completed["sha256"] == hashlib.sha256(result).hexdigest()


def test_single_connection_uses_sequential_paths(tmp_path: Path, fast_retries) -> None:
    server = FakeServer(b"q" * 700_000)
    task, client = _task(tmp_path, server, connections=1)
    asyncio.run(task.download())
    asyncio.run(client.aclose())
    assert server.ranges == [] and task.source.read_bytes() == server.source
