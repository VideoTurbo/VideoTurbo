import asyncio
import copy

import httpx
import pytest
from conftest import raises_rendered
from pydantic import ValidationError

from video_turbo import vertex_auth
from video_turbo.config import (
    VERTEX_AI_DEFAULT_BASE_URL,
    TranslationConfig,
    TranslationUpstreamConfig,
    vertex_ai_base_url,
)
from video_turbo.i18n import render
from video_turbo.llm_config import LlmNode
from video_turbo.models import Cue
from video_turbo.translation import (
    TranslationBatchError,
    TranslationError,
    TranslationMetrics,
    TranslationNodeResponseError,
    TranslationUpstreamPool,
    probe_translation_responses,
    translate_cues,
)
from video_turbo.translation.api import (
    api_format,
    build_http_payload,
    extract_responses_content,
    normalize_vertex_response,
    request_endpoint,
)
from video_turbo.translation.errors import describe_http_error

VERTEX_ENDPOINT = (
    "https://aiplatform.googleapis.com/v1/publishers/google/models/"
    "gemini-3-flash:generateContent"
)

def _vertex_config(**overrides) -> TranslationConfig:
    values = {
        "provider": "vertex_ai",
        "model": "gemini-3-flash",
        "api_key": "vertex-key",
        "max_retries": 0,
    }
    values.update(overrides)
    return TranslationConfig(**values)


def _vertex_response(text: str, *, finish_reason: str = "STOP") -> dict:
    return {
        "responseId": "resp_vertex",
        "modelVersion": "gemini-3-flash-001",
        "candidates": [
            {
                "finishReason": finish_reason,
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "先想一想", "thought": True},
                        {"text": text},
                    ],
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 7,
        },
    }


def test_vertex_payload_maps_schema_thinking_and_safety() -> None:
    cue = Cue(cue_id=1, start=0, end=1, source="おはよう")
    config = _vertex_config(
        reasoning_effort="low", structured_output=True, max_output_tokens=4096
    )

    payload = build_http_payload(config, [cue], [cue])

    assert payload["systemInstruction"]["parts"][0]["text"].startswith(
        "你是专业的日语影视中文字幕译者"
    )
    assert payload["contents"][0]["role"] == "user"
    assert "target_cue_ids=[1]" in payload["contents"][0]["parts"][0]["text"]
    generation_config = payload["generationConfig"]
    assert generation_config["responseMimeType"] == "application/json"
    assert generation_config["maxOutputTokens"] == 4096
    assert generation_config["thinkingConfig"] == {"thinkingLevel": "LOW"}
    schema = generation_config["responseJsonSchema"]
    assert schema["properties"]["translations"]["items"]["properties"]["cue_id"][
        "enum"
    ] == [1]
    assert {item["threshold"] for item in payload["safetySettings"]} == {"OFF"}
    assert "HARM_CATEGORY_SEXUALLY_EXPLICIT" in {
        item["category"] for item in payload["safetySettings"]
    }
    for key in ("model", "instructions", "input", "messages", "text", "store"):
        assert key not in payload
    assert request_endpoint(config) == VERTEX_ENDPOINT
    assert api_format(config).key_headers(config) == {"x-goog-api-key": "vertex-key"}


def test_vertex_payload_omits_optional_fields_but_always_disables_safety_filters() -> None:
    cue = Cue(cue_id=1, start=0, end=1, source="おはよう")
    payload = build_http_payload(_vertex_config(), [cue], [cue])

    assert payload["generationConfig"] == {"responseMimeType": "application/json"}
    assert {item["threshold"] for item in payload["safetySettings"]} == {"OFF"}


def test_vertex_translation_uses_api_key_header_and_skips_thoughts() -> None:
    calls: list[tuple[str, dict, dict]] = []

    class FakeClient:
        async def post(self, endpoint, *, headers, json) -> httpx.Response:
            calls.append((endpoint, headers, json))
            return httpx.Response(
                200, json=_vertex_response('{"translations":[{"cue_id":1,"text":"谢谢"}]}')
            )

    cue = Cue(cue_id=1, start=0, end=1, source="ありがとう")
    metrics = TranslationMetrics(1, 1)

    translated = asyncio.run(
        translate_cues([cue], _vertex_config(), http_client=FakeClient(), metrics=metrics)
    )

    assert render(translated[0].target) == "谢谢"
    endpoint, headers, _ = calls[0]
    assert endpoint == VERTEX_ENDPOINT
    assert headers == {"x-goog-api-key": "vertex-key"}
    assert metrics.model_cue_counts == {"gemini-3-flash-001": 1}
    assert metrics.model_usage()["provider_cue_counts"] == {"gemini": 1}
    assert metrics.prompt_tokens == 100
    assert metrics.completion_tokens == 27
    assert metrics.reasoning_tokens == 7


