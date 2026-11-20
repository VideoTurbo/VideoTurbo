"""与翻译接口通信：按接口类型（OpenAI Responses、Chat Completions、Vertex AI）组织请求和解析响应。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from time import monotonic
from typing import Any

import httpx

from ..config import TranslationConfig
from ..i18n import msg
from ..languages import LanguagePair
from ..models import Cue
from ..vertex_auth import VertexAuthError, access_token, is_service_account
from .errors import (
    TranslationBatchError,
    TranslationError,
    TranslationNodeResponseError,
    describe_http_error,
    describe_request_error,
    is_enum_schema_rejection,
    is_schema_rejection,
)
from .prompts import DEFAULT_PAIR, build_system_prompt, build_user_prompt

logger = logging.getLogger(__name__)


def extract_json(text: str | None) -> list[dict]:
    if not isinstance(text, str) or not text.strip():
        raise TranslationNodeResponseError(msg("translation.api.empty"))
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TranslationNodeResponseError(msg("translation.badResponse")) from exc
    if isinstance(payload, dict):
        payload = payload.get("translations")
    if not isinstance(payload, list):
        raise TranslationNodeResponseError(msg("translation.api.noTranslation"))
    return payload


def extract_responses_content(
    payload: dict[str, Any], *, allow_legacy_chat: bool = False
) -> tuple[str, str | None]:
    """从 Responses API 原始 HTTP 响应中提取最终文本和状态。"""
    status_value = payload.get("status")
    status = str(status_value) if status_value is not None else None
    if status == "incomplete":
        details = payload.get("incomplete_details") or {}
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "max_output_tokens":
            raise TranslationBatchError(msg("translation.api.maxTokens"))
        if reason == "content_filter":
            raise TranslationBatchError(msg("translation.api.filtered"))
        raise TranslationBatchError(msg("translation.api.incomplete", reason=reason or msg('translation.api.unknownReason')))
    if status in {"failed", "cancelled"}:
        error = payload.get("error") or {}
        detail = error.get("message") if isinstance(error, dict) else error
        raise TranslationNodeResponseError(
            msg("translation.api.status", status=status, detail=str(detail).strip() or msg('translation.api.noReason'))
        )

    top_level_text = payload.get("output_text")
    if isinstance(top_level_text, str) and top_level_text.strip():
        return top_level_text, status

    # Some third-party gateways expose /responses but still return the older
    # Chat Completions envelope. Requests always use the Responses contract,
    # but accepting this envelope makes the migration tolerant of such gateways.
    choices = payload.get("choices")
    if (
        allow_legacy_chat
        and isinstance(choices, list)
        and choices
        and isinstance(choices[0], dict)
    ):
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter":
            raise TranslationBatchError(msg("translation.api.filtered"))
        if finish_reason == "length":
            raise TranslationBatchError(msg("translation.api.lengthLimit"))
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content, str(finish_reason) if finish_reason is not None else status

    text_parts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise TranslationBatchError(msg("translation.api.refused"))
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
    text = "\n".join(text_parts)
    if not text.strip():
        raise TranslationNodeResponseError(msg("translation.api.empty"))
    return text, status


def build_request_payload(
    config: TranslationConfig,
    batch: Sequence[Cue],
    context: Sequence[Cue],
    *,
    cue_id_enum: bool = True,
    pair: LanguagePair = DEFAULT_PAIR,
) -> dict:
    payload: dict[str, Any] = {
        "model": config.model,
        "instructions": build_system_prompt(pair),
        "input": build_user_prompt(batch, context),
        "store": False,
    }
    if config.reasoning_effort is not None:
        payload["reasoning"] = {"effort": config.reasoning_effort}
    if config.max_output_tokens is not None:
        payload["max_output_tokens"] = config.max_output_tokens
    if config.structured_output:
        target_ids = [cue.cue_id for cue in batch]
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "subtitle_translations",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "translations": {
                            "type": "array",
                            "minItems": len(batch),
                            "maxItems": len(batch),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "cue_id": {
                                        "type": "integer",
                                        **({"enum": target_ids} if cue_id_enum else {}),
                                    },
                                    "text": {"type": "string", "minLength": 1},
                                },
                                "required": ["cue_id", "text"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["translations"],
                    "additionalProperties": False,
                },
            }
        }
    return payload


def build_http_payload(
    config: TranslationConfig,
    batch: Sequence[Cue],
    context: Sequence[Cue],
    *,
    cue_id_enum: bool = True,
    pair: LanguagePair = DEFAULT_PAIR,
) -> dict:
    """按接口类型生成请求体：先生成 Responses 结构，再由 api_format 转换。"""
    request = build_request_payload(config, batch, context, cue_id_enum=cue_id_enum, pair=pair)
    return api_format(config).payload(config, request)

VERTEX_HARM_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)
VERTEX_THINKING_LEVELS = {
    "none": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
}


def _build_vertex_payload(config: TranslationConfig, responses_payload: dict) -> dict:
    """把 Responses 请求体转换成 Vertex AI generateContent 请求体。"""
    generation_config: dict[str, Any] = {"responseMimeType": "application/json"}
    text_config = responses_payload.get("text")
    if isinstance(text_config, dict):
        generation_config["responseJsonSchema"] = text_config["format"]["schema"]
    if "max_output_tokens" in responses_payload:
        generation_config["maxOutputTokens"] = responses_payload["max_output_tokens"]
    reasoning = responses_payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") in VERTEX_THINKING_LEVELS:
        generation_config["thinkingConfig"] = {
            "thinkingLevel": VERTEX_THINKING_LEVELS[reasoning["effort"]]
        }
    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": responses_payload["instructions"]}]},
        "contents": [{"role": "user", "parts": [{"text": responses_payload["input"]}]}],
        "generationConfig": generation_config,
        # 字幕翻译固定关闭安全过滤；输出仍被过滤时按内容过滤处理并缩小批次。
        "safetySettings": [
            {"category": category, "threshold": "OFF"} for category in VERTEX_HARM_CATEGORIES
        ],
    }
    return payload


class ApiFormat:
    """一类翻译接口：请求地址、请求体、认证方式和成功响应的整理。

    请求体以 OpenAI Responses 的结构生成（build_request_payload），各类接口再转换成自己的格式；
    成功响应整理成 Responses 结构，后面的解析与统计只认这一种结构。
    新增接口类型时加一个子类，并在 api_format 中登记。
    """

    path = ""

    def endpoint(self, config: TranslationConfig) -> str:
        return str(config.base_url).rstrip("/") + self.path

    def payload(self, config: TranslationConfig, request: dict) -> dict:
        return request

    def key_headers(self, config: TranslationConfig) -> dict[str, str]:
        return {"Authorization": f"Bearer {config.api_key}"}

    async def auth_headers(self, client: Any, config: TranslationConfig) -> dict[str, str]:
        return self.key_headers(config)

    def normalize(self, payload: Any) -> Any:
        return payload


class ResponsesApi(ApiFormat):
    path = "/responses"


class ChatCompletionsApi(ApiFormat):
    path = "/chat/completions"

    def payload(self, config: TranslationConfig, request: dict) -> dict:
        payload: dict[str, Any] = {
            "model": request["model"],
            "messages": [
                {"role": "system", "content": request["instructions"]},
                {"role": "user", "content": request["input"]},
            ],
        }
        if "max_output_tokens" in request:
            payload["max_tokens"] = request["max_output_tokens"]
        if "text" in request:
            format_config = request["text"]["format"]
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": format_config["name"],
                    "strict": format_config["strict"],
                    "schema": format_config["schema"],
                },
            }
        return payload


class VertexApi(ApiFormat):
    def endpoint(self, config: TranslationConfig) -> str:
        base_url = str(config.base_url).rstrip("/")
        return f"{base_url}/publishers/google/models/{config.model}:generateContent"

    def payload(self, config: TranslationConfig, request: dict) -> dict:
        return _build_vertex_payload(config, request)

    def key_headers(self, config: TranslationConfig) -> dict[str, str]:
        return {"x-goog-api-key": str(config.api_key)}

    async def auth_headers(self, client: Any, config: TranslationConfig) -> dict[str, str]:
        """服务账号先换取（或复用）OAuth 访问令牌；API Key 直接放在请求头。"""
        if not is_service_account(config.api_key):
            return self.key_headers(config)
        try:
            token = await access_token(client, str(config.api_key))
        except VertexAuthError as exc:
            # 与网络错误走同一条路径：记为节点失败并触发冷却、熔断。
            raise httpx.RequestError(str(exc)) from exc
        return {"Authorization": f"Bearer {token}"}

    def normalize(self, payload: Any) -> dict[str, Any] | None:
        return normalize_vertex_response(payload)


RESPONSES_API = ResponsesApi()
CHAT_COMPLETIONS_API = ChatCompletionsApi()
VERTEX_API = VertexApi()


def api_format(config: TranslationConfig) -> ApiFormat:
    if config.provider == "vertex_ai":
        return VERTEX_API
    return CHAT_COMPLETIONS_API if config.api_mode == "chat_completions" else RESPONSES_API


def request_endpoint(config: TranslationConfig) -> str:
    return api_format(config).endpoint(config)


async def post_request(client: Any, endpoint: str, config: TranslationConfig, payload: dict) -> Any:
    headers = await api_format(config).auth_headers(client, config)
    return await client.post(endpoint, headers=headers, json=payload)

def translation_schema(payload: dict) -> dict | None:
    if "text" in payload:
        return payload["text"]["format"]["schema"]
    if "response_format" in payload:
        return payload["response_format"]["json_schema"]["schema"]
    generation_config = payload.get("generationConfig")
    if isinstance(generation_config, dict):
        return generation_config.get("responseJsonSchema")
    return None


def remove_translation_schema(payload: dict) -> None:
    payload.pop("text", None)
    payload.pop("response_format", None)
    generation_config = payload.get("generationConfig")
    if isinstance(generation_config, dict):
        generation_config.pop("responseJsonSchema", None)


VERTEX_FILTER_FINISH_REASONS = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "RECITATION",
    "IMAGE_SAFETY",
}


def normalize_vertex_response(payload: Any) -> dict[str, Any] | None:
    """把 generateContent 成功响应转换成 Responses 结构，复用同一套解析与统计。"""
    if not isinstance(payload, dict):
        return None
    usage_metadata = payload.get("usageMetadata")
    usage: dict[str, Any] = {}
    if isinstance(usage_metadata, dict):
        thoughts = usage_metadata.get("thoughtsTokenCount") or 0
        candidates_tokens = usage_metadata.get("candidatesTokenCount") or 0
        try:
            usage = {
                "input_tokens": int(usage_metadata.get("promptTokenCount") or 0),
                "output_tokens": int(candidates_tokens) + int(thoughts),
                "output_tokens_details": {"reasoning_tokens": int(thoughts)},
            }
        except (TypeError, ValueError):
            usage = {}
    normalized: dict[str, Any] = {
        "id": payload.get("responseId"),
        "model": payload.get("modelVersion"),
        "usage": usage,
    }
    prompt_feedback = payload.get("promptFeedback")
    if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
        normalized["status"] = "incomplete"
        normalized["incomplete_details"] = {"reason": "content_filter"}
        return normalized
    candidates = payload.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    if not isinstance(candidate, dict):
        normalized["status"] = "failed"
        normalized["error"] = {"message": msg("translation.api.vertexNoCandidate")}
        return normalized
    finish_reason = str(candidate.get("finishReason") or "STOP")
    if finish_reason == "MAX_TOKENS":
        normalized["status"] = "incomplete"
        normalized["incomplete_details"] = {"reason": "max_output_tokens"}
        return normalized
    if finish_reason in VERTEX_FILTER_FINISH_REASONS:
        normalized["status"] = "incomplete"
        normalized["incomplete_details"] = {"reason": "content_filter"}
        return normalized
    if finish_reason not in {"STOP", "FINISH_REASON_UNSPECIFIED"}:
        normalized["status"] = "failed"
        normalized["error"] = {"message": f"finishReason={finish_reason}"}
        return normalized
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    text_parts = [
        part["text"]
        for part in parts or []
        if isinstance(part, dict)
        and not part.get("thought")
        and isinstance(part.get("text"), str)
    ]
    normalized["status"] = "completed"
    normalized["output_text"] = "".join(text_parts)
    return normalized


def read_response_payload(response: Any, config: TranslationConfig) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    # 错误响应保持原样，供 describe_http_error 读取各接口自己的错误字段。
    if response.status_code < 400:
        return api_format(config).normalize(payload)
    return payload


async def probe_translation_responses(client: Any, config: TranslationConfig) -> dict[str, Any]:
    """用一条短字幕确认翻译节点可请求且返回兼容的结果。"""
    # 与语言无关的短句：只确认节点可请求且返回结构可解析。
    cues = [Cue(cue_id=1, start=0, end=1, source="OK")]
    payload = build_http_payload(config, cues, cues)
    endpoint = request_endpoint(config)
    started_at = monotonic()
    structured_output = translation_schema(payload) is not None
    cue_id_enum = structured_output

    while True:
        try:
            response = await post_request(client, endpoint, config, payload)
        except httpx.RequestError as exc:
            raise TranslationError(
                describe_request_error(exc, config.request_timeout_seconds)
            ) from exc
        response_payload = read_response_payload(response, config)
        if cue_id_enum and is_enum_schema_rejection(
            response.status_code, response_payload, response.text
        ):
            schema = translation_schema(payload)
            assert schema is not None
            cue_id_schema = schema["properties"][
                "translations"
            ]["items"]["properties"]["cue_id"]
            cue_id_schema.pop("enum")
            cue_id_enum = False
            continue
        if structured_output and is_schema_rejection(
            response.status_code, response_payload, response.text
        ):
            remove_translation_schema(payload)
            structured_output = False
            continue
        if response.status_code >= 400:
            raise TranslationError(
                describe_http_error(
                    response.status_code, response_payload, response.text
                )
            )
        if not isinstance(response_payload, dict):
            raise TranslationNodeResponseError(msg("translation.badResponse"))
        content, status = extract_responses_content(
            response_payload, allow_legacy_chat=True
        )
        validate_translation_items(extract_json(content), cues)
        return {
            "compatible": True,
            "endpoint": endpoint,
            "model": str(response_payload.get("model") or config.model),
            "status": status,
            "structured_output": structured_output,
            "duration_seconds": round(monotonic() - started_at, 3),
        }


def validate_translation_items(items: list[dict], batch: Sequence[Cue]) -> dict[int, str]:
    expected_ids = {cue.cue_id for cue in batch}
    result: dict[int, str] = {}
    for item in items:
        try:
            cue_id = int(item["cue_id"])
            text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise TranslationBatchError(msg("translation.api.missingFields")) from exc
        if cue_id not in expected_ids or not text:
            raise TranslationBatchError(msg("translation.api.invalidCue", cue_id=cue_id))
        if cue_id in result:
            raise TranslationBatchError(msg("translation.api.duplicateCue", cue_id=cue_id))
        result[cue_id] = text
    if result.keys() != expected_ids:
        missing = sorted(expected_ids - result.keys())
        raise TranslationBatchError(msg("translation.api.missingCues", missing=missing))
    return result
