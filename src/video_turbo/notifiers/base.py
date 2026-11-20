"""通知插件接口。

每种推送方式一个 :class:`Notifier` 子类，主控把达到所选级别的“提醒”逐条交给它发送。内置通用 Webhook；
Telegram、Bark 等以单个 ``.py`` 文件发布，在控制台“设置 → 插件”输入地址安装，
也可以通过 Python entry point ``video_turbo.notifiers`` 注册。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal

from ..i18n import msg, t
from ..models import utc_now_iso
from ..plugins.base import ConfigurablePlugin

# 插件接口版本，插件文件用 ``PLUGIN_API = <版本>`` 声明自己适配的版本，
# 主程序加载 PLUGIN_API_MIN_VERSION 到 PLUGIN_API_VERSION 之间的插件。
# - 只新增（新的可选方法、字段）：PLUGIN_API_VERSION 加一，MIN 不变，旧插件照常加载；
# - 删改已有接口：PLUGIN_API_VERSION 加一，MIN 改成与它相同，旧插件需要作者更新。
# 历史：+1 支持 label、description 和字段文字写成 {语言: 文字}（多语言）。
PLUGIN_API_VERSION = 2
PLUGIN_API_MIN_VERSION = 1

NoticeLevel = Literal["info", "success", "warning", "error"]
# 通知配置按最低级别过滤：info、success 同级。
LEVEL_RANK: dict[str, int] = {"info": 0, "success": 0, "warning": 1, "error": 2}


@dataclass(frozen=True)
class Notice:
    level: NoticeLevel
    title: str
    message: str
    created_at: str
    # 控制台“提醒”中的编号；测试通知为 0。
    id: int = 0


class Notifier(ConfigurablePlugin):
    @abstractmethod
    async def send(self, notice: Notice) -> None:
        """发送一条通知。失败时抛出 PluginCallError。"""

    async def test(self) -> str:
        await self.send(
            Notice(
                level="info",
                # 直接发给外部服务，没有查看者：按当前请求的语言翻译好再发。
                title=t("notifier.test.title"),
                message=t("notifier.test.message"),
                created_at=utc_now_iso(),
            )
        )
        return msg("notifier.test.sent")
