"""主控到云实例的 SSH 反向隧道：云 Worker 不需要能访问主控的公网地址。

主控主动用 SSH 连上实例，并请求实例把本机的 ``127.0.0.1:8766`` 转发回主控的 Worker 专用端口。
自动部署写入云 Worker 的 ``server_url`` 固定为 :data:`WORKER_SERVER_URL`，流量全部走这条出站连接。

为哪些实例维持隧道：已绑定到机器、平台插件提供 SSH 登录信息、并且主控记录过主机密钥的实例
（主机密钥只在自动部署时记录，所以手动部署后绑定的实例不会被连接）。连接断开后退避重连；
主控重启后由 :meth:`TunnelManager.run` 重新建立。主机密钥不一致时拒绝连接并提醒，需要用户
在创建任务上选择“信任新密钥并重新部署”。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

import asyncssh

from ..cloud.base import CAP_SSH, CloudError, SshInfo
from ..database import Database
from ..i18n import msg
from .cloud_service import CloudService

logger = logging.getLogger(__name__)

# 实例上监听的端口，只绑定在回环地址上，实例外部无法访问。
TUNNEL_REMOTE_PORT = 8766
WORKER_SERVER_URL = f"http://127.0.0.1:{TUNNEL_REMOTE_PORT}"
SYNC_SECONDS = 10
RETRY_SECONDS = (5, 10, 20, 40, 60)
ENSURE_TIMEOUT_SECONDS = 90

# 上一条连接没有正常断开时，实例上残留的 sshd 会话仍占着端口；只结束占用该端口的 sshd 会话进程。
_FREE_PORT_SCRIPT = f"""
for pid in $(ss -Hltnp 'sport = :{TUNNEL_REMOTE_PORT}' 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do
    case "$(cat /proc/$pid/comm 2>/dev/null)" in sshd*) kill "$pid" ;; esac
