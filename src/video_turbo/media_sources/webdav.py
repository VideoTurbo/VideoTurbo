"""内置的 WebDAV 媒体库来源：群晖、威联通、Nextcloud、OpenList 等都能提供 WebDAV。

每台服务器一份配置（``credential_fields``），在控制台“网络存储”里添加。主程序和 GPU 机器都用这个类：
GPU 机器直连服务器下载影片；连不上时（例如服务器只在家里局域网），由 GPU 机器改为经主程序中转。

只依赖标准库和 httpx，GPU 机器上也能直接加载。
"""

from __future__ import annotations

import base64
import posixpath
import secrets
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from ..i18n import msg
from ..plugins.base import (
    CredentialField,
    PluginCallError,
    StateCallback,
    builtin_icon,
    normalize_base_url,
    response_error,
)
from .base import SMALL_FILE_LIMIT, MediaInput, MediaSourcePlugin, SourceEntry

_DAV = "{DAV:}"
_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    b"</d:prop></d:propfind>"
)
# GPU 机器检查能否直连服务器的超时：连不上就尽快改走主程序中转。
DIRECT_CHECK_TIMEOUT = 10


def _mtime_ns(text: str | None) -> int:
    if not text:
        return 0
    try:
        return int(parsedate_to_datetime(text).timestamp() * 1_000_000_000)
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0


