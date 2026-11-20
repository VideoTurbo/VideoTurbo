"""媒体服务器刷新与通知推送：都在后台执行，有超时，失败不影响字幕发布和云主机流程。

- ``MediaRefresher``：字幕或影片修复发布后调用 ``request()``，立即返回；后台逐个刷新已启用的媒体服务器。
  刷新进行中再次请求只合并为“结束后再刷新一次”。某台连续失败只提醒一次，恢复后再提醒。
- ``NoticeService``：写入控制台“提醒”，再按每份通知配置的最低级别推送。推送失败只记日志和状态，
  不再写提醒，避免通知失败本身又触发通知。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..database import Database
from ..i18n import msg, notification_locale, render
from ..models import utc_now_iso
from ..notifiers.base import LEVEL_RANK, Notice
from ..plugins.base import describe_error
from ..plugins.connections import DEFAULT_MIN_LEVEL, Connections
from ..plugins.settings import PluginSettings

logger = logging.getLogger(__name__)

REFRESH_TIMEOUT_SECONDS = 45
SEND_TIMEOUT_SECONDS = 15
QUEUE_LIMIT = 200


class ConnectionStatus:
    """每份配置最近一次调用的结果，只保存在内存，供控制台展示。"""

    def __init__(self) -> None:
        self._status: dict[str, dict[str, Any]] = {}

    def ok(self, connection_id: str) -> None:
        self._status[connection_id] = {"last_ok_at": utc_now_iso(), "last_error": None}

    def failed(self, connection_id: str, message: str) -> None:
        previous = self._status.get(connection_id, {})
        self._status[connection_id] = {
            "last_ok_at": previous.get("last_ok_at"),
            "last_error": message,
            "last_error_at": utc_now_iso(),
        }

    def get(self, connection_id: str) -> dict[str, Any]:
        return dict(self._status.get(connection_id, {}))

    def failing(self, connection_id: str) -> bool:
        return bool(self._status.get(connection_id, {}).get("last_error"))


class NoticeService:
    def __init__(
        self,
        database: Database,
        *,
        deliver: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.database = database
        # 本地预览不推送；控制台的“测试”按钮不受影响。
        self.deliver = deliver
        self.transport = transport
        self.connections = Connections(database, "notifier")
        self.settings = PluginSettings(database)
        self.status = ConnectionStatus()
        self._queue: asyncio.Queue[Notice] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(QUEUE_LIMIT)
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = self._queue = self._loop = None

    def notify(self, level: str, title: str, message: str) -> int:
        """写入提醒并排队推送。可以在任意线程调用。"""
        log = (
            logger.error
            if level == "error"
            else logger.warning
            if level == "warning"
            else logger.info
        )
        log("%s：%s", title, message)
        notice_id = self.database.add_notice(level, title, message)  # type: ignore[arg-type]
        if self.deliver and self._loop is not None:
            # 推送给外部（Webhook 等）时没有查看者，按 VIDEO_TURBO_LANGUAGE 翻译。
            locale = notification_locale()
            notice = Notice(  # type: ignore[arg-type]
                level, render(title, locale), render(message, locale), utc_now_iso(), notice_id
            )
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self._loop:
                self._enqueue(notice)
            elif not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._enqueue, notice)
        return notice_id

    def _enqueue(self, notice: Notice) -> None:
        if self._queue is None:
            return
        if self._queue.full():
            # 接收端长时间不可用时丢弃最旧的，控制台“提醒”中仍有完整记录。
            self._queue.get_nowait()
            logger.warning("待推送的通知过多，已丢弃最早的一条。")
        self._queue.put_nowait(notice)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            notice = await self._queue.get()
            await self.deliver_one(notice)

    async def deliver_one(self, notice: Notice) -> None:
        for connection_id, record, plugin in self.connections.active():
            minimum = record.get("min_level", DEFAULT_MIN_LEVEL)
            if LEVEL_RANK.get(notice.level, 0) < LEVEL_RANK.get(minimum, 1):
                continue
            try:
                async with plugin(
                    record.get("values") or {},
                    **self.settings.bind(plugin),
                    transport=self.transport,
                ) as client:
                    await asyncio.wait_for(client.send(notice), SEND_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 插件代码的任何异常都不能中断推送队列
                message = describe_error(exc)
                logger.warning("推送通知到“%s”失败：%s", record.get("name"), message)
                self.status.failed(connection_id, message)
            else:
                self.status.ok(connection_id)


class MediaRefresher:
    def __init__(
        self,
        database: Database,
        notices: NoticeService,
        *,
        enabled: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 本地预览不刷新媒体服务器；控制台的“测试”按钮不受影响。
        self.enabled = enabled
        self.transport = transport
        self.notices = notices
        self.connections = Connections(database, "media_server")
        self.settings = PluginSettings(database)
        self.status = ConnectionStatus()
        self._pending = False
        self._task: asyncio.Task[None] | None = None

    def request(self) -> None:
        """请求刷新全部已启用的媒体服务器，立即返回。"""
        if not self.enabled or not self.connections.active():
            return
        self._pending = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._pending:
            self._pending = False
            await self.refresh_all()

    async def refresh_all(self) -> None:
        for connection_id, record, plugin in self.connections.active():
            name = record.get("name") or plugin.label
            try:
                async with plugin(
                    record.get("values") or {},
                    **self.settings.bind(plugin),
                    transport=self.transport,
                    timeout=30,
                ) as client:
                    await asyncio.wait_for(client.refresh(), REFRESH_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 一台服务器失败不影响其他服务器和发布流程
                message = describe_error(exc)
                if not self.status.failing(connection_id):
                    self.notices.notify("warning", msg("notice.mediaServer.refreshFailed", name=name), message)
                else:
                    logger.warning("刷新媒体服务器“%s”失败：%s", name, message)
                self.status.failed(connection_id, message)
            else:
                if self.status.failing(connection_id):
                    self.notices.notify("success", msg("notice.mediaServer.recovered", name=name), msg("notice.mediaServer.refreshOk"))
                self.status.ok(connection_id)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
