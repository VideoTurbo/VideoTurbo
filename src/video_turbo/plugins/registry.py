"""插件注册表：按类型登记内置插件、entry point 中的第三方包，以及在控制台按地址安装的插件
（由 ``installer`` 加载后调用 ``register``）。

第三方包在 ``pyproject.toml`` 中按类型声明即可，无需修改本项目::

    [project.entry-points."video_turbo.cloud_providers"]
    myplatform = "my_package.provider:MyPlatformProvider"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import entry_points

from ..cloud import base as cloud_base
from ..cloud.providers.manual_ssh import ManualSshProvider
from ..i18n import msg
from ..media_servers import base as media_base
from ..media_servers.emby import EmbyServer, JellyfinServer
from ..media_servers.fnos import FnosServer
from ..media_servers.plex import PlexServer
from ..media_sources import base as source_base
from ..media_sources.webdav import WebDavSource
from ..notifiers import base as notifier_base
from ..notifiers.webhook import WebhookNotifier
from ..plugins.base import localize_plugin

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginKind:
    id: str
    label: str
    base: type
    api_version: int
    min_api_version: int
    entry_point_group: str
    builtin: tuple[type, ...]
    # 内置的是否作为插件列在“设置 → 插件”中。为 False 时内置的算主程序自带功能，只在各自的页面里选用；
    # 这一类的第三方插件照常列出。
    builtin_listed: bool = True

    @property
    def api_versions(self) -> range:
        """能加载的插件接口版本（含两端），见各类插件 base.py 中的 PLUGIN_API_MIN_VERSION。"""
        return range(self.min_api_version, self.api_version + 1)


KINDS: dict[str, PluginKind] = {
    kind.id: kind
    for kind in (
        PluginKind(
            "cloud",
            msg("plugins.kind.cloud"),
            cloud_base.CloudProvider,
            cloud_base.PLUGIN_API_VERSION,
            cloud_base.PLUGIN_API_MIN_VERSION,
            "video_turbo.cloud_providers",
            (ManualSshProvider,),
        ),
        PluginKind(
            "media_server",
            msg("plugins.kind.mediaServer"),
            media_base.MediaServer,
            media_base.PLUGIN_API_VERSION,
            media_base.PLUGIN_API_MIN_VERSION,
            "video_turbo.media_servers",
            (EmbyServer, JellyfinServer, PlexServer, FnosServer),
            builtin_listed=False,
        ),
        PluginKind(
            "media_source",
            msg("plugins.kind.mediaSource"),
            source_base.MediaSourcePlugin,
            source_base.PLUGIN_API_VERSION,
            source_base.PLUGIN_API_MIN_VERSION,
            "video_turbo.media_sources",
            # 本地文件夹是内置来源（media_sources.local），不是插件。
            (WebDavSource,),
        ),
        PluginKind(
            "notifier",
            msg("plugins.kind.notifier"),
            notifier_base.Notifier,
            notifier_base.PLUGIN_API_VERSION,
            notifier_base.PLUGIN_API_MIN_VERSION,
            "video_turbo.notifiers",
            (WebhookNotifier,),
        ),
    )
}

_registered: dict[str, dict[str, type]] | None = None


def _load_kind(kind: PluginKind) -> dict[str, type]:
    plugins: dict[str, type] = {plugin.id: localize_plugin(plugin) for plugin in kind.builtin}
    for entry in entry_points(group=kind.entry_point_group):
        try:
            plugin = entry.load()
        except Exception:
            logger.exception("加载%s插件 %s 失败，已跳过。", kind.label, entry.name)
            continue
        if not (isinstance(plugin, type) and issubclass(plugin, kind.base)):
            logger.warning(
                "%s插件 %s 不是 %s 子类，已跳过。", kind.label, entry.name, kind.base.__name__
            )
            continue
        if plugin.id in plugins:
            logger.warning(
                "%s插件 %s 的 id %s 与已有插件重复，已跳过。", kind.label, entry.name, plugin.id
            )
            continue
        plugins[plugin.id] = localize_plugin(plugin)
    return plugins


def _all() -> dict[str, dict[str, type]]:
    global _registered
    if _registered is None:
        _registered = {kind.id: _load_kind(kind) for kind in KINDS.values()}
    return _registered


def providers(kind: str) -> dict[str, type]:
    return dict(_all()[kind])


def get(kind: str, plugin_id: str) -> type | None:
    return _all()[kind].get(plugin_id)


def is_builtin(kind: str, plugin_id: str) -> bool:
    """plugin_id 是不是这一类的内置插件（随主程序代码提供）。"""
    return any(plugin.id == plugin_id for plugin in KINDS[kind].builtin)


def find(plugin_id: str) -> tuple[str, type] | None:
    """任意类型中 id 为 plugin_id 的插件：(类型, 插件类)。"""
    for kind, plugins in _all().items():
        if plugin_id in plugins:
            return kind, plugins[plugin_id]
    return None


def kind_of(plugin: type) -> PluginKind | None:
    return next((kind for kind in KINDS.values() if issubclass(plugin, kind.base)), None)


def register(kind: str, plugin: type) -> None:
    """注册插件，立即生效；同 id 会覆盖。"""
    _all()[kind][plugin.id] = localize_plugin(plugin)


def unregister(kind: str, plugin_id: str) -> None:
    _all()[kind].pop(plugin_id, None)
