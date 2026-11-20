"""GPU 机器加载主程序下发的媒体库来源插件。

插件源码按文件指纹（SHA-256）从主程序下载，核对一致才执行；同一指纹只加载一次。
主程序安装插件时已做过静态检查并由用户核对过指纹，这里不再重复检查。
"""

from __future__ import annotations

import hashlib
import sys
import threading
import types

from ..i18n import msg
from .base import PLUGIN_API_MIN_VERSION, PLUGIN_API_VERSION, MediaSourcePlugin

# {(文件指纹, 插件类 id): 插件类}。一个插件文件里可以有多个插件类。
_loaded: dict[tuple[str, str], type[MediaSourcePlugin]] = {}
_lock = threading.Lock()


class PluginLoadError(RuntimeError):
    pass


def cached(sha256: str, plugin_id: str) -> type[MediaSourcePlugin] | None:
    with _lock:
        return _loaded.get((sha256.lower(), plugin_id))


def load(source: str, sha256: str, plugin_id: str) -> type[MediaSourcePlugin]:
    """执行插件源码，返回其中 id 为 plugin_id 的媒体库来源插件类。"""
    sha256 = sha256.lower()
    if hashlib.sha256(source.encode()).hexdigest() != sha256:
        raise PluginLoadError(msg("plugins.loader.shaMismatch"))
    with _lock:
        if (sha256, plugin_id) in _loaded:
            return _loaded[(sha256, plugin_id)]
        name = f"video_turbo_media_source_{sha256[:16]}_{plugin_id}"
        module = types.ModuleType(name)
        module.__file__ = f"<媒体库来源插件 {plugin_id}>"
        sys.modules[name] = module
        try:
            exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
        except Exception as exc:
            sys.modules.pop(name, None)
            raise PluginLoadError(
                msg("plugins.loader.loadFailed", plugin_id=plugin_id, exc=exc, error_type=type(exc).__name__)
            ) from exc
        declared = module.__dict__.get("PLUGIN_API")
        if isinstance(declared, dict):
            declared = declared.get("media_source")
        if (
            isinstance(declared, bool)
            or not isinstance(declared, int)
            or not PLUGIN_API_MIN_VERSION <= declared <= PLUGIN_API_VERSION
        ):
            sys.modules.pop(name, None)
            raise PluginLoadError(
                msg("plugins.loader.apiMismatch", plugin_id=plugin_id)
            )
        plugin = next(
            (
                value
                for value in module.__dict__.values()
                if isinstance(value, type)
                and issubclass(value, MediaSourcePlugin)
                and value is not MediaSourcePlugin
                and getattr(value, "id", None) == plugin_id
            ),
            None,
        )
        if plugin is None:
            sys.modules.pop(name, None)
            raise PluginLoadError(msg("plugins.loader.notFound", plugin_id=plugin_id))
        _loaded[(sha256, plugin_id)] = plugin
        return plugin
