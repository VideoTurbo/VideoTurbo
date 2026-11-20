"""查找影片海报并生成控制台缩略图。

查找顺序（只读媒体库里的文件，不访问 nfo 里的网络地址）：

1. 影片同目录下带片名前缀的海报：``片名-poster.jpg``、``片名.poster.png``、``片名_poster.jpeg`` 等；
2. 与影片同名的图片：``片名.jpg``、``片名.jpeg``、``片名.png``、``片名.bmp``；
3. 不带前缀的 ``poster.*``，仅当目录里只有这一部影片时使用，避免合集目录张冠李戴；
4. ``片名.nfo``（目录只有这一部影片时也读 ``movie.nfo``）中指向本地文件的
   ``<art><poster>``、``<thumb aspect="poster">``、``<poster>``、``<thumb>``。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

from .media_sources.base import MediaSource, SourceEntry
from .media_sources.local import LocalSource

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp")
THUMBNAIL_WIDTH = 240
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _directory_entries(source: MediaSource, directory: Path) -> list[SourceEntry]:
    try:
        return [entry for entry in source.list_dir(str(directory)) if not entry.name.startswith(".")]
    except OSError:
        return []


def _only_video(video: Path, entries: list[SourceEntry], video_extensions: set[str]) -> bool:
    return not any(
        entry.name != video.name
        and os.path.splitext(entry.name)[1].lower() in video_extensions
        and not entry.is_dir
        for entry in entries
    )


def _nfo_candidates(text: str) -> list[str]:
    """按优先级返回 nfo 中的海报路径；XML 不规范时退回正则提取。"""
    values: list[str] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        root = None
    if root is not None:
        for xpath in ("./art/poster", "./thumb[@aspect='poster']", "./poster", "./thumb", ".//thumb"):
            for node in root.findall(xpath):
                if node.text and node.text.strip():
                    values.append(node.text.strip())
    else:
        for tag in ("poster", "thumb"):
            values.extend(
                match.strip()
                for match in re.findall(rf"<{tag}\b[^>]*>([^<]+)</{tag}>", text, re.IGNORECASE)
                if match.strip()
            )
    return list(dict.fromkeys(values))


def _resolve_nfo_path(source: MediaSource, value: str, nfo: Path) -> Path | None:
    if _URL.match(value) and not value.lower().startswith("file://"):
        return None
    if value.lower().startswith("file://"):
        value = value[7:]
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = nfo.parent / candidate
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    try:
        return candidate if source.is_file(str(candidate)) else None
    except OSError:
        return None


def find_poster(
    video: Path, video_extensions: Iterable[str], source: MediaSource | None = None
) -> Path | None:
    """按模块说明的顺序查找海报；source 默认是本地文件夹。"""
    source = source or LocalSource()
    extensions = {"." + value.lower().lstrip(".") for value in video_extensions}
    directory = video.parent
    entries = _directory_entries(source, directory)
    stem = video.stem.lower()
    by_name = {entry.name.lower(): entry for entry in entries}
    only_video = _only_video(video, entries, extensions)

    prefixed: list[str] = []
    for entry in entries:
        name = entry.name.lower()
        base, ext = os.path.splitext(name)
        if ext not in IMAGE_EXTENSIONS or not base.startswith(stem):
            continue
        rest = base[len(stem) :]
        if rest in {"-poster", ".poster", "_poster", " poster", " - poster"} and not entry.is_dir:
            prefixed.append(entry.name)
    if prefixed:
        return directory / min(
            prefixed, key=lambda n: IMAGE_EXTENSIONS.index(os.path.splitext(n)[1].lower())
        )

    for ext in IMAGE_EXTENSIONS:
        entry = by_name.get(stem + ext)
        if entry is not None and not entry.is_dir:
            return directory / entry.name

    if only_video:
        for ext in IMAGE_EXTENSIONS:
            entry = by_name.get("poster" + ext)
            if entry is not None and not entry.is_dir:
                return directory / entry.name

    nfo_names = [f"{stem}.nfo"] + (["movie.nfo"] if only_video else [])
    for nfo_name in nfo_names:
        entry = by_name.get(nfo_name)
        if entry is None or entry.is_dir:
            continue
        nfo = directory / entry.name
        try:
            text = source.read_bytes(str(nfo), limit=1024 * 1024).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for value in _nfo_candidates(text):
            resolved = _resolve_nfo_path(source, value, nfo)
            if resolved is not None:
                return resolved
    return None


def thumbnail_name(media_id: int, poster: Path, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha1(f"{poster}\0{size}\0{mtime_ns}".encode()).hexdigest()[:16]
    return f"{media_id}-{digest}.jpg"


def make_thumbnail(poster: Path, target: Path, width: int = THUMBNAIL_WIDTH) -> bool:
    """用 FFmpeg 生成缩略图；失败返回 False，由调用方回退到原图。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.tmp.jpg")
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(poster),
                "-vf", f"scale='min({width},iw)':-2", "-frames:v", "1", "-q:v", "4", str(temporary),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        temporary.unlink(missing_ok=True)
        return False
    if completed.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        return False
    os.replace(temporary, target)
    return True
