"""内置 Samba 媒体库来源和本机读取桥。smbclient 用替身代替：把 UNC 路径映射到临时目录。"""

import io
import os
import shutil
import types
from pathlib import Path

import httpx
import pytest
from conftest import raises_rendered
from smbprotocol.exceptions import SMBAuthenticationError, SMBOSError
from smbprotocol.header import NtStatus

from video_turbo.i18n import render
from video_turbo.media_sources import bridge, smb
from video_turbo.media_sources.smb import SmbSource
from video_turbo.plugins.base import PluginCallError, PluginConfigError

SETTINGS = {"host": "nas", "share": "video", "username": "user", "password": "pw"}


class FakeSmbClient(types.SimpleNamespace):
    """smbclient 的替身：\\\\nas\\video\\… 映射到 root 下，记录每次调用的连接参数。"""

    def __init__(self, root: Path, *, fail: Exception | None = None) -> None:
        super().__init__(root=root, fail=fail, calls=[], resets=0)

    def _real(self, unc: str, options: dict) -> Path:
        self.calls.append((unc, options))
        if self.fail:
            raise self.fail
        parts = unc.lstrip("\\").split("\\")
        assert parts[:2] == ["nas", "video"], unc
        return self.root.joinpath(*parts[2:])

    def _missing(self, unc: str) -> SMBOSError:
        return SMBOSError(NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, unc)

    def scandir(self, unc: str, **options):
        real = self._real(unc, options)
        if not real.is_dir():
            raise self._missing(unc)
        for item in sorted(real.iterdir()):
            stat = item.stat()
            yield types.SimpleNamespace(
                name=item.name,
                is_dir=item.is_dir,
                stat=lambda stat=stat: types.SimpleNamespace(
                    st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns, st_ino=stat.st_ino
                ),
            )

    def stat(self, unc: str, **options):
        real = self._real(unc, options)
        if not real.exists():
            raise self._missing(unc)
        stat = real.stat()
        return types.SimpleNamespace(
            st_size=stat.st_size,
            st_mtime_ns=stat.st_mtime_ns,
            st_ino=stat.st_ino,
            st_file_attributes=0x10 if real.is_dir() else 0x20,
        )

    def open_file(self, unc: str, mode: str = "r", **options):
        real = self._real(unc, options)
        if "r" in mode and not real.exists():
            raise self._missing(unc)
        return open(real, mode)

    def makedirs(self, unc: str, exist_ok: bool = False, **options) -> None:
        self._real(unc, options).mkdir(parents=True, exist_ok=exist_ok)

    def rename(self, unc: str, target: str, **options) -> None:
        real, destination = self._real(unc, options), self._real(target, options)
        if destination.exists():
            raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_COLLISION, target)
        real.rename(destination)

    def replace(self, unc: str, target: str, **options) -> None:
        os.replace(self._real(unc, options), self._real(target, options))

    def remove(self, unc: str, **options) -> None:
        self._real(unc, options).unlink()

    def reset_connection_cache(self, connection_cache=None) -> None:
        self.resets += 1


@pytest.fixture
def share(tmp_path: Path, monkeypatch) -> FakeSmbClient:
    root = tmp_path / "share"
    (root / "电影" / "子目录").mkdir(parents=True)
    (root / "电影" / "片 A.mkv").write_bytes(b"0123456789")
    (root / "电影" / ".hidden").write_bytes(b"x")
    client = FakeSmbClient(root)
    monkeypatch.setattr(smb, "_smbclient", lambda: client)
    return client


def test_values_validation_and_summary() -> None:
    merged = SmbSource.merge_values({}, {"host": " \\\\nas\\ ", "share": "/video/", "password": "pw"})
    assert (merged["host"], merged["share"]) == ("nas", "video")
    assert SmbSource.summary(merged) == "\\\\nas\\video"
    with raises_rendered(PluginConfigError, "只填 IP 地址或主机名"):
        SmbSource.merge_values({}, {"host": "smb://nas/video", "share": "video"})
    with raises_rendered(PluginConfigError, "不要带斜杠"):
        SmbSource.merge_values({}, {"host": "nas", "share": "video/电影"})


