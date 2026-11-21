"""GPU 机器加载内置媒体库来源（WebDAV、Samba）。

内置来源随程序代码一起安装，不需要从主程序下载源码。这里不经过插件注册表：注册表会导入云平台等
只有主程序才安装的依赖。
"""

from __future__ import annotations

from importlib import import_module

from ..i18n import msg
from .base import MediaSourcePlugin
from .loader import PluginLoadError

# {插件 id: (模块, 类名)}，与 plugins.registry 中媒体库来源的内置列表一致。
BUILTIN_SOURCES: dict[str, tuple[str, str]] = {
    "webdav": (".webdav", "WebDavSource"),
    "smb": (".smb", "SmbSource"),
}


def load(plugin_id: str) -> type[MediaSourcePlugin]:
    """按 id 加载内置来源；不认识的 id 或导入失败（例如缺少依赖）时抛出 PluginLoadError。"""
    if plugin_id not in BUILTIN_SOURCES:
        raise PluginLoadError(msg("plugins.loader.notFound", plugin_id=plugin_id))
    module_name, class_name = BUILTIN_SOURCES[plugin_id]
    try:
        module = import_module(module_name, __package__)
    except ImportError as exc:
        raise PluginLoadError(
            msg("plugins.loader.loadFailed", plugin_id=plugin_id, exc=exc, error_type=type(exc).__name__)
        ) from exc
    return getattr(module, class_name)
