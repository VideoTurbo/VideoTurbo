import asyncio
import io

import pytest

from video_turbo.deployment import deployment, is_desktop
from video_turbo.orchestrator.app import _watch_stdin


def _watch(stream) -> list[bool]:
    loop = asyncio.new_event_loop()
    called: list[bool] = []
    try:
        _watch_stdin(stream, loop, lambda: called.append(True))
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    return called


def test_stdin_close_stops_the_server() -> None:
    # 外壳写入的内容一律丢弃，读到结束才退出。
    assert _watch(io.BytesIO(b"ping\n" * 1000)) == [True]


def test_missing_or_broken_stdin_also_stops_the_server() -> None:
    class Broken:
        def read(self, size: int) -> bytes:
            raise OSError("pipe closed")

    assert _watch(None) == [True]
    assert _watch(Broken()) == [True]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "source"), ("docker", "docker"), (" Desktop ", "desktop"), ("other", "source")],
)
def test_deployment_kind(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str) -> None:
    if value is None:
        monkeypatch.delenv("VIDEO_TURBO_DEPLOYMENT", raising=False)
    else:
        monkeypatch.setenv("VIDEO_TURBO_DEPLOYMENT", value)
    assert deployment() == expected
    assert is_desktop() == (expected == "desktop")
