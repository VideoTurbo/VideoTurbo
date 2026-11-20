"""插件接口：按地址或本地上传安装 / 更新 / 卸载插件，插件设置，以及媒体服务器、通知的配置。

插件的每次安装、更新、卸载都写入“提醒”（会按通知配置推送出去），内容含来源（地址或上传的文件名）和 SHA-256，便于事后核对；
重新下载的内容与确认的 SHA-256 不一致时同样提醒。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..i18n import msg
from ..media_sources.base import MediaSourcePlugin
from ..plugins import installer, registry, repositories
from ..plugins.base import PluginCallError, PluginConfigError, PluginInfo, describe_error
from ..plugins.connections import Connections
from ..plugins.settings import PluginSettings, data_key
from .context import app_state
from .integrations import ConnectionStatus, NoticeService

router = APIRouter()

TEST_TIMEOUT_SECONDS = 30


class PluginPreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class PluginConfirmation(BaseModel):
    # 用户核对过的源码 SHA-256：主控重新下载后内容一致才执行。
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    # 用户核对过的远程图标 SHA-256；为空表示没有远程图标（或预览时图标下载失败），不保存图标。
    icon_sha256: str = Field(default="", pattern=r"^([0-9a-fA-F]{64})?$")


class PluginInstallRequest(PluginPreviewRequest, PluginConfirmation):
    pass


class PluginUploadPreview(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    # 大小按 UTF-8 字节数在 installer 中检查（上限 1 MB）。
    source: str = Field(min_length=1, max_length=installer.MAX_SOURCE_BYTES)


class PluginUploadInstall(PluginUploadPreview, PluginConfirmation):
    pass


# 表单提交的值：文本、数字、开关；None 表示清空。
FormValue = bool | int | float | str | None


class ConnectionValues(BaseModel):
    # 省略的字段保留原值；密钥字段提交空字符串也表示保留。
    values: dict[str, FormValue] = Field(default_factory=dict)


class ConnectionUpdate(ConnectionValues):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    # 仅通知配置使用：达到该级别的提醒才推送。
    min_level: Literal["info", "warning", "error"] | None = None


class ConnectionCreate(ConnectionUpdate):
    type: str = Field(min_length=1, max_length=64)


def _notices(request: Request) -> NoticeService:
    return app_state(request).notices


def _transport(request: Request) -> Any:
    return app_state(request).plugin_transport


def _plugin_error(
    request: Request, exc: installer.PluginError, url: str, sha256: str = ""
) -> HTTPException:
    # 本地上传时两次提交的是浏览器里同一份文件，不一致只会是请求被改动，不必写入提醒。
    if exc.mismatch and url:
        _notices(request).notify(
            "warning",
            msg("plugins.audit.changedTitle"),
            msg("plugins.audit.changed", url=url, sha256=sha256.lower()),
        )
    return HTTPException(status_code=409 if exc.conflict else 400, detail=exc.message)


def _audit(request: Request, action: str, plugin: dict[str, Any]) -> None:
    kinds = plugin.get("kinds") or ()
    ids = [kind["id"] if isinstance(kind, dict) else kind for kind in kinds]
    lines = [
        msg("plugins.audit.kinds", kinds=[kind['label'] for kind in installer.kinds_of(ids)]),
        msg("plugins.audit.source", url=plugin['url'])
        if plugin.get("url")
        else msg("plugins.audit.sourceUpload", filename=plugin.get('filename', '')),
    ]
    if plugin.get("version"):
        lines.append(msg("plugins.audit.version", version=plugin['version']))
    if plugin.get("sha256"):
        lines.append(msg("plugins.audit.sha", sha256=plugin['sha256']))
    if plugin.get("icon_url"):
        lines.append(msg("plugins.audit.icon", url=plugin['icon_url'], sha256=plugin.get('icon_sha256', '')))
    label = plugin.get("label") or plugin.get("id", "")
    _notices(request).notify("info", msg("plugins.audit.title", action=action, label=label), "\n".join(lines))


def _settings(request: Request) -> PluginSettings:
    return PluginSettings(app_state(request).database)


def _configurable(plugin_id: str) -> list[type[PluginInfo]]:
    """插件中声明了插件设置的插件类（同一文件里的共用一份设置）。"""
    return [
        plugin
        for _, plugin in installer.members(plugin_id)
        if issubclass(plugin, PluginInfo) and plugin.settings_fields
    ]


def _with_settings(request: Request, item: dict[str, Any]) -> dict[str, Any]:
    """插件列表项补上：有没有插件设置、必填设置是否还没填写；媒体库来源插件的使用数加上用它的媒体库数量。"""
    configurable = _configurable(item["id"])
    item = {
        **item,
        "has_settings": bool(configurable),
        "settings_missing": bool(configurable)
        and bool(_settings(request).missing(configurable[0])),
    }
    sources = {
        member["id"] for member in item.get("members", ()) if member["kind"] == "media_source"
    }
    if sources:
        item["usage"] = item.get("usage", 0) + sum(
            1 for library in app_state(request).config.media_libraries if library.source in sources
        )
    return item


@router.get("/api/plugins")
def list_plugins(request: Request) -> dict:
    return {
        "safe_mode": installer.safe_mode(),
        "kinds": [{"id": kind.id, "label": kind.label} for kind in registry.KINDS.values()],
        "plugins": [
            _with_settings(request, item)
            for item in installer.describe_all(app_state(request).database)
        ],
    }


def _settings_plugins(plugin_id: str) -> list[type[PluginInfo]]:
    plugins = _configurable(plugin_id)
    if not plugins:
        raise HTTPException(status_code=404, detail=msg("plugins.api.noSettings"))
    return plugins


def _media_source(plugins: list[type[PluginInfo]]) -> type[MediaSourcePlugin] | None:
    return next((plugin for plugin in plugins if issubclass(plugin, MediaSourcePlugin)), None)


@router.get("/api/plugins/{plugin_id}/settings")
def get_plugin_settings(request: Request, plugin_id: str) -> dict:
    plugins = _settings_plugins(plugin_id)
    info = next(
        (
            item
            for item in installer.describe(app_state(request).database)
            if item["id"] == plugin_id
        ),
        None,
    )
    file_info = (
        {
            "id": plugin_id,
            "label": info["label"],
            "homepage": info["homepage"],
            "icon": info["icon"],
        }
        if info
        else {}
    )
    return {
        **plugins[0].describe(),
        **file_info,
        **_settings(request).describe(plugins[0]),
        # 媒体库来源插件只有插件设置，可以直接测试；其他插件在各自的配置里测试。
        "testable": _media_source(plugins) is not None,
    }


@router.put("/api/plugins/{plugin_id}/settings")
def save_plugin_settings(request: Request, plugin_id: str, payload: ConnectionValues) -> dict:
    plugins = _settings_plugins(plugin_id)
    store = _settings(request)
    try:
        settings = store.merged(plugins, payload.values)
    except PluginConfigError as exc:
        raise _config_error(exc) from exc
    return {"saved": True, "changed": store.save(plugins[0], settings)}


@router.post("/api/plugins/{plugin_id}/settings/test")
async def test_plugin_settings(request: Request, plugin_id: str, payload: ConnectionValues) -> dict:
    """用表单里的设置（未保存）测试媒体库来源插件。测试时插件保存的状态不写入数据库。"""
    plugins = _settings_plugins(plugin_id)
    plugin = _media_source(plugins)
    if plugin is None:
        raise HTTPException(
            status_code=400, detail=msg("plugins.api.testInConnection")
        )
    store = _settings(request)
    try:
        settings = store.merged(plugins, payload.values)
    except PluginConfigError as exc:
        raise _config_error(exc) from exc
    # 设置没变时带上已保存的状态（例如登录令牌），变了就从空状态开始，和保存后的行为一致。
    state = store.state(data_key(plugin)) if settings == store.settings(plugin) else {}
    return await _run_test(plugin, {}, request, settings=settings, state=state)


@router.post("/api/plugins/preview")
async def preview_plugin(request: Request, payload: PluginPreviewRequest) -> dict:
    """下载并静态检查，不执行；返回 SHA-256 供用户核对。"""
    try:
        return await installer.preview(payload.url, _transport(request))
    except installer.PluginError as exc:
        raise _plugin_error(request, exc, payload.url) from exc


@router.post("/api/plugins")
async def install_plugin(request: Request, payload: PluginInstallRequest) -> dict:
    try:
        result = await installer.install(
            app_state(request).database,
            payload.url,
            payload.sha256,
            _transport(request),
            icon_sha256=payload.icon_sha256,
        )
    except installer.PluginError as exc:
        raise _plugin_error(request, exc, payload.url, payload.sha256) from exc
    _audit(request, msg("plugins.audit.installed"), result)
    return result


@router.post("/api/plugins/upload/preview")
async def preview_uploaded_plugin(request: Request, payload: PluginUploadPreview) -> dict:
    """静态检查上传的插件文件，不执行；返回 SHA-256 供用户核对。"""
    try:
        return await installer.preview_upload(payload.filename, payload.source, _transport(request))
    except installer.PluginError as exc:
        raise HTTPException(status_code=409 if exc.conflict else 400, detail=exc.message) from exc


@router.post("/api/plugins/upload")
async def install_uploaded_plugin(request: Request, payload: PluginUploadInstall) -> dict:
    """安装上传的插件文件；已从本地上传安装的同一插件会被替换。"""
    try:
        result = await installer.install_upload(
            app_state(request).database,
            payload.filename,
            payload.source,
            payload.sha256,
            _transport(request),
            icon_sha256=payload.icon_sha256,
        )
    except installer.PluginError as exc:
        raise _plugin_error(request, exc, "", payload.sha256) from exc
    if result["changed"]:
        _audit(request, msg("plugins.audit.updated") if result["updated"] else msg("plugins.audit.installed"), result)
    return result


class RepositoryRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


@router.get("/api/plugin-repositories")
async def list_repositories(request: Request) -> dict:
    """已添加的插件仓库和其中的插件（每次重新下载仓库的插件列表）。"""
    return {
        "repositories": await repositories.catalog(app_state(request).database, _transport(request))
    }


@router.post("/api/plugin-repositories")
async def add_repository(request: Request, payload: RepositoryRequest) -> dict:
    try:
        return await repositories.add(app_state(request).database, payload.url, _transport(request))
    except installer.PluginError as exc:
        raise HTTPException(status_code=409 if exc.conflict else 400, detail=exc.message) from exc


@router.delete("/api/plugin-repositories/{repository_id}")
def remove_repository(request: Request, repository_id: str) -> dict:
    try:
        repositories.remove(app_state(request).database, repository_id)
    except installer.PluginError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return {"removed": True}


def _installed_url(request: Request, plugin_id: str) -> str:
    return app_state(request).database.installed_plugins().get(plugin_id, {}).get("url", "")


@router.post("/api/plugins/{plugin_id}/update/check")
async def check_plugin_update(request: Request, plugin_id: str) -> dict:
    """重新下载并静态检查，不执行；有变化时返回新的 SHA-256 供用户核对。"""
    try:
        return await installer.check_update(
            app_state(request).database, plugin_id, _transport(request)
        )
    except installer.PluginError as exc:
        raise _plugin_error(request, exc, _installed_url(request, plugin_id)) from exc


@router.post("/api/plugins/{plugin_id}/update")
async def update_plugin(request: Request, plugin_id: str, payload: PluginConfirmation) -> dict:
    try:
        result = await installer.update(
            app_state(request).database,
            plugin_id,
            payload.sha256,
            _transport(request),
            icon_sha256=payload.icon_sha256,
        )
    except installer.PluginError as exc:
        raise _plugin_error(
            request, exc, _installed_url(request, plugin_id), payload.sha256
        ) from exc
    if result["changed"]:
        _audit(request, msg("plugins.audit.updated"), result)
    return result


@router.delete("/api/plugins/{plugin_id}")
def uninstall_plugin(request: Request, plugin_id: str) -> dict:
    database = app_state(request).database
    record = database.installed_plugins().get(plugin_id, {})
    clouds = {member for kind, member in record.get("members") or () if kind == "cloud"}
    if clouds:
        profiles = database.cloud_profiles()
        if any(
            profiles.get(database.get_cloud_launch(launch_id)["provider"], {}).get("type") in clouds
            for launch_id in database.active_cloud_launch_ids()
        ):
            raise HTTPException(
                status_code=409, detail=msg("plugins.api.busy")
            )
    label = next(
        (item["label"] for item in installer.describe(database) if item["id"] == plugin_id),
        plugin_id,
    )
    try:
        removed = installer.uninstall(database, plugin_id)
    except installer.PluginError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    _audit(request, msg("plugins.audit.uninstalled"), {**removed, "id": plugin_id, "label": label, "sha256": ""})
    return {"removed": True}


def _config_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=msg("plugins.api.notFound"))
    return HTTPException(status_code=400, detail=describe_error(exc))


def _test_source(
    plugin: type[MediaSourcePlugin], settings: dict[str, Any], state: dict[str, Any]
) -> str:
    with plugin(settings, state=state, timeout=TEST_TIMEOUT_SECONDS) as source:
        return source.test()


async def _run_test(
    plugin: type,
    values: dict[str, Any],
    request: Request,
    *,
    settings: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> dict:
    """测试插件。媒体库来源插件用 settings 和 state；其他插件用这份配置 values 和已保存的插件设置。"""
    try:
        if issubclass(plugin, MediaSourcePlugin):
            # 媒体库来源插件的方法是同步的，放到线程里调用。
            message = await asyncio.wait_for(
                asyncio.to_thread(_test_source, plugin, settings or {}, state or {}),
                TEST_TIMEOUT_SECONDS + 5,
            )
        else:
            async with plugin(
                values, **_settings(request).bind(plugin), transport=_transport(request)
            ) as client:
                message = await asyncio.wait_for(client.test(), TEST_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=msg("plugins.api.testFailed", error=describe_error(exc))) from exc
    return {"ok": True, "message": message}


def _register_connections(path: str, kind: str, status: str) -> None:
    """为一类插件注册配置接口。status 是 app.state 上保存调用状态的服务名。"""

    def connections(request: Request) -> Connections:
        return Connections(app_state(request).database, kind)

    def statuses(request: Request) -> ConnectionStatus:
        return getattr(request.app.state, status).status

    @router.get(f"/api/{path}/types", name=f"{kind}_types")
    def list_types() -> list[dict]:
        return [plugin.describe() for plugin in registry.providers(kind).values()]

    @router.post(f"/api/{path}/types/{{plugin_type}}/test", name=f"{kind}_type_test")
    async def test_type(request: Request, plugin_type: str, payload: ConnectionValues) -> dict:
        plugin = registry.get(kind, plugin_type)
        if plugin is None:
            raise HTTPException(status_code=404, detail=msg("plugins.api.unknown"))
        try:
            values = plugin.merge_values({}, payload.values)
        except PluginConfigError as exc:
            raise _config_error(exc) from exc
        return await _run_test(plugin, values, request)

    @router.get(f"/api/{path}", name=f"{kind}_list")
    def list_connections(request: Request) -> list[dict]:
        state = statuses(request)
        return [{**item, **state.get(item["id"])} for item in connections(request).describe()]

    @router.post(f"/api/{path}", name=f"{kind}_create")
    def create_connection(request: Request, payload: ConnectionCreate) -> dict:
        store = connections(request)
        try:
            _, record = store.prepare_new(
                payload.type, payload.name, payload.enabled, payload.values, payload.min_level
            )
        except (KeyError, PluginConfigError) as exc:
            raise _config_error(exc) from exc
        return {"id": store.create(record), "saved": True}

    @router.put(f"/api/{path}/{{connection_id}}", name=f"{kind}_update")
    def update_connection(request: Request, connection_id: str, payload: ConnectionUpdate) -> dict:
        store = connections(request)
        try:
            _, record = store.prepare_update(
                connection_id, payload.name, payload.enabled, payload.values, payload.min_level
            )
        except (KeyError, PluginConfigError) as exc:
            raise _config_error(exc) from exc
        store.save(connection_id, record)
        return {"saved": True}

    @router.delete(f"/api/{path}/{{connection_id}}", name=f"{kind}_delete")
    def delete_connection(request: Request, connection_id: str) -> dict:
        try:
            connections(request).delete(connection_id)
        except KeyError as exc:
            raise _config_error(exc) from exc
        return {"removed": True}

    @router.post(f"/api/{path}/{{connection_id}}/test", name=f"{kind}_test")
    async def test_connection(
        request: Request, connection_id: str, payload: ConnectionValues
    ) -> dict:
        store = connections(request)
        try:
            saved = store.record(connection_id)
            plugin = store.plugin(saved)
            if plugin is None:
                raise PluginCallError(msg("plugins.api.notInstalled"))
            values = plugin.merge_values(saved.get("values") or {}, payload.values)
        except (KeyError, PluginConfigError, PluginCallError) as exc:
            raise _config_error(exc) from exc
        return await _run_test(plugin, values, request)


_register_connections("media-servers", "media_server", "media_refresher")
_register_connections("notifiers", "notifier", "notices")
