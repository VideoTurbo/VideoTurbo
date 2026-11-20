"""内置的 Emby 与 Jellyfin。Jellyfin 源自 Emby，刷新和服务器信息接口相同，认证请求头不同。"""

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

_URL_FIELD = CredentialField(
    "url",
    msg("mediaServer.field.url"),
    secret=False,
    placeholder="http://192.168.1.10:8096",
    help=msg("mediaServer.field.urlHelp"),
    binds_secrets=True,
)


class EmbyServer(MediaServer):
    id: ClassVar[str] = "emby"
    label: ClassVar[str] = "Emby"
    homepage: ClassVar[str] = "https://emby.media"
    icon: ClassVar[str] = builtin_icon("emby.svg")
    description: ClassVar[str] = msg("mediaServer.emby.description")
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        _URL_FIELD,
        CredentialField(
            "api_key",
            "API Key",
            placeholder=msg("mediaServer.emby.keyPlaceholder"),
            help=msg("mediaServer.field.keyHelp"),
        ),
    )
    # 依次尝试的路径前缀：Emby 的接口同时挂在 /emby 下和根路径下。
    path_prefixes: ClassVar[tuple[str, ...]] = ("/emby", "")

    @classmethod
    def validate_values(cls, values: dict[str, str]) -> dict[str, str]:
        return {**values, "url": normalize_base_url(values["url"])}

    def headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self.values["api_key"]}

    async def _request(self, method: str, path: str) -> httpx.Response:
        response: httpx.Response | None = None
        for prefix in self.path_prefixes:
            response = await self._client.request(
                method, self.values["url"] + prefix + path, headers=self.headers()
            )
            if response.status_code != 404:
                break
        assert response is not None
        if error := response_error(response, self.label):
            raise error
        return response

    async def refresh(self) -> None:
        await self._request("POST", "/Library/Refresh")

    async def test(self) -> str:
        response = await self._request("GET", "/System/Info")
        try:
            info = response.json()
        except ValueError as exc:
            raise PluginCallError(
                msg("mediaServer.notServer", label=self.label)
            ) from exc
        if not isinstance(info, dict) or not (info.get("ServerName") or info.get("Version")):
            raise PluginCallError(msg("mediaServer.badInfo", label=self.label))
        name = info.get("ServerName") or self.label
        return msg("mediaServer.testOk", name=name, version=info.get('Version') or '').rstrip()


class JellyfinServer(EmbyServer):
    id: ClassVar[str] = "jellyfin"
    label: ClassVar[str] = "Jellyfin"
    homepage: ClassVar[str] = "https://jellyfin.org"
    icon: ClassVar[str] = builtin_icon("jellyfin.svg")
    description: ClassVar[str] = msg("mediaServer.jellyfin.description")
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        _URL_FIELD,
        CredentialField(
            "api_key",
            "API Key",
            placeholder=msg("mediaServer.jellyfin.keyPlaceholder"),
            help=msg("mediaServer.field.keyHelp"),
        ),
    )
    path_prefixes: ClassVar[tuple[str, ...]] = ("",)

    def headers(self) -> dict[str, str]:
        # 新版 Jellyfin 默认不再接受 X-Emby-Token 等旧写法。
        return {"Authorization": f'MediaBrowser Token="{self.values["api_key"]}"'}
