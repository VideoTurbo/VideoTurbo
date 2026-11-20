"""控制台管理的翻译节点。

一个节点就是一个完整的接口：协议、地址、一个 API Key 和模型，归入「翻译主池 / 翻译兜底池」之一。
同一网站有多个 Key 时，添加多个节点即可。

运行时每个已启用的节点对应一个 TranslationUpstreamConfig，沿用最少占用调度、冷却和连续失败熔断逻辑。
"""

from __future__ import annotations

import secrets
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import (
    VERTEX_AI_DEFAULT_BASE_URL,
    TranslationUpstreamConfig,
    _validate_vertex_id,
    vertex_ai_base_url,
)
from .i18n import msg
from .vertex_auth import VertexAuthError, is_service_account, parse_service_account

LlmProtocol = Literal["openai_responses", "openai_chat", "vertex_ai"]
LlmPool = Literal["translation", "fallback"]
# 仅 Vertex AI：api_key 字段存 API Key，或整份服务账号 JSON 文本。
VertexAuth = Literal["api_key", "service_account"]
ReasoningEffort = Literal["none", "low", "medium", "high"]

PROTOCOL_LABELS = {
    "openai_responses": "OpenAI Responses",
    "openai_chat": "OpenAI Chat Completions",
    "vertex_ai": "Vertex AI",
}
POOL_LABELS = {"translation": msg("llm.pool.translation"), "fallback": msg("llm.pool.fallback")}


def new_id() -> str:
    return secrets.token_hex(6)


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    return "…" + value[-4:] if len(value) > 8 else msg("llm.keySaved")


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LlmNode(_Base):
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, max_length=60)
    pool: LlmPool = "translation"
    protocol: LlmProtocol = "openai_responses"
    # OpenAI 兼容协议必填；Vertex AI 可留空，按 project/location 或 express mode 地址请求。
    base_url: str | None = None
    auth: VertexAuth = "api_key"
    # 服务账号认证时由 JSON 里的 project_id 决定，忽略提交的值。
    project: str | None = None
    location: str | None = "global"
    # 空字符串表示尚未填写，节点显示“缺少 Key”且不参与调度。
    api_key: str = ""
    model: str = Field(min_length=1, max_length=200)
    concurrency: int = Field(default=2, ge=1, le=8)
    reasoning_effort: ReasoningEffort | None = None
    # 关闭后不参与调度（与连续失败后的自动熔断不同，熔断需在节点卡片上重新启用）。
    enabled: bool = True
    # 最近一次从接口拉取的模型列表，供下拉框搜索；可随时手动输入列表外的模型。
    models: list[str] = Field(default_factory=list)
    models_fetched_at: str | None = None

    @field_validator("name", "model")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(msg("llm.nameModelRequired"))
        return value

    @field_validator("api_key")
    @classmethod
    def strip_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(msg("llm.urlScheme", value=value))
        return value

    @field_validator("project", "location")
    @classmethod
    def validate_vertex_ids(cls, value: str | None) -> str | None:
        return _validate_vertex_id(value)

    @model_validator(mode="after")
    def validate_node(self) -> LlmNode:
        if self.protocol != "vertex_ai":
            self.auth = "api_key"
            if not self.base_url:
                raise ValueError(msg("llm.noUrl", name=self.name))
            return self
        if self.auth == "api_key":
            if is_service_account(self.api_key):
                raise ValueError(msg("llm.serviceAccountAsKey"))
            return self
        # 服务账号：地址只由 JSON 里的项目 ID 和地区决定。
        self.base_url = None
        self.project = None
        if self.api_key:
            if not is_service_account(self.api_key):
                raise ValueError(msg("llm.serviceAccountRequired"))
            try:
                info = parse_service_account(self.api_key)
            except VertexAuthError as exc:
                raise ValueError(str(exc)) from exc
            self.project = _validate_vertex_id(info["project_id"])
        return self

    @property
    def uses_service_account(self) -> bool:
        return self.protocol == "vertex_ai" and self.auth == "service_account"

    @property
    def provider_kind(self) -> str:
        return "vertex_ai" if self.protocol == "vertex_ai" else "openai_compatible"

    @property
    def api_mode(self) -> str:
        return "chat_completions" if self.protocol == "openai_chat" else "responses"

    def resolved_base_url(self) -> str:
        if self.protocol != "vertex_ai":
            return str(self.base_url)
        if self.project:
            return vertex_ai_base_url(self.project, self.location)
        return self.base_url or VERTEX_AI_DEFAULT_BASE_URL

    def public(self) -> dict:
        """返回给控制台的视图：Key 只显示掩码，永不回传明文。"""
        data = self.model_dump(mode="json", exclude={"api_key"})
        data["key_configured"] = bool(self.api_key)
        # 服务账号显示账号邮箱，便于辨认上传的是哪份密钥。
        data["key_masked"] = (
            parse_service_account(self.api_key)["client_email"]
            if self.uses_service_account and self.api_key
            else mask_secret(self.api_key)
        )
        data["resolved_base_url"] = self.resolved_base_url()
        return data