class WebDavSource(MediaSourcePlugin):
    id: ClassVar[str] = "webdav"
    label: ClassVar[str] = "WebDAV"
    homepage: ClassVar[str] = ""
    icon: ClassVar[str] = builtin_icon("webdav.svg")
    description: ClassVar[str] = msg("webdav.description")
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        CredentialField(
            "url",
            msg("webdav.field.url"),
            secret=False,
            placeholder="https://192.168.1.10:5006/video",
            help=msg("webdav.field.urlHelp"),
            binds_secrets=True,
        ),
        CredentialField("username", msg("webdav.field.username"), secret=False, required=False),
        CredentialField(
            "password",
            msg("webdav.field.password"),
            required=False,
            help=msg("mediaServer.field.keyHelp"),
        ),
        CredentialField(
            "insecure",
            msg("webdav.field.insecure"),
            type="switch",
            help=msg("webdav.field.insecureHelp"),
        ),
    )

    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
        *,
        state: Mapping[str, Any] | None = None,
        on_state: StateCallback | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        super().__init__(settings, state=state, on_state=on_state, transport=transport, timeout=timeout)
        if self.settings.get("insecure"):
            # NAS 常用自签名证书：用户打开“忽略证书错误”时不校验。
            self.client.close()
            self.client = httpx.Client(
                transport=transport, timeout=timeout, follow_redirects=False, verify=False
            )
        self.base = str(self.settings.get("url") or "").rstrip("/")
        self.base_path = urlsplit(self.base).path.rstrip("/")

    @classmethod
    def validate_values(cls, values: dict[str, Any]) -> dict[str, Any]:
        return {**values, "url": normalize_base_url(values["url"], msg("webdav.field.url"))}

    @classmethod
    def summary(cls, values: dict[str, Any]) -> str:
        return str(values.get("url") or "")

    # ---- 请求 ----

    def _headers(self) -> dict[str, str]:
        username = str(self.settings.get("username") or "")
        if not username:
            return {}
        token = base64.b64encode(f"{username}:{self.settings.get('password') or ''}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _url(self, path: str, *, directory: bool = False) -> str:
        path = "/" + path.strip("/")
        if directory and path != "/":
            path += "/"
        return self.base + quote(path, safe="/")

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        return self.client.request(method, url, headers=headers, **kwargs)

    def _check(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise PluginCallError(msg("webdav.forbidden", status_code=response.status_code))
        if error := response_error(response, self.label):
            raise error

    def _propfind(self, path: str, depth: str) -> list[SourceEntry] | None:
        """PROPFIND 的结果；不存在时返回 None。目录地址不带斜杠时服务器可能要求跳转，带上斜杠再试一次。"""
        response = None
        for directory in (False, True):
            response = self._request(
                "PROPFIND",
                self._url(path, directory=directory),
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                content=_PROPFIND_BODY,
            )
            if not response.is_redirect:
                break
        assert response is not None
        if response.status_code == 404:
            return None
        self._check(response)
        return self._parse(response.content)

    def _relative(self, href: str) -> str:
        path = unquote(urlsplit(href).path)
        if self.base_path and (path == self.base_path or path.startswith(self.base_path + "/")):
            path = path[len(self.base_path) :]
        return "/" + path.strip("/")

    def _parse(self, content: bytes) -> list[SourceEntry]:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise PluginCallError(msg("webdav.badResponse")) from exc
        entries = []
        for item in root.iter(f"{_DAV}response"):
            href = item.findtext(f"{_DAV}href")
            if not href:
                continue
            prop = None
            for propstat in item.iter(f"{_DAV}propstat"):
                status = propstat.findtext(f"{_DAV}status") or ""
                if " 200 " in f"{status} ":
                    prop = propstat.find(f"{_DAV}prop")
                    break
            if prop is None:
                continue
            resource = prop.find(f"{_DAV}resourcetype")
            is_dir = resource is not None and resource.find(f"{_DAV}collection") is not None
            try:
                size = int(prop.findtext(f"{_DAV}getcontentlength") or 0)
            except ValueError:
                size = 0
            entries.append(
                SourceEntry(
                    path=self._relative(href),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    mtime_ns=_mtime_ns(prop.findtext(f"{_DAV}getlastmodified")),
                )
            )
        return entries

    # ---- MediaSource ----

    def list_dir(self, path: str) -> list[SourceEntry]:
        path = "/" + path.strip("/")
        entries = self._propfind(path, "1")
        if entries is None:
            raise FileNotFoundError(path)
        return sorted(
            (
                entry
                for entry in entries
                if entry.path != path and not entry.name.startswith(".")
                and posixpath.dirname(entry.path) == path
            ),
            key=lambda entry: entry.path,
        )

    def stat(self, path: str) -> SourceEntry | None:
        path = "/" + path.strip("/")
        entries = self._propfind(path, "0")
        if not entries:
            return None
        return next((entry for entry in entries if entry.path == path), entries[0])

    def read_bytes(self, path: str, limit: int = SMALL_FILE_LIMIT) -> bytes:
        with self.client.stream("GET", self._url(path), headers=self._headers()) as response:
            if response.status_code == 404:
                raise FileNotFoundError(path)
            self._check(response)
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > limit:
                    raise ValueError(msg("sources.local.tooLarge", name=posixpath.basename(path)))
            return bytes(data)

    def _make_dirs(self, directory: str) -> None:
        """依次创建缺少的上级目录。已存在时服务器返回 405。"""
        current = ""
        for part in [item for item in directory.strip("/").split("/") if item]:
            current += "/" + part
            response = self._request("MKCOL", self._url(current, directory=True))
            if response.status_code not in {201, 405} and not response.is_redirect:
                self._check(response)

    def _move(self, source: str, target: str, *, overwrite: bool) -> None:
        response = self._request(
            "MOVE",
            self._url(source),
            headers={"Destination": self._url(target), "Overwrite": "T" if overwrite else "F"},
        )
        if response.status_code == 412:
            raise FileExistsError(target)
        if response.status_code == 404:
            raise FileNotFoundError(source)
        self._check(response)

    def write_bytes(self, path: str, data: bytes, *, overwrite: bool) -> None:
        directory = posixpath.dirname("/" + path.strip("/"))
        self._make_dirs(directory)
        if not overwrite and self.stat(path) is not None:
            raise FileExistsError(path)
        # 先写到同目录的临时文件再改名，写完才出现在目标位置。
        temporary = posixpath.join(directory, f".{posixpath.basename(path)}.{secrets.token_hex(4)}.tmp")
        response = self._request("PUT", self._url(temporary), content=data)
        self._check(response)
        try:
            self._move(temporary, path, overwrite=overwrite)
        except BaseException:
            self._request("DELETE", self._url(temporary))
            raise

    def rename(self, source: str, target: str) -> None:
        self._make_dirs(posixpath.dirname("/" + target.strip("/")))
        self._move(source, target, overwrite=False)

    def media_input(self, path: str) -> MediaInput:
        return MediaInput(self._url(path), self._headers())

    def download(self, path: str) -> MediaInput:
        """GPU 机器直连服务器的下载地址。先用短超时检查能否连上，连不上时报错，由 GPU 机器改走主程序中转。"""
        target = self.media_input(path)
        try:
            response = self._request("HEAD", target.url, timeout=DIRECT_CHECK_TIMEOUT)
        except httpx.HTTPError as exc:
            raise PluginCallError(msg("webdav.unreachable", error=type(exc).__name__)) from exc
        if response.status_code == 404:
            raise FileNotFoundError(path)
        self._check(response)
        return target

    def test(self) -> str:
        entries = self._propfind("/", "1")
        if entries is None:
            raise PluginCallError(msg("webdav.rootMissing"))
        count = sum(1 for entry in entries if entry.path != "/")
        return msg("webdav.testOk", count=count)
