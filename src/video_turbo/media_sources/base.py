"""媒体库来源接口。

媒体库的文件可以在主程序容器内的文件夹里（内置的 :class:`~.local.LocalSource`），也可以在网盘里
（:class:`MediaSourcePlugin` 插件，例如 OpenList、115）。主程序扫描、读写字幕、查找海报都通过
:class:`MediaSource`，不直接访问文件系统。

网盘插件是单个 ``.py`` 文件，在控制台“设置 → 插件”安装，并在插件卡片的设置按钮里填写账号等插件设置
（``settings_fields``）；添加媒体库时选择插件作为来源。同一个插件类在两处运行：

- 主程序：浏览目录、列出影片、读写字幕和海报等小文件、改名，以及给 ffprobe 读取文件头的地址；
- GPU 机器：领取网盘影片的字幕任务后，调用 :meth:`MediaSourcePlugin.download` 取得下载地址，
  自己边下载边抽音频，影片不经过主程序。GPU 机器按文件指纹（SHA-256）从主程序下载插件源码，
  插件设置和插件状态随任务下发，只保存在内存中；插件在 GPU 机器上用 ``save_state`` 保存的状态在下载结束后回传主程序。

因此插件模块只能依赖标准库、httpx 和本模块，不能导入主程序的其他模块。
所有方法都是同步的：主程序在线程里调用，插件可以直接使用 ``self.client``（``httpx.Client``）。
路径一律是来源内以 ``/`` 开头、``/`` 分隔的绝对路径。
"""

from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, ClassVar, Self
from urllib.parse import unquote, urlsplit

import httpx

from ..i18n import msg
from ..plugins.base import PluginInfo, StateCallback

# 插件接口版本，插件文件用 ``PLUGIN_API = <版本>`` 声明自己适配的版本，
# 主程序加载 PLUGIN_API_MIN_VERSION 到 PLUGIN_API_VERSION 之间的插件。
# - 只新增（新的可选方法、字段）：PLUGIN_API_VERSION 加一，MIN 不变，旧插件照常加载；
# - 删改已有接口：PLUGIN_API_VERSION 加一，MIN 改成与它相同，旧插件需要作者更新。
# 历史：+1 支持 label、description 和字段文字写成 {语言: 文字}（多语言）。
PLUGIN_API_VERSION = 3
PLUGIN_API_MIN_VERSION = 2

# read_bytes 的默认上限：字幕、nfo 和海报都远小于这个值。
SMALL_FILE_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """来源中的一个文件或目录。"""

    path: str
    is_dir: bool
    size: int = 0
    # 最后修改时间（纳秒）；“新文件稳定等待”和探测结果缓存都依赖它。
    mtime_ns: int = 0
    # 改名、移动后不变的文件标识（本地是“设备号:inode”，网盘是文件 id），用来认出改名的影片；
    # 来源不提供时为 None。
    file_key: str | None = None

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


@dataclass(frozen=True)
class MediaInput:
    """ffmpeg / ffprobe 的输入：本地路径，或 http(s) 地址加请求头。"""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def ffmpeg_args(self, *, reconnect: bool = False) -> list[str]:
        """放在 ffmpeg / ffprobe 命令里的输入参数。reconnect：网络中断时自动重连（长时间下载用）。"""
        args: list[str] = []
        if reconnect and self.url.lower().startswith(("http://", "https://")):
            args += ["-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "30"]
        if self.headers:
            args += ["-headers", "".join(f"{key}: {value}\r\n" for key, value in self.headers.items())]
        return [*args, "-i", self.url]

    @property
    def name(self) -> str:
        """出错时给用户看的文件名，不含地址里的令牌。"""
        if "://" not in self.url:
            return posixpath.basename(self.url)
        return posixpath.basename(unquote(urlsplit(self.url).path)) or msg("sources.networkFile")


def join(directory: str, name: str) -> str:
    return posixpath.join(directory or "/", name)


def has_extension(name: str, extensions: Iterable[str]) -> bool:
    suffix = posixpath.splitext(name)[1].lower()
    return bool(suffix) and suffix in {"." + value.lower().lstrip(".") for value in extensions}


class MediaSource(ABC):
    """主程序读写媒体库文件的统一接口。"""

    # True：文件不在主程序本机，字幕任务由 GPU 机器直接下载影片抽音频。
    remote: ClassVar[bool] = False

    @abstractmethod
    def list_dir(self, path: str) -> list[SourceEntry]:
        """目录下一层的文件和子目录，不含以 . 开头的隐藏项；目录不存在时抛出 FileNotFoundError。"""

    @abstractmethod
    def stat(self, path: str) -> SourceEntry | None:
        """文件或目录的信息；不存在时返回 None。"""

    @abstractmethod
    def read_bytes(self, path: str, limit: int = SMALL_FILE_LIMIT) -> bytes:
        """读取小文件（字幕、nfo、海报）；超过 limit 字节时抛出 ValueError。"""

    @abstractmethod
    def write_bytes(self, path: str, data: bytes, *, overwrite: bool) -> None:
        """写入小文件（发布字幕），写完才出现在目标位置。

        overwrite 为 False 且目标已存在时抛出 FileExistsError；缺少的上级目录自动创建。
        """

    @abstractmethod
    def rename(self, source: str, target: str) -> None:
        """改名或移动文件；目标已存在时抛出 FileExistsError。"""

    @abstractmethod
    def media_input(self, path: str) -> MediaInput:
        """主程序运行 ffprobe（扫描时只读文件头）或 ffmpeg 时使用的输入。"""

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def is_file(self, path: str) -> bool:
        entry = self.stat(path)
        return entry is not None and not entry.is_dir

    def walk(self, root: str, extensions: Iterable[str]) -> Iterator[SourceEntry]:
        """递归列出扩展名符合的文件。同一目录的文件连续产出，扫描时可以逐目录缓存字幕列表。"""
        extensions = tuple(extensions)
        pending = [root]
        while pending:
            directory = pending.pop(0)
            try:
                entries = self.list_dir(directory)
            except FileNotFoundError:
                continue
            for entry in entries:
                if not entry.is_dir and has_extension(entry.name, extensions):
                    yield entry
            pending[0:0] = [entry.path for entry in entries if entry.is_dir]

    def close(self) -> None:
        """释放连接；之后不再调用其他方法。"""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class MediaSourcePlugin(PluginInfo, MediaSource):
    """网盘等媒体库来源插件的基类：一个插件对应一个网盘账号，按次创建实例。

    ``settings_fields`` 声明需要填写的项（地址、账号、令牌、Cookie 等），运行时从 ``self.settings`` 读取；
    登录得到的令牌等用 ``save_state`` 保存，下次从 ``self.state`` 读取。媒体库来源插件没有多份配置，
    不使用 ``credential_fields``。不跟随重定向，带着密钥的请求不会被转发到配置之外的地址；
    需要跟随时请在具体请求上自行处理。
    """

    remote = True

    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
        *,
        state: Mapping[str, Any] | None = None,
        on_state: StateCallback | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self._attach(settings, state, on_state)
        self.client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    @abstractmethod
    def test(self) -> str:
        """验证插件设置（账号能否访问），返回给用户看的一句话结果。失败时抛出 PluginCallError。"""

    def download(self, path: str) -> MediaInput:
        """在 GPU 机器上调用：影片的下载地址（需支持 Range 请求）和请求头。

        地址可以是临时直链；GPU 机器下载中断时会重新调用。默认与 :meth:`media_input` 相同。
        """
        return self.media_input(path)
