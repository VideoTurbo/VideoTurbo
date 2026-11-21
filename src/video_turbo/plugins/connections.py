"""媒体服务器、通知、网络存储（有 ``credential_fields`` 的媒体库来源插件，如 WebDAV、Samba）的配置：
每类可以添加多份，各自启用或停用。网络存储的每份配置是一个媒体库来源，媒体库的 ``source`` 存配置 id。

记录保存在 runtime_settings（``media_servers``、``notifiers``、``media_sources``）::

    {配置 id: {"type": 插件 id, "name": 名称, "enabled": bool, "values": {字段: 值}, "min_level": ...}}

values 中的密钥字段不回显；插件已卸载或加载失败的配置保留，标记为不可用，重新安装插件后恢复。
"""

from __future__ import annotations

import uuid
from typing import Any

from ..database import Database
from ..i18n import msg
from . import registry
from .base import PluginConfigError, PluginInfo, icon_uri, masked_values

ID_PREFIX = {"media_server": "ms-", "notifier": "nt-", "media_source": "src-"}
# 通知配置的默认最低级别：警告和错误。
DEFAULT_MIN_LEVEL = "warning"
MIN_LEVELS = ("info", "warning", "error")


class Connections:
    def __init__(self, database: Database, kind: str) -> None:
        if kind not in ID_PREFIX:
            raise ValueError(msg("plugins.connection.badKind", kind=kind))
        self.database = database
        self.kind = kind

    def records(self) -> dict[str, dict[str, Any]]:
        return self.database.plugin_connections(self.kind)

    def plugin(self, record: dict[str, Any]) -> type[PluginInfo] | None:
        return registry.get(self.kind, str(record.get("type", "")))  # type: ignore[return-value]

    def record(self, connection_id: str) -> dict[str, Any]:
        record = self.records().get(connection_id)
        if record is None:
            raise KeyError(connection_id)
        return record

    def describe(self) -> list[dict[str, Any]]:
        result = []
        for connection_id, record in self.records().items():
            plugin = self.plugin(record)
            values: dict[str, Any] = record.get("values") or {}
            fields = plugin.credential_fields if plugin else ()
            item: dict[str, Any] = {
                "id": connection_id,
                "type": record.get("type", ""),
                "type_label": plugin.label if plugin else record.get("type", ""),
                "type_icon": icon_uri(plugin.icon) if plugin else "",
                "name": record.get("name", ""),
                "enabled": bool(record.get("enabled", True)),
                "available": plugin is not None,
                "summary": plugin.summary(values) if plugin else "",
                "values": masked_values(fields, values),
            }
            if self.kind == "notifier":
                item["min_level"] = record.get("min_level", DEFAULT_MIN_LEVEL)
            result.append(item)
        return result

    def active(self) -> list[tuple[str, dict[str, Any], type[PluginInfo]]]:
        """已启用且插件可用的配置。"""
        return [
            (connection_id, record, plugin)
            for connection_id, record in self.records().items()
            if record.get("enabled", True) and (plugin := self.plugin(record)) is not None
        ]

    def _name(self, name: str, connection_id: str = "") -> str:
        name = name.strip()
        if not name:
            raise PluginConfigError(msg("api.compute.nameRequired"))
        if any(
            other_id != connection_id and record.get("name") == name
            for other_id, record in self.records().items()
        ):
            raise PluginConfigError(msg("api.compute.nameTaken"))
        return name

    def _options(self, min_level: str | None, saved: dict[str, Any]) -> dict[str, Any]:
        if self.kind != "notifier":
            return {}
        level = min_level or saved.get("min_level") or DEFAULT_MIN_LEVEL
        if level not in MIN_LEVELS:
            raise PluginConfigError(msg("plugins.connection.badLevel"))
        return {"min_level": level}

    def prepare_new(
        self,
        plugin_type: str,
        name: str,
        enabled: bool,
        values: dict[str, Any],
        min_level: str | None = None,
    ) -> tuple[type[PluginInfo], dict[str, Any]]:
        """校验新配置，返回插件类和待保存的记录（尚未保存）。"""
        plugin = registry.get(self.kind, plugin_type)
        if plugin is None:
            raise KeyError(plugin_type)
        return plugin, {  # type: ignore[return-value]
            "type": plugin_type,
            "name": self._name(name),
            "enabled": enabled,
            "values": plugin.merge_values({}, values),
            **self._options(min_level, {}),
        }

    def prepare_update(
        self,
        connection_id: str,
        name: str,
        enabled: bool,
        values: dict[str, Any],
        min_level: str | None = None,
    ) -> tuple[type[PluginInfo], dict[str, Any]]:
        saved = self.record(connection_id)
        plugin = self.plugin(saved)
        if plugin is None:
            raise PluginConfigError(msg("plugins.connection.pluginMissing"))
        return plugin, {
            **saved,
            "name": self._name(name, connection_id),
            "enabled": enabled,
            "values": plugin.merge_values(saved.get("values") or {}, values),
            **self._options(min_level, saved),
        }

    def create(self, record: dict[str, Any]) -> str:
        connection_id = ID_PREFIX[self.kind] + uuid.uuid4().hex[:12]
        self.database.set_plugin_connection(self.kind, connection_id, record)
        return connection_id

    def save(self, connection_id: str, record: dict[str, Any]) -> None:
        self.database.set_plugin_connection(self.kind, connection_id, record)

    def delete(self, connection_id: str) -> None:
        self.record(connection_id)
        self.database.set_plugin_connection(self.kind, connection_id, None)