done
sleep 1
"""


class TunnelError(RuntimeError):
    """建立隧道失败。fatal 表示重试无意义，需要用户处理。"""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class Tunnel(Protocol):
    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


Connector = Callable[[SshInfo, str, int], Awaitable[Tunnel]]


async def open_tunnel(ssh: SshInfo, local_host: str, local_port: int) -> Tunnel:
    """连接实例并建立反向端口转发；返回的连接关闭即隧道断开。"""
    if not ssh.host_key:
        raise TunnelError(msg("tunnel.noHostKey"), fatal=True)
    try:
        trusted = ([asyncssh.import_public_key(ssh.host_key)], [], [])
    except asyncssh.KeyImportError as exc:
        raise TunnelError(msg("tunnel.hostKeyUnreadable", exc=exc), fatal=True) from exc
    try:
        connection = await asyncssh.connect(
            ssh.host,
            port=ssh.port,
            username=ssh.username,
            password=ssh.password,
            known_hosts=trusted,
            client_keys=None,
            agent_path=None,
            connect_timeout=20,
            keepalive_interval=15,
            keepalive_count_max=3,
        )
    except asyncssh.HostKeyNotVerifiable as exc:
        raise TunnelError(
            msg("tunnel.hostKeyChanged"),
            fatal=True,
        ) from exc
    except asyncssh.PermissionDenied as exc:
        raise TunnelError(msg("tunnel.authRejected"), fatal=True) from exc
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise TunnelError(msg("tunnel.sshFailed", host=ssh.host, port=ssh.port, exc=exc)) from exc
    try:
        for attempt in range(2):
            try:
                listener = await connection.forward_remote_port(
                    "127.0.0.1", TUNNEL_REMOTE_PORT, local_host, local_port
                )
            except (asyncssh.Error, asyncssh.ChannelListenError):
                # ChannelListenError 不是 asyncssh.Error 的子类。
                listener = None
            if listener is not None:
                return connection
            if attempt == 0:
                try:
                    await connection.run(_FREE_PORT_SCRIPT, check=False, timeout=15)
                except (asyncssh.Error, TimeoutError, OSError):
                    break
        raise TunnelError(
            msg("tunnel.forwardingDenied", port=TUNNEL_REMOTE_PORT)
        )
    except BaseException:
        connection.close()
        raise


class TunnelManager:
    def __init__(
        self,
        service: CloudService,
        database: Database,
        *,
        local_host: str,
        local_port: int,
        connector: Connector = open_tunnel,
        sync_seconds: float = SYNC_SECONDS,
        retry_seconds: tuple[float, ...] = RETRY_SECONDS,
    ) -> None:
        self.service = service
        self.database = database
        self.local_host = local_host
        self.local_port = local_port
        self.connector = connector
        self.sync_seconds = sync_seconds
        self.retry_seconds = retry_seconds
        self.tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        # None：已连通；字符串：尚未连通的原因。
        self.errors: dict[tuple[str, str], str | None] = {}
        self._changed = asyncio.Event()


    def desired(self) -> set[tuple[str, str]]:
        keys = set()
        for binding in self.database.list_cloud_bindings():
            provider = self.service.provider(binding["provider"])
            if (
                provider is not None
                and CAP_SSH in provider.capabilities
                and self.database.cloud_host_key(binding["provider"], binding["instance_id"])
            ):
                keys.add((binding["provider"], binding["instance_id"]))
        return keys

    def sync(self) -> None:
        desired = self.desired()
        for key in [key for key in self.tasks if key not in desired]:
            self.tasks.pop(key).cancel()
            self.errors.pop(key, None)
        for key in desired:
            task = self.tasks.get(key)
            if task is None or task.done():
                self.errors.setdefault(key, msg("tunnel.connecting"))
                self.tasks[key] = asyncio.create_task(self._maintain(key))

    async def run(self) -> None:
        while True:
            try:
                self.sync()
            except Exception:
                logger.exception("同步云主机连接时出错。")
            await asyncio.sleep(self.sync_seconds)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


    def error(self, provider_id: str, instance_id: str) -> str | None:
        """隧道没有连通的原因；已连通或这台实例不需要隧道时返回 None。"""
        return self.errors.get((provider_id, instance_id))

    def _set(self, key: tuple[str, str], error: str | None) -> None:
        if key in self.tasks:
            self.errors[key] = error
        self._changed.set()
        self._changed = asyncio.Event()

    async def ensure(
        self, provider_id: str, instance_id: str, timeout: float = ENSURE_TIMEOUT_SECONDS
    ) -> str | None:
        """部署前调用：等隧道连通。返回 None 表示已连通，否则返回失败原因。"""
        key = (provider_id, instance_id)
        self.sync()
        if key not in self.tasks:
            return msg("tunnel.notBound")
        deadline = time.monotonic() + timeout
        while self.errors.get(key) is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                break
        return self.errors.get(key)


    async def _ssh(self, key: tuple[str, str]) -> SshInfo | str:
        provider_id, instance_id = key
        try:
            instances = await self.service.instances(provider_id)
        except CloudError as exc:
            return msg("tunnel.readFailed", message=exc.message)
        instance = next((item for item in instances if item.id == instance_id), None)
        if instance is None:
            return msg("tunnel.instanceGone")
        if instance.status != "running":
            return msg("tunnel.notRunning", status=instance.status_text or instance.status)
        if instance.ssh is None:
            return msg("tunnel.noSsh")
        ssh = instance.ssh.model_copy()
        ssh.host_key = self.database.cloud_host_key(provider_id, instance_id)
        return ssh

    def _where(self, key: tuple[str, str]) -> str:
        binding = self.database.cloud_binding_for_instance(*key)
        label = self.service.provider_label(key[0])
        if binding:
            return msg("tunnel.whereBound", label=label, machine=binding['machine'], title=binding['instance_title'] or key[1][:8])
        return msg("tunnel.whereHost", label=label, id=key[1][:8])

    async def _maintain(self, key: tuple[str, str]) -> None:
        failures = 0
        notified: str | None = None
        while True:
            ssh = await self._ssh(key)
            tunnel: Tunnel | None = None
            message: str
            reachable = not isinstance(ssh, str)
            if isinstance(ssh, str):
                message = ssh
            else:
                try:
                    tunnel = await self.connector(ssh, self.local_host, self.local_port)
                except TunnelError as exc:
                    message = str(exc)
                except Exception as exc:  # 连接库的意外错误不应让维护任务退出
                    logger.exception("建立 %s 的隧道时出错。", key)
                    message = msg("launch.internalError", exc=exc)
            if tunnel is not None:
                failures = 0
                if notified is not None:
                    self.service.notify(
                        "success", msg("tunnel.recoveredTitle"), msg("tunnel.recovered", where=self._where(key))
                    )
                    notified = None
                logger.info("%s 到主程序的连接（SSH 隧道）已建立。", self._where(key))
                self._set(key, None)
                try:
                    await tunnel.wait_closed()
                finally:
                    tunnel.close()
                message = msg("tunnel.reconnecting")
                logger.info("%s 到主程序的连接（SSH 隧道）已断开，准备重连。", self._where(key))
                self._set(key, message)
                await asyncio.sleep(self.retry_seconds[0])
                continue
            self._set(key, message)
            # 实例关机属于正常情况；只有实例在运行却连不上时才提醒，同一原因只提醒一次。
            if reachable:
                failures += 1
                if failures >= 3 and notified != message:
                    notified = message
                    self.service.notify(
                        "warning",
                        msg("tunnel.lostTitle"),
                        msg("tunnel.lost", message=message, where=self._where(key)),
                    )
                await asyncio.sleep(self.retry_seconds[min(failures, len(self.retry_seconds)) - 1])
            else:
                failures = 0
                await asyncio.sleep(self.sync_seconds * 2)
