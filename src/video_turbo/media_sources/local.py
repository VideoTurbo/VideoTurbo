"""内置来源：主程序所在机器（Docker 里是容器内）的文件夹。路径写法见 source_paths。"""

from __future__ import annotations

import os
import stat as stat_flags
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..i18n import msg
from ..source_paths import text
from .base import SMALL_FILE_LIMIT, MediaInput, MediaSource, SourceEntry, has_extension

# Windows 的隐藏和系统项（$RECYCLE.BIN、System Volume Information 等）与 . 开头的项一样不列出。
_HIDDEN_ATTRIBUTES = getattr(stat_flags, "FILE_ATTRIBUTE_HIDDEN", 0) | getattr(stat_flags, "FILE_ATTRIBUTE_SYSTEM", 0)


def _hidden(name: str, stat: os.stat_result) -> bool:
    return name.startswith(".") or bool(getattr(stat, "st_file_attributes", 0) & _HIDDEN_ATTRIBUTES)


def _entry(path: str | Path, stat: os.stat_result, is_dir: bool) -> SourceEntry:
    return SourceEntry(
        path=text(path),
        is_dir=is_dir,
        size=0 if is_dir else stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        # 同一文件系统内改名或移动文件时，设备号和 inode 保持不变。
        file_key=None if is_dir else f"{stat.st_dev}:{stat.st_ino}",
    )


class LocalSource(MediaSource):
    remote = False

    def list_dir(self, path: str) -> list[SourceEntry]:
        entries: list[SourceEntry] = []
        try:
            with os.scandir(path) as iterator:
                for item in iterator:
                    if item.name.startswith("."):
                        continue
                    try:
                        is_dir = item.is_dir()
                        if not is_dir and not item.is_file():
                            continue
                        stat = item.stat()
                        if _hidden(item.name, stat):
                            continue
                        entries.append(_entry(item.path, stat, is_dir))
                    except OSError:
                        continue
        except NotADirectoryError as exc:
            raise FileNotFoundError(path) from exc
        return entries

    def stat(self, path: str) -> SourceEntry | None:
        try:
            stat = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return _entry(path, stat, os.path.isdir(path))

    def read_bytes(self, path: str, limit: int = SMALL_FILE_LIMIT) -> bytes:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError(msg("sources.local.tooLarge", name=os.path.basename(path)))
        return data

    def write_bytes(self, path: str, data: bytes, *, overwrite: bool) -> None:
        target = Path(path)
        if target.exists() and not overwrite:
            raise FileExistsError(msg("sources.local.exists", target=target))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)

    def rename(self, source: str, target: str) -> None:
        if os.path.exists(target):
            raise FileExistsError(target)
        os.rename(source, target)

    def media_input(self, path: str) -> MediaInput:
        return MediaInput(path)

    def walk(self, root: str, extensions: Iterable[str]) -> Iterator[SourceEntry]:
        base = Path(root)
        if not base.exists():
            return
        hidden_dirs: dict[Path, bool] = {}

        def in_hidden_dir(path: Path) -> bool:
            # 只有 Windows 有隐藏属性：回收站等系统文件夹里的影片不扫描。
            if not _HIDDEN_ATTRIBUTES:
                return False
            for directory in path.relative_to(base).parents:
                if directory == Path("."):
                    continue
                if directory not in hidden_dirs:
                    try:
                        hidden_dirs[directory] = _hidden(directory.name, (base / directory).stat())
                    except OSError:
                        hidden_dirs[directory] = True
                if hidden_dirs[directory]:
                    return True
            return False

        # rglob 连续产出同一目录的文件，扫描时按目录缓存字幕列表。
        for path in base.rglob("*"):
            if path.name.startswith(".") or not has_extension(path.name, extensions):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file() and not _hidden(path.name, stat) and not in_hidden_dir(path):
                yield _entry(path, stat, False)
