"""控制台海报列：数据库缓存海报位置，请求图片时校验并按需生成缩略图。

- 列表接口只读数据库缓存，不扫描目录，由 :func:`poster_url` 给出图片地址。
- 图片请求时经媒体库来源校验缓存：影片路径变了（改名、移动、影片修复改名）或海报文件大小/修改时间变了，
  就重新查找；缩略图文件名包含海报路径与修改时间，旧缩略图随之清理。
- 没找到海报也会缓存，1 天后或下次扫描后再查找。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..database import Database
from ..media_sources.base import MediaSource
from ..posters import find_poster, make_thumbnail, thumbnail_name
from ..source_paths import text as path_text
from .sources import MediaSources, SourceUnavailable

logger = logging.getLogger(__name__)

MISSING_RETRY = timedelta(days=1)


def _fresh(checked_at: str | None, now: datetime) -> bool:
    if not checked_at:
        return False
    try:
        return now - datetime.fromisoformat(checked_at) < MISSING_RETRY
    except ValueError:
        return False


def poster_url(row: dict[str, Any], now: datetime | None = None) -> str | None:
    """列表行的海报地址；None 表示已确认没有海报，控制台不发请求。"""
    media_id = row.get("media_id")
    path = row.get("media_path") or row.get("path")
    if media_id is None or not path:
        return None
    base = f"/api/media/{media_id}/poster"
    if row.get("poster_media_path") == path:
        if row.get("poster_path"):
            return f"{base}?v={row.get('poster_mtime_ns') or 0}"
        if _fresh(row.get("poster_checked_at"), now or datetime.now(UTC)):
            return None
    return base


def attach_poster_urls(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    for row in rows:
        row["poster_url"] = poster_url(row, now)
        for key in ("poster_path", "poster_mtime_ns", "poster_media_path", "poster_checked_at"):
            row.pop(key, None)
    return rows


class PosterService:
    def __init__(
        self, config: AppConfig, database: Database, cache_dir: Path, sources: MediaSources
    ) -> None:
        self.config = config
        self.database = database
        self.cache_dir = cache_dir
        self.sources = sources
        # 一页最多 100 张图同时请求，限制 FFmpeg 并发，避免拖慢 NAS。
        self.semaphore = asyncio.Semaphore(3)
        # 同一影片可能同时出现在任务列表和影片列表，串行处理避免重复查找和生成。
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, media_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(media_id, threading.Lock())

    async def thumbnail(self, media_id: int) -> Path | None:
        async with self.semaphore:
            return await asyncio.to_thread(self._thumbnail_sync, media_id)

    def _resolve(self, media: dict[str, Any], source: MediaSource) -> tuple[Path, int, int] | None:
        path = str(media["path"])
        cached = media.get("poster_path")
        if media.get("poster_media_path") == path:
            if cached:
                entry = source.stat(cached)
                if (
                    entry is not None
                    and entry.size == media.get("poster_size")
                    and entry.mtime_ns == media.get("poster_mtime_ns")
                ):
                    return Path(cached), entry.size, entry.mtime_ns
            elif _fresh(media.get("poster_checked_at"), datetime.now(UTC)):
                return None
        found = find_poster(Path(path), self.config.scan.video_extensions, source)
        entry = source.stat(path_text(found)) if found is not None else None
        self.database.set_poster_cache(
            media["id"],
            media_path=path,
            poster=found if entry else None,
            size=entry.size if entry else None,
            mtime_ns=entry.mtime_ns if entry else None,
        )
        if found is None or entry is None:
            self._remove_thumbnails(media["id"])
            return None
        return found, entry.size, entry.mtime_ns

    def _remove_thumbnails(self, media_id: int, keep: str | None = None) -> None:
        if not self.cache_dir.is_dir():
            return
        for old in self.cache_dir.glob(f"{media_id}-*.jpg"):
            if old.name != keep:
                old.unlink(missing_ok=True)

    def _thumbnail_sync(self, media_id: int) -> Path | None:
        with self._lock(media_id):
            return self._thumbnail_locked(media_id)

    def _thumbnail_locked(self, media_id: int) -> Path | None:
        media = self.database.get_media(media_id)
        if media is None:
            return None
        try:
            source = self.sources.for_library(str(media["library_id"]))
        except SourceUnavailable:
            return None
        with source:
            try:
                resolved = self._resolve(media, source)
            except OSError:
                logger.warning("查找 %s 的海报失败。", media["path"], exc_info=True)
                return None
            if resolved is None:
                return None
            poster, size, mtime_ns = resolved
            name = thumbnail_name(media_id, poster, size, mtime_ns)
            target = self.cache_dir / name
            if target.is_file():
                return target
            if source.remote:
                return self._remote_thumbnail(source, poster, target, media_id)
        if make_thumbnail(poster, target):
            self._remove_thumbnails(media_id, keep=name)
            return target
        logger.warning("无法为 %s 生成缩略图，改为返回原图。", poster)
        return poster

    def _remote_thumbnail(
        self, source: MediaSource, poster: Path, target: Path, media_id: int
    ) -> Path | None:
        """网盘海报下载到缓存目录再生成缩略图；生成失败时不返回原图（原图不在本机）。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        original = self.cache_dir / f"{target.stem}.original{poster.suffix.lower()}"
        try:
            original.write_bytes(source.read_bytes(path_text(poster)))
            if not make_thumbnail(original, target):
                logger.warning("无法为 %s 生成缩略图。", poster)
                return None
        except (OSError, ValueError):
            logger.warning("下载海报 %s 失败。", poster, exc_info=True)
            return None
        finally:
            original.unlink(missing_ok=True)
        self._remove_thumbnails(media_id, keep=target.name)
        return target
