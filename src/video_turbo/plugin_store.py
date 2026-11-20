"""插件相关的持久化：按地址安装的插件源码、插件仓库、媒体服务器和通知插件的配置，以及插件设置和插件状态。
全部保存在 runtime_settings。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .store_base import StoreBase

INSTALLED_KEY = "plugins"
# 各类插件配置在 runtime_settings 中的键。云平台配置有自己的结构，见 cloud_store。
CONNECTION_KEYS = {
    "media_server": "media_servers",
    "notifier": "notifiers",
}
# 插件设置与插件状态：{插件 id: {"settings": {...}, "state": {...}}}。插件卸载后保留，重新安装后恢复。
PLUGIN_DATA_KEY = "plugin_data"
# 插件仓库：[{id, url, index_url, name, added_at}]，按添加顺序。
REPOSITORIES_KEY = "plugin_repositories"
# 插件状态可能在线程里（媒体库来源插件）与控制台保存设置同时写入，读改写要串行。
_plugin_data_lock = threading.Lock()


class PluginStoreMixin(StoreBase):

    def installed_plugins(self) -> dict[str, dict[str, str]]:
        """按地址安装的插件：{插件 id: {kind, url, sha256, source, installed_at, updated_at}}。"""
        stored = self.runtime_json(INSTALLED_KEY)
        return stored if isinstance(stored, dict) else {}

    def set_installed_plugin(self, plugin_id: str, plugin: dict[str, str] | None) -> None:
        plugins = self.installed_plugins()
        if plugin is None:
            plugins.pop(plugin_id, None)
        else:
            plugins[plugin_id] = plugin
        self.set_runtime_json(INSTALLED_KEY, plugins)

    def plugin_repositories(self) -> list[dict[str, str]]:
        stored = self.runtime_json(REPOSITORIES_KEY)
        return (
            [item for item in stored if isinstance(item, dict)] if isinstance(stored, list) else []
        )

    def set_plugin_repositories(self, repositories: list[dict[str, str]]) -> None:
        self.set_runtime_json(REPOSITORIES_KEY, repositories)

    def plugin_connections(self, kind: str) -> dict[str, dict[str, Any]]:
        """某类插件的配置：{配置 id: {type, name, enabled, values, ...}}，values 中含密钥。"""
        stored = self.runtime_json(CONNECTION_KEYS[kind])
        return stored if isinstance(stored, dict) else {}

    def set_plugin_connection(
        self, kind: str, connection_id: str, record: dict[str, Any] | None
    ) -> None:
        records = self.plugin_connections(kind)
        if record is None:
            records.pop(connection_id, None)
        else:
            records[connection_id] = record
        self.set_runtime_json(CONNECTION_KEYS[kind], records)

    def plugin_data(self, plugin_id: str) -> dict[str, Any]:
        """插件设置与插件状态：{"settings": {...}, "state": {...}}，settings 中含密钥。"""
        stored = self.runtime_json(PLUGIN_DATA_KEY)
        record = stored.get(plugin_id) if isinstance(stored, dict) else None
        if not isinstance(record, dict):
            return {"settings": {}, "state": {}}
        return {
            "settings": dict(record.get("settings") or {}),
            "state": dict(record.get("state") or {}),
        }

    def update_plugin_data(
        self, plugin_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        """读出插件设置与状态，交给 change 修改后写回，返回写回的记录。"""
        with _plugin_data_lock:
            stored = self.runtime_json(PLUGIN_DATA_KEY)
            records = stored if isinstance(stored, dict) else {}
            record = change(self.plugin_data(plugin_id))
            records[plugin_id] = record
            self.set_runtime_json(PLUGIN_DATA_KEY, records)
            return record
