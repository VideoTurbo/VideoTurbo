"""Vertex AI 服务账号 JSON 认证：用私钥签发 JWT，向 Google 换取 OAuth 访问令牌。

节点的凭据字段要么是 API Key，要么是整份服务账号 JSON 文本；以 ``{`` 开头的按 JSON 处理。
"""

from __future__ import annotations

import base64
import json
from time import time
from typing import Any

from .i18n import msg

TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKEN_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
TOKEN_LIFETIME_SECONDS = 3600
# 令牌在过期前这段时间内就重新获取，避免请求途中过期。
TOKEN_REFRESH_MARGIN_SECONDS = 300
_REQUIRED_FIELDS = ("project_id", "private_key", "client_email")

# (client_email, private_key_id) -> (access_token, 过期的 Unix 时间)
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


class VertexAuthError(ValueError):
    """服务账号 JSON 无效，或换取访问令牌失败。"""


def is_service_account(credential: str | None) -> bool:
    return bool(credential) and credential.lstrip().startswith("{")


def parse_service_account(credential: str) -> dict[str, Any]:
    """校验并解析服务账号 JSON 文本。"""
    try:
        info = json.loads(credential)
    except ValueError as exc:
        raise VertexAuthError(msg("vertex.notJson")) from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise VertexAuthError(
            msg("vertex.notServiceAccount")
        )
    missing = [name for name in _REQUIRED_FIELDS if not isinstance(info.get(name), str) or not info[name].strip()]
    if missing:
        raise VertexAuthError(msg("vertex.incomplete", fields=missing))
    return info


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_assertion(info: dict[str, Any], now: float) -> str:
    """生成 RS256 签名的 JWT，用于 jwt-bearer 授权。"""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:
        raise VertexAuthError(msg("vertex.missingCrypto")) from exc

    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = info["private_key_id"]
    issued_at = int(now)
    claims = {
        "iss": info["client_email"],
        "scope": TOKEN_SCOPE,
        "aud": TOKEN_URL,
        "iat": issued_at,
        "exp": issued_at + TOKEN_LIFETIME_SECONDS,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    try:
        key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise VertexAuthError(msg("vertex.badKey")) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise VertexAuthError(msg("vertex.notRsa"))
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


async def access_token(client: Any, credential: str) -> str:
    """返回服务账号的访问令牌；未过期时复用缓存。client 是 httpx.AsyncClient。"""
    info = parse_service_account(credential)
    cache_key = (info["client_email"], str(info.get("private_key_id") or ""))
    now = time()
    cached = _token_cache.get(cache_key)
    if cached and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > now:
        return cached[0]
    response = await client.post(
        TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": build_assertion(info, now),
        },
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error_description") or body.get("error") or "")
        detail = detail or response.text.strip().replace("\n", " ")[:300] or msg("vertex.noContent")
        raise VertexAuthError(
            msg("vertex.tokenFailed", status_code=response.status_code, detail=detail, email=info['client_email'])
        )
    try:
        lifetime = float(body.get("expires_in") or TOKEN_LIFETIME_SECONDS)
    except (TypeError, ValueError):
        lifetime = float(TOKEN_LIFETIME_SECONDS)
    _token_cache[cache_key] = (body["access_token"], now + lifetime)
    return body["access_token"]
