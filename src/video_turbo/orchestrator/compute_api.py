"""云主机页：已接入的机器、云平台账号、云主机、绑定和开机记录。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import Response

from ..cloud.base import CAP_LAUNCH, CAP_SHUTDOWN, CloudError, CloudErrorKind
from ..cloud_models import (
    CloudBindingRequest,
    CloudProfileCreate,
    CloudProfileUpdate,
    CloudSettings,
    CloudSettingsUpdate,
    CredentialUpdate,
    LaunchRequest,
    SaveImageRequest,
    ShutdownModeUpdate,
    normalize_model_dir,
)
from ..database import Database
from ..i18n import msg, render, t
from ..plugins import registry
from .cloud_service import CloudService
from .context import app_state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.delete("/api/workers/{name}")
def delete_worker(request: Request, name: str) -> dict:
    database: Database = app_state(request).database
    worker = database.get_worker(name)
    if worker is None:
        raise HTTPException(status_code=404, detail=msg("api.compute.machineNotFound"))
    try:
        seen = datetime.fromisoformat(str(worker["last_seen_at"]))
        age = (datetime.now(UTC) - seen).total_seconds()
    except (TypeError, ValueError):
        age = float("inf")
    online = worker.get("status") in {"online", "busy"} and (
        age <= app_state(request).config.workers.heartbeat_timeout_seconds
    )
    if online:
        raise HTTPException(status_code=409, detail=msg("api.compute.machineOnline"))
    if not database.delete_worker(name):
        raise HTTPException(status_code=409, detail=msg("api.compute.machineBusy"))
    return {"deleted": True, "name": name}


def _cloud_http_error(exc: CloudError) -> HTTPException:
    status = 400 if exc.kind in {CloudErrorKind.AUTH, CloudErrorKind.FATAL} else 502
    return HTTPException(status_code=status, detail=exc.message)


def _cloud(request: Request) -> CloudService:
    return app_state(request).cloud


def _known_provider(request: Request, provider_id: str) -> None:
    if not any(item["id"] == provider_id for item in _cloud(request).describe_providers()):
        raise HTTPException(status_code=404, detail=msg("api.compute.unknownProvider"))


@router.get("/api/cloud/providers")
def cloud_providers(request: Request) -> list[dict]:
    return _cloud(request).describe_providers()


@router.get("/api/cloud/provider-types")
def cloud_provider_types() -> list[dict]:
    return [provider.describe() for provider in registry.providers("cloud").values()]


@router.get("/api/cloud/orphan-providers")
def cloud_orphan_providers(request: Request) -> list[dict]:
    """插件已卸载或加载失败的平台配置。"""
    return _cloud(request).orphan_profiles()


@router.post("/api/cloud/provider-types/{provider_type}/test")
async def test_cloud_provider_type(
    request: Request, provider_type: str, payload: CredentialUpdate
) -> dict:
    provider = registry.get("cloud", provider_type)
    if provider is None:
        raise HTTPException(status_code=404, detail=msg("api.compute.unknownType"))
    try:
        async with provider(
            provider.merge_credentials({}, payload.values), transport=_cloud(request).transport
        ) as client:
            message = await client.test()
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    return {"ok": True, "message": message}


def _cloud_profile_name(service: CloudService, name: str, profile_id: str = "") -> str:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail=msg("api.compute.nameRequired"))
    if any(
        item["id"] != profile_id and item["label"] == name
        for item in service.describe_providers()
    ):
        raise HTTPException(status_code=409, detail=msg("api.compute.nameTaken"))
    return name


def _cloud_profile(provider_type: str, name: str, model_dir: str) -> dict[str, str]:
    try:
        model_dir = normalize_model_dir(model_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    profile = {"type": provider_type, "name": name}
    if model_dir:
        profile["model_dir"] = model_dir
    return profile


@router.post("/api/cloud/providers")
async def create_cloud_provider(request: Request, payload: CloudProfileCreate) -> dict:
    service = _cloud(request)
    provider = registry.get("cloud", payload.type)
    if provider is None:
        raise HTTPException(status_code=404, detail=msg("api.compute.unknownType"))
    profile = _cloud_profile(payload.type, _cloud_profile_name(service, payload.name), payload.model_dir)
    try:
        async with provider(
            provider.merge_credentials({}, payload.values), transport=service.transport
        ) as client:
            authorized = await client.prepare_credentials()
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    profile_id = "cloud-" + uuid.uuid4().hex[:12]
    service.database.set_cloud_profile(profile_id, profile)
    service.save_credentials(profile_id, authorized)
    return {"id": profile_id, "saved": True}


@router.put("/api/cloud/providers/{provider_id}")
async def update_cloud_provider(
    request: Request, provider_id: str, payload: CloudProfileUpdate
) -> dict:
    _known_provider(request, provider_id)
    service = _cloud(request)
    provider = service.provider(provider_id)
    profile = _cloud_profile(
        provider.id, _cloud_profile_name(service, payload.name, provider_id), payload.model_dir
    )
    try:
        merged = service.merge_credentials(provider_id, payload.values)
        async with service.client(provider_id, merged) as client:
            authorized = await client.prepare_credentials()
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    service.database.set_cloud_profile(provider_id, profile)
    service.save_credentials(provider_id, authorized)
    return {"saved": True}


@router.delete("/api/cloud/providers/{provider_id}")
def delete_cloud_provider(request: Request, provider_id: str) -> dict:
    service = _cloud(request)
    # 插件已卸载的配置不在平台列表中，但仍然可以删除。
    if provider_id not in service.database.cloud_profiles():
        raise HTTPException(status_code=404, detail=msg("api.compute.unknownProvider"))
    if any(
        service.database.get_cloud_launch(launch_id)["provider"] == provider_id
        for launch_id in service.database.active_cloud_launch_ids()
    ):
        raise HTTPException(
            status_code=409, detail=msg("api.compute.providerBusy")
        )
    service.save_credentials(provider_id, None)
    service.database.set_cloud_profile(provider_id, None)
    return {"removed": True}


@router.put("/api/cloud/providers/{provider_id}/credentials")
async def save_cloud_credentials(request: Request, provider_id: str, payload: CredentialUpdate) -> dict:
    _known_provider(request, provider_id)
    service = _cloud(request)
    try:
        merged = service.merge_credentials(provider_id, payload.values)
        async with service.client(provider_id, merged) as client:
            authorized = await client.prepare_credentials()
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    service.save_credentials(provider_id, authorized)
    return {"saved": True}


@router.delete("/api/cloud/providers/{provider_id}/credentials")
def clear_cloud_credentials(request: Request, provider_id: str) -> dict:
    _known_provider(request, provider_id)
    _cloud(request).save_credentials(provider_id, None)
    return {"cleared": True}


@router.post("/api/cloud/providers/{provider_id}/test")
async def test_cloud_credentials(request: Request, provider_id: str, payload: CredentialUpdate | None = None) -> dict:
    """用表单中的值（留空的字段沿用已保存的值）测试连接，不保存。"""
    _known_provider(request, provider_id)
    service = _cloud(request)
    try:
        merged = service.merge_credentials(provider_id, payload.values if payload else {})
        async with service.client(provider_id, merged) as client:
            message = await client.test()
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    return {"ok": True, "message": message}


@router.get("/api/cloud/instances")
async def cloud_instances(request: Request) -> list[dict]:
    return await _cloud(request).all_instances()


@router.post("/api/cloud/instances/{provider_id}/{instance_id}/shutdown")
async def shutdown_cloud_instance(request: Request, provider_id: str, instance_id: str) -> dict:
    _known_provider(request, provider_id)
    try:
        await _cloud(request).manual_shutdown(provider_id, instance_id)
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    return {"accepted": True}


@router.post("/api/cloud/instances/{provider_id}/{instance_id}/destroy")
async def destroy_cloud_instance(request: Request, provider_id: str, instance_id: str) -> dict:
    _known_provider(request, provider_id)
    try:
        await _cloud(request).manual_destroy(provider_id, instance_id)
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    return {"accepted": True}


@router.post("/api/cloud/instances/{provider_id}/{instance_id}/forget")
def forget_cloud_instance(request: Request, provider_id: str, instance_id: str) -> dict:
    _known_provider(request, provider_id)
    try:
        _cloud(request).manual_forget(provider_id, instance_id)
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    return {"removed": True}


@router.post("/api/cloud/instances/{provider_id}/{instance_id}/save-image")
async def save_cloud_instance_image(
    request: Request, provider_id: str, instance_id: str, payload: SaveImageRequest | None = None
) -> dict:
    _known_provider(request, provider_id)
    try:
        await _cloud(request).manual_save_image(
            provider_id, instance_id, payload.image_name if payload else ""
        )
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    return {"accepted": True}


def _cloud_settings_view(database: Database) -> dict:
    # Jasna 许可证码不回显，只返回是否已保存。
    return {
        **database.cloud_settings().model_dump(),
        "jasna_license_set": bool(database.jasna_license()),
    }


@router.get("/api/settings/cloud")
def get_cloud_settings(request: Request) -> dict:
    return _cloud_settings_view(app_state(request).database)


@router.put("/api/settings/cloud")
def update_cloud_settings(request: Request, payload: CloudSettingsUpdate) -> dict:
    database: Database = app_state(request).database
    settings = CloudSettings.model_validate(payload.model_dump(exclude={"jasna_license"}))
    database.set_cloud_settings(settings)
    if payload.jasna_license and payload.jasna_license.strip():
        database.set_jasna_license(payload.jasna_license)
    return _cloud_settings_view(database)


@router.delete("/api/settings/cloud/jasna-license")
def clear_jasna_license(request: Request) -> dict:
    app_state(request).database.set_jasna_license(None)
    return _cloud_settings_view(app_state(request).database)


@router.put("/api/cloud/bindings/{machine}")
async def bind_cloud_instance(request: Request, machine: str, payload: CloudBindingRequest) -> dict:
    _known_provider(request, payload.provider)
    service = _cloud(request)
    try:
        instances = await service.instances(payload.provider, max_age=0)
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    instance = next((item for item in instances if item.id == payload.instance_id), None)
    if instance is None:
        raise HTTPException(status_code=404, detail=msg("api.compute.instanceNotFound"))
    payload.instance_title = instance.title or payload.instance_title
    provider = service.provider(payload.provider)
    if provider and CAP_SHUTDOWN not in provider.capabilities:
        payload.shutdown_mode = "off"  # 平台插件不能关机
    try:
        binding = app_state(request).database.bind_cloud_instance(machine, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return binding


@router.put("/api/cloud/bindings/{machine}/shutdown-mode")
def update_cloud_shutdown_mode(request: Request, machine: str, payload: ShutdownModeUpdate) -> dict:
    database: Database = app_state(request).database
    binding = database.get_cloud_binding(machine)
    if binding is None:
        raise HTTPException(status_code=404, detail=msg("api.compute.notBound"))
    provider = _cloud(request).provider(binding["provider"])
    if payload.shutdown_mode != "off" and provider and CAP_SHUTDOWN not in provider.capabilities:
        raise HTTPException(
            status_code=400, detail=msg("api.compute.noShutdown", label=provider.label)
        )
    database.update_cloud_binding(machine, shutdown_mode=payload.shutdown_mode)
    return {"machine": machine, "shutdown_mode": payload.shutdown_mode}


@router.delete("/api/cloud/bindings/{machine}")
def unbind_cloud_instance(request: Request, machine: str) -> dict:
    if not app_state(request).database.unbind_cloud_instance(machine):
        raise HTTPException(status_code=404, detail=msg("api.compute.notBound"))
    return {"unbound": True}


def _launch_provider(request: Request, provider_id: str) -> None:
    _known_provider(request, provider_id)
    provider = next(i for i in _cloud(request).describe_providers() if i["id"] == provider_id)
    if CAP_LAUNCH not in provider["capabilities"]:
        raise HTTPException(status_code=400, detail=msg("api.compute.noLaunch", label=provider['label']))
    if not provider["configured"]:
        raise HTTPException(status_code=400, detail=msg("api.compute.noCredentials", label=provider['label']))


@router.get("/api/cloud/providers/{provider_id}/launch-options")
async def cloud_launch_options(request: Request, provider_id: str) -> dict:
    _launch_provider(request, provider_id)
    try:
        async with _cloud(request).client(provider_id) as client:
            gpus = await client.gpu_offers()
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    describe = next(i for i in _cloud(request).describe_providers() if i["id"] == provider_id)
    return {
        "methods": describe["launch_methods"],
        "capabilities": describe["capabilities"],
        "gpu_counts": describe["gpu_counts"],
        "gpus": [gpu.model_dump() for gpu in gpus],
    }


@router.get("/api/cloud/providers/{provider_id}/launch-targets")
async def cloud_launch_targets(
    request: Request,
    provider_id: str,
    method: str = Query(min_length=1, max_length=64),
    q: str = Query(default="", max_length=100),
) -> list[dict]:
    _launch_provider(request, provider_id)
    try:
        async with _cloud(request).client(provider_id) as client:
            targets = await client.launch_targets(method, q)
    except CloudError as exc:
        raise _cloud_http_error(exc) from exc
    return [target.model_dump() for target in targets]


@router.post("/api/cloud/launches")
async def create_cloud_launch(request: Request, payload: LaunchRequest) -> dict:
    _launch_provider(request, payload.provider)
    describe = next(i for i in _cloud(request).describe_providers() if i["id"] == payload.provider)
    if payload.method not in {method["id"] for method in describe["launch_methods"]}:
        raise HTTPException(status_code=400, detail=msg("api.compute.badMethod"))
    database: Database = app_state(request).database
    if CAP_SHUTDOWN not in describe["capabilities"]:
        payload.shutdown_mode = "off"  # 平台插件不能关机
    launch_id = await asyncio.to_thread(database.create_cloud_launch, payload)
    launch = await asyncio.to_thread(database.get_cloud_launch, launch_id, with_log=False)
    # 给已有实例开机或重新连接：它之前的创建任务不再保留。
    if instance_id := _cloud(request).launch_instance_id(launch):
        await asyncio.to_thread(
            _cloud(request).drop_instance_launches, payload.provider, instance_id, keep=launch_id
        )
    # 开机任务是 asyncio 任务，必须在事件循环线程中创建。
    app_state(request).cloud_launcher.start(launch_id)
    return launch


@router.get("/api/cloud/launches")
def list_cloud_launches(request: Request) -> list[dict]:
    return app_state(request).database.list_cloud_launches(50)


@router.get("/api/cloud/launches/{launch_id}")
def get_cloud_launch(request: Request, launch_id: str) -> dict:
    launch = app_state(request).database.get_cloud_launch(launch_id)
    if launch is None:
        raise HTTPException(status_code=404, detail=msg("api.launchNotFound"))
    return launch


@router.get("/api/cloud/launches/{launch_id}/log")
def download_cloud_launch_log(request: Request, launch_id: str) -> Response:
    """下载完整任务日志：数据库只留末尾供弹窗显示，完整内容在日志文件里。"""
    database: Database = app_state(request).database
    launch = database.get_cloud_launch(launch_id)
    if launch is None:
        raise HTTPException(status_code=404, detail=msg("api.launchNotFound"))
    path = database.cloud_launch_log_path(launch_id)
    try:
        body = path.read_bytes()
    except FileNotFoundError:
        body = launch["log"].encode("utf-8")
    # 下载的是纯文本，不经过 I18nMiddleware：这里直接按请求语言翻译。
    header = "\n".join(
        [
            t("api.launchLog.job", id=launch_id, provider=launch["provider"], gpu=launch["gpu_label"], count=launch["gpu_count"]),
            t("api.launchLog.state", state=launch["state"], message=launch["message"] or ""),
            t("api.launchLog.host", host=launch["instance_id"] or "-", machine=launch["machine"] or "-"),
            t("api.launchLog.times", created=launch["created_at"], updated=launch["updated_at"]),
            "",
            "",
        ]
    )
    body = render(body.decode("utf-8", errors="replace")).encode("utf-8")
    return Response(
        header.encode("utf-8") + body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="video-turbo-launch-{launch_id}.log"'},
    )


@router.post("/api/cloud/launches/{launch_id}/cancel")
async def cancel_cloud_launch(request: Request, launch_id: str) -> dict:
    if app_state(request).database.get_cloud_launch(launch_id, with_log=False) is None:
        raise HTTPException(status_code=404, detail=msg("api.launchNotFound"))
    await app_state(request).cloud_launcher.cancel(launch_id)
    return app_state(request).database.get_cloud_launch(launch_id, with_log=False)


@router.post("/api/cloud/launches/{launch_id}/redeploy")
async def redeploy_cloud_launch(
    request: Request, launch_id: str, forget_host_key: bool = False
) -> dict:
    launch = app_state(request).database.get_cloud_launch(launch_id, with_log=False)
    if launch is None:
        raise HTTPException(status_code=404, detail=msg("api.launchNotFound"))
    if launch["state"] not in {"deploy_failed", "nvenc_failed", "running", "cancelled"} or not launch["instance_id"]:
        raise HTTPException(status_code=409, detail=msg("api.compute.redeployRunningOnly"))
    if not launch["services"]:
        raise HTTPException(status_code=409, detail=msg("api.compute.nothingToInstall"))
    # forget_host_key：实例重装系统后主机密钥会变，由用户确认后忘记旧密钥，本次连接重新记录。
    app_state(request).cloud_launcher.redeploy(launch_id, forget_host_key=forget_host_key)
    return app_state(request).database.get_cloud_launch(launch_id, with_log=False)


@router.post("/api/cloud/launches/{launch_id}/continue-asr")
async def continue_cloud_launch_asr(request: Request, launch_id: str) -> dict:
    if app_state(request).database.get_cloud_launch(launch_id, with_log=False) is None:
        raise HTTPException(status_code=404, detail=msg("api.launchNotFound"))
    try:
        app_state(request).cloud_launcher.continue_asr(launch_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return app_state(request).database.get_cloud_launch(launch_id, with_log=False)


@router.delete("/api/cloud/launches/{launch_id}")
def delete_cloud_launch(request: Request, launch_id: str) -> dict:
    if not app_state(request).database.delete_cloud_launch(launch_id):
        raise HTTPException(status_code=409, detail=msg("api.compute.launchActive"))
    return {"deleted": True}
