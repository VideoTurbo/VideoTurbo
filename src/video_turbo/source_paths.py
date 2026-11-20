"""来源内路径的统一写法：一律用 / 分隔的字符串。

数据库、接口和各来源之间传递的影片、字幕、海报路径都是这种字符串，按前缀判断"在不在某个媒体库里"。
- Linux / macOS 与网盘来源：以 / 开头，例如 ``/media/library_1/a.mkv``。
- Windows 本地文件夹：盘符或网络共享开头，例如 ``D:/影片/a.mkv``、``//nas/share/a.mkv``。
  Windows 的文件接口同样接受 / 分隔，所以这些字符串可以直接打开文件。

代码里常把路径包成 ``Path`` 取文件名、上级目录；转回字符串时必须用 :func:`text`，不能用 ``str()``：
Windows 上 ``str(Path)`` 会换成 \\ 分隔，与数据库里的写法对不上。Linux / macOS 上两者相同。
"""

from __future__ import annotations

import os
import posixpath
import re
import string
from pathlib import PurePath

from .i18n import msg

WINDOWS = os.name == "nt"
_DRIVE = re.compile(r"^([A-Za-z]):(?:/(.*))?$")


def text(path: str | os.PathLike[str]) -> str:
    """路径转成来源内的写法（/ 分隔）。"""
    if isinstance(path, PurePath):
        return path.as_posix()
    value = os.fspath(path)
    return value.replace("\\", "/") if WINDOWS else value


def _check_parts(parts: list[str], original: str) -> list[str]:
    if any(part in {".", ".."} for part in parts):
        raise ValueError(msg("config.libraryPathDots", text=original))
    return parts


def _split_windows(value: str, original: str) -> tuple[str, list[str]]:
    """Windows 本地路径拆成根（``D:/`` 或 ``//服务器/共享``）和其余各级。"""
    if value.startswith("//"):
        parts = [part for part in value[2:].split("/") if part]
        if len(parts) < 2:
            raise ValueError(msg("config.libraryPathAbsolute", text=original))
        _check_parts(parts[:2], original)
        return f"//{parts[0]}/{parts[1]}", _check_parts(parts[2:], original)
    match = _DRIVE.match(value)
    if match is None:
        raise ValueError(msg("config.libraryPathAbsolute", text=original))
    rest = [part for part in (match.group(2) or "").split("/") if part]
    return f"{match.group(1).upper()}:/", _check_parts(rest, original)


def normalize(value: object, *, local: bool, windows: bool | None = None) -> str:
    """规范来源内的绝对路径：统一分隔符、去掉多余的 /；不是绝对路径或含 . / .. 时抛出 ValueError。

    local：本地文件夹来源。只有 Windows 上的本地来源使用盘符写法，网盘来源始终以 / 开头。
    """
    windows = WINDOWS if windows is None else windows
    original = str(value or "").strip()
    cleaned = original.replace("\\", "/")
    if local and windows:
        root, parts = _split_windows(cleaned, original)
        if not parts:
            return root
        return root.rstrip("/") + "/" + "/".join(parts)
    if not cleaned.startswith("/"):
        raise ValueError(msg("config.libraryPathAbsolute", text=original))
    parts = _check_parts([part for part in cleaned.split("/") if part], original)
    return "/" + "/".join(parts)


def is_root(path: str, *, local: bool, windows: bool | None = None) -> bool:
    if local and (WINDOWS if windows is None else windows):
        return not _split_windows(path.replace("\\", "/"), path)[1]
    return path == "/"


def parent(path: str, *, local: bool, windows: bool | None = None) -> str:
    """上级文件夹；根目录的上级是它自己。"""
    windows = WINDOWS if windows is None else windows
    if is_root(path, local=local, windows=windows):
        return path
    result = posixpath.dirname(path)
    # posixpath 把 D:/影片 的上级算成 D:，补回根目录的 /。
    if local and windows and re.fullmatch(r"[A-Za-z]:", result):
        return result + "/"
    return result


def contains(parent_path: str, child: str) -> bool:
    """child 在 parent_path 里面（不含相等）。根目录 ``/``、``D:/`` 末尾已有 /，不能再加一个。"""
    prefix = parent_path if parent_path.endswith("/") else parent_path + "/"
    return child != parent_path and child.startswith(prefix)


def overlap(first: str, second: str) -> bool:
    """两个路径相同或互相包含。"""
    return first == second or contains(first, second) or contains(second, first)


def windows_drives() -> list[str]:
    """本机存在的盘符根目录，例如 ``["C:/", "D:/"]``。"""
    return [f"{letter}:/" for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]
