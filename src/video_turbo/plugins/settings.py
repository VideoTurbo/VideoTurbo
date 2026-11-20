"""插件设置（``settings_fields``，在插件卡片的设置弹窗里填写）和插件状态（插件用 ``save_state`` 保存的数据）。

插件设置被所有配置共用；媒体库来源插件没有多份配置，插件设置就是它的全部配置。
同一插件文件里的多个插件类共用一份设置和状态（按插件文件的 id 保存，见 :func:`data_key`）。
用户保存的设置有变化时清空插件状态：换了账号或地址后，插件之前保存的令牌不再适用。
"""

from __future__ import annotations

from typing import Any

from ..database import Database
from .base import PluginInfo, masked_values, missing_required, with_defaults


def data_key(plugin: type) -> str:
    """插件设置与状态的保存位置：在控制台安装的插件按插件文件的 id，其他按插件类的 id。"""
    return getattr(plugin, "plugin_key", "") or plugin.id


class PluginSettings:
    def __init__(self, database: Database) -> None:
        self.database = database

    def settings(self, plugin: type[PluginInfo]) -> dict[str, Any]:
        """已保存的插件设置；没保存过的项取插件声明的默认值。"""
        saved = self.database.plugin_data(data_key(plugin))["settings"]
        return with_defaults(plugin.settings_fields, saved)

    def state(self, key: str) -> dict[str, Any]:
        """插件状态；key 是 :func:`data_key` 的结果。"""
        return self.database.plugin_data(key)["state"]

    def describe(self, plugin: type[PluginInfo]) -> dict[str, Any]:
        """设置弹窗需要的字段和已保存的值（密钥只返回掩码）。"""
        return {
            "fields": [field.describe() for field in plugin.settings_fields],
            "values": masked_values(plugin.settings_fields, self.settings(plugin)),
        }

    def missing(self, plugin: type[PluginInfo]) -> list[str]:
        """还没填写的必填设置项名称。"""
        return missing_required(plugin.settings_fields, self.settings(plugin))

    def merged(self, plugins: list[type[PluginInfo]], values: dict[str, Any]) -> dict[str, Any]:
        """表单提交的设置叠加到已保存的设置上并校验（不保存）；不合法时抛出 PluginConfigError。

        plugins 是共用这份设置的插件类（同一插件文件里的），依次校验。
        """
        first, *others = plugins
        settings = first.merge_settings(self.settings(first), values)
        for plugin in others:
            settings = plugin.validate_settings(settings)
        return settings

    def save(self, plugin: type[PluginInfo], settings: dict[str, Any]) -> bool:
        """保存设置；有变化时清空插件状态。返回是否有变化。"""
        changed = False

        def change(record: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed
            changed = with_defaults(plugin.settings_fields, record["settings"]) != settings
            return {"settings": settings, "state": {} if changed else record["state"]}

        self.database.update_plugin_data(data_key(plugin), change)
        return changed

    def save_state(self, key: str, changes: dict[str, Any]) -> None:
        """插件保存的状态：changes 中值为 None 的项删除，其余写入。key 是 :func:`data_key` 的结果。"""

        def change(record: dict[str, Any]) -> dict[str, Any]:
            state = dict(record["state"])
            for key_, value in changes.items():
                if value is None:
                    state.pop(key_, None)
                else:
                    state[key_] = value
            return {**record, "state": state}

        self.database.update_plugin_data(key, change)

    def bind(self, plugin: type[PluginInfo]) -> dict[str, Any]:
        """创建插件实例时传入的设置、状态和保存状态的回调。"""
        key = data_key(plugin)
        return {
            "settings": self.settings(plugin),
            "state": self.state(key),
            "on_state": lambda changes: self.save_state(key, changes),
        }
