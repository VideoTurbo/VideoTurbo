"""内置来源：主程序容器内的文件夹。"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..i18n import msg
from .base import SMALL_FILE_LIMIT, MediaInput, MediaSource, SourceEntry, has_extension


def _entry(path: str, stat: os.stat_result, is_dir: bool) -> SourceEntry:
    return SourceEntry(
        path=path,
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
                        entries.append(_entry(item.path, item.stat(), is_dir))
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
        # rglob 连续产出同一目录的文件，扫描时按目录缓存字幕列表。
        for path in base.rglob("*"):
            if path.name.startswith(".") or not has_extension(path.name, extensions):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                yield _entry(str(path), stat, False)
