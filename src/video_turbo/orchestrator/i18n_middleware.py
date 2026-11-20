"""按请求语言翻译 JSON 响应里的 ``msg`` 标记（见 video_turbo.i18n）。

放在最外层：接口返回值、HTTPException、登录校验的拒绝信息都经过这里。
其他类型的响应（字幕、音频、日志下载）原样通过。
"""

from __future__ import annotations

import json
from typing import Any

from ..i18n import negotiate, render_data, use_locale

_MARKERS = ("⟦i18n:".encode(), b"\\u27e6i18n:")


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def render_json_body(body: bytes, locale: str) -> bytes:
    if not any(marker in body for marker in _MARKERS):
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    return json.dumps(
        render_data(data, locale), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


class I18nMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        locale = negotiate(_header(scope, b"accept-language"))
        start: dict[str, Any] | None = None
        chunks: list[bytes] = []

        async def send_localized(message: dict[str, Any]) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                content_type = next(
                    (v for k, v in message.get("headers") or () if k.lower() == b"content-type"), b""
                )
                if content_type.startswith(b"application/json"):
                    start = message
                    return
                await send(message)
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = render_json_body(b"".join(chunks), locale)
            headers = [
                (key, value)
                for key, value in start.get("headers") or ()
                if key.lower() not in {b"content-length", b"vary"}
            ]
            headers += [(b"content-length", str(len(body)).encode()), (b"vary", b"Accept-Language")]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        with use_locale(locale):
            await self.app(scope, receive, send_localized)
