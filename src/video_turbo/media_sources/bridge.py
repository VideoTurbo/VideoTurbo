"""本机读取桥：把 ffmpeg / ffprobe 读不了的来源（例如 Samba）暴露成本机回环地址上的 http 地址。

来源登记一个“打开文件”的函数和文件大小，得到 ``http://127.0.0.1:<端口>/<随机令牌>``；ffmpeg 按 Range
请求读取，每个请求单独打开文件、定位后分块返回。地址只在本机回环接口上监听，令牌随机，过期后失效。

主程序和 GPU 机器各自在进程内启动一个（第一次登记时），之后一直复用。
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import BinaryIO
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 登记的地址有效期：足够抽完一部长片的音频。
ENTRY_TTL_SECONDS = 12 * 3600
CHUNK = 1024 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


@dataclass
class _Entry:
    open: Callable[[], BinaryIO]
    size: int
    name: str
    expires_at: float


class _Bridge:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    def _start(self) -> ThreadingHTTPServer:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:
                bridge._serve(self, body=False)

            def do_GET(self) -> None:
                bridge._serve(self, body=True)

            def log_message(self, format: str, *args: object) -> None:
                logger.debug("读取桥：" + format, *args)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="media-bridge", daemon=True).start()
        return server

    def register(self, opener: Callable[[], BinaryIO], size: int, name: str) -> str:
        """登记一个文件，返回 ffmpeg 可以读取的本机地址。name 只用于出错时显示文件名。"""
        now = time.monotonic()
        token = secrets.token_urlsafe(24)
        with self._lock:
            for key in [key for key, entry in self._entries.items() if entry.expires_at < now]:
                del self._entries[key]
            self._entries[token] = _Entry(opener, size, name, now + ENTRY_TTL_SECONDS)
            if self._server is None:
                self._server = self._start()
            port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/{token}/{name}"

    def _entry(self, path: str) -> _Entry | None:
        token = path.lstrip("/").split("/", 1)[0]
        with self._lock:
            entry = self._entries.get(token)
        if entry is None or entry.expires_at < time.monotonic():
            return None
        return entry

    def _serve(self, handler: BaseHTTPRequestHandler, *, body: bool) -> None:
        entry = self._entry(handler.path)
        if entry is None:
            handler.send_error(404)
            return
        start, end = 0, entry.size - 1
        ranged = False
        match = _RANGE.match(handler.headers.get("Range", "").strip())
        if match and entry.size:
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), entry.size - 1) if last else entry.size - 1
            elif last:
                start = max(entry.size - int(last), 0)
            if start > end:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{entry.size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            ranged = True
        length = max(end - start + 1, 0)
        handler.send_response(206 if ranged else 200)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(length))
        if ranged:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{entry.size}")
        handler.end_headers()
        if not body or not length:
            return
        try:
            with entry.open() as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # ffmpeg 读够了就断开（例如 ffprobe 只读文件头），不算错误。
            pass
        except Exception:
            logger.warning("读取桥读取 %s 失败。", entry.name, exc_info=True)
            handler.close_connection = True


_bridge = _Bridge()


def register(opener: Callable[[], BinaryIO], size: int, name: str) -> str:
    """登记一个文件，返回 ffmpeg / ffprobe 可以读取的本机 http 地址（支持 Range）。"""
    return _bridge.register(opener, size, quote(name or "file", safe=""))