class LlmSettings(_Base):
    nodes: list[LlmNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_nodes(self) -> LlmSettings:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError(msg("llm.duplicateId"))
        # 翻译主池与兜底池共用一套节点状态，节点名在两个池之间也不能重复。
        names: set[str] = set()
        for node in self.nodes:
            if node.name in names:
                raise ValueError(msg("llm.duplicateName", name=node.name))
            names.add(node.name)
        return self

    def node(self, node_id: str) -> LlmNode | None:
        return next((item for item in self.nodes if item.id == node_id), None)

    def public(self) -> dict:
        return {"nodes": [node.public() for node in self.nodes]}


def pool_upstreams(llm: LlmSettings, pool: LlmPool) -> list[TranslationUpstreamConfig]:
    """把一个节点池里已启用的节点转换为运行时节点配置。"""
    upstreams: list[TranslationUpstreamConfig] = []
    for node in llm.nodes:
        if node.pool != pool or not node.enabled:
            continue
        fields = {
            "name": node.name,
            "provider": node.provider_kind,
            "api_mode": node.api_mode,
            "base_url": node.resolved_base_url(),
            "model": node.model,
            # 上级 api_key 恒为空，缺少 Key 的节点会显示“配置不完整”。
            "api_key": node.api_key or None,
            "concurrency": node.concurrency,
            "reasoning_effort": node.reasoning_effort,
            # 始终请求 JSON Schema 严格输出；接口不支持时翻译流程自动退回普通 JSON。
            "structured_output": True,
        }
        upstreams.append(TranslationUpstreamConfig(**fields))
    return upstreams


# 控制台可修改的翻译设置；批次、重试、超时、检查点等参数使用内置默认值。
TRANSLATION_TUNING_FIELDS = (
    "enabled",
    "job_concurrency",
)


def build_translation_config(llm: LlmSettings, tuning: dict) -> dict:
    """生成 translation 段：节点全部来自数据库。"""
    upstreams = pool_upstreams(llm, "translation")
    fallback_upstreams = pool_upstreams(llm, "fallback")
    return {
        **{key: value for key, value in tuning.items() if key in TRANSLATION_TUNING_FIELDS},
        # 总并发不单独设置：等于节点池各节点并发之和（主池、兜底池取较大者）。
        "concurrency": max(
            sum(item.concurrency for item in upstreams),
            sum(item.concurrency for item in fallback_upstreams),
            1,
        ),
        "provider": "openai_compatible",
        # 节点池模式下的上级地址、模型和 Key 只作为继承来源；全部留空，避免与节点混淆，
        # 也让翻译检查点不随节点增删而失效。
        "base_url": None,
        "model": None,
        "api_key": None,
        "upstreams": [item.model_dump(exclude_unset=True) for item in upstreams],
        "fallback_upstreams": [item.model_dump(exclude_unset=True) for item in fallback_upstreams],
    }

