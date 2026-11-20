"""控制台配置接口：翻译节点和运行设置，保存后立即热更新。媒体库见 libraries_api。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .. import languages
from ..config import AppConfig
from ..config_store import (
    LLM_KEY,
    MANAGED_SECTIONS,
    ConfigError,
    ConfigStore,
    describe_validation_error,
)
from ..i18n import msg
from ..llm_config import (
    POOL_LABELS,
    PROTOCOL_LABELS,
    LlmNode,
    LlmPool,
    LlmProtocol,
    LlmSettings,
    ReasoningEffort,
    VertexAuth,
)
from ..models import utc_now_iso
from ..vertex_auth import access_token
from .context import app_state

logger = logging.getLogger(__name__)
router = APIRouter()

MODEL_LIST_TIMEOUT_SECONDS = 20


def apply_runtime_config(app: Any, config: AppConfig) -> None:
    """把新配置切换到所有服务；进行中的翻译批次继续使用旧节点对象完成。"""
    app.state.config = config
    app.state.orchestrator.apply_config(config)
    for name in ("restore", "cloud", "posters"):
        service = getattr(app.state, name, None)
        if service is not None:
            service.config = config
    logging.getLogger().setLevel(getattr(logging, config.server.log_level.upper(), logging.INFO))


def _store(request: Request) -> ConfigStore:
    return app_state(request).config_store


def _lock(request: Request) -> asyncio.Lock:
    return app_state(request).config_lock


def _config_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=describe_validation_error(exc))
    return HTTPException(status_code=422, detail=str(exc))


async def _save_llm(request: Request, llm: LlmSettings) -> dict:
    try:
        config = await asyncio.to_thread(_store(request).save_llm, llm)
    except ConfigError as exc:
        raise _config_error(exc) from exc
    apply_runtime_config(request.app, config)
    return llm.public()


def _validated_llm(data: dict) -> LlmSettings:
    try:
        return LlmSettings.model_validate(data)
    except ValidationError as exc:
        raise _config_error(exc) from exc


@router.get("/api/config")
async def get_config(request: Request) -> dict:
    store = _store(request)
    config: AppConfig = app_state(request).config
    settings = await asyncio.to_thread(store.public_settings)
    llm = await asyncio.to_thread(store.llm)
    return {
        "settings": settings,
        "llm": llm.public(),
        "protocols": PROTOCOL_LABELS,
        "pools": POOL_LABELS,
        # 与容器部署绑定、只能在 docker-compose.yml 中修改的项，控制台只读展示。
        "deployment": {
            "port": config.server.port,
            "worker_port": config.server.worker_port,
            "restore_backup_dir": str(config.restore.backup_dir) if config.restore.backup_dir else None,
        },
        # 媒体库可选的源语言和目标语言，前端不硬编码语言列表。
        "languages": languages.public_table(),
    }


@router.put("/api/config/{section}")
async def update_config_section(request: Request, section: str) -> dict:
    # 媒体库逐个保存，见 libraries_api。
    if section not in MANAGED_SECTIONS or section == "media_libraries":
        raise HTTPException(status_code=404, detail=msg("api.config.badSection", section=section))
    try:
        value = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=msg("api.config.bodyNotJson")) from exc
    async with _lock(request):
        try:
            config = await asyncio.to_thread(_store(request).update_section, section, value)
        except ConfigError as exc:
            raise _config_error(exc) from exc
        apply_runtime_config(request.app, config)
    return {"saved": True, "section": section, "warnings": config.warnings()}


class NodeInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    pool: LlmPool = "translation"
    protocol: LlmProtocol = "openai_responses"
    base_url: str | None = None
    project: str | None = None
    auth: VertexAuth = "api_key"
    location: str | None = "global"
    # None 表示保留已保存的值（控制台不回显明文）；空字符串表示清空。
    api_key: str | None = None
    # 复制节点时沿用来源节点已保存的 Key（仅在 api_key 为 None 的新建请求中生效）。
    copy_key_from: str | None = None
    model: str = Field(min_length=1, max_length=200)
    concurrency: int = Field(default=2, ge=1, le=8)
    reasoning_effort: ReasoningEffort | None = None
    enabled: bool = True


KEY_REQUIRED_DETAIL = msg("api.config.keyRequired")


def _require_key_for_new_address(source: LlmNode | None, node: LlmNode, key_supplied: bool) -> None:
    """沿用已保存的 Key 时，请求地址必须与保存时一致：否则已保存的 Key 会被发往新的地址。"""
    if (
        source is not None
        and source.api_key
        and not key_supplied
        and source.resolved_base_url() != node.resolved_base_url()
    ):
        raise HTTPException(status_code=422, detail=KEY_REQUIRED_DETAIL)


def _node_from_input(payload: NodeInput, llm: LlmSettings, existing: LlmNode | None) -> LlmNode:
    key_source = existing or (llm.node(payload.copy_key_from) if payload.copy_key_from else None)
    api_key = payload.api_key if payload.api_key is not None else (key_source.api_key if key_source else "")
    fields = payload.model_dump(exclude={"api_key", "copy_key_from"})
    if existing:
        fields.update(id=existing.id)
    # 模型列表缓存只在接口地址不变时沿用。
    cache = existing or key_source
    try:
        node = LlmNode(**fields, api_key=api_key)
    except ValidationError as exc:
        raise _config_error(exc) from exc
    _require_key_for_new_address(key_source, node, payload.api_key is not None)
    if cache and cache.protocol == node.protocol and cache.resolved_base_url() == node.resolved_base_url():
        node.models, node.models_fetched_at = cache.models, cache.models_fetched_at
    return node


@router.post("/api/llm/nodes")
async def create_node(request: Request, payload: NodeInput) -> dict:
    async with _lock(request):
        llm = _store(request).llm()
        node = _node_from_input(payload, llm, None)
        data = llm.model_dump()
        data["nodes"].append(node.model_dump())
        result = await _save_llm(request, _validated_llm(data))
    return {**result, "id": node.id}


@router.put("/api/llm/nodes/{node_id}")
async def update_node(request: Request, node_id: str, payload: NodeInput) -> dict:
    async with _lock(request):
        llm = _store(request).llm()
        existing = llm.node(node_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=msg("api.config.modelNotFound"))
        node = _node_from_input(payload, llm, existing)
        data = llm.model_dump()
        data["nodes"] = [node.model_dump() if item["id"] == node_id else item for item in data["nodes"]]
        return await _save_llm(request, _validated_llm(data))


@router.delete("/api/llm/nodes/{node_id}")
async def delete_node(request: Request, node_id: str) -> dict:
    async with _lock(request):
        llm = _store(request).llm()
        if llm.node(node_id) is None:
            raise HTTPException(status_code=404, detail=msg("api.config.modelNotFound"))
        data = llm.model_dump()
        data["nodes"] = [item for item in data["nodes"] if item["id"] != node_id]
        return await _save_llm(request, _validated_llm(data))


class ModelListRequest(BaseModel):
    # 已保存的节点：未填写的字段和 Key 沿用保存值。新建节点时不传，全部使用表单里的值。
    node_id: str | None = None
    protocol: LlmProtocol | None = None
    base_url: str | None = None
    project: str | None = None
    auth: VertexAuth | None = None
    location: str | None = None
    api_key: str | None = None


async def fetch_model_ids(node: LlmNode) -> list[str]:
    """从节点接口拉取模型 ID 列表。"""
    api_key = node.api_key or None
    base_url = node.resolved_base_url().rstrip("/")
    async with httpx.AsyncClient(timeout=MODEL_LIST_TIMEOUT_SECONDS) as client:
        if node.protocol == "vertex_ai":
            # Vertex AI 的模型目录接口在 v1beta1，地址不含项目路径。
            host = base_url.split("/v1", 1)[0]
            response = await client.get(
                f"{host}/v1beta1/publishers/google/models",
                headers=(
                    {"Authorization": f"Bearer {await access_token(client, node.api_key)}"}
                    if node.uses_service_account and node.api_key
                    else {"x-goog-api-key": api_key or ""}
                ),
                params={"pageSize": 200},
            )
        else:
            response = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:300]
        raise ValueError(msg("api.config.httpError", status_code=response.status_code, detail=detail or msg('api.config.noContent')))
    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError(msg("api.config.notModelList")) from exc
    items: list[Any] = []
    if isinstance(body, dict):
        for key in ("data", "models", "publisherModels"):
            if isinstance(body.get(key), list):
                items = body[key]
                break
    elif isinstance(body, list):
        items = body
    ids: list[str] = []
    for item in items:
        value = item if isinstance(item, str) else (item.get("id") or item.get("name") if isinstance(item, dict) else None)
        if not value:
            continue
        value = str(value).removeprefix("models/").rsplit("/models/", 1)[-1]
        if value not in ids:
            ids.append(value)
    if not ids:
        raise ValueError(msg("api.config.noModelList"))
    return sorted(ids)


@router.post("/api/llm/models")
async def list_models(request: Request, payload: ModelListRequest) -> dict:
    store = _store(request)
    saved = store.llm().node(payload.node_id) if payload.node_id else None
    if payload.node_id and saved is None:
        raise HTTPException(status_code=404, detail=msg("api.config.modelNotFound"))
    # 只用于拼接请求地址，名称和模型是占位值。
    fields = saved.model_dump() if saved else {"name": "临时", "model": "临时"}
    # 表单里新填的凭据优先；没填时沿用已保存的（地址变化时必须重新填写 Key）。
    for name in ("protocol", "base_url", "project", "auth", "location", "api_key"):
        value = getattr(payload, name)
        if value is not None and value != "":
            fields[name] = value
    try:
        node = LlmNode.model_validate(fields)
    except ValidationError as exc:
        raise _config_error(exc) from exc
    _require_key_for_new_address(saved, node, bool(payload.api_key))
    try:
        models = await fetch_model_ids(node)
    except (ValueError, httpx.HTTPError) as exc:
        message = str(exc) or type(exc).__name__
        raise HTTPException(status_code=502, detail=msg("api.config.listFailed", message=message)) from exc
    # 表单里的地址与已保存的一致时才写入缓存，避免把草稿地址的模型记到旧节点上。
    if saved is not None and (saved.protocol, saved.resolved_base_url()) == (node.protocol, node.resolved_base_url()):
        async with _lock(request):
            data = store.llm().model_dump()
            for item in data["nodes"]:
                if item["id"] == saved.id:
                    item["models"] = models
                    item["models_fetched_at"] = utc_now_iso()
            # 只更新缓存的模型列表，不影响运行配置，无需热更新。
            await asyncio.to_thread(
                store.database.set_runtime_json, LLM_KEY, _validated_llm(data).model_dump(mode="json")
            )
    return {"models": models}
