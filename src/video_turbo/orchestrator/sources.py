"""主程序按媒体库打开来源：本地文件夹或网盘插件（媒体库来源插件，账号等在插件设置里填写）。

数据库判断已发布字幕、影片是否存在时也经过这里（实现 ``database.MediaFiles``）。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import LOCAL_SOURCE, AppConfig, MediaLibraryConfig
from ..database import Database
from ..i18n import msg
from ..media_sources.base import MediaSource, MediaSourcePlugin
from ..media_sources.local import LocalSource
from ..plugins import registry
from ..plugins.base import PluginCallError, describe_error, icon_uri
from ..plugins.settings import PluginSettings, data_key

logger = logging.getLogger(__name__)

SOURCE_KIND = "media_source"
LOCAL_LABEL = msg("sources.local")


class SourceUnavailable(RuntimeError):
    """媒体库的来源暂时不能使用；message 直接展示给用户。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MediaSources:
    def __init__(self, database: Database, config: AppConfig) -> None:
        self.database = database
        self.config = config
        self.local = LocalSource()
        self.settings = PluginSettings(database)

    def plugin(self, source_id: str) -> type[MediaSourcePlugin] | None:
        return registry.get(SOURCE_KIND, source_id)  # type: ignore[return-value]

    def problem(self, plugin: type[MediaSourcePlugin]) -> str | None:
        """插件不能使用的原因：必填设置还没填写。"""
        missing = self.settings.missing(plugin)
        if missing:
            return msg("sources.needsSettings", label=plugin.label, fields=list(missing))
        return None

    def _plugin(self, source_id: str) -> type[MediaSourcePlugin]:
        plugin = self.plugin(source_id)
        if plugin is None:
            raise SourceUnavailable(msg("sources.pluginMissing", source_id=source_id))
        if problem := self.problem(plugin):
            raise SourceUnavailable(problem)
        return plugin

    def open(self, source_id: str) -> MediaSource:
        """打开来源，用完调用 close（或用 with）。来源不可用时抛出 SourceUnavailable。"""
        if source_id == LOCAL_SOURCE:
            return self.local
        plugin = self._plugin(source_id)
        return plugin(**self.settings.bind(plugin))

    def describe(self) -> list[dict[str, Any]]:
        """可以作为媒体库来源的网盘插件，以及不能使用的原因。"""
        return [
            {
                "id": plugin.id,
                "label": plugin.label,
                "description": plugin.description,
                "icon": icon_uri(plugin.icon),
                "has_settings": bool(plugin.settings_fields),
                "problem": self.problem(plugin),  # type: ignore[arg-type]
            }
            for plugin in registry.providers(SOURCE_KIND).values()
        ]

    def library(self, library_id: str) -> MediaLibraryConfig:
        library = self.config.library(library_id)
        if library is None:
            raise SourceUnavailable(msg("api.worker.libraryDeleted"))
        return library

    def for_library(self, library_id: str) -> MediaSource:
        return self.open(self.library(library_id).source)

    def is_remote(self, library_id: str | None) -> bool:
        library = self.config.library(library_id)
        return bool(library and not library.is_local)

    def label(self, source_id: str) -> str:
        if source_id == LOCAL_SOURCE:
            return LOCAL_LABEL
        plugin = self.plugin(source_id)
        return plugin.label if plugin else msg("sources.pluginMissingLabel", source_id=source_id)

    # ---- database.MediaFiles ----

    def exists(self, library_id: str, path: str) -> bool:
        library = self.config.library(library_id)
        source_id = library.source if library else LOCAL_SOURCE
        try:
            with self.open(source_id) as source:
                return source.is_file(path)
        except SourceUnavailable:
            return False
        except Exception as exc:  # noqa: BLE001 - 网盘出错时按不存在处理，不中断调用方
            logger.warning("检查文件 %s 失败：%s", path, describe_error(exc))
            return False

    def rename(self, library_id: str, source_path: str, target_path: str) -> None:
        library = self.config.library(library_id)
        try:
            with self.open(library.source if library else LOCAL_SOURCE) as source:
                source.rename(source_path, target_path)
        except SourceUnavailable as exc:
            raise OSError(exc.message) from exc
        except PluginCallError as exc:
            raise OSError(exc.message) from exc

    # ---- GPU 机器直接取片 ----

    def transfer(self, library_id: str, path: str) -> dict[str, Any]:
        """网盘影片交给 GPU 机器直接下载所需的信息：插件 id、源码指纹、插件设置与状态和路径。"""
        library = self.library(library_id)
        if library.is_local:
            raise SourceUnavailable(msg("sources.localViaMain"))
        plugin = self._plugin(library.source)
        installed = self.database.installed_plugins().get(data_key(plugin)) or {}
        if not installed.get("source") or not installed.get("sha256"):
            raise SourceUnavailable(
                msg("sources.notInstalledInDashboard", label=plugin.label)
            )
        return {
            "plugin_id": plugin.id,
            "plugin_sha256": installed["sha256"],
            "settings": self.settings.settings(plugin),
            "state": self.settings.state(data_key(plugin)),
            "path": path,
        }
