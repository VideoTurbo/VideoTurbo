"""主控上行限速：所有 Worker、所有连接下载文件时共享同一个总速率。"""

from __future__ import annotations

import asyncio
import time

from fastapi.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

from ..models import TransferSettings

MIB = 1024 * 1024


class RateLimiter:
    """令牌桶。速率为 None 时不限速；运行中可随时调整。"""

    def __init__(self, bytes_per_second: float | None = None) -> None:
        self._lock = asyncio.Lock()
        self._rate: float | None = None
        self._tokens = 0.0
        self._updated = time.monotonic()
        self.set_rate(bytes_per_second)

    @property
    def rate(self) -> float | None:
        return self._rate

    def set_rate(self, bytes_per_second: float | None) -> None:
        self._rate = bytes_per_second if bytes_per_second and bytes_per_second > 0 else None
        # 调整速率后不保留旧的突发额度，新速率立即生效。
        self._tokens = 0.0
        self._updated = time.monotonic()

    def _burst(self, rate: float) -> float:
        # 允许约 0.25 秒的突发，平滑多连接之间的发送。
        return max(rate / 4, 256 * 1024)

    async def acquire(self, size: int) -> None:
        if self._rate is None or size <= 0:
            return
        # asyncio.Lock 先到先得，各连接轮流发送，共享总速率。
        async with self._lock:
            rate = self._rate
            if rate is None:
                return
            now = time.monotonic()
            self._tokens = min(self._burst(rate), self._tokens + (now - self._updated) * rate)
            self._updated = now
            self._tokens -= size
            if self._tokens < 0:
                wait = -self._tokens / rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._updated = time.monotonic()


class ThrottledFileResponse(FileResponse):
    """按 RateLimiter 限速发送的 FileResponse，保留 Range 断点续传。"""

    def __init__(self, *args, limiter: RateLimiter, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.limiter.rate is None:
            await super().__call__(scope, receive, send)
            return
        # pathsend 由服务器直接发送整个文件，无法逐块限速。
        extensions = {key: value for key, value in scope.get("extensions", {}).items() if key != "http.response.pathsend"}
        scope = {**scope, "extensions": extensions}

        async def throttled_send(message: Message) -> None:
            if message["type"] == "http.response.body":
                await self.limiter.acquire(len(message.get("body", b"")))
            await send(message)

        await super().__call__(scope, receive, throttled_send)


def limit_bytes(settings: TransferSettings) -> float | None:
    """控制台的“下载限速”（MB/s，0 表示不限）换算成字节每秒。"""
    return settings.upload_limit_mb * MIB if settings.upload_limit_mb else None
