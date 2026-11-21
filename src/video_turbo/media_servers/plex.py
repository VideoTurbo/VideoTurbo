"""内置的 Plex Media Server：用 X-Plex-Token 认证，刷新全部媒体库。"""

from __future__ import annotations

from typing import ClassVar

import httpx

from ..i18n import msg
from ..plugins.base import (
    CredentialField,
    PluginCallError,
    builtin_icon,
    normalize_base_url,
    response_error,
)
from .base import MediaServer


class PlexServer(MediaServer):
    id: ClassVar[str] = "plex"
    label: ClassVar[str] = "Plex"
    homepage: ClassVar[str] = "https://www.plex.tv"
    icon: ClassVar[str] = builtin_icon("plex.svg")
    description: ClassVar[str] = msg("mediaServer.plex.description")
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        CredentialField(
            "url",
            msg("mediaServer.field.url"),
            secret=False,
            placeholder="http://192.168.1.10:32400",
            help=msg("mediaServer.field.urlHelp"),
            binds_secrets=True,
        ),
        CredentialField(
            "token",
            "X-Plex-Token",
            placeholder=msg("mediaServer.plex.tokenPlaceholder"),
            help=msg("mediaServer.field.keyHelp"),
        ),
    )

    @classmethod
    def validate_values(cls, values: dict[str, str]) -> dict[str, str]:
        return {**values, "url": normalize_base_url(values["url"])}

    async def _request(self, path: str) -> httpx.Response:
        # Plex 默认返回 XML，Accept 要求返回 JSON。
        response = await self._client.get(
            self.values["url"] + path,
            headers={"X-Plex-Token": self.values["token"], "Accept": "application/json"},
        )
        if response.status_code in {401, 403}:
            raise PluginCallError(msg("mediaServer.plex.forbidden", status_code=response.status_code))
        if error := response_error(response, self.label):
            raise error
        return response

    async def refresh(self) -> None:
        await self._request("/library/sections/all/refresh")

    async def test(self) -> str:
        response = await self._request("/")
        try:
            info = response.json()
        except ValueError as exc:
            raise PluginCallError(msg("mediaServer.notServer", label=self.label)) from exc
        container = info.get("MediaContainer") if isinstance(info, dict) else None
        if not isinstance(container, dict) or not (
            container.get("friendlyName") or container.get("version")
        ):
            raise PluginCallError(msg("mediaServer.badInfo", label=self.label))
        name = container.get("friendlyName") or self.label
        return msg("mediaServer.testOk", name=name, version=container.get("version") or "").rstrip()
