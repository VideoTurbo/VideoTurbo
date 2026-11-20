"""媒体库接口：列出、添加、修改、删除媒体库，以及添加时浏览来源里的文件夹。

每个媒体库的修改立即保存并热更新。来源（本地文件夹或网盘插件）和文件夹在创建后不能修改：
已有的影片记录都按这个文件夹登记，换文件夹请新建媒体库。网盘插件的账号等在插件设置里填写，见 ``plugins_api``。
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import languages, source_paths
from ..config import LOCAL_SOURCE, AppConfig
from ..config_store import ConfigError, ConfigStore
from ..deployment import is_desktop
from ..i18n import msg
from ..media_sources.base import MediaSource
from ..plugins.base import describe_error, icon_uri
from .config_api import apply_runtime_config
from .context import app_state
from .sources import LOCAL_LABEL, MediaSources, SourceUnavailable

router = APIRouter()

# 本地来源可以浏览的根目录：只在这些目录里选媒体库，不暴露主程序所在机器的其他文件。
# Docker 里是容器内的 /media；Windows 上未设置时是本机全部盘符。桌面版由外壳设置 VIDEO_TURBO_MEDIA_ROOTS。
DEFAULT_MEDIA_ROOTS = ("/media",)


class LibraryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    source_language: str = languages.DEFAULT_SOURCE_LANGUAGE
    target_language: str = languages.DEFAULT_TARGET_LANGUAGE


class LibraryCreate(LibraryUpdate):
    source: str = Field(default=LOCAL_SOURCE, min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=1000)


def _sources(request: Request) -> MediaSources:
    return app_state(request).orchestrator.sources


def _store(request: Request) -> ConfigStore:
    return app_state(request).config_store


def media_roots() -> list[str]:
    """VIDEO_TURBO_MEDIA_ROOTS 按系统的路径分隔符（Linux / macOS 是 :，Windows 是 ;）分开。"""
    configured = os.getenv("VIDEO_TURBO_MEDIA_ROOTS")
    if configured:
        items = configured.split(os.pathsep)
    elif source_paths.WINDOWS:
        items = source_paths.windows_drives()
    else:
        items = list(DEFAULT_MEDIA_ROOTS)
    roots: list[str] = []
    for item in items:
        if not item.strip():
            continue
        try:
            roots.append(source_paths.normalize(item, local=True))
        except ValueError:
            continue
    return list(dict.fromkeys(roots))


def _inside_roots(path: str, roots: list[str]) -> bool:
    return any(path == root or source_paths.contains(root, path) for root in roots)


def _request_path(source_id: str, value: str) -> str:
    """请求里的文件夹路径换成来源内的写法。Linux / macOS 与网盘允许省略开头的 /。

    先按字面消去 . 和 ..：越出媒体目录的路径随后按"不在媒体目录里"拒绝。
    """
    local = source_id == LOCAL_SOURCE
    value = value.strip().replace("\\", "/")
    if not (local and source_paths.WINDOWS):
        value = "/" + value.lstrip("/")
    value = posixpath.normpath(value)
    try:
        return source_paths.normalize(value, local=local)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _source_info(sources: MediaSources, source_id: str) -> dict[str, Any]:
    """来源的显示信息：名称、插件类型和图标，以及不可用的原因。"""
    if source_id == LOCAL_SOURCE:
        return {"source_label": LOCAL_LABEL, "source_type": LOCAL_LABEL, "source_icon": "", "problem": None}
    plugin = sources.plugin(source_id)
    problem = None
    try:
        sources.open(source_id).close()
    except SourceUnavailable as exc:
        problem = exc.message
    return {
        "source_label": sources.label(source_id),
        "source_type": plugin.label if plugin else source_id,
        "source_icon": icon_uri(plugin.icon) if plugin else "",
        "problem": problem,
    }


def _describe(request: Request) -> list[dict[str, Any]]:
    config: AppConfig = app_state(request).config
    sources = _sources(request)
    stats = app_state(request).database.library_stats()
    infos: dict[str, dict[str, Any]] = {}
    result = []
    for library in config.media_libraries:
        if library.source not in infos:
            infos[library.source] = _source_info(sources, library.source)
        stat = stats.get(library.id) or {}
        result.append(
            {
                **library.model_dump(mode="json"),
                **infos[library.source],
                "source_language_label": languages.get(library.source_language).label,
                "target_language_label": languages.get(library.target_language).label,
                "media_count": int(stat.get("media_count") or 0),
                "scanned_at": stat.get("scanned_at"),
            }
        )
    return result


@router.get("/api/libraries")
async def list_libraries(request: Request) -> list[dict]:
    return await asyncio.to_thread(_describe, request)


@router.get("/api/libraries/sources")
def list_sources(request: Request) -> list[dict]:
    """添加媒体库时可选的网盘插件（本地文件夹总是可选，不在列表中）。"""
    return _sources(request).describe()


async def _save(request: Request, libraries: list[dict[str, Any]]) -> None:
    async with app_state(request).config_lock:
        try:
            config = await asyncio.to_thread(
                _store(request).update_section, "media_libraries", libraries
            )
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        apply_runtime_config(request.app, config)


def _saved_libraries(request: Request) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in app_state(request).config.media_libraries]


def _check_languages(payload: LibraryUpdate) -> None:
    try:
        languages.validate_pair(payload.source_language, payload.target_language)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _check_folder(sources: MediaSources, source_id: str, path: str) -> None:
    if source_id == LOCAL_SOURCE and not _inside_roots(path, media_roots()):
        raise HTTPException(
            status_code=422,
            detail=msg("api.libraries.localRoots", roots=list(media_roots())),
        )
    try:
        with sources.open(source_id) as source:
            entry = source.stat(path)
    except SourceUnavailable as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=msg("api.libraries.readFailed", error=describe_error(exc))) from exc
    if entry is None or not entry.is_dir:
        detail = (
            msg("api.libraries.localNotFound.desktop" if is_desktop() else "api.libraries.localNotFound", path=path)
            if source_id == LOCAL_SOURCE
            else msg("api.libraries.driveNotFound", path=path)
        )
        raise HTTPException(status_code=422, detail=detail)


@router.post("/api/libraries")
async def create_library(request: Request, payload: LibraryCreate) -> dict:
    _check_languages(payload)
    sources = _sources(request)
    path = _request_path(payload.source, payload.path)
    await asyncio.to_thread(_check_folder, sources, payload.source, path)
    library_id = f"lib-{uuid.uuid4().hex[:8]}"
    libraries = _saved_libraries(request)
    libraries.append({**payload.model_dump(), "path": path, "id": library_id})
    await _save(request, libraries)
    return {"id": library_id, "saved": True}


def _find(libraries: list[dict[str, Any]], library_id: str) -> dict[str, Any]:
    library = next((item for item in libraries if item["id"] == library_id), None)
    if library is None:
        raise HTTPException(status_code=404, detail=msg("api.libraries.notFound"))
    return library


@router.put("/api/libraries/{library_id}")
async def update_library(request: Request, library_id: str, payload: LibraryUpdate) -> dict:
    _check_languages(payload)
    libraries = _saved_libraries(request)
    _find(libraries, library_id).update(payload.model_dump())
    await _save(request, libraries)
    return {"saved": True}


@router.delete("/api/libraries/{library_id}")
async def delete_library(request: Request, library_id: str) -> dict:
    database = app_state(request).database
    libraries = _saved_libraries(request)
    _find(libraries, library_id)
    busy = await asyncio.to_thread(database.library_busy_reason, library_id)
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    await _save(request, [item for item in libraries if item["id"] != library_id])
    removed = await asyncio.to_thread(database.delete_library_media, library_id)
    return {"removed": True, "media_removed": removed}


def _browse(sources: MediaSources, config: AppConfig, source_id: str, path: str | None) -> dict:
    used = [library for library in config.media_libraries if library.source == source_id]
    roots = media_roots() if source_id == LOCAL_SOURCE else ["/"]
    if path is None:
        # 本地只有一个根目录时直接进入；有多个时先列出根目录。
        if len(roots) == 1:
            path = roots[0]
        else:
            return {
                "path": None,
                "parent": None,
                "root": None,
                "current": None,
                "folders": [_folder(root, used) for root in roots],
            }
    path = _request_path(source_id, path)
    if source_id == LOCAL_SOURCE and not _inside_roots(path, roots):
        raise HTTPException(status_code=403, detail=msg("api.libraries.browseRoots"))
    try:
        with sources.open(source_id) as source:
            folders = _list_folders(source, path)
    except SourceUnavailable as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=msg("api.libraries.folderNotFound", path=path)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=msg("api.libraries.readFailed", error=describe_error(exc))) from exc
    if path in roots:
        parent = None if len(roots) == 1 else ""
    else:
        parent = source_paths.parent(path, local=source_id == LOCAL_SOURCE)
    root = next((item for item in roots if _inside_roots(path, [item])), "/")
    return {
        "path": path,
        "parent": parent,
        # 当前所在的根目录（本地是媒体目录，网盘是 /），面包屑从这里开始。
        "root": root,
        # 当前文件夹本身是否已被使用，控制台据此决定能不能选它。
        "current": _folder(path, used),
        "folders": [_folder(item, used) for item in folders],
    }


def _list_folders(source: MediaSource, path: str) -> list[str]:
    entries = source.list_dir(path)
    return sorted((entry.path for entry in entries if entry.is_dir), key=str.lower)


def _folder(path: str, used: list[Any]) -> dict[str, Any]:
    exact = next((library.name for library in used if library.path == path), None)
    # 重叠分两种：在某个媒体库里面（子文件夹），或里面有媒体库（父文件夹）。两种都不能添加，
    # 否则同一部影片会被两个媒体库扫描；父文件夹多半只是路过，控制台不当作问题突出显示。
    inside = next((library.name for library in used if _contains(library.path, path)), None)
    contains = next((library.name for library in used if _contains(path, library.path)), None)
    return {
        "name": posixpath.basename(path) or path,
        "path": path,
        "library": exact,
        "inside": inside,
        "contains": contains,
    }


def _contains(parent: str, child: str) -> bool:
    return source_paths.contains(parent, child)


@router.get("/api/libraries/browse")
async def browse_folders(
    request: Request,
    source: str = Query(default=LOCAL_SOURCE, min_length=1, max_length=64),
    path: str | None = Query(default=None, max_length=1000),
) -> dict:
    """列出来源里某个文件夹的子文件夹。path 为空时从根目录开始；parent 为 "" 表示回到根目录列表。"""
    return await asyncio.to_thread(
        _browse, _sources(request), app_state(request).config, source, path or None
    )
