"""云 GPU 实例的空闲自动关机。

主控按“机器”判断（同机多个 Worker 实例合并）：

1. 机器上的 Worker 在线，没有持有任何识别或影片修复任务租约（含后台预取、回传），
   并且没有它能处理的待办任务（已发现、提取中、等待 GPU 的字幕任务，或它支持的排队影片修复任务），
   持续 ``idle_minutes`` 分钟后进入 draining：主控不再向这台机器派单；
2. draining 持续 ``DRAIN_SECONDS`` 秒后再确认一次仍然空闲，调用平台插件关机；期间出现新任务则取消关机；
3. 平台实例进入已关机后记为 stopped，并在控制台提醒；关机失败按退避时间重试。

不需要 Worker 配合。自动部署期间（含首次自检）不计空闲，平台保底暂时关闭，结束后恢复。
实例在运行但 Worker 不在线（进程崩溃）时不自动关机，
由平台侧空闲关机（``backstop_minutes``）兜底。
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..cloud.base import (
    CAP_DESTROY,
    CAP_FORGET,
    CAP_IDLE_CLOSE,
    CAP_SAVE_IMAGE,
    CAP_SHUTDOWN,
    CloudError,
    CloudErrorKind,
    CloudInstance,
    CloudProvider,
)
from ..cloud_models import CloudSettings
from ..config import AppConfig
from ..database import Database
from ..i18n import msg
from ..models import is_ltx_model, utc_now_iso
from ..plugins import registry
from ..restore_store import machine_key
from .integrations import NoticeService

logger = logging.getLogger(__name__)

LOOP_SECONDS = 20
DRAIN_SECONDS = 60
STOP_TIMEOUT_SECONDS = 600
INSTANCE_CACHE_SECONDS = 15
RETRY_MINUTES = (1, 2, 5, 10, 30)
BACKSTOP_RETRY_SECONDS = 300
# 关机后超过这段时间仍收到心跳，才认为绑定有误（Worker 心跳间隔默认 30 秒）。
MISMATCH_GRACE_SECONDS = 60


def mask_secret(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    return "*" * 4 + value[-4:] if len(value) > 8 else "*" * len(value)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class CloudService:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        orchestrator: Any,
        *,
        auto_enabled: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        notices: NoticeService | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.orchestrator = orchestrator
        # 本地预览只展示，不自动关机，也不改动平台侧空闲关机设置。
        self.auto_enabled = auto_enabled
        self.transport = transport
        self._cache: dict[str, tuple[float, list[CloudInstance] | None, str | None]] = {}
        self._backstop_tried: dict[str, tuple[int, float]] = {}
        self._warned: set[str] = set()
        # SSH 隧道管理器（cloud_tunnel.TunnelManager），由应用启动时设置；仅用于展示隧道状态。
        self.tunnels: Any = None
        # 提醒写入数据库并推送到通知配置；未传入时只写数据库。
        self.notices = notices or NoticeService(database, deliver=False)


    def provider(self, provider_id: str) -> type[CloudProvider] | None:
        profile = self.database.cloud_profiles().get(provider_id)
        return registry.get("cloud", profile["type"]) if profile else None

    def provider_label(self, provider_id: str) -> str:
        profile = self.database.cloud_profiles().get(provider_id, {})
        provider = self.provider(provider_id)
        return profile.get("name") or (provider.label if provider else provider_id)

    def model_dir(self, provider_id: str) -> str:
        """自动部署使用的模型目录：配置里填写的，否则平台默认；空表示登录用户的 $HOME/models。"""
        profile = self.database.cloud_profiles().get(provider_id, {})
        provider = self.provider(provider_id)
        return profile.get("model_dir") or (provider.default_model_dir if provider else "")

    def client(self, provider_id: str, credentials: dict[str, str] | None = None) -> CloudProvider:
        provider = self.provider(provider_id)
        if provider is None:
            raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.unknownProvider", provider_id=provider_id))
        values = (
            self.database.cloud_credentials(provider_id) if credentials is None else credentials
        )
        return provider(values, transport=self.transport)

    def orphan_profiles(self) -> list[dict[str, str]]:
        """插件已卸载或加载失败的平台配置：保留凭据，重新安装插件后恢复。"""
        return [
            {
                "id": profile_id,
                "label": profile.get("name") or profile_id,
                "type": profile.get("type", ""),
            }
            for profile_id, profile in self.database.cloud_profiles().items()
            if registry.get("cloud", profile.get("type", "")) is None
        ]

    def describe_providers(self) -> list[dict[str, Any]]:
        result = []
        profiles = self.database.cloud_profiles()
        for provider_id in profiles:
            provider = self.provider(provider_id)
            if provider is None:
                continue
            values = self.database.cloud_credentials(provider_id)
            info = provider.describe()
            info.update(
                id=provider_id,
                type=provider.id,
                type_label=provider.label,
                label=self.provider_label(provider_id),
            )
            info["model_dir"] = profiles.get(provider_id, {}).get("model_dir", "")
            info["configured"] = provider.credentials_configured(values)
            info["values"] = {
                field.key: mask_secret(values.get(field.key, ""))
                if field.secret
                else values.get(field.key, "")
                for field in provider.credential_fields
            }
            if provider.id == "ssh":
                # 只返回可编辑的连接信息，机器密码绝不回显。
                try:
                    machines = json.loads(values.get("machines", "[]"))
                except ValueError:
                    machines = []
                info["machines"] = [
                    {key: machine.get(key, "") for key in ("id", "host", "port", "username")}
                    for machine in machines
                ]
                if machines:
                    info["values"].update(info["machines"][0])
                    info["values"]["password"] = mask_secret(machines[0].get("password") or "")
            cached = self._cache.get(provider_id)
            info["last_error"] = cached[2] if cached else None
            result.append(info)
        return result

    def merge_credentials(self, provider_id: str, values: dict[str, str]) -> dict[str, str]:
        """把表单提交的值合并进已保存的凭据：留空的字段保留原值。"""
        provider = self.provider(provider_id)
        if provider is None:
            raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.unknownProvider", provider_id=provider_id))
        return provider.merge_credentials(self.database.cloud_credentials(provider_id), values)

    def save_credentials(self, provider_id: str, values: dict[str, str] | None) -> None:
        self.database.set_cloud_credentials(provider_id, values)
        self.invalidate(provider_id)


    def invalidate(self, provider_id: str) -> None:
        self._cache.pop(provider_id, None)

    async def instances(
        self, provider_id: str, *, max_age: float = INSTANCE_CACHE_SECONDS
    ) -> list[CloudInstance]:
        cached = self._cache.get(provider_id)
        if cached and time.monotonic() - cached[0] < max_age:
            if cached[1] is None:
                raise CloudError(CloudErrorKind.TRANSIENT, cached[2] or msg("cloud.service.listFailed"))
            return cached[1]
        try:
            async with self.client(provider_id) as client:
                items = await client.list_instances()
        except CloudError as exc:
            self._cache[provider_id] = (time.monotonic(), None, exc.message)
            raise
        self._cache[provider_id] = (time.monotonic(), items, None)
        return items

    def cached_instance(self, provider_id: str, instance_id: str) -> CloudInstance | None:
        cached = self._cache.get(provider_id)
        if not cached or cached[1] is None:
            return None
        return next((item for item in cached[1] if item.id == instance_id), None)

    def configured_provider_ids(self) -> list[str]:
        return [item["id"] for item in self.describe_providers() if item["configured"]]

    async def all_instances(self) -> list[dict[str, Any]]:
        bindings = {
            (b["provider"], b["instance_id"]): b["machine"]
            for b in self.database.list_cloud_bindings()
        }
        result = []
        for provider_id in self.configured_provider_ids():
            provider = self.provider(provider_id)
            entry: dict[str, Any] = {
                "provider": provider_id,
                "label": self.provider_label(provider_id),
                "capabilities": sorted(provider.capabilities) if provider else [],
                "launch_methods": [method.id for method in provider.launch_methods]
                if provider
                else [],
                "instances": [],
                "error": None,
            }
            try:
                items = await self.instances(provider_id)
            except CloudError as exc:
                entry["error"] = exc.message
            else:
                order = {"running": 0, "starting": 1, "stopping": 2, "unknown": 3, "stopped": 4}
                entry["instances"] = [
                    {**item.model_dump(), "machine": bindings.get((provider_id, item.id))}
                    for item in sorted(items, key=lambda i: (order[i.status], -(i.started_at or 0)))
                ]
            result.append(entry)
        return result


    def claims_blocked(self, worker_name: str) -> bool:
        binding = self.database.get_cloud_binding(machine_key(worker_name))
        return bool(binding and binding["state"] in {"draining", "stopping"})

    def binding_views(self) -> dict[str, dict[str, Any]]:
        settings = self.database.cloud_settings()
        views = {}
        for binding in self.database.list_cloud_bindings():
            provider = self.provider(binding["provider"])
            cached = self._cache.get(binding["provider"])
            instance = self.cached_instance(binding["provider"], binding["instance_id"])
            views[binding["machine"]] = {
                **binding,
                "provider_label": self.provider_label(binding["provider"]),
                "provider_missing": provider is None,
                "configured": bool(self.database.cloud_credentials(binding["provider"])),
                "instance": instance.model_dump() if instance else None,
                "instance_error": cached[2] if cached else None,
                "idle_minutes": settings.idle_minutes,
                "can_shutdown": bool(provider and CAP_SHUTDOWN in provider.capabilities),
                "tunnel_error": self.tunnels.error(binding["provider"], binding["instance_id"])
                if self.tunnels
                else None,
            }
        return views

    def status(self) -> dict[str, Any]:
        return {
            "auto_enabled": self.auto_enabled,
            "providers_configured": bool(self.configured_provider_ids()),
            "launch_providers": [
                {"id": item["id"], "label": item["label"]}
                for item in self.describe_providers()
                if item["configured"] and "launch" in item["capabilities"]
            ],
            "bindings": self.binding_views(),
            "notices": self.database.notice_summary(),
            "launches": [
                {**launch, "target_instance_id": self.launch_instance_id(launch)}
                for launch in self.database.list_cloud_launches(50)
            ],
            "jasna_license_set": bool(self.database.jasna_license()),
        }


    def launch_instance_id(self, launch: dict[str, Any]) -> str | None:
        """创建任务对应的实例：抢到后是抢到的实例；开启已有实例时，抢到前就是目标实例。"""
        if launch["instance_id"]:
            return launch["instance_id"]
        provider = self.provider(launch["provider"])
        methods = provider.launch_methods if provider else ()
        method = next((m for m in methods if m.id == launch["method"]), None)
        return launch["target_id"] if method and not method.creates_instance else None

    def drop_instance_launches(
        self, provider_id: str, instance_id: str, *, keep: str | None = None
    ) -> None:
        """每台实例只保留最新一条创建任务：删除它已结束的其他任务（进行中的不动）。"""
        for launch in self.database.list_cloud_launches(1000):
            if (
                launch["id"] != keep
                and launch["provider"] == provider_id
                and self.launch_instance_id(launch) == instance_id
            ):
                self.database.delete_cloud_launch(launch["id"])

    def notify(self, level: str, title: str, message: str) -> None:
        self.notices.notify(level, title, message)

    def _warn_once(self, key: str, title: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.notify("warning", title, message)


    async def manual_shutdown(self, provider_id: str, instance_id: str) -> None:
        async with self.client(provider_id) as client:
            if CAP_SHUTDOWN not in client.capabilities:
                raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.noShutdown", label=client.label))
            await client.shutdown(instance_id)
        binding = self.database.cloud_binding_for_instance(provider_id, instance_id)
        if binding:
            self.database.update_cloud_binding(
                binding["machine"],
                state="stopping",
                stop_requested_at=utc_now_iso(),
                last_error=None,
            )
        label = self.provider_label(provider_id)
        cached = self.cached_instance(provider_id, instance_id)
        name = (
            (binding and binding["instance_title"]) or (cached and cached.title) or instance_id[:8]
        )
        target = msg("cloud.service.targetBound", label=label, name=name, machine=binding['machine']) if binding else msg("cloud.service.targetHost", label=label, name=name)
        self.invalidate(provider_id)
        self.notify("info", msg("cloud.service.manualShutdownTitle"), msg("cloud.service.manualShutdown", target=target))

    async def manual_destroy(self, provider_id: str, instance_id: str) -> None:
        async with self.client(provider_id) as client:
            if CAP_DESTROY not in client.capabilities:
                raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.noDestroy", label=client.label))
            instance = await self._require_stopped(provider_id, instance_id)
            try:
                await client.destroy(instance_id)
            finally:
                self.invalidate(provider_id)
        binding = self.database.cloud_binding_for_instance(provider_id, instance_id)
        if binding:
            self.database.unbind_cloud_instance(binding["machine"])
        self.drop_instance_launches(provider_id, instance_id)
        self.notify(
            "info",
            msg("cloud.service.destroyTitle"),
            msg("cloud.service.destroy", label=client.label, name=instance.title or instance_id),
        )

    def manual_forget(self, provider_id: str, instance_id: str) -> None:
        """从主控移除一台实例的记录（不影响平台上的实例），同时解除绑定并忘记主机密钥。"""
        provider = self.provider(provider_id)
        if provider is None or CAP_FORGET not in provider.capabilities:
            raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.cannotForget"))
        self.save_credentials(
            provider_id,
            provider.forget_instance(self.database.cloud_credentials(provider_id), instance_id),
        )
        binding = self.database.cloud_binding_for_instance(provider_id, instance_id)
        if binding:
            self.database.unbind_cloud_instance(binding["machine"])
        self.database.set_cloud_host_key(provider_id, instance_id, None)
        self.drop_instance_launches(provider_id, instance_id)

    async def manual_save_image(
        self, provider_id: str, instance_id: str, image_name: str = ""
    ) -> None:
        async with self.client(provider_id) as client:
            if CAP_SAVE_IMAGE not in client.capabilities:
                raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.noImage", label=client.label))
            instance = await self._require_stopped(provider_id, instance_id)
            try:
                await client.save_image(instance_id, image_name.strip())
            finally:
                self.invalidate(provider_id)
        self.notify(
            "info",
            msg("cloud.service.imageTitle"),
            msg("cloud.service.image", label=client.label, name=instance.title or instance_id, image=image_name.strip() or msg('cloud.service.defaultImageName')),
        )

    async def _require_stopped(self, provider_id: str, instance_id: str) -> CloudInstance:
        # 不信任界面和缓存里的状态，操作前重新从平台读取。
        items = await self.instances(provider_id, max_age=0)
        instance = next((item for item in items if item.id == instance_id), None)
        if instance is None:
            raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.instanceGone"))
        if instance.status != "stopped":
            raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.notStopped"))
        return instance


    async def process_once(self) -> None:
        bindings = self.database.list_cloud_bindings()
        if not bindings:
            return
        for provider_id in {binding["provider"] for binding in bindings}:
            if self.provider(provider_id) and self.database.cloud_credentials(provider_id):
                try:
                    await self.instances(provider_id, max_age=LOOP_SECONDS - 5)
                except CloudError as exc:
                    logger.warning("读取云主机列表失败：%s", exc.message)
        if not self.auto_enabled:
            return
        settings = self.database.cloud_settings()
        members: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for worker in self.database.list_workers():
            members[machine_key(worker["name"])].append(worker)
        for binding in bindings:
            try:
                await self._evaluate(binding, settings, members.get(binding["machine"], []))
            except Exception:
                logger.exception("处理云主机 %s 的自动关机时出错。", binding["machine"])

    def worker_online(self, worker: dict[str, Any], now: datetime) -> bool:
        seen = _parse(str(worker.get("last_seen_at") or ""))
        return (
            worker.get("status") in {"online", "busy"}
            and seen is not None
            and (now - seen).total_seconds() <= self.config.workers.heartbeat_timeout_seconds
        )

    def _has_pending_work(self, online: list[dict[str, Any]]) -> bool:
        if self.orchestrator.paused:
            return False  # 暂停时不会派单，等同于没有待办。
        if getattr(self.orchestrator, "scan_status", "idle") == "running":
            return True  # 扫描中可能马上出现新任务。
        asr = self.database.asr_settings(self.config.asr)
        ensemble = asr.mode == "whisperjav" and asr.whisperjav_ensemble
        can_asr = any(
            asr.mode in worker.get("supported_asr_modes", [])
            and (not ensemble or worker.get("supports_whisperjav_ensemble"))
            for worker in online
        )
        if can_asr and self.database.pending_asr_jobs():
            return True
        models = {
            model
            for worker in online
            if worker.get("supports_restore")
            for model in worker.get("restore_models", [])
        }
        return any(
            model in models or (is_ltx_model(model) and "ltx" in models)
            for model in self.database.pending_restore_models()
        )

    async def _evaluate(
        self, binding: dict[str, Any], settings: CloudSettings, members: list[dict[str, Any]]
    ) -> None:
        """按云主机在平台上的状态和机器的忙闲推进一台绑定：监视 → 停止派单 → 关机。"""
        provider_id = binding["provider"]
        provider = self.provider(provider_id)
        cached = self._cache.get(provider_id)
        if provider is None or cached is None or cached[1] is None:
            return  # 看不到实例列表时不做任何决定。
        now = datetime.now(UTC)
        online = [worker for worker in members if self.worker_online(worker, now)]
        instance = self.cached_instance(provider_id, binding["instance_id"])
        title = binding["instance_title"] or binding["instance_id"][:8]
        where = msg("cloud.service.where", title=title, machine=binding['machine'], platform=self.provider_label(provider_id))

        if instance is None:
            self._on_missing_instance(binding, where)
            return
        if instance.status == "stopped":
            self._on_stopped(binding, online, now, title, where)
            return
        self._warned.discard(f"mismatch:{binding['machine']}:{binding['instance_id']}")
        if instance.status in {"starting", "unknown"}:
            if binding["state"] == "stopped":
                self.database.update_cloud_binding(binding["machine"], state="watching")
            return
        if instance.status == "stopping":
            if binding["state"] != "stopping":
                self.database.update_cloud_binding(
                    binding["machine"],
                    state="stopping",
                    stop_requested_at=binding["stop_requested_at"] or utc_now_iso(),
                )
            return
        await self._on_running(binding, provider, settings, members, online, now, where)

    def _set_watching(self, binding: dict[str, Any], *, reset_attempts: bool = True) -> None:
        """回到监视状态并清空空闲计时（已经是这样时不写数据库）。"""
        fields: dict[str, Any] = {"state": "watching", "idle_since": None, "drain_started_at": None}
        if reset_attempts:
            fields.update(attempts=0, next_attempt_at=None)
        if any(binding.get(key) != value for key, value in fields.items()):
            self.database.update_cloud_binding(binding["machine"], **fields)

    def _on_missing_instance(self, binding: dict[str, Any], where: str) -> None:
        message = msg("cloud.service.bindingGone")
        if binding["last_error"] != message:
            self.database.update_cloud_binding(
                binding["machine"],
                state="watching",
                idle_since=None,
                drain_started_at=None,
                last_error=message,
            )
            self.notify("warning", msg("cloud.service.bindingGoneTitle"), msg("launch.whereMessage", where=where, message=message))

    def _on_stopped(
        self,
        binding: dict[str, Any],
        online: list[dict[str, Any]],
        now: datetime,
        title: str,
        where: str,
    ) -> None:
        machine = binding["machine"]
        stopped_at = _parse(binding["stopped_at"])
        if binding["state"] != "stopped":
            if binding["state"] == "stopping":
                self.notify("success", msg("cloud.service.stoppedTitle"), msg("cloud.service.stopped", where=where))
            stopped_at = now
            self.database.update_cloud_binding(
                machine,
                state="stopped",
                stopped_at=now.isoformat(),
                idle_since=None,
                drain_started_at=None,
                stop_requested_at=None,
                last_error=None,
                attempts=0,
                next_attempt_at=None,
                backstop_minutes=None,
            )
        # 关机后旧心跳还要过一会儿才超时；只有关机之后仍有新心跳，才说明 Worker 不在这台实例上。
        grace = timedelta(seconds=MISMATCH_GRACE_SECONDS)
        if stopped_at and any(
            (_parse(str(worker.get("last_seen_at") or "")) or now) > stopped_at + grace
            for worker in online
        ):
            self._warn_once(
                f"mismatch:{machine}:{binding['instance_id']}",
                msg("cloud.service.mismatchTitle"),
                msg("cloud.service.mismatch", machine=machine, title=title),
            )

    async def _on_running(
        self,
        binding: dict[str, Any],
        provider: type[CloudProvider],
        settings: CloudSettings,
        members: list[dict[str, Any]],
        online: list[dict[str, Any]],
        now: datetime,
        where: str,
    ) -> None:
        machine = binding["machine"]
        update = self.database.update_cloud_binding
        if binding["state"] == "stopped":
            binding = {**binding, "state": "watching"}
            update(machine, state="watching", last_error=None)
        if binding["state"] == "stopping":
            requested = _parse(binding["stop_requested_at"])
            if requested is None or (now - requested).total_seconds() > STOP_TIMEOUT_SECONDS:
                message = (
                    msg("cloud.service.stopTimeout", minutes=STOP_TIMEOUT_SECONDS // 60)
                )
                update(machine, state="failed", last_error=message, next_attempt_at=now.isoformat())
                self.notify("error", msg("cloud.service.stopTimeoutTitle"), msg("launch.whereMessage", where=where, message=message))
            return

        provider_id, instance_id = binding["provider"], binding["instance_id"]
        deploying = self.database.cloud_instance_deploying(provider_id, instance_id)
        await self._apply_backstop(binding, provider, settings, deploying=deploying)
        # 字幕服务可能已上线并跑完任务，但同机影片修复仍在安装/下载模型。
        # 部署覆盖整台实例；await 期间也可能启动重新部署，必须再核对一次。
        if deploying or self.database.cloud_instance_deploying(provider_id, instance_id):
            self._set_watching(binding)
            return
        if binding["shutdown_mode"] == "off":
            self._set_watching(binding)
            return
        if not online:
            # 实例在运行但 Worker 不在线：可能正在部署或进程崩溃，不自动关机，由平台空闲关机兜底。
            self._set_watching(binding, reset_attempts=False)
            return
        busy = any(worker.get("current_job_id") for worker in online) or self.database.machine_busy(
            [worker["name"] for worker in members]
        )
        if busy or self._has_pending_work(online):
            if binding["state"] == "draining":
                logger.info("%s 出现新任务，取消关机。", where)
            self._set_watching(binding)
            return
        await self._advance_idle(binding, provider, settings, now, where)

    async def _advance_idle(
        self,
        binding: dict[str, Any],
        provider: type[CloudProvider],
        settings: CloudSettings,
        now: datetime,
        where: str,
    ) -> None:
        """机器空闲：开始计时 → 超过空闲时间后停止派单 → 确认没有任务后关机（失败时按时重试）。"""
        machine = binding["machine"]
        update = self.database.update_cloud_binding
        idle_since = _parse(binding["idle_since"])
        if idle_since is None:
            update(machine, idle_since=now.isoformat())
            return
        if binding["state"] == "watching":
            if now - idle_since >= timedelta(minutes=settings.idle_minutes):
                update(machine, state="draining", drain_started_at=now.isoformat())
                logger.info("%s 已空闲 %d 分钟，停止派单，准备关机。", where, settings.idle_minutes)
            return
        if binding["state"] == "failed":
            retry_at = _parse(binding["next_attempt_at"])
            if retry_at is None or now >= retry_at:
                update(machine, state="draining", drain_started_at=now.isoformat())
            return
        if binding["state"] == "draining":
            drain_started = _parse(binding["drain_started_at"]) or now
            if (now - drain_started).total_seconds() >= DRAIN_SECONDS:
                minutes = int((now - idle_since).total_seconds() // 60)
                await self._shutdown(binding, provider, where, msg("cloud.service.idleReason", minutes=minutes))

    async def _shutdown(
        self, binding: dict[str, Any], provider: type[CloudProvider], where: str, reason: str
    ) -> None:
        if self.database.cloud_instance_deploying(binding["provider"], binding["instance_id"]):
            return
        machine = binding["machine"]
        if CAP_SHUTDOWN not in provider.capabilities:
            self.database.update_cloud_binding(
                machine, state="watching", shutdown_mode="off", last_error=msg("cloud.service.pluginNoShutdown")
            )
            self.notify(
                "error",
                msg("cloud.service.noAutoShutdownTitle"),
                msg("cloud.service.noAutoShutdown", where=where, label=provider.label),
            )
            return
        try:
            async with self.client(binding["provider"]) as client:
                await client.shutdown(binding["instance_id"])
        except CloudError as exc:
            attempts = int(binding["attempts"] or 0) + 1
            delay = (
                30
                if exc.kind in {CloudErrorKind.AUTH, CloudErrorKind.FATAL}
                else RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)]
            )
            retry_at = datetime.now(UTC) + timedelta(minutes=delay)
            self.database.update_cloud_binding(
                machine,
                state="failed",
                last_error=exc.message,
                attempts=attempts,
                next_attempt_at=retry_at.isoformat(),
            )
            if attempts == 1 or exc.kind == CloudErrorKind.AUTH:
                self.notify(
                    "error", msg("cloud.service.shutdownFailedTitle"), msg("cloud.service.shutdownFailed", where=where, message=exc.message, delay=delay)
                )
            return
        self.invalidate(binding["provider"])
        self.database.update_cloud_binding(
            machine,
            state="stopping",
            stop_requested_at=utc_now_iso(),
            last_error=None,
            attempts=0,
            next_attempt_at=None,
        )
        self.notify("info", msg("cloud.service.shuttingDownTitle"), msg("cloud.service.shuttingDown", where=where, reason=reason))

    async def _apply_backstop(
        self, binding: dict[str, Any], provider: type[CloudProvider], settings: CloudSettings,
        *, deploying: bool = False,
    ) -> None:
        if CAP_IDLE_CLOSE not in provider.capabilities or not provider.idle_close_choices:
            return
        if deploying or binding["shutdown_mode"] == "off" or not settings.backstop_minutes:
            desired = 0
        else:
            choices = sorted(provider.idle_close_choices)
            desired = next(
                (choice for choice in choices if choice >= settings.backstop_minutes), choices[-1]
            )
        applied = binding["backstop_minutes"]
        if applied == desired or (applied is None and desired == 0 and not deploying):
            return
        machine = binding["machine"]
        previous_desired, tried_at = self._backstop_tried.get(machine, (-1, float("-inf")))
        # 部署开始/结束时立即切换；同一目标失败后仍按原来的间隔重试。
        if previous_desired == desired and time.monotonic() - tried_at < BACKSTOP_RETRY_SECONDS:
            return
        self._backstop_tried[machine] = (desired, time.monotonic())
        try:
            async with self.client(binding["provider"]) as client:
                await client.set_idle_close(binding["instance_id"], desired)
        except CloudError as exc:
            logger.warning("设置 %s 的平台空闲关机失败：%s", machine, exc.message)
            self.database.update_cloud_binding(
                machine, last_error=msg("cloud.service.backstopFailed", message=exc.message)
            )
            return
        self.database.update_cloud_binding(machine, backstop_minutes=desired)
        logger.info(
            "%s 的平台空闲关机已设为 %s。", machine, f"{desired} 分钟" if desired else "关闭"
        )
