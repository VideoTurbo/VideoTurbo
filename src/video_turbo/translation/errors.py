"""翻译请求的错误类型，以及把 HTTP 和网络错误整理成可读原因。"""

from __future__ import annotations

from typing import Any

from ..i18n import msg
from ..vertex_auth import VertexAuthError


class TranslationNotConfigured(RuntimeError):
    pass


class TranslationError(RuntimeError):
    pass


class TranslationBatchError(TranslationError):
    """缩小字幕批次后可能恢复的模型输出错误。"""


class TranslationNodeResponseError(TranslationBatchError):
    """HTTP 成功但节点返回了无效或不兼容的响应。"""


class TranslationTransportBatchError(TranslationBatchError):
    """缩小批次后顺序重试的超时或临时网关错误。"""


class TranslationFatalError(TranslationError):
    """切换模型或缩小批次都无法恢复的请求错误。"""


def describe_request_error(exc: Exception, timeout_seconds: int) -> str:
    """把 httpx 网络异常转换成不会丢失信息的用户可读文案。"""
    error_name = type(exc).__name__
    detail = str(exc).strip()
    if error_name == "ConnectTimeout":
        return msg("translation.error.connectTimeout", timeout_seconds=timeout_seconds)
    if error_name == "ReadTimeout":
        return msg("translation.error.readTimeout", timeout_seconds=timeout_seconds)
    if error_name == "WriteTimeout":
        return msg("translation.error.writeTimeout", timeout_seconds=timeout_seconds)
    if error_name == "PoolTimeout":
        return msg("translation.error.poolTimeout", timeout_seconds=timeout_seconds)
    if error_name == "ConnectError":
        prefix = msg("translation.error.connect")
    elif error_name == "RemoteProtocolError":
        prefix = msg("translation.error.disconnected")
    elif isinstance(exc.__cause__, VertexAuthError):
        prefix = msg("translation.error.vertexAuth")
    else:
        prefix = msg("translation.error.network")
    return msg("translation.error.withDetail", prefix=prefix, detail=detail or error_name)


def describe_http_error(
    status_code: int,
    response_payload: dict[str, Any] | None,
    response_text: str,
) -> str:
    """把网关 HTTP 错误归因，并转换成适合直接展示的文案。"""
    error = response_payload.get("error") if isinstance(response_payload, dict) else None
    error_code = str(error.get("code") or "") if isinstance(error, dict) else ""
    provider_message = (
        str(error.get("message") or "").strip()
        if isinstance(error, dict)
        else str(error or "").strip()
    )
    known_errors = {
        "rate_limit_exceeded": (
            msg("translation.error.rpm")
        ),
        "concurrency_limit_exceeded": msg("translation.error.concurrency"),
        "upstream_cooling": msg("translation.error.cooling"),
        "upstream_header_timeout": msg("translation.error.headerTimeout"),
    }
    if error_code in known_errors:
        return msg("translation.error.known", status_code=status_code, message=known_errors[error_code])

    status_messages = {
        401: msg("translation.error.401"),
        403: msg("translation.error.403"),
        408: msg("translation.error.408"),
        429: msg("translation.error.429"),
        502: msg("translation.error.502"),
        503: msg("translation.error.503"),
        504: msg("translation.error.504"),
    }
    if "api key not valid" in provider_message.casefold() or "API_KEY_INVALID" in response_text:
        # Google 对无效 API Key 返回 400 INVALID_ARGUMENT，而不是 401。
        summary = status_messages[401]
    elif status_code in status_messages:
        summary = status_messages[status_code]
    elif status_code >= 500:
        summary = msg("translation.error.server")
    else:
        summary = msg("translation.error.rejected")

    detail = provider_message or response_text.strip().replace("\n", " ")[:300]
    suffix = msg("translation.error.providerDetail", detail=detail) if detail else ""
    return msg("translation.error.summary", summary=summary, status_code=status_code, suffix=suffix)


def is_schema_rejection(
    status_code: int,
    response_payload: dict[str, Any] | None,
    response_text: str,
) -> bool:
    """只把明确指向结构化输出的 400/422 当作 Schema 不兼容。"""
    if status_code not in {400, 422}:
        return False
    error = response_payload.get("error") if isinstance(response_payload, dict) else None
    details: list[str] = []
    if isinstance(error, dict):
        param = str(error.get("param") or "").strip().casefold()
        if param in {"text", "text.format", "response_format"}:
            return True
        details.extend(
            str(error.get(key) or "") for key in ("code", "type", "param", "message")
        )
    elif error is not None:
        details.append(str(error))
    details.append(response_text)
    normalized = " ".join(details).strip().casefold()
    if not normalized:
        # 少数兼容网关只返回空的 400；保留一次无 Schema 降级机会。
        return True
    return any(
        marker in normalized
        for marker in (
            "json_schema",
            "json schema",
            "response_format",
            "response_schema",
            "response_json_schema",
            "responsejsonschema",
            "responseschema",
            "text.format",
            "text format",
            "structured_output",
            "structured output",
        )
    )


def is_enum_schema_rejection(
    status_code: int,
    response_payload: dict[str, Any] | None,
    response_text: str,
) -> bool:
    if not is_schema_rejection(status_code, response_payload, response_text):
        return False
    error = response_payload.get("error") if isinstance(response_payload, dict) else None
    detail = f"{error} {response_text}".casefold()
    return "enum" in detail and any(
        marker in detail
        for marker in (
            "response_schema",
            "json_schema",
            "response_format",
            "responsejsonschema",
            "responseschema",
        )
    )