def test_list_stat_read_write_and_rename(share: FakeSmbClient) -> None:
    with SmbSource({**SETTINGS, "domain": "HOME", "port": 4445}) as source:
        entries = source.list_dir("/电影")
        assert [(entry.path, entry.is_dir, entry.size) for entry in entries] == [
            ("/电影/子目录", True, 0),
            ("/电影/片 A.mkv", False, 10),
        ]
        assert entries[1].file_key and entries[1].file_key.startswith("smb:")
        assert source.stat("/电影").is_dir and source.stat("/没有") is None
        assert source.read_bytes("/电影/片 A.mkv") == b"0123456789"
        with pytest.raises(ValueError):
            source.read_bytes("/电影/片 A.mkv", limit=3)
        with pytest.raises(FileNotFoundError):
            source.list_dir("/没有")
        assert b"".join(source.read_range("/电影/片 A.mkv", 2, 5)) == b"2345"
        source.write_bytes("/电影/新目录/片 A.zh.srt", b"1", overwrite=False)
        assert (share.root / "电影" / "新目录" / "片 A.zh.srt").read_bytes() == b"1"
        with pytest.raises(FileExistsError):
            source.write_bytes("/电影/新目录/片 A.zh.srt", b"2", overwrite=False)
        source.write_bytes("/电影/新目录/片 A.zh.srt", b"2", overwrite=True)
        assert (share.root / "电影" / "新目录" / "片 A.zh.srt").read_bytes() == b"2"
        assert not list(share.root.rglob("*.tmp"))
        source.rename("/电影/片 A.mkv", "/电影/改名/片 A.mkv")
        assert (share.root / "电影" / "改名" / "片 A.mkv").exists()
        (share.root / "电影" / "B.mkv").write_bytes(b"b")
        with pytest.raises(FileExistsError):
            source.rename("/电影/B.mkv", "/电影/改名/片 A.mkv")
        assert render(source.test()) == "连接成功：共享根目录下有 1 项。"
    unc, options = share.calls[0]
    assert unc == "\\\\nas\\video\\电影"
    assert options["username"] == "HOME\\user" and options["password"] == "pw" and options["port"] == 4445
    assert share.resets == 1


def test_errors_are_readable(tmp_path: Path, monkeypatch) -> None:
    cases = [
        (SMBAuthenticationError("bad password"), "检查账号、密码"),
        (ValueError("Failed to connect to 'nas:445'"), "无法连接 Samba 服务器"),
        (ConnectionRefusedError("refused"), "无法连接 Samba 服务器"),
        (SMBOSError(NtStatus.STATUS_ACCESS_DENIED, "x"), "拒绝访问"),
    ]
    for error, text in cases:
        client = FakeSmbClient(tmp_path, fail=error)
        monkeypatch.setattr(smb, "_smbclient", lambda client=client: client)
        with SmbSource(SETTINGS) as source, raises_rendered(PluginCallError, text):
            source.test()

    def missing():
        raise PluginCallError("缺少 smbprotocol")

    monkeypatch.setattr(smb, "_smbclient", missing)
    with SmbSource(SETTINGS) as source, raises_rendered(PluginCallError, "缺少 smbprotocol"):
        source.download("/a.mkv")


def test_bridge_serves_ranges() -> None:
    data = bytes(range(256)) * 8
    url = bridge.register(lambda: io.BytesIO(data), len(data), "片 A.mkv")
    assert url.startswith("http://127.0.0.1:") and url.endswith("/%E7%89%87%20A.mkv")
    with httpx.Client() as client:
        full = client.get(url)
        assert full.status_code == 200 and full.content == data and full.headers["accept-ranges"] == "bytes"
        part = client.get(url, headers={"Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == data[10:20]
        assert part.headers["content-range"] == f"bytes 10-19/{len(data)}"
        assert client.get(url, headers={"Range": "bytes=-5"}).content == data[-5:]
        assert client.get(url, headers={"Range": f"bytes={len(data)}-"}).status_code == 416
        head = client.head(url)
        assert head.status_code == 200 and head.headers["content-length"] == str(len(data))
        # 令牌不对时 404。
        assert client.get(url.rsplit("/", 2)[0] + "/wrong/x").status_code == 404


def test_media_input_reads_through_bridge_after_close(share: FakeSmbClient) -> None:
    with SmbSource(SETTINGS) as source:
        target = source.media_input("/电影/片 A.mkv")
        with pytest.raises(FileNotFoundError):
            source.media_input("/电影/没有.mkv")
    # 主程序取得地址、关闭来源后才运行 ffprobe：地址仍然可用。
    assert target.headers == {}
    with httpx.Client() as client:
        assert client.get(target.url, headers={"Range": "bytes=3-6"}).content == b"3456"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 FFmpeg")
def test_ffprobe_reads_bridge(tmp_path: Path, share: FakeSmbClient) -> None:
    import subprocess

    from video_turbo.media import run_ffprobe

    movie = share.root / "电影" / "tone.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(movie)],
        check=True,
    )
    with SmbSource(SETTINGS) as source:
        target = source.media_input("/电影/tone.mp4")
    probe = run_ffprobe(target)
    assert probe["streams"][0]["codec_type"] == "audio"
