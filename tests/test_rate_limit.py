import asyncio
import time
from pathlib import Path

from conftest import prepare_server
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.rate_limit import MIB, RateLimiter, ThrottledFileResponse


def test_rate_limiter_caps_total_rate_across_connections() -> None:
    limiter = RateLimiter(4 * MIB)
    chunk = 64 * 1024

    async def connection() -> None:
        for _ in range(8):  # 每条连接 512 KB
            await limiter.acquire(chunk)

    async def run() -> float:
        started = time.monotonic()
        await asyncio.gather(*(connection() for _ in range(4)))  # 共 2 MB
        return time.monotonic() - started

    elapsed = asyncio.run(run())
    assert 0.4 <= elapsed < 1.5


def test_rate_limiter_unlimited_and_runtime_change() -> None:
    limiter = RateLimiter(None)
    assert limiter.rate is None
    started = time.monotonic()
    asyncio.run(limiter.acquire(100 * MIB))
    assert time.monotonic() - started < 0.1
    limiter.set_rate(9 * MIB)
    assert limiter.rate == 9 * MIB
    limiter.set_rate(0)
    assert limiter.rate is None


def test_throttled_file_response_keeps_range_and_counts_bytes(tmp_path: Path) -> None:
    data = bytes(range(256)) * 1024  # 256 KB
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    limiter = RateLimiter(64 * MIB)
    sent: list[int] = []
    original = limiter.acquire

    async def counting(size: int) -> None:
        sent.append(size)
        await original(size)

    limiter.acquire = counting  # type: ignore[method-assign]
    mini = FastAPI()

    @mini.get("/file")
    async def file() -> ThrottledFileResponse:
        return ThrottledFileResponse(path, limiter=limiter)

    with TestClient(mini) as client:
        full = client.get("/file")
        assert full.status_code == 200 and full.content == data
        assert sum(sent) == len(data)
        sent.clear()
        part = client.get("/file", headers={"Range": "bytes=1000-70999"})
        assert part.status_code == 206 and part.content == data[1000:71000]
        assert sum(sent) == 70000


def test_transfer_settings_api_updates_limiter(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch)

    with TestClient(app) as client:
        assert client.get("/api/settings/transfer").json() == {"upload_limit_mb": None}
        assert app.state.upload_limiter.rate is None
        assert client.put("/api/settings/transfer", json={"upload_limit_mb": 9}).status_code == 200
        assert client.get("/api/settings/transfer").json() == {"upload_limit_mb": 9.0}
        assert app.state.upload_limiter.rate == 9 * MIB
        assert client.put("/api/settings/transfer", json={"upload_limit_mb": 0}).status_code == 422

    # 重启主控后沿用已保存的限速。
    with TestClient(app) as client:
        assert app.state.upload_limiter.rate == 9 * MIB
        assert client.put("/api/settings/transfer", json={"upload_limit_mb": None}).status_code == 200
        assert app.state.upload_limiter.rate is None