def test_vertex_schema_rejection_retries_without_response_json_schema() -> None:
    calls: list[dict] = []

    class FakeClient:
        async def post(self, endpoint, *, headers, json) -> httpx.Response:
            calls.append(copy.deepcopy(json))
            if "responseJsonSchema" in json["generationConfig"]:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "message": (
                                "Invalid JSON payload received. Unknown name "
                                '"responseJsonSchema" at \'generation_config\''
                            ),
                        }
                    },
                )
            return httpx.Response(
                200, json=_vertex_response('{"translations":[{"cue_id":1,"text":"谢谢"}]}')
            )

    cue = Cue(cue_id=1, start=0, end=1, source="ありがとう")
    metrics = TranslationMetrics(1, 1)

    translated = asyncio.run(
        translate_cues(
            [cue],
            _vertex_config(structured_output=True),
            http_client=FakeClient(),
            metrics=metrics,
        )
    )

    assert render(translated[0].target) == "谢谢"
    assert len(calls) == 2
    assert "responseJsonSchema" in calls[0]["generationConfig"]
    assert calls[1]["generationConfig"] == {"responseMimeType": "application/json"}
    assert metrics.schema_fallback_count == 1


@pytest.mark.parametrize(
    ("payload", "error_type", "message"),
    [
        (
            _vertex_response("", finish_reason="MAX_TOKENS"),
            TranslationBatchError,
            "max_output_tokens",
        ),
        (_vertex_response("", finish_reason="SAFETY"), TranslationBatchError, "内容审核"),
        (
            _vertex_response("", finish_reason="PROHIBITED_CONTENT"),
            TranslationBatchError,
            "内容审核",
        ),
        (
            {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
            TranslationBatchError,
            "内容审核",
        ),
        ({"candidates": []}, TranslationNodeResponseError, "没有返回候选结果"),
        (
            _vertex_response("", finish_reason="MALFORMED_FUNCTION_CALL"),
            TranslationNodeResponseError,
            "MALFORMED_FUNCTION_CALL",
        ),
    ],
)
def test_vertex_finish_reasons_map_to_translation_errors(
    payload: dict, error_type: type[Exception], message: str
) -> None:
    normalized = normalize_vertex_response(payload)
    assert normalized is not None

    with raises_rendered(error_type, message):
        extract_responses_content(normalized)


def test_vertex_invalid_api_key_is_reported_as_configuration_error() -> None:
    message = describe_http_error(
        400,
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "API key not valid. Please pass a valid API key.",
            }
        },
        "",
    )

    assert render(message).startswith("配置错误：翻译 API Key 无效或已失效（错误码 400）")


def test_vertex_probe_uses_generate_content() -> None:
    calls: list[str] = []

    class FakeClient:
        async def post(self, endpoint, *, headers, json) -> httpx.Response:
            calls.append(endpoint)
            assert headers == {"x-goog-api-key": "vertex-key"}
            text = '{"translations":[{"cue_id":1,"text":"谢谢"}]}'
            return httpx.Response(200, json=_vertex_response(text))

    config = _vertex_config(structured_output=True)

    translation = asyncio.run(probe_translation_responses(FakeClient(), config))

    assert translation["compatible"] is True
    assert translation["model"] == "gemini-3-flash-001"
    assert translation["structured_output"] is True
    assert calls == [VERTEX_ENDPOINT]


def test_vertex_provider_defaults_base_url_and_keeps_explicit_one() -> None:
    assert _vertex_config().base_url == VERTEX_AI_DEFAULT_BASE_URL
    explicit = _vertex_config(
        base_url="https://aiplatform.googleapis.com/v1/projects/p/locations/global"
    )
    assert request_endpoint(explicit) == (
        "https://aiplatform.googleapis.com/v1/projects/p/locations/global"
        "/publishers/google/models/gemini-3-flash:generateContent"
    )
    assert TranslationConfig().base_url == "https://api.deepseek.com"


