"""手动 SSH 插件：接入任何提供 SSH 登录的云 GPU，不依赖平台接口。

用户在云平台网页上开机，在主控填写地址、端口、账号和密码；主控通过 SSH 部署 Worker，
并建立反向隧道让 Worker 连回主控（见 ``orchestrator/cloud_tunnel``）。

没有平台接口，所以不能开机、关机、销毁或保存镜像：这些仍在平台网页上操作，也没有自动关机。
实例是否在运行按 SSH 端口能否连通判断。云平台重启实例后登录地址或端口变化时，需要重新添加。

机器列表（含密码）与其他平台的授权一样保存在 ``runtime_settings.cloud_credentials``，不回显密码。
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from typing import Any, ClassVar

import asyncssh

from ...i18n import msg
from ...plugins.base import builtin_icon
from ..base import (
    CAP_FORGET,
    CAP_LAUNCH,
    CAP_LIST,
    CAP_SSH,
    CloudError,
    CloudErrorKind,
    CloudInstance,
    CloudProvider,
    CredentialField,
    LaunchMethod,
    LaunchTarget,
    SshInfo,
    instance_status_text,
)

PROBE_TIMEOUT_SECONDS = 5
GPU_NAME_COMMAND = "nvidia-smi --id=0 --query-gpu=name --format=csv,noheader"
# ssh 命令中带一个参数的选项；除 -p、-l 外都忽略。
_OPTIONS_WITH_VALUE = set("bcDEeFIiJLlmOopQRSWw")
_DESTINATION = re.compile(
    r"^(?:ssh://)?(?:(?P<user>[^@\s]+)@)?(?P<host>\[[^\]]+\]|[^:\s]+)(?::(?P<port>\d+))?/?$"
)


def parse_ssh_command(text: str) -> tuple[str, int, str]:
    """解析 ``ssh -p 12345 root@host``、``root@host:12345`` 等写法，返回 (host, port, username)。"""
    try:
        words = shlex.split(text.strip())
    except ValueError as exc:
        raise CloudError(CloudErrorKind.FATAL, msg("ssh.parseFailed", exc=exc)) from exc
    if words and words[0] == "ssh":
        words = words[1:]
    port: int | None = None
    username: str | None = None
    destination: str | None = None
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word.startswith("-") and len(word) > 1:
            flag, value = word[1], word[2:]
            if flag not in _OPTIONS_WITH_VALUE:
                continue
            if not value and index < len(words):
                value = words[index]
                index += 1
            if flag == "p":
                port = int(value) if value.isdigit() else None
                if port is None:
                    raise CloudError(CloudErrorKind.FATAL, msg("ssh.portNotNumber", value=value))
            elif flag == "l":
                username = value
        elif destination is None:
            destination = word
    match = _DESTINATION.match(destination or "")
    if match is None:
        raise CloudError(
            CloudErrorKind.FATAL,
            msg("ssh.noHost"),
        )
    host = match["host"].strip("[]")
    port = port or (int(match["port"]) if match["port"] else 22)
    username = username or match["user"] or "root"
    if not 1 <= port <= 65535:
        raise CloudError(CloudErrorKind.FATAL, msg("ssh.portRange", port=port))
    return host, port, username


def machine_id(host: str, port: int) -> str:
    return re.sub(r"[^A-Za-z0-9.-]", "_", f"{host}-{port}")


async def reachable(host: str, port: int) -> bool:
    """SSH 端口能否连通：没有平台接口时用它判断实例是否在运行。"""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), PROBE_TIMEOUT_SECONDS
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def probe_gpu(ssh: SshInfo) -> str:
    """登录实例并读取 GPU 型号，用来在添加机器时验证登录信息。

    这里只验证能否登录，不记录主机密钥；主机密钥在首次部署时记录，之后每次连接都校验。
    """
    try:
        async with asyncssh.connect(
            ssh.host,
            port=ssh.port,
            username=ssh.username,
            password=ssh.password,
            known_hosts=None,
            client_keys=None,
            agent_path=None,
            connect_timeout=20,
        ) as connection:
            result = await connection.run(GPU_NAME_COMMAND, check=False, timeout=30)
    except asyncssh.PermissionDenied as exc:
        raise CloudError(CloudErrorKind.AUTH, msg("ssh.authRejected")) from exc
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise CloudError(
            CloudErrorKind.FATAL,
            msg("ssh.connectFailed", host=ssh.host, port=ssh.port, exc=exc),
        ) from exc
    lines = str(result.stdout or "").strip().splitlines()
    if result.exit_status != 0 or not lines:
        raise CloudError(
            CloudErrorKind.FATAL, msg("ssh.noGpu")
        )
    return lines[0].strip()


def _machines(values: dict[str, str]) -> list[dict[str, Any]]:
    try:
        stored = json.loads(values.get("machines") or "[]")
    except ValueError:
        return []
    return [item for item in stored if isinstance(item, dict) and item.get("id")]


class ManualSshProvider(CloudProvider):
    id: ClassVar[str] = "ssh"
    label: ClassVar[str] = "SSH"
    icon: ClassVar[str] = builtin_icon("ssh.svg")
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_LIST, CAP_LAUNCH, CAP_SSH, CAP_FORGET})
    credential_mode: ClassVar[str] = "machines"
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        CredentialField(
            key="host",
            label=msg("ssh.field.host"),
            secret=False,
            placeholder=msg("ssh.field.hostPlaceholder"),
        ),
        CredentialField("port", msg("ssh.field.port"), secret=False, default="22"),
        CredentialField(
            "username",
            msg("ssh.field.user"),
            secret=False,
            default="root",
            help=msg("ssh.field.userHelp"),
        ),
        CredentialField(
            key="password",
            label=msg("ssh.field.password"),
            placeholder=msg("ssh.field.passwordPlaceholder"),
            help=msg("ssh.field.passwordHelp"),
            trim=False,
        ),
    )
    launch_methods: ClassVar[tuple[LaunchMethod, ...]] = (
        LaunchMethod(
            id="connect",
            label=msg("ssh.method.label"),
            target_label=msg("ssh.method.target"),
            creates_instance=False,
            configurable_gpu=False,
            help=msg("ssh.method.help"),
        ),
    )


    @classmethod
    def credentials_configured(cls, values: dict[str, str]) -> bool:
        return bool(_machines(values))

    @classmethod
    def merge_credentials(cls, saved: dict[str, str], values: dict[str, str]) -> dict[str, str]:
        """表单只描述一台要添加（或更新密码）的机器；已保存的机器原样保留。"""
        merged = {"machines": saved.get("machines", "[]")}
        command = (values.get("ssh_command") or "").strip()
        host = (values.get("host") or "").strip()
        password = values.get("password") or ""
        if not host and not command:
            if password:
                raise CloudError(CloudErrorKind.AUTH, msg("ssh.requireHost"))
            if not _machines(saved):
                raise CloudError(CloudErrorKind.AUTH, msg("ssh.requireHostPassword"))
            return merged
        if host:
            if (
                any(c.isspace() for c in host)
                or any(c in host for c in "/@")
                or host.startswith("-")
            ):
                raise CloudError(CloudErrorKind.FATAL, msg("ssh.hostFormat"))
            host = host.strip("[]")
            try:
                port = int((values.get("port") or "22").strip())
            except ValueError as exc:
                raise CloudError(CloudErrorKind.FATAL, msg("ssh.portFormat")) from exc
            if not 1 <= port <= 65535:
                raise CloudError(CloudErrorKind.FATAL, msg("ssh.portFormat"))
            username = (values.get("username") or "root").strip() or "root"
            if any(c.isspace() for c in username) or username.startswith("-"):
                raise CloudError(CloudErrorKind.FATAL, msg("ssh.userSpaces"))
        else:
            host, port, username = parse_ssh_command(command)
        existing = next(
            (
                m
                for m in _machines(saved)
                if m["id"] == (values.get("machine_id") or machine_id(host, port))
            ),
            None,
        )
        if values.get("machine_id") and existing is None:
            raise CloudError(CloudErrorKind.FATAL, msg("ssh.removed"))
        if not password:
            if existing is None:
                raise CloudError(CloudErrorKind.AUTH, msg("ssh.requirePassword"))
            if (str(existing.get("host")), int(existing.get("port") or 0)) != (host, port):
                # 沿用已保存的密码时地址不能变：否则密码会被发往新的主机。
                raise CloudError(CloudErrorKind.AUTH, msg("ssh.passwordRebind"))
            password = str(existing.get("password") or "")
        return {
            **merged,
            "host": host,
            "port": str(port),
            "username": username,
            "password": password,
            "machine_id": existing["id"] if existing else "",
        }

    def _pending(self) -> SshInfo | None:
        if self.credentials.get("host"):
            return SshInfo(
                host=self.credentials["host"],
                port=int(self.credentials["port"]),
                username=self.credentials["username"],
                password=self.credentials.get("password"),
            )
        command = self.credentials.get("ssh_command")
        if not command:
            return None
        host, port, username = parse_ssh_command(command)
        return SshInfo(
            host=host, port=port, username=username, password=self.credentials.get("password")
        )

    async def prepare_credentials(self) -> dict[str, str]:
        machines = _machines(self.credentials)
        pending = self._pending()
        if pending is not None:
            gpu_name = await probe_gpu(pending)
            entry = {
                "id": self.credentials.get("machine_id") or machine_id(pending.host, pending.port),
                "host": pending.host,
                "port": pending.port,
                "username": pending.username,
                "password": pending.password,
                "gpu_name": gpu_name,
            }
            machines = [m for m in machines if m["id"] != entry["id"]] + [entry]
        return {"machines": json.dumps(machines, ensure_ascii=False)}

    @classmethod
    def forget_instance(cls, saved: dict[str, str], instance_id: str) -> dict[str, str] | None:
        machines = [m for m in _machines(saved) if m["id"] != instance_id]
        return {"machines": json.dumps(machines, ensure_ascii=False)} if machines else None

    async def test(self) -> str:
        pending = self._pending()
        if pending is not None:
            return msg("ssh.testOk", gpu=await probe_gpu(pending))
        instances = await self.list_instances()
        online = sum(1 for instance in instances if instance.status == "running")
        return msg("ssh.added", online=online, count=len(instances))


    async def list_instances(self) -> list[CloudInstance]:
        machines = _machines(self.credentials)
        online = await asyncio.gather(
            *(reachable(str(m["host"]), int(m["port"])) for m in machines)
        )
        instances = []
        for machine, up in zip(machines, online, strict=True):
            status = "running" if up else "stopped"
            instances.append(
                CloudInstance(
                    id=machine["id"],
                    title=f"{machine['host']}:{machine['port']}",
                    status=status,
                    status_text=instance_status_text(status, "" if up else msg("ssh.unreachable")),
                    gpu_name=str(machine.get("gpu_name") or ""),
                    gpu_count=1,
                    ssh=SshInfo(
                        host=str(machine["host"]),
                        port=int(machine["port"]),
                        username=str(machine.get("username") or "root"),
                        password=machine.get("password"),
                    ),
                )
            )
        return instances

    async def launch_targets(self, method: str, query: str = "") -> list[LaunchTarget]:
        return [
            LaunchTarget(
                id=machine["id"],
                label=f"{machine['host']}:{machine['port']}",
                detail=str(machine.get("gpu_name") or ""),
            )
            for machine in _machines(self.credentials)
        ]

    async def launch(self, method: str, target_id: str, gpu_id: str, gpu_count: int) -> str | None:
        machine = next((m for m in _machines(self.credentials) if m["id"] == target_id), None)
        if machine is None:
            raise CloudError(CloudErrorKind.FATAL, msg("ssh.removedAgain"))
        if not await reachable(str(machine["host"]), int(machine["port"])):
            raise CloudError(
                CloudErrorKind.FATAL,
                msg("ssh.offline", host=machine['host'], port=machine['port']),
            )
        return target_id
