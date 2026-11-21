"""媒体服务器插件接口。

每种媒体服务器一个 :class:`MediaServer` 子类。内置 Emby 与 Jellyfin；只用文件夹管理影片时不添加即可。
其他服务器以单个 ``.py`` 文件发布，在控制台“设置 → 插件”输入地址安装，
也可以通过 Python entry point ``video_turbo.media_servers`` 注册。
"""

from __future__ import annotations

from abc import abstractmethod

from ..plugins.base import ConfigurablePlugin

# 插件接口版本，插件文件用 ``PLUGIN_API = <版本>`` 声明自己适配的版本，
# 主程序加载 PLUGIN_API_MIN_VERSION 到 PLUGIN_API_VERSION 之间的插件。
# - 只新增（新的可选方法、字段）：PLUGIN_API_VERSION 加一，MIN 不变，旧插件照常加载；
# - 删改已有接口：PLUGIN_API_VERSION 加一，MIN 改成与它相同，旧插件需要作者更新。
# 历史：+1 支持 label、description 和字段文字写成 {语言: 文字}（多语言）。
#       +1 字段支持 visible_when（按其他字段的值显示）。
PLUGIN_API_VERSION = 3
PLUGIN_API_MIN_VERSION = 1


class MediaServer(ConfigurablePlugin):
    @abstractmethod
    async def refresh(self) -> None:
        """通知媒体服务器重新扫描媒体库。失败时抛出 PluginCallError。"""
