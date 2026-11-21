"""内置的 Samba（SMB）媒体库来源：直接访问 NAS 或电脑上的共享文件夹，不需要先挂载。

每台服务器（共享）一份配置，在控制台“网络存储”里添加。用 smbprotocol 访问共享；ffmpeg 读不了
``smb://``，给 ffmpeg / ffprobe 的地址经本机读取桥（:mod:`.bridge`）转成回环 http 地址。
GPU 机器直连共享读取影片，连不上（或没装 smbprotocol）时改为经主程序中转。

smbprotocol 在用到时才导入：缺少依赖时插件仍能加载，调用时给出明确提示。
"""

from __future__ import annotations

import errno
import logging
import posixpath
import secrets
from collections.abc import Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from ..i18n import msg
from ..plugins.base import (
    CredentialField,
    PluginCallError,
    PluginConfigError,
    StateCallback,
    builtin_icon,
)
from . import bridge
from .base import RANGE_CHUNK, SMALL_FILE_LIMIT, MediaInput, MediaSourcePlugin, SourceEntry

logger = logging.getLogger(__name__)

DEFAULT_PORT = 445
# 连接超时：GPU 机器直连不了时尽快改走主程序中转。
CONNECT_TIMEOUT = 10


def _smbclient() -> Any:
    try:
        import smbclient
    except ImportError as exc:
        raise PluginCallError(msg("smb.missingLibrary")) from exc
    return smbclient


class _Share:
    """一个共享的连接：UNC 路径、连接参数，以及把 smbclient 的异常转成调用方能处理的异常。

    连接按 cache 复用，close 时断开。主程序登记给读取桥的地址也用它读取，不依赖插件实例。
    """

    def __init__(self, settings: Mapping[str, Any], cache: dict[str, Any] | None = None) -> None:
        self.settings = dict(settings)
        self.cache: dict[str, Any] = {} if cache is None else cache
        self.used = False

    def _options(self) -> dict[str, Any]:
        username = str(self.settings.get("username") or "")
        domain = str(self.settings.get("domain") or "")
        if username and domain and "\\" not in username and "@" not in username:
            username = f"{domain}\\{username}"
        return {
            "username": username or None,
            "password": str(self.settings.get("password") or "") or None,
            "port": int(self.settings.get("port") or DEFAULT_PORT),
            "connection_timeout": CONNECT_TIMEOUT,
            "connection_cache": self.cache,
        }

    def unc(self, path: str) -> str:
        parts = [part for part in path.split("/") if part]
        return "\\\\" + "\\".join([str(self.settings["host"]), str(self.settings["share"]), *parts])

    def call(self, name: str, path: str, *args: Any, **kwargs: Any) -> Any:
        """调用 smbclient 的函数，把连接、认证和文件错误转成调用方能处理的异常。"""
        client = _smbclient()
        from smbprotocol import exceptions as smb_errors
        from smbprotocol.header import NtStatus

        self.used = True
        try:
            result = getattr(client, name)(self.unc(path), *args, **self._options(), **kwargs)
            # 列目录是惰性的：在这里读完，出错时同样转换。
            return list(result) if name == "scandir" else result
        except smb_errors.SMBOSError as exc:
            if exc.errno == errno.ENOENT:
                raise FileNotFoundError(path) from exc
            if exc.errno == errno.EEXIST:
                raise FileExistsError(path) from exc
            if exc.errno == errno.EACCES or exc.ntstatus == NtStatus.STATUS_ACCESS_DENIED:
                raise PluginCallError(msg("smb.forbidden")) from exc
            raise PluginCallError(msg("smb.failed", error=exc.strerror or str(exc))) from exc
        except (smb_errors.SMBAuthenticationError, smb_errors.LogonFailure, smb_errors.AccessDenied) as exc:
            raise PluginCallError(msg("smb.forbidden")) from exc
        except smb_errors.BadNetworkName as exc:
            raise PluginCallError(msg("smb.shareMissing", share=self.settings.get("share", ""))) from exc
        except (ValueError, OSError, smb_errors.SMBConnectionClosed) as exc:
            # smbprotocol 连不上服务器时抛出 ValueError（Failed to connect to ...）。
            raise PluginCallError(msg("smb.unreachable", error=str(exc) or type(exc).__name__)) from exc
        except smb_errors.SMBException as exc:
            raise PluginCallError(msg("smb.failed", error=str(exc) or type(exc).__name__)) from exc

    def open(self, path: str, mode: str = "rb") -> BinaryIO:
        return self.call("open_file", path, mode=mode)

    def close(self) -> None:
        if not self.used:
            return
        try:
            _smbclient().reset_connection_cache(connection_cache=self.cache)
        except Exception:
            logger.debug("断开 Samba 连接失败。", exc_info=True)