def test_upstreams_and_fallback_can_mix_providers() -> None:
    config = TranslationConfig(
        api_key="grok-key",
        model="grok-4",
        fallback_provider="vertex_ai",
        fallback_base_url=VERTEX_AI_DEFAULT_BASE_URL,
        fallback_model="gemini-3-flash",
        fallback_api_key="vertex-key",
        upstreams=[
            TranslationUpstreamConfig(name="grok-1", base_url="http://grok/v1"),
            TranslationUpstreamConfig(
                name="vertex-1",
                provider="vertex_ai",
                base_url=VERTEX_AI_DEFAULT_BASE_URL,
                model="gemini-3-flash",
                api_key="vertex-key",
            ),
        ],
    )

    snapshot = {item["name"]: item for item in TranslationUpstreamPool(config).snapshot()}
    fallback = config.fallback_request_config()

    assert snapshot["grok-1"]["provider"] == "openai_compatible"
    assert snapshot["vertex-1"]["provider"] == "vertex_ai"
    assert fallback is not None
    assert fallback.provider == "vertex_ai"
    assert api_format(fallback).key_headers(fallback) == {"x-goog-api-key": "vertex-key"}


def test_vertex_project_and_location_build_regional_or_global_base_url() -> None:
    assert vertex_ai_base_url(None, "us-central1") == VERTEX_AI_DEFAULT_BASE_URL
    assert vertex_ai_base_url("my-proj", None) == (
        "https://aiplatform.googleapis.com/v1/projects/my-proj/locations/global"
    )
    regional = _vertex_config(
        project="my-proj",
        location="us-central1",
        base_url="https://ignored.example.com/v1",
    )
    assert request_endpoint(regional) == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/my-proj"
        "/locations/us-central1/publishers/google/models/gemini-3-flash:generateContent"
    )


@pytest.mark.parametrize("value", ["my proj", "proj/../x", "US-CENTRAL1"])
def test_vertex_project_and_location_reject_unsafe_values(value: str) -> None:
    with raises_rendered(ValidationError, "格式无效"):
        _vertex_config(project="my-proj", location=value)


def test_vertex_upstreams_inherit_or_override_project_and_location() -> None:
    config = TranslationConfig(
        provider="vertex_ai",
        project="main-proj",
        location="us-central1",
        model="gemini-3-flash",
        api_key="vertex-key",
        upstreams=[
            TranslationUpstreamConfig(name="inherit"),
            TranslationUpstreamConfig(name="own-region", location="europe-west4"),
            TranslationUpstreamConfig(name="own-project", project="other-proj"),
            TranslationUpstreamConfig(
                name="explicit-url", base_url="https://custom.example.com/v1"
            ),
        ],
    )

    snapshot = {item["name"]: item for item in TranslationUpstreamPool(config).snapshot()}

    assert config.ready_upstream_count == 4
    assert snapshot["inherit"]["base_url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/main-proj"
        "/locations/us-central1"
    )
    assert snapshot["own-region"]["base_url"] == (
        "https://europe-west4-aiplatform.googleapis.com/v1/projects/main-proj"
        "/locations/europe-west4"
    )
    assert snapshot["own-project"]["base_url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/other-proj"
        "/locations/us-central1"
    )
    assert snapshot["explicit-url"]["base_url"] == "https://custom.example.com/v1"


def test_vertex_fallback_uses_own_project_while_primary_stays_openai() -> None:
    config = TranslationConfig(
        api_key="deepseek-key",
        fallback_provider="vertex_ai",
        fallback_project="fallback-proj",
        fallback_model="gemini-3-flash",
        fallback_api_key="vertex-key",
        fallback_upstreams=[TranslationUpstreamConfig(name="vertex-fallback")],
    )

    pool_config = config.fallback_pool_config()
    assert pool_config is not None
    snapshot = TranslationUpstreamPool(pool_config).snapshot()

    assert config.base_url == "https://api.deepseek.com"
    assert config.ready_fallback_upstream_count == 1
    assert snapshot[0]["provider"] == "vertex_ai"
    assert snapshot[0]["base_url"] == (
        "https://aiplatform.googleapis.com/v1/projects/fallback-proj/locations/global"
    )

    single = TranslationConfig(
        api_key="deepseek-key",
        fallback_provider="vertex_ai",
        fallback_model="gemini-3-flash",
    ).fallback_request_config()
    assert single is not None
    assert single.base_url == VERTEX_AI_DEFAULT_BASE_URL


