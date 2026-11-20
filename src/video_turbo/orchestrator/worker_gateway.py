"""Worker 专用端口：只放行 Worker 实际调用的接口，其余一律 404。

用于把主控暴露到公网（端口转发）时只映射这个端口，控制台和管理接口留在局域网。
与主端口共用同一个 FastAPI 应用和运行状态，只在入口过滤：

- 方法和路径必须在白名单内（不含控制台、/health）；
- 请求头必须带正确的 Worker 密钥，在读取请求体之前检查，未授权的大请求不会被读入内存；
- 不满足任一条件时返回同样的 404，不暴露接口是否存在、密钥是否正确。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..security import secure_equals

_ID = r"[^/]+"
WORKER_ROUTES: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(methods), re.compile(f"^{pattern}$"))
    for methods, pattern in (
        ({"POST"}, r"/api/workers/register"),
        ({"POST"}, rf"/api/workers/{_ID}/heartbeat"),
        ({"POST"}, rf"/api/workers/{_ID}/claim"),
        ({"POST"}, rf"/api/workers/{_ID}/restore/claim"),
        ({"GET", "HEAD"}, rf"/api/jobs/{_ID}/audio"),
        ({"GET"}, r"/api/worker/plugins/[0-9a-fA-F]{64}"),
        ({"POST"}, rf"/api/jobs/{_ID}/asr-error"),
        ({"POST"}, rf"/api/jobs/{_ID}/asr-result"),
        ({"POST"}, rf"/api/jobs/{_ID}/plugin-state"),
        ({"GET", "HEAD"}, rf"/api/restore/jobs/{_ID}/source"),
        ({"GET", "PUT"}, rf"/api/restore/jobs/{_ID}/upload"),
        ({"PUT"}, rf"/api/restore/jobs/{_ID}/upload/chunk"),
        ({"POST"}, rf"/api/restore/jobs/{_ID}/complete"),
        ({"POST"}, rf"/api/restore/jobs/{_ID}/error"),
    )
)

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def is_worker_route(method: str, path: str) -> bool:
    return any(method in methods and pattern.match(path) for methods, pattern in WORKER_ROUTES)


NOT_FOUND = b'{"detail":"Not Found"}'


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


class WorkerGateway:
    def __init__(self, app: Any) -> None:
        self.app = app

    def _authorized(self, scope: Scope) -> bool:
        expected = getattr(getattr(self.app, "state", None), "worker_token", None)
        return bool(expected) and secure_equals(_header(scope, b"x-worker-token"), expected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            # 生命周期由主端口负责，这里直接确认，避免重复启动后台任务。
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if (
            scope["type"] != "http"
            or not is_worker_route(scope.get("method", ""), scope.get("path", ""))
            or not self._authorized(scope)
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(NOT_FOUND)).encode())]})
            await send({"type": "http.response.body", "body": NOT_FOUND})
            return
        await self.app(scope, receive, send)