class SmbSource(MediaSourcePlugin):
    id: ClassVar[str] = "smb"
    label: ClassVar[str] = "Samba"
    homepage: ClassVar[str] = ""
    icon: ClassVar[str] = builtin_icon("smb.svg")
    description: ClassVar[str] = msg("smb.description")
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        CredentialField(
            "host",
            msg("smb.field.host"),
            secret=False,
            placeholder="192.168.1.10",
            help=msg("smb.field.hostHelp"),
            binds_secrets=True,
        ),
        CredentialField(
            "share",
            msg("smb.field.share"),
            secret=False,
            placeholder="video",
            help=msg("smb.field.shareHelp"),
        ),
        CredentialField("username", msg("smb.field.username"), secret=False, required=False),
        CredentialField(
            "password",
            msg("smb.field.password"),
            required=False,
            help=msg("mediaServer.field.keyHelp"),
        ),
        CredentialField(
            "port",
            msg("smb.field.port"),
            type="number",
            required=False,
            default=DEFAULT_PORT,
            min=1,
            max=65535,
            binds_secrets=True,
        ),
        CredentialField(
            "domain",
            msg("smb.field.domain"),
            secret=False,
            required=False,
            help=msg("smb.field.domainHelp"),
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
        # 每个实例自己的连接，close 时断开；不连接，第一次访问时才连。
        self._share = _Share(self.settings)

    @classmethod
    def validate_values(cls, values: dict[str, Any]) -> dict[str, Any]:
        host = str(values["host"]).strip().strip("\\/")
        if not host or any(char in host for char in "\\/ "):
            raise PluginConfigError(msg("smb.badHost"))
        share = str(values["share"]).strip().strip("\\/")
        if not share or any(char in share for char in "\\/"):
            raise PluginConfigError(msg("smb.badShare"))
        return {**values, "host": host, "share": share}

    @classmethod
    def summary(cls, values: dict[str, Any]) -> str:
        return f"\\\\{values.get('host', '')}\\{values.get('share', '')}"

    def close(self) -> None:
        super().close()
        self._share.close()

    # ---- MediaSource ----

    def _entry(self, path: str, stat: Any, is_dir: bool) -> SourceEntry:
        file_id = int(getattr(stat, "st_ino", 0) or 0)
        return SourceEntry(
            path=path,
            is_dir=is_dir,
            size=0 if is_dir else int(stat.st_size),
            mtime_ns=int(getattr(stat, "st_mtime_ns", 0) or 0),
            file_key=f"smb:{file_id}" if file_id and not is_dir else None,
        )

    def list_dir(self, path: str) -> list[SourceEntry]:
        path = "/" + path.strip("/")
        result = []
        for item in self._share.call("scandir", path):
            if item.name.startswith("."):
                continue
            is_dir = item.is_dir()
            result.append(self._entry(posixpath.join(path, item.name), item.stat(), is_dir))
        return sorted(result, key=lambda entry: entry.path)

    def stat(self, path: str) -> SourceEntry | None:
        path = "/" + path.strip("/")
        try:
            stat = self._share.call("stat", path)
        except FileNotFoundError:
            return None
        return self._entry(path, stat, bool(stat.st_file_attributes & 0x10))

    def read_bytes(self, path: str, limit: int = SMALL_FILE_LIMIT) -> bytes:
        with self._share.open(path) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError(msg("sources.local.tooLarge", name=posixpath.basename(path)))
        return data

    def write_bytes(self, path: str, data: bytes, *, overwrite: bool) -> None:
        path = "/" + path.strip("/")
        directory = posixpath.dirname(path)
        self._share.call("makedirs", directory, exist_ok=True)
        if not overwrite and self.stat(path) is not None:
            raise FileExistsError(path)
        # 先写到同目录的临时文件再改名，写完才出现在目标位置。
        temporary = posixpath.join(directory, f".{posixpath.basename(path)}.{secrets.token_hex(4)}.tmp")
        with self._share.open(temporary, "wb") as handle:
            handle.write(data)
        try:
            self._share.call("replace" if overwrite else "rename", temporary, self._share.unc(path))
        except BaseException:
            try:
                self._share.call("remove", temporary)
            except Exception:
                logger.warning("删除 Samba 临时文件 %s 失败。", temporary, exc_info=True)
            raise

    def rename(self, source: str, target: str) -> None:
        target = "/" + target.strip("/")
        self._share.call("makedirs", posixpath.dirname(target), exist_ok=True)
        if self.stat(target) is not None:
            raise FileExistsError(target)
        self._share.call("rename", source, self._share.unc(target))

    def read_range(self, path: str, start: int, end: int) -> Iterator[bytes]:
        with self._share.open(path) as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(RANGE_CHUNK, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

    def media_input(self, path: str) -> MediaInput:
        """ffmpeg / ffprobe 的输入：经本机读取桥的回环 http 地址。

        地址在本实例关闭后仍可用（主程序取得地址后才运行 ffprobe），读取时用自己的连接。
        """
        entry = self.stat(path)
        if entry is None or entry.is_dir:
            raise FileNotFoundError(path)
        # 读取桥的每个请求都用这份连接；地址过期前连接一直保留，供 ffprobe 多次按范围读取。
        reader = _Share(self.settings)
        return MediaInput(bridge.register(lambda: reader.open(path), entry.size, posixpath.basename(path)))

    def download(self, path: str) -> MediaInput:
        """GPU 机器直连共享读取影片。连不上或缺少 smbprotocol 时报错，由 GPU 机器改走主程序中转。"""
        return self.media_input(path)

    def test(self) -> str:
        count = sum(1 for item in self._share.call("scandir", "/") if not item.name.startswith("."))
        return msg("smb.testOk", count=count)