def test_openai_upstream_without_base_url_is_rejected() -> None:
    with raises_rendered(ValidationError, "缺少接口地址（base_url）"):
        TranslationConfig(upstreams=[TranslationUpstreamConfig(name="grok-1")])


def test_vertex_location_defaults_to_global() -> None:
    assert TranslationConfig().location == "global"
    config = _vertex_config(project="my-proj")
    assert config.base_url == (
        "https://aiplatform.googleapis.com/v1/projects/my-proj/locations/global"
    )


# ---- 服务账号 JSON 认证 ----


def _service_account_json(project_id: str = "demo-project") -> str:
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": project_id,
            "private_key_id": "kid-1",
            "private_key": pem,
            "client_email": f"translator@{project_id}.iam.gserviceaccount.com",
        }
    )


class _ServiceAccountClient:
    """同时充当 Google 令牌接口和 Vertex AI 接口。"""

    def __init__(self, *, token_status: int = 200) -> None:
        self.token_requests: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.token_status = token_status

    async def post(self, endpoint, *, headers=None, json=None, data=None) -> httpx.Response:
        if endpoint == vertex_auth.TOKEN_URL:
            self.token_requests.append(data)
            if self.token_status >= 400:
                return httpx.Response(
                    self.token_status,
                    json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."},
                )
            return httpx.Response(200, json={"access_token": "ya29.token", "expires_in": 3600})
        self.calls.append((endpoint, headers))
        return httpx.Response(
            200, json=_vertex_response('{"translations":[{"cue_id":1,"text":"谢谢"}]}')
        )


def test_service_account_node_reads_project_from_json_and_hides_secret() -> None:
    credential = _service_account_json()
    node = LlmNode(
        name="sa",
        protocol="vertex_ai",
        auth="service_account",
        # 表单里残留的项目 ID 和自定义地址都被忽略。
        project="other-project",
        base_url="https://proxy.example/v1",
        location="us-central1",
        api_key=credential,
        model="gemini-3-flash",
    )

    assert node.project == "demo-project"
    assert node.resolved_base_url() == vertex_ai_base_url("demo-project", "us-central1")
    public = node.public()
    assert public["key_masked"] == "translator@demo-project.iam.gserviceaccount.com"
    assert "api_key" not in public and "PRIVATE KEY" not in str(public)


@pytest.mark.parametrize(
    ("auth", "api_key"),
    [
        ("api_key", '{"type": "service_account"}'),
        ("service_account", "plain-api-key"),
        ("service_account", '{"type": "authorized_user"}'),
        ("service_account", '{"type": "service_account", "project_id": "p"}'),
    ],
)
def test_vertex_auth_and_credential_must_match(auth: str, api_key: str) -> None:
    with pytest.raises(ValidationError):
        LlmNode(name="v", protocol="vertex_ai", auth=auth, api_key=api_key, model="gemini")


def test_service_account_translation_uses_cached_bearer_token(monkeypatch) -> None:
    monkeypatch.setattr(vertex_auth, "_token_cache", {})
    node = LlmNode(
        name="sa",
        protocol="vertex_ai",
        auth="service_account",
        api_key=_service_account_json(),
        model="gemini-3-flash",
    )
    config = _vertex_config(base_url=node.resolved_base_url(), api_key=node.api_key)
    client = _ServiceAccountClient()
    cue = Cue(cue_id=1, start=0, end=1, source="ありがとう")

    for _ in range(2):
        translated = asyncio.run(translate_cues([cue], config, http_client=client))
        assert render(translated[0].target) == "谢谢"

    assert len(client.token_requests) == 1
    assert client.token_requests[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert client.token_requests[0]["assertion"].count(".") == 2
    endpoint = (
        "https://aiplatform.googleapis.com/v1/projects/demo-project/locations/global"
        "/publishers/google/models/gemini-3-flash:generateContent"
    )
    assert client.calls == [(endpoint, {"Authorization": "Bearer ya29.token"})] * 2


def test_service_account_token_failure_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(vertex_auth, "_token_cache", {})
    config = _vertex_config(api_key=_service_account_json())

    with raises_rendered(TranslationError, "Vertex AI 认证失败.*Invalid JWT Signature"):
        asyncio.run(probe_translation_responses(_ServiceAccountClient(token_status=400), config))
