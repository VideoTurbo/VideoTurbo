"""主程序按媒体库打开来源：本地文件夹、网盘插件（媒体库来源插件，账号等在插件设置里填写），
或网络存储（WebDAV、Samba 等可以连多台的来源，每台是一份配置，媒体库的 ``source`` 存配置 id）。

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
from ..plugins.connections import ID_PREFIX, Connections
from ..plugins.settings import PluginSettings, data_key

logger = logging.getLogger(__name__)

SOURCE_KIND = "media_source"
LOCAL_LABEL = msg("sources.local")
CONNECTION_PREFIX = ID_PREFIX[SOURCE_KIND]


def is_connection(source_id: str) -> bool:
    """来源是不是网络存储的一份配置（否则是本地文件夹或单设置的网盘插件 id）。"""
    return source_id.startswith(CONNECTION_PREFIX)


def has_connections(plugin: type) -> bool:
    """插件按“每台服务器一份配置”使用（声明了 credential_fields），在“网络存储”里添加。"""
    return bool(getattr(plugin, "credential_fields", ()))


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
        self.connections = Connections(database, SOURCE_KIND)

    def _connection(self, source_id: str) -> dict[str, Any] | None:
        return self.connections.records().get(source_id)

    def plugin(self, source_id: str) -> type[MediaSourcePlugin] | None:
        """来源对应的插件类；网络存储的配置按它的类型找。"""
        if is_connection(source_id):
            record = self._connection(source_id)
            return self.connections.plugin(record) if record else None  # type: ignore[return-value]
        return registry.get(SOURCE_KIND, source_id)  # type: ignore[return-value]

    def state_key(self, source_id: str) -> str:
        """插件状态的保存位置：网络存储每份配置单独一份，其他按插件。"""
        if is_connection(source_id):
            return source_id
        plugin = self.plugin(source_id)
        return data_key(plugin) if plugin else source_id

    def bind(self, source_id: str) -> tuple[type[MediaSourcePlugin], dict[str, Any]]:
        """可用的插件类和创建实例的参数（设置、状态、保存状态的回调）。不可用时抛出 SourceUnavailable。

        网络存储的设置是插件设置叠加这份配置的值。
        """
        if not is_connection(source_id):
            plugin = self._plugin(source_id)
            return plugin, self.settings.bind(plugin)
        record = self._connection(source_id)
        if record is None:
            raise SourceUnavailable(msg("sources.connectionMissing"))
        plugin = self.connections.plugin(record)  # type: ignore[assignment]
        if plugin is None:
            raise SourceUnavailable(msg("sources.pluginMissing", source_id=record.get("type", "")))
        if not record.get("enabled", True):
            raise SourceUnavailable(msg("sources.connectionDisabled", name=record.get("name", "")))
        key = self.state_key(source_id)
        return plugin, {  # type: ignore[return-value]
            "settings": {**self.settings.settings(plugin), **(record.get("values") or {})},
            "state": self.settings.state(key),
            "on_state": lambda changes: self.settings.save_state(key, changes),
        }

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
        plugin, kwargs = self.bind(source_id)
        return plugin(**kwargs)

    def describe(self) -> list[dict[str, Any]]:
        """可以作为媒体库来源的网盘插件和已启用的网络存储，以及不能使用的原因。"""
        result: list[dict[str, Any]] = [
            {
                "id": plugin.id,
                "kind": "plugin",
                "label": plugin.label,
                "description": plugin.description,
                "icon": icon_uri(plugin.icon),
                "has_settings": bool(plugin.settings_fields),
                "problem": self.problem(plugin),  # type: ignore[arg-type]
            }
            for plugin in registry.providers(SOURCE_KIND).values()
            if not has_connections(plugin)
        ]
        for connection_id, record, plugin in self.connections.active():
            result.append(
                {
                    "id": connection_id,
                    "kind": "connection",
                    "label": record.get("name", ""),
                    "description": " · ".join(
                        part for part in (plugin.label, plugin.summary(record.get("values") or {})) if part
                    ),
                    "icon": icon_uri(plugin.icon),
                    "has_settings": False,
                    "problem": None,
                }
            )
        return result

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
        if is_connection(source_id):
            record = self._connection(source_id)
            return str(record.get("name", "")) if record else msg("sources.connectionMissingLabel")
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
        """网盘影片交给 GPU 机器直接下载所需的信息：插件 id、源码指纹、插件设置与状态和路径。

        内置来源（WebDAV、Samba）由 GPU 机器用自带的代码加载（``builtin``），不下载源码；
        其他插件按源码指纹从主程序下载。
        """
        library = self.library(library_id)
        if library.is_local:
            raise SourceUnavailable(msg("sources.localViaMain"))
        plugin, kwargs = self.bind(library.source)
        builtin = registry.is_builtin(SOURCE_KIND, plugin.id)
        sha256 = ""
        if not builtin:
            installed = self.database.installed_plugins().get(data_key(plugin)) or {}
            if not installed.get("source") or not installed.get("sha256"):
                raise SourceUnavailable(
                    msg("sources.notInstalledInDashboard", label=plugin.label)
                )
            sha256 = installed["sha256"]
        return {
            "plugin_id": plugin.id,
            "plugin_sha256": sha256,
            "builtin": builtin,
            "settings": kwargs["settings"],
            "state": kwargs["state"],
            "path": path,
        }
