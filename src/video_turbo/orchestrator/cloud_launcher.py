"""开云机任务：抢卡 → 等待开机 → 绑定 → 建立 SSH 隧道 → SSH 部署 Worker → 等待 Worker 连上主控。

云 Worker 通过主控建立的 SSH 反向隧道连接主控（见 ``cloud_tunnel``），不需要主控有公网地址。

部署哪些服务（字幕识别、影片修复，可都不选）由创建任务时的 services 决定，重新部署沿用同一选择；
实例上已安装但这次没选的服务不会启动。

每个任务只用一种开机方式（开启已关机实例、私有镜像新建、Pod 新建，由用户选择），抢到 1 台即停。
缺货时按间隔持续重试；限流时退避（最长 30 秒）；授权无效、余额不足等致命错误立即停止。
请求超时或平台接受但未返回实例时，对照实例列表核对，避免重复开机。

支持平台空闲关机的平台会设置该保底；不支持的平台在主控失联或 Worker 离线时需手动关机。
主控重启后，进行中的任务自动继续。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Self

from ..cloud.base import CAP_SSH, CloudError, CloudErrorKind, CloudInstance, LaunchMethod
from ..cloud_models import CloudBindingRequest
from ..database import Database
from ..i18n import msg
from ..models import utc_now_iso
from ..restore_store import machine_key
from .cloud_deploy import (
    DeployError,
    NvencPrecheckError,
    deploy_worker,
    gpu_profile,
    query_machine_id,
)
from .cloud_service import CloudService
from .cloud_tunnel import WORKER_SERVER_URL, TunnelManager
from .worker_bundle import BundleError, WorkerBundle, build_bundle

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 30
BOOT_TIMEOUT_SECONDS = 900
BOOT_POLL_SECONDS = 5
SSH_INFO_TIMEOUT_SECONDS = 120
CONFIRM_SECONDS = 30
WORKER_ONLINE_TIMEOUT_SECONDS = 300
LOG_FLUSH_SECONDS = 2

Deployer = Callable[..., Awaitable[None]]
MachineIdentifier = Callable[..., Awaitable[str]]


class LaunchFailed(Exception):
    pass


class _BackToGrabbing(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()



class _DeployFailed(Exception):
    """自动安装失败；message 直接显示在开机记录里。"""


class _LaunchLog:
    """开机安装日志：先缓存在内存里，定时批量写入数据库；“[安装]”开头的行同时作为当前进度显示。"""

    def __init__(self, database: Any, launch_id: str) -> None:
        self.database = database
        self.launch_id = launch_id
        self.buffer: list[str] = []
        self._flusher: asyncio.Task | None = None

    def __call__(self, line: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.buffer.append(f"{stamp} {line}\n")
        if line.startswith("[安装]"):
            self.database.update_cloud_launch(self.launch_id, message=line.removeprefix("[安装] "))

    def flush(self) -> None:
        if self.buffer:
            self.database.append_cloud_launch_log(self.launch_id, "".join(self.buffer))
            self.buffer.clear()

    async def _flush_periodically(self) -> None:
        # 按时间写入而不是等下一行：脚本长时间没输出时，已收到的内容也不会积压在内存里。
        while True:
            await asyncio.sleep(LOG_FLUSH_SECONDS)
            self.flush()

    async def __aenter__(self) -> Self:
        self._flusher = asyncio.create_task(self._flush_periodically())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
        self.flush()

class CloudLauncher:
    def __init__(
        self,
        service: CloudService,
        database: Database,
        *,
        worker_token: Callable[[], str],
        tunnels: TunnelManager | None = None,
        deployer: Deployer = deploy_worker,
        identifier: MachineIdentifier = query_machine_id,
        bundle_factory: Callable[[], WorkerBundle] = build_bundle,
        poll_seconds: float = BOOT_POLL_SECONDS,
    ) -> None:
        self.service = service
        self.database = database
        self.worker_token = worker_token
        self.tunnels = tunnels
        self.deployer = deployer
        self.identifier = identifier
        self.bundle_factory = bundle_factory
        self.poll_seconds = poll_seconds
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()


    def resume_all(self) -> None:
        for launch_id in self.database.active_cloud_launch_ids():
            self.start(launch_id)

    def start(self, launch_id: str) -> None:
        if launch_id in self.tasks and not self.tasks[launch_id].done():
            return
        self._cancelled.discard(launch_id)
        self.tasks[launch_id] = asyncio.create_task(self._run(launch_id))

    async def cancel(self, launch_id: str) -> None:
        self._cancelled.add(launch_id)
        task = self.tasks.get(launch_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        launch = self.database.get_cloud_launch(launch_id, with_log=False)
        if launch and launch["state"] in {"grabbing", "booting", "deploying"}:
            message = (
                msg("launch.stoppedBilling")
                if launch["instance_id"] and launch["state"] != "grabbing"
                else msg("launch.stopped")
            )
            self.database.update_cloud_launch(
                launch_id, state="cancelled", message=message, finished_at=_now()
            )

    async def close(self) -> None:
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def redeploy(self, launch_id: str, *, forget_host_key: bool = False) -> None:
        if forget_host_key:
            launch = self.database.get_cloud_launch(launch_id, with_log=False)
            if launch and launch["instance_id"]:
                self.database.set_cloud_host_key(launch["provider"], launch["instance_id"], None)
                self._log(launch_id, msg("launch.hostKeyForgotten"))
        self.database.update_cloud_launch(
            launch_id, state="deploying", message=msg("launch.redeploying"), finished_at=None, asr_only=False
        )
        self.start(launch_id)

    def continue_asr(self, launch_id: str) -> None:
        launch = self.database.get_cloud_launch(launch_id, with_log=False)
        if (
            not launch or launch["state"] != "nvenc_failed" or not launch["instance_id"]
            or not {"asr", "restore"}.issubset(launch["services"])
        ):
            raise ValueError(msg("launch.continueNotAllowed"))
        self.database.update_cloud_launch(
            launch_id, state="deploying", message=msg("launch.continuingAsr"),
            finished_at=None, asr_only=True,
        )
        self._log(launch_id, msg("launch.continueAsrLog"))
        self.start(launch_id)


    def _log(self, launch_id: str, line: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.database.append_cloud_launch_log(launch_id, f"{stamp} {line}\n")

    def _label(self, launch: dict[str, Any]) -> str:
        name = self.service.provider_label(launch["provider"])
        return f"{name} · {launch['gpu_label']} · {launch['target_label']}"

    async def _run(self, launch_id: str) -> None:
        try:
            while True:
                launch = self.database.get_cloud_launch(launch_id, with_log=False)
                if launch is None:
                    return
                state = launch["state"]
                if state == "grabbing":
                    instance_id = await self._grab(launch)
                    self.database.update_cloud_launch(
                        launch_id,
                        state="booting",
                        instance_id=instance_id,
                        grabbed_at=_now(),
                        message=msg("launch.grabbed"),
                    )
                    self._log(launch_id, msg("launch.rentedLog", instance_id=instance_id))
                    self.service.drop_instance_launches(
                        launch["provider"], instance_id, keep=launch_id
                    )
                    self.service.notify(
                        "success",
                        msg("launch.grabbedTitle"),
                        msg("launch.grabbedMessage", label=self._label(launch), id=instance_id[:8]),
                    )
                elif state == "booting":
                    try:
                        instance = await self._wait_boot(launch)
                    except _BackToGrabbing as exc:
                        self.database.update_cloud_launch(
                            launch_id, state="grabbing", message=str(exc)
                        )
                        self._log(launch_id, str(exc))
                        continue
                    await self._after_boot(launch, instance)
                elif state == "deploying":
                    await self._deploy(launch)
                    return
                else:
                    return
        except asyncio.CancelledError:
            raise
        except LaunchFailed as exc:
            self._fail(launch_id, "failed", str(exc))
        except Exception as exc:
            logger.exception("开云机任务 %s 异常。", launch_id)
            self._fail(launch_id, "failed", msg("launch.internalError", exc=exc))

    def _fail(self, launch_id: str, state: str, message: str) -> None:
        launch = self.database.get_cloud_launch(launch_id, with_log=False)
        self.database.update_cloud_launch(
            launch_id, state=state, message=message, finished_at=_now()
        )
        self._log(launch_id, message)
        if launch:
            title = msg("launch.deployFailedTitle") if state == "deploy_failed" else msg("launch.launchFailedTitle")
            billing = msg("launch.stillBilling") if launch["instance_id"] and state != "failed" else ""
            self.service.notify("error", title, msg("launch.failedMessage", billing=billing, message=message, label=self._label(launch)))


    def _method(self, launch: dict[str, Any]) -> LaunchMethod:
        provider = self.service.provider(launch["provider"])
        if provider is None:
            raise LaunchFailed(msg("launch.noProvider", provider=launch['provider']))
        method = next((m for m in provider.launch_methods if m.id == launch["method"]), None)
        if method is None:
            raise LaunchFailed(msg("launch.noMethod", label=provider.label, method=launch['method']))
        return method

    async def _instances(self, provider_id: str) -> list[CloudInstance]:
        return await self.service.instances(provider_id, max_age=0)

    async def _grab(self, launch: dict[str, Any]) -> str:
        launch_id = launch["id"]
        method = self._method(launch)
        interval = float(launch["interval_seconds"])
        try:
            instances = await self._instances(launch["provider"])
        except CloudError as exc:
            if not exc.retryable:
                raise LaunchFailed(exc.message) from exc
            instances = []
        baseline = {instance.id for instance in instances}
        if not method.creates_instance:
            target = next((i for i in instances if i.id == launch["target_id"]), None)
            if instances and target is None:
                raise LaunchFailed(msg("launch.targetGone"))
            if target and target.status in {"running", "starting"}:
                self._log(launch_id, msg("launch.alreadyRunning"))
                return target.id
        attempts = int(launch["attempts"] or 0)
        wait = interval
        self._log(launch_id, msg("launch.grabStart", label=method.label, interval=f"{interval:g}"))
        async with self.service.client(launch["provider"]) as client:
            while True:
                attempts += 1
                try:
                    instance_id = await client.launch(
                        launch["method"], launch["target_id"], launch["gpu_id"], launch["gpu_count"]
                    )
                except CloudError as exc:
                    if exc.kind in {CloudErrorKind.AUTH, CloudErrorKind.FATAL}:
                        raise LaunchFailed(
                            msg("launch.attemptFatal", attempts=attempts, message=exc.message)
                        ) from exc
                    if exc.kind == CloudErrorKind.RATE_LIMIT:
                        wait = min(MAX_BACKOFF_SECONDS, max(wait * 2, interval))
                        message = msg("launch.attemptRateLimited", attempts=attempts, wait=f"{wait:g}")
                    elif exc.kind == CloudErrorKind.OUT_OF_STOCK:
                        wait = interval
                        message = msg("launch.attemptOutOfStock", attempts=attempts)
                    else:
                        wait = interval
                        message = msg("launch.attempt", attempts=attempts, message=exc.message)
                        # 超时或平台异常时请求可能已经成功，核对一次避免重复开机。
                        found = await self._find_launched(launch, method, baseline)
                        if found:
                            self._log(launch_id, msg("launch.attemptStarted", message=message))
                            return found
                    self.database.update_cloud_launch(launch_id, attempts=attempts, message=message)
                    if (
                        attempts <= 3
                        or attempts % 30 == 0
                        or exc.kind != CloudErrorKind.OUT_OF_STOCK
                    ):
                        self._log(launch_id, message)
                    await asyncio.sleep(wait)
                    continue
                self.database.update_cloud_launch(launch_id, attempts=attempts)
                if instance_id:
                    return instance_id
                # 平台接受了请求但没有返回实例 ID：等实例出现在列表中。
                deadline = time.monotonic() + CONFIRM_SECONDS
                while time.monotonic() < deadline:
                    found = await self._find_launched(launch, method, baseline)
                    if found:
                        return found
                    await asyncio.sleep(self.poll_seconds)
                raise LaunchFailed(
                    msg("launch.noNewInstance")
                )

    async def _find_launched(
        self, launch: dict[str, Any], method: LaunchMethod, baseline: set[str]
    ) -> str | None:
        try:
            instances = await self._instances(launch["provider"])
        except CloudError:
            return None
        if not method.creates_instance:
            target = next((i for i in instances if i.id == launch["target_id"]), None)
            return target.id if target and target.status in {"running", "starting"} else None
        fresh = [i for i in instances if i.id not in baseline]
        candidates = [i for i in fresh if not i.gpu_id or i.gpu_id == launch["gpu_id"]] or fresh
        return candidates[0].id if candidates else None


    async def _wait_boot(self, launch: dict[str, Any]) -> CloudInstance:
        method = self._method(launch)
        started = time.monotonic()
        running_since: float | None = None
        need_ssh = bool(launch["deploy"])
        while True:
            try:
                instances = await self._instances(launch["provider"])
            except CloudError:
                instances = None
            instance = (
                next((i for i in instances if i.id == launch["instance_id"]), None)
                if instances is not None
                else None
            )
            elapsed = time.monotonic() - started
            if instance is not None:
                if instance.status == "running":
                    running_since = running_since or time.monotonic()
                    if not need_ssh or instance.ssh:
                        return instance
                    if time.monotonic() - running_since > SSH_INFO_TIMEOUT_SECONDS:
                        raise LaunchFailed(
                            msg("launch.noSsh")
                        )
                elif instance.status == "stopped" and elapsed > 10:
                    if not method.creates_instance:
                        raise _BackToGrabbing(msg("launch.backToGrabbing"))
                    raise LaunchFailed(msg("launch.bootFailed", status_text=instance.status_text))
            elif instances is not None and elapsed > 60:
                raise LaunchFailed(msg("launch.instanceMissing"))
            if elapsed > BOOT_TIMEOUT_SECONDS:
                raise LaunchFailed(
                    msg("launch.bootTimeout", minutes=BOOT_TIMEOUT_SECONDS // 60)
                )
            await asyncio.sleep(self.poll_seconds)

    async def _after_boot(self, launch: dict[str, Any], instance: CloudInstance) -> None:
        launch_id = launch["id"]
        self._log(launch_id, msg("launch.runningLog", gpu_name=instance.gpu_name, name=instance.title or instance.id))
        if not launch["deploy"]:
            self.database.update_cloud_launch(
                launch_id,
                state="running",
                message=msg("launch.runningNoDeploy"),
                finished_at=_now(),
            )
            self.service.notify(
                "success", msg("launch.startedTitle"), msg("launch.startedMessage", label=self._label(launch))
            )
            return
        # 机器标识由 GPU UUID 决定，部署时通过 SSH 读取后再绑定实例。
        self.database.update_cloud_launch(launch_id, state="deploying", message=msg("launch.deploying"))

    def _bind_machine(self, launch: dict[str, Any], instance: CloudInstance, machine: str) -> None:
        """把实例绑定到按 GPU 识别出的机器；实例之前绑定在其他名称时先解除。"""
        existing = self.database.cloud_binding_for_instance(launch["provider"], instance.id)
        if existing and existing["machine"] != machine:
            self.database.unbind_cloud_instance(existing["machine"])
        self.database.bind_cloud_instance(
            machine,
            CloudBindingRequest(
                provider=launch["provider"],
                instance_id=instance.id,
                instance_title=instance.title or None,
                shutdown_mode=launch["shutdown_mode"],
            ),
        )
        self._log(launch["id"], msg("launch.boundLog", machine=machine, mode=launch['shutdown_mode']))
        self.database.update_cloud_launch(launch["id"], machine=machine)


    async def _deploy(self, launch: dict[str, Any]) -> None:
        """开机后：检查云主机、识别显卡、建立连接、安装服务，然后等服务连上主程序。"""
        launch_id = launch["id"]
        try:
            instance, profile, bundle = await self._deploy_target(launch)
            async with _LaunchLog(self.database, launch_id) as log:
                machine = await self._install(launch, instance, profile, bundle, log)
        except _DeployFailed as exc:
            self._fail(launch_id, "deploy_failed", str(exc))
            return
        if machine is not None:
            await self._report_online(launch, instance, machine)

    async def _deploy_target(self, launch: dict[str, Any]) -> tuple[Any, Any, Any]:
        """安装前的检查：平台能提供 SSH、云主机在运行、显卡受支持；返回云主机、显卡配置和安装包。"""
        provider = self.service.provider(launch["provider"])
        if provider is None or CAP_SSH not in provider.capabilities:
            raise _DeployFailed(msg("launch.noSshPlugin"))
        try:
            instances = await self._instances(launch["provider"])
        except CloudError as exc:
            raise _DeployFailed(msg("launch.readFailed", message=exc.message)) from exc
        instance = next((i for i in instances if i.id == launch["instance_id"]), None)
        if instance is None or instance.status != "running" or instance.ssh is None:
            raise _DeployFailed(msg("launch.notRunning"))
        gpu = instance.gpu_name or launch["gpu_label"]
        profile = gpu_profile(gpu)
        if profile is None:
            raise _DeployFailed(msg("launch.gpuUnsupported", gpu=gpu))
        try:
            bundle = await asyncio.to_thread(self.bundle_factory)
        except BundleError as exc:
            raise _DeployFailed(str(exc)) from exc
        return instance, profile, bundle

    async def _install(
        self, launch: dict[str, Any], instance: Any, profile: Any, bundle: Any, log: _LaunchLog
    ) -> str | None:
        """识别机器、建立连接并安装服务；返回机器标识。NVENC 检查未通过时停下等用户决定，返回 None。"""
        # 首次连接记录主机密钥，之后每次连接都校验（见 cloud_deploy._connect）。
        known_key = self.database.cloud_host_key(launch["provider"], instance.id)
        instance.ssh.host_key = known_key
        try:
            try:
                machine = await self.identifier(instance.ssh, log)
            finally:
                if instance.ssh.host_key and instance.ssh.host_key != known_key:
                    self.database.set_cloud_host_key(
                        launch["provider"], instance.id, instance.ssh.host_key
                    )
            self._bind_machine(launch, instance, machine)
        except (DeployError, ValueError) as exc:
            raise _DeployFailed(msg("launch.identifyFailed", exc=exc)) from exc
        log(f"[安装] 机器标识 {machine}（按显卡自动生成）")
        if self.tunnels is not None:
            log("[安装] 建立和主程序的连接")
            tunnel_error = await self.tunnels.ensure(launch["provider"], instance.id)
            if tunnel_error:
                raise _DeployFailed(msg("launch.tunnelFailed", tunnel_error=tunnel_error))
        services = ["asr"] if launch["asr_only"] else list(launch["services"])
        deploy_settings = {"server_url": WORKER_SERVER_URL, "worker_token": self.worker_token()}
        if "restore" in services and (license_code := self.database.jasna_license()):
            deploy_settings["jasna_license"] = license_code
        try:
            await self.deployer(
                instance.ssh,
                bundle=bundle,
                profile=profile,
                services=services,
                settings=deploy_settings,
                model_dir=self.service.model_dir(launch["provider"]),
                update_jasna=self.database.cloud_settings().update_jasna,
                log=log,
            )
        except NvencPrecheckError as exc:
            self._stop_for_nvenc(launch, services, exc, log)
            return None
        except DeployError as exc:
            raise _DeployFailed(str(exc)) from exc
        return machine

    def _stop_for_nvenc(
        self, launch: dict[str, Any], services: list[str], exc: NvencPrecheckError, log: _LaunchLog
    ) -> None:
        message = msg("launch.nvencStopped", exc=exc)
        if "asr" in services:
            message += msg("launch.nvencContinueHint")
        message += msg("launch.nvencBilling")
        self.database.update_cloud_launch(
            launch["id"], state="nvenc_failed", message=message, finished_at=_now()
        )
        log(f"[安装] {message}")
        self.service.notify(
            "error", msg("launch.nvencTitle"), msg("launch.labelMessage", message=message, label=self._label(launch))
        )

    async def _report_online(self, launch: dict[str, Any], instance: Any, machine: str) -> None:
        launch_id = launch["id"]
        self.database.update_cloud_launch(launch_id, message=msg("launch.waitingOnline"))
        if await self._wait_worker(machine):
            self.database.update_cloud_launch(
                launch_id, state="running", message=msg("launch.online", machine=machine), finished_at=_now()
            )
            self._log(launch_id, msg("launch.connectedLog", machine=machine))
            self.service.notify(
                "success",
                msg("launch.onlineTitle"),
                msg("launch.onlineMessage", machine=machine, label=self._label(launch)),
            )
            return
        tunnel_error = self.tunnels.error(launch["provider"], instance.id) if self.tunnels else None
        message = msg("launch.notOnline", minutes=WORKER_ONLINE_TIMEOUT_SECONDS // 60) + (
            msg("launch.tunnelMissing", tunnel_error=tunnel_error) if tunnel_error else msg("launch.checkLogs")
        )
        self.database.update_cloud_launch(launch_id, state="running", message=message, finished_at=_now())
        self._log(launch_id, message)
        self.service.notify("warning", msg("launch.notOnlineTitle"), msg("launch.notOnlineMessage", message=message, label=self._label(launch)))

    async def _wait_worker(self, machine: str) -> bool:
        deadline = time.monotonic() + WORKER_ONLINE_TIMEOUT_SECONDS
        since = utc_now_iso()
        while time.monotonic() < deadline:
            for worker in self.database.list_workers():
                if machine_key(worker["name"]) == machine and str(worker["last_seen_at"]) >= since:
                    return True
            await asyncio.sleep(self.poll_seconds)
        return False
