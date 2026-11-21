"""内置 WebDAV 媒体库来源：用内存里的假 WebDAV 服务器测试目录、读写、改名和直连检查。"""

import base64
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest
from conftest import raises_rendered

from video_turbo.i18n import render
from video_turbo.media_sources.webdav import WebDavSource
from video_turbo.plugins.base import PluginCallError, PluginConfigError

BASE = "/dav"
MTIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class FakeDav:
    """最小的 WebDAV 服务器：目录集合 + 文件内容，路径不含 BASE 前缀。"""

    def __init__(self, *, auth: tuple[str, str] | None = ("user", "pw")) -> None:
        self.dirs = {"/"}
        self.files: dict[str, bytes] = {}
        self.auth = auth
        self.requests: list[httpx.Request] = []

    def add(self, path: str, data: bytes = b"") -> None:
        parts = path.strip("/").split("/")
        for index in range(1, len(parts)):
            self.dirs.add("/" + "/".join(parts[:index]))
        self.files[path] = data

    def _path(self, url: str) -> str:
        path = unquote(urlsplit(url).path)
        assert path.startswith(BASE)
        return "/" + path[len(BASE) :].strip("/")

    def _entry(self, path: str) -> str:
        is_dir = path in self.dirs
        href = quote(BASE + path + ("/" if is_dir and path != "/" else ""))
        props = "<d:resourcetype><d:collection/></d:resourcetype>" if is_dir else (
            f"<d:resourcetype/><d:getcontentlength>{len(self.files[path])}</d:getcontentlength>"
        )
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}"
            f"<d:getlastmodified>{format_datetime(MTIME, usegmt=True)}</d:getlastmodified>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.auth:
            token = base64.b64encode(":".join(self.auth).encode()).decode()
            if request.headers.get("Authorization") != f"Basic {token}":
                return httpx.Response(401)
        path = self._path(str(request.url))
        method = request.method
        exists = path in self.dirs or path in self.files
        if method == "PROPFIND":
            if not exists:
                return httpx.Response(404)
            children = []
            if request.headers["Depth"] == "1" and path in self.dirs:
                prefix = path.rstrip("/") + "/"
                children = [
                    item
                    for item in sorted(self.dirs | set(self.files))
                    if item != path and item.startswith(prefix) and "/" not in item[len(prefix) :]
                ]
            body = "".join(self._entry(item) for item in [path, *children])
            return httpx.Response(207, text=f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{body}</d:multistatus>')
        if method in {"GET", "HEAD"}:
            if path not in self.files:
                return httpx.Response(404)
            data = self.files[path]
            return httpx.Response(200, content=b"" if method == "HEAD" else data, headers={"Content-Length": str(len(data))})
        if method == "MKCOL":
            if exists:
                return httpx.Response(405)
            self.dirs.add(path)
            return httpx.Response(201)
        if method == "PUT":
            self.files[path] = request.content
            return httpx.Response(201)
        if method == "MOVE":
            target = self._path(request.headers["Destination"])
            if path not in self.files:
                return httpx.Response(404)
            if target in self.files and request.headers["Overwrite"] == "F":
                return httpx.Response(412)
            self.files[target] = self.files.pop(path)
            return httpx.Response(201)
        if method == "DELETE":
            self.files.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(405)


SETTINGS = {"url": "http://nas:5005/dav", "username": "user", "password": "pw"}


def _source(server: FakeDav, settings: dict | None = None) -> WebDavSource:
    return WebDavSource(settings or SETTINGS, transport=httpx.MockTransport(server))


def test_address_validation() -> None:
    assert WebDavSource.merge_values({}, {"url": " http://nas/dav/ "}) == {"url": "http://nas/dav"}
    with raises_rendered(PluginConfigError, "WebDAV 地址"):
        WebDavSource.merge_values({}, {"url": "nas/dav"})
    assert WebDavSource.summary({"url": "http://nas/dav", "password": "x"}) == "http://nas/dav"


def test_list_stat_and_read() -> None:
    server = FakeDav()
    server.add("/电影/片 A.mkv", b"video")
    server.add("/电影/片 A.ja.srt", b"subs")
    server.add("/电影/.hidden", b"x")
    server.dirs.add("/电影/子目录")
    with _source(server) as source:
        entries = source.list_dir("/电影")
        assert [(entry.path, entry.is_dir, entry.size) for entry in entries] == [
            ("/电影/子目录", True, 0),
            ("/电影/片 A.ja.srt", False, 4),
            ("/电影/片 A.mkv", False, 5),
        ]
        assert entries[2].mtime_ns == int(MTIME.timestamp() * 1_000_000_000)
        assert source.stat("/电影/片 A.mkv").size == 5
        assert source.stat("/电影").is_dir
        assert source.stat("/没有") is None
        assert source.read_bytes("/电影/片 A.ja.srt") == b"subs"
        with pytest.raises(ValueError):
            source.read_bytes("/电影/片 A.mkv", limit=2)
        with pytest.raises(FileNotFoundError):
            source.list_dir("/没有")
        assert [entry.path for entry in source.walk("/", ["mkv"])] == ["/电影/片 A.mkv"]
        target = source.media_input("/电影/片 A.mkv")
    assert target.url == "http://nas:5005/dav/%E7%94%B5%E5%BD%B1/%E7%89%87%20A.mkv"
    assert target.headers == {"Authorization": "Basic " + base64.b64encode(b"user:pw").decode()}


def test_write_and_rename() -> None:
    server = FakeDav()
    server.add("/电影/片.mkv", b"video")
    with _source(server) as source:
        source.write_bytes("/电影/新目录/片.zh.srt", b"1", overwrite=False)
        assert server.files["/电影/新目录/片.zh.srt"] == b"1"
        assert "/电影/新目录" in server.dirs
        with pytest.raises(FileExistsError):
            source.write_bytes("/电影/新目录/片.zh.srt", b"2", overwrite=False)
        source.write_bytes("/电影/新目录/片.zh.srt", b"2", overwrite=True)
        assert server.files["/电影/新目录/片.zh.srt"] == b"2"
        # 临时文件都改名或删除了。
        assert not [path for path in server.files if path.endswith(".tmp")]
        source.rename("/电影/片.mkv", "/电影/改名/片.mkv")
        assert "/电影/改名/片.mkv" in server.files and "/电影/片.mkv" not in server.files
        server.add("/电影/另一部.mkv")
        with pytest.raises(FileExistsError):
            source.rename("/电影/另一部.mkv", "/电影/改名/片.mkv")


def test_errors_and_direct_check() -> None:
    server = FakeDav()
    server.add("/a.mkv", b"v")
    with _source(server, {**SETTINGS, "password": "wrong"}) as source, raises_rendered(PluginCallError, "检查账号、密码"):
        source.test()
    with _source(server) as source:
        assert render(source.test()) == "连接成功：根目录下有 1 项。"
        assert source.download("/a.mkv").url.endswith("/dav/a.mkv")
        with pytest.raises(FileNotFoundError):
            source.download("/b.mkv")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with WebDavSource(SETTINGS, transport=httpx.MockTransport(unreachable)) as source, raises_rendered(PluginCallError, "无法直连"):
        source.download("/a.mkv")
    with _source(FakeDav(auth=None), {"url": "http://nas/dav"}) as source:
        assert source.media_input("/a.mkv").headers == {}
    bad = httpx.MockTransport(lambda request: httpx.Response(207, text="<html"))
    with WebDavSource(SETTINGS, transport=bad) as source, raises_rendered(PluginCallError, "无法识别"):
        source.stat("/a.mkv")


def test_insecure_switch_disables_certificate_check() -> None:
    with WebDavSource({**SETTINGS, "url": "https://nas/dav", "insecure": True}) as source:
        assert source.client._transport._pool._ssl_context.verify_mode.name == "CERT_NONE"
    with WebDavSource({**SETTINGS, "url": "https://nas/dav"}) as source:
        assert source.client._transport._pool._ssl_context.verify_mode.name == "CERT_REQUIRED"
