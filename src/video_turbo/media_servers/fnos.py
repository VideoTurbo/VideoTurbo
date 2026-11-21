"""内置的飞牛影视（飞牛 fnOS 自带的媒体服务器）。

飞牛影视没有公开 API，这里按它网页客户端的协议实现：每个请求带 ``authx`` 签名头，
用账号密码登录拿到令牌，再调用“扫描全部媒体库”。飞牛升级改了协议时这里需要跟着更新。

- 地址是飞牛 NAS 的网页地址（默认端口 5666），影视接口在 ``/v`` 下；
- 开启了访问码的设备先访问 ``/c/<访问码>``，通过后同一连接里的请求才能访问登录接口；
- 响应是 ``{"code": 0, "msg": "...", "data": ...}``，code 不为 0 表示业务失败。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, ClassVar
from urllib.parse import quote

from ..i18n import localized, msg
from ..plugins.base import (
    CredentialField,
    PluginCallError,
    builtin_icon,
    normalize_base_url,
    response_error,
)
from .base import MediaServer

# 飞牛影视网页客户端参与签名的固定串和客户端标识，与网页客户端一致。
_SIGN_SALT = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
_CLIENT_KEY = "16CCEB3D-AB42-077D-36A1-F355324E4237"
_APP_NAME = "trimemedia-web"


class _ApiMissing(PluginCallError):
    """接口不存在（404 或返回的不是飞牛影视的响应）：旧版飞牛影视没有 v2 登录接口。"""


class _ApiError(PluginCallError):
    """飞牛影视返回业务错误码。"""


def _md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def authx(path: str, body: str, *, nonce: str | None = None, timestamp: str | None = None) -> str:
    """请求的 authx 签名头。path 是以 /v 开头的接口路径，body 是请求正文（GET 时为空）。"""
    nonce = nonce or str(100000 + secrets.randbelow(900000))
    timestamp = timestamp or str(int(time.time() * 1000))
    sign = _md5("_".join([_SIGN_SALT, path, nonce, timestamp, _md5(body), _CLIENT_KEY]))
    return f"nonce={nonce}&timestamp={timestamp}&sign={sign}"


class FnosServer(MediaServer):
    id: ClassVar[str] = "fnos"
    label: ClassVar[str] = localized({"zh-Hans": "飞牛影视", "en": "fnOS Video"})
    homepage: ClassVar[str] = "https://www.fnnas.com"
    icon: ClassVar[str] = builtin_icon("fnos.svg")
    description: ClassVar[str] = msg("mediaServer.fnos.description")
    credential_fields: ClassVar[tuple[CredentialField, ...]] = (
        CredentialField(
            "url",
            msg("mediaServer.field.url"),
            secret=False,
            placeholder="http://192.168.1.10:5666",
            help=msg("mediaServer.fnos.urlHelp"),
            binds_secrets=True,
        ),
        CredentialField(
            "username",
            msg("mediaServer.fnos.username"),
            secret=False,
            help=msg("mediaServer.fnos.usernameHelp"),
        ),
        CredentialField(
            "password",
            msg("mediaServer.fnos.password"),
            help=msg("mediaServer.field.keyHelp"),
        ),
        CredentialField(
            "access_code",
            msg("mediaServer.fnos.accessCode"),
            required=False,
            help=msg("mediaServer.fnos.accessCodeHelp"),
        ),
    )

    _token: str | None = None

    @classmethod
    def validate_values(cls, values: dict[str, Any]) -> dict[str, Any]:
        url = normalize_base_url(values["url"])
        # 影视接口在 /v 下；用户填了影视页面的地址时去掉，统一保存 NAS 的网页地址。
        return {**values, "url": url.removesuffix("/v")}

    async def _call(
        self,
        api: str,
        data: dict[str, Any] | None = None,
        *,
        method: str | None = None,
        version: str = "v1",
    ) -> Any:
        """调用影视接口，返回 data；业务失败时抛出 PluginCallError。"""
        path = f"/v/api/{version}{api}"
        method = method or ("GET" if data is None else "POST")
        body = json.dumps(data, ensure_ascii=False) if data else ""
        headers = {"Accept": "application/json", "authx": authx(path, body)}
        if method != "GET":
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = self._token
        response = await self._client.request(
            method, self.values["url"] + path, headers=headers, content=body.encode() if body else None
        )
        if response.status_code == 404:
            raise _ApiMissing(msg("plugins.call.error", service=self.label, status_code=404))
        if error := response_error(response, self.label):
            raise error
        try:
            result = response.json()
        except ValueError as exc:
            raise _ApiMissing(msg("mediaServer.notServer", label=self.label)) from exc
        if not isinstance(result, dict) or "code" not in result:
            raise _ApiMissing(msg("mediaServer.notServer", label=self.label))
        code = result.get("code")
        if code != 0:
            raise _ApiError(msg("mediaServer.fnos.apiError", code=code, message=result.get("msg") or ""))
        return result.get("data")

    async def _check_access_code(self) -> None:
        code = str(self.values.get("access_code") or "")
        if not code:
            return
        response = await self._client.get(f"{self.values['url']}/c/{quote(code, safe='')}")
        if response.status_code == 404:
            raise PluginCallError(msg("mediaServer.fnos.accessCodeWrong"))
        if response.status_code >= 400:
            raise PluginCallError(
                msg("plugins.call.error", service=self.label, status_code=response.status_code)
            )

    async def _login(self) -> None:
        if self._token:
            return
        await self._check_access_code()
        username = str(self.values["username"])
        password = str(self.values["password"])
        try:
            data = await self._call(
                "/user/loginByPassword",
                {
                    "username": username,
                    "password": hashlib.sha256(password.encode()).hexdigest(),
                    "app_name": _APP_NAME,
                },
                version="v2",
            )
        except _ApiError as exc:
            raise PluginCallError(msg("mediaServer.fnos.loginFailed", reason=exc.message)) from exc
        except _ApiMissing:
            # 旧版飞牛影视没有 v2 登录接口，退回 v1 明文登录；账号密码错误时上面已经直接报错。
            try:
                data = await self._call(
                    "/login", {"username": username, "password": password, "app_name": _APP_NAME}
                )
            except _ApiError as exc:
                raise PluginCallError(msg("mediaServer.fnos.loginFailed", reason=exc.message)) from exc
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise PluginCallError(msg("mediaServer.fnos.loginFailed", reason=""))
        self._token = str(token)

    async def refresh(self) -> None:
        await self._login()
        started = await self._call("/mdb/scanall", {}, method="POST")
        if not started:
            raise PluginCallError(msg("mediaServer.fnos.scanNotStarted"))

    async def test(self) -> str:
        await self._login()
        info = await self._call("/sys/version")
        version = ""
        if isinstance(info, dict):
            version = str(info.get("mediasrvVersion") or info.get("version") or "")
        return msg("mediaServer.testOk", name=self.label, version=version).rstrip()
