"""内置的通用 Webhook：以 JSON POST 发送每条提醒，可选 HMAC-SHA256 签名。

请求体::

    {"event": "notice", "id": 12, "level": "warning", "title": "…", "message": "…",
     "created_at": "2026-01-01T00:00:00+00:00", "source": "video-turbo"}

填写签名密钥时附带请求头 ``X-VideoTurbo-Timestamp``（Unix 秒）和
``X-VideoTurbo-Signature: sha256=<hex>``，签名内容为 ``<timestamp>.<请求体原始字节>``。
接收端应使用常量时间比较，并拒绝时间戳与当前时间相差过大的请求以防重放。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import ClassVar
from urllib.parse import urlsplit

from .. import __version__
from ..i18n import msg
from ..plugins.base import CredentialField, PluginConfigError, builtin_icon, response_error
from .base import Notice, Notifier


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


class WebhookNotifier(Notifier):
    id: ClassVar[str] = "webhook"
    label: ClassVar[str] = "Webhook"
    icon: ClassVar[str] = builtin_icon("webhook.svg")
    description: ClassVar[str] = msg("webhook.description")
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        CredentialField(
            "url",
            msg("webhook.field.url"),
            placeholder="https://example.com/hooks/video-turbo",
            help=msg("webhook.field.urlHelp"),
            binds_secrets=True,
        ),
        CredentialField(
            "secret",
            msg("webhook.field.secret"),
            required=False,
            placeholder=msg("webhook.field.secretPlaceholder"),
            help=msg("webhook.field.secretHelp"),
            trim=False,
        ),
    )

    @classmethod
    def validate_values(cls, values: dict[str, str]) -> dict[str, str]:
        parts = urlsplit(values["url"])
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise PluginConfigError(msg("webhook.urlScheme"))
        if parts.username or parts.password:
            raise PluginConfigError(msg("webhook.urlCredentials"))
        # 查询参数里常带令牌，原样保留；# 之后的内容不会发给服务器，去掉。
        return {**values, "url": parts._replace(fragment="").geturl()}

    @classmethod
    def summary(cls, values: dict[str, str]) -> str:
        return urlsplit(values.get("url", "")).hostname or ""

    async def send(self, notice: Notice) -> None:
        body = json.dumps(
            {
                "event": "notice",
                "id": notice.id,
                "level": notice.level,
                "title": notice.title,
                "message": notice.message,
                "created_at": notice.created_at,
                "source": "video-turbo",
            },
            ensure_ascii=False,
        ).encode()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"VideoTurbo/{__version__}",
        }
        if secret := self.values.get("secret"):
            timestamp = str(int(time.time()))
            headers["X-VideoTurbo-Timestamp"] = timestamp
            headers["X-VideoTurbo-Signature"] = sign(secret, timestamp, body)
        response = await self._client.post(self.values["url"], content=body, headers=headers)
        if error := response_error(response, "Webhook"):
            raise error
