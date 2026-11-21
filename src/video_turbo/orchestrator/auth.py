"""控制台管理口令：登录页 + 会话 Cookie。

首次打开控制台时设定口令，scrypt 哈希后保存在 ``runtime_settings.admin_auth``。登录后发放
HttpOnly 会话 Cookie（到期时间 + HMAC 签名，签名密钥与口令一起保存）；修改口令会更换密钥，
其他设备上的旧会话随即失效。

Worker 接口（``worker_gateway.WORKER_ROUTES``）不走会话校验，由 ``X-Worker-Token`` 校验。

中间件在读取请求体之前完成全部检查：

- 会话或 Worker 密钥无效的请求直接拒绝，请求体不会被读入内存；
- 请求体有大小上限（未登录的登录接口最小），超过时返回 413；影片修复回传是流式写盘，自带上限；
- 修改数据的请求必须来自控制台自己的页面（``Sec-Fetch-Site`` / ``Origin``），其他网页即使带着
  会话 Cookie 发起请求也会被拒绝。不带这两个请求头的非浏览器客户端不受影响。
忘记口令时停止主控，删除 ``runtime_settings`` 中 ``key = 'admin_auth'`` 的记录，再启动后重新设定。

桌面版：外壳每次启动主程序时生成随机令牌（``VIDEO_TURBO_DESKTOP_TOKEN``），应用内窗口打开
``/api/auth/desktop?token=…`` 即登录，不需要口令。这种会话用每次启动随机生成、只在内存里的密钥签名，
主程序重启后失效（外壳会带着新令牌重新登录）。局域网里的其他设备仍要口令，而且口令只能在这台电脑上设定，
避免第一个连上来的设备抢先设定。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..database import Database
from ..deployment import is_desktop
from ..environment import getenv
from ..i18n import msg
from ..security import secure_equals
from .context import app_state
from .worker_gateway import is_worker_route

SETTING_KEY = "admin_auth"
DESKTOP_TOKEN_ENV = "VIDEO_TURBO_DESKTOP_TOKEN"
# 桌面版应用内窗口的会话以此开头，用内存里的密钥签名。
DESKTOP_PREFIX = "d."
LOOPBACK = frozenset({"127.0.0.1", "::1"})
DESKTOP_TOKEN_HEADER = "X-VideoTurbo-Desktop-Token"
COOKIE_NAME = "jf_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 300
# 测试可以关闭整体校验；生产环境没有对应的开关。
ENFORCE = True

PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/login",
        "/favicon.svg",
        "/favicon.ico",
        "/api/auth/status",
        "/api/auth/setup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/desktop",
        "/api/auth/desktop/worker",
        "/api/auth/desktop/local-worker",
    }
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 请求体上限。登录相关接口无需登录即可访问，只收一个口令。
PUBLIC_BODY_LIMIT = 64 * 1024
ADMIN_BODY_LIMIT = 8 * 1024 * 1024
# Worker 回传的识别结果（整部影片的字幕与元数据）。
WORKER_BODY_LIMIT = 64 * 1024 * 1024
# 影片修复回传边收边写盘，不读入内存，由接口自己限制分块大小。
_STREAMED_UPLOAD = re.compile(r"^/api/restore/jobs/[^/]+/upload(/chunk)?$")

LOGIN_HTML = (Path(__file__).with_name("static") / "login.html").read_text(
    encoding="utf-8"
)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


class AdminAuth:
    """口令与会话校验。记录缓存在内存中，请求鉴权不查数据库。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        record = database.runtime_json(SETTING_KEY)
        self._record: dict[str, str] | None = record if isinstance(record, dict) else None
        self._failures: dict[str, tuple[int, float]] = {}
        self._desktop_token = (getenv(DESKTOP_TOKEN_ENV) or "").strip()
        self._desktop_secret = secrets.token_hex(32) if self._desktop_token else ""

    @property
    def configured(self) -> bool:
        return self._record is not None

    def set_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        record = {
            "salt": salt.hex(),
            "hash": _hash_password(password, salt),
            "secret": secrets.token_hex(32),
        }
        self.database.set_runtime_json(SETTING_KEY, record)
        self._record = record

    def verify_password(self, password: str) -> bool:
        record = self._record
        if record is None:
            return False
        candidate = _hash_password(password, bytes.fromhex(record["salt"]))
        return hmac.compare_digest(candidate, record["hash"])

    def _sign(self, secret: str, expires: int) -> str:
        return hmac.new(bytes.fromhex(secret), str(expires).encode(), hashlib.sha256).hexdigest()

    def issue_token(self) -> str:
        if self._record is None:
            raise RuntimeError("尚未设定管理口令")
        expires = int(time.time()) + SESSION_SECONDS
        return f"{expires}.{self._sign(self._record['secret'], expires)}"

    def check_desktop_token(self, token: str) -> bool:
        return bool(self._desktop_token) and secure_equals(token, self._desktop_token)

    def issue_desktop_token(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        return f"{DESKTOP_PREFIX}{expires}.{self._sign(self._desktop_secret, expires)}"

    @staticmethod
    def is_desktop_session(token: str | None) -> bool:
        return bool(token) and str(token).startswith(DESKTOP_PREFIX)

    def verify_token(self, token: str | None) -> bool:
        if not token:
            return False
        if token.startswith(DESKTOP_PREFIX):
            secret = self._desktop_secret
            token = token[len(DESKTOP_PREFIX):]
        else:
            secret = self._record["secret"] if self._record is not None else ""
        if not secret:
            return False
        raw_expires, _, signature = token.partition(".")
        if not raw_expires.isdigit() or int(raw_expires) < time.time():
            return False
        return hmac.compare_digest(signature, self._sign(secret, int(raw_expires)))

    def blocked(self, client: str) -> bool:
        count, started = self._failures.get(client, (0, 0.0))
        if time.monotonic() - started > FAILURE_WINDOW_SECONDS:
            self._failures.pop(client, None)
            return False
        return count >= MAX_FAILURES

    def record_failure(self, client: str) -> None:
        now = time.monotonic()
        count, started = self._failures.get(client, (0, now))
        if now - started > FAILURE_WINDOW_SECONDS:
            count, started = 0, now
        self._failures[client] = (count + 1, started)

    def clear_failures(self, client: str) -> None:
        self._failures.pop(client, None)


def _cookie(scope: dict[str, Any], name: str) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                cookie_name, _, cookie_value = part.strip().partition("=")
                if cookie_name == name:
                    return cookie_value
    return None


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1")
    return ""


async def _reject(send: Any, status: int, detail: str, code: str | None = None) -> None:
    payload = {"detail": detail} if code is None else {"detail": detail, "code": code}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _cross_site(scope: dict[str, Any]) -> bool:
    """请求是否由其他网站的页面发起。

    浏览器在 HTTPS 和 localhost 下发送 Sec-Fetch-Site；局域网明文 HTTP 下只有 Origin，
    与 Host 比较主机和端口（不比较协议：HTTPS 反向代理后主控看到的是 http）。
    """
    fetch_site = _header(scope, b"sec-fetch-site").strip().lower()
    if fetch_site:
        return fetch_site not in {"same-origin", "none"}
    origin = _header(scope, b"origin").strip().lower()
    if not origin:
        return False
    return origin.partition("://")[2] != _header(scope, b"host").strip().lower()


def _limited(receive: Any, limit: int) -> Any:
    received = 0

    async def limited_receive() -> dict[str, Any]:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                # 在接口读取请求体时抛出，由应用的异常处理返回 413。
                raise StarletteHTTPException(status_code=413, detail=msg("auth.tooLarge"))
        return message

    return limited_receive


class AdminAuthMiddleware:
    """除登录相关接口和 Worker 接口外，所有请求都需要有效的会话 Cookie。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def _forward(
        self, scope: dict[str, Any], receive: Any, send: Any, limit: int | None
    ) -> None:
        if scope["type"] != "http" or limit is None:
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared.isdigit() and int(declared) > limit:
            await _reject(send, 413, msg("auth.tooLarge"))
            return
        await self.app(scope, _limited(receive, limit), send)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        if is_worker_route(method, path):
            # 在读取请求体之前校验密钥：未授权的大请求不会被读入内存。
            expected = getattr(scope["app"].state, "worker_token", None)
            if not expected or not secure_equals(_header(scope, b"x-worker-token"), expected):
                await _reject(send, 401, msg("common.worker_token_invalid"))
                return
            streamed = method == "PUT" and _STREAMED_UPLOAD.match(path)
            await self._forward(scope, receive, send, None if streamed else WORKER_BODY_LIMIT)
            return
        if scope["type"] == "http" and method not in SAFE_METHODS and _cross_site(scope):
            await _reject(send, 403, msg("auth.crossSite"), "cross_site")
            return
        if path in PUBLIC_PATHS:
            await self._forward(scope, receive, send, PUBLIC_BODY_LIMIT)
            return
        auth: AdminAuth | None = getattr(scope["app"].state, "admin_auth", None)
        if not ENFORCE or (auth is not None and auth.verify_token(_cookie(scope, COOKIE_NAME))):
            await self._forward(scope, receive, send, ADMIN_BODY_LIMIT)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        page_request = (
            method == "GET"
            and not path.startswith("/api/")
            and "text/html" in _header(scope, b"accept")
        )
        if page_request:
            await send(
                {
                    "type": "http.response.start",
                    "status": 303,
                    "headers": [(b"location", b"/login"), (b"cache-control", b"no-store")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        setup = auth is not None and not auth.configured
        await _reject(
            send,
            401,
            msg("auth.setupFirst") if setup else msg("auth.loginFirst"),
            "setup_required" if setup else "login_required",
        )


class PasswordPayload(BaseModel):
    password: str = Field(max_length=200)


class NewPasswordPayload(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


router = APIRouter()


def _auth(request: Request) -> AdminAuth:
    return app_state(request).admin_auth


def _client(request: Request) -> str:
    return request.client.host if request.client else ""


def _setup_allowed(request: Request) -> bool:
    """桌面版只允许在这台电脑上设定口令；Docker 版沿用“首次打开的人设定”。"""
    return not is_desktop() or _client(request) in LOOPBACK


def _start_session(request: Request, response: Response, token: str | None = None) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token or _auth(request).issue_token(),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _renew_session(request: Request, response: Response) -> None:
    """设定或修改口令后为当前浏览器发放新会话；桌面版应用内窗口保留原来不需要口令的会话。"""
    current = request.cookies.get(COOKIE_NAME)
    auth = _auth(request)
    if auth.is_desktop_session(current) and auth.verify_token(current):
        return
    _start_session(request, response)


@router.get("/api/auth/desktop/worker", include_in_schema=False)
async def desktop_worker(request: Request) -> dict:
    """桌面版外壳开启本机识别时取接入信息：只接受本机请求和这次启动的令牌（请求头）。"""
    auth = _auth(request)
    if _client(request) not in LOOPBACK or not auth.check_desktop_token(request.headers.get(DESKTOP_TOKEN_HEADER, "")):
        raise HTTPException(status_code=403, detail=msg("auth.desktopTokenInvalid"))
    state = app_state(request)
    return {"worker_token": state.worker_token, "worker_port": state.config.server.worker_port}


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store"})


@router.get("/api/auth/status")
async def auth_status(request: Request) -> dict:
    auth = _auth(request)
    session = request.cookies.get(COOKIE_NAME)
    return {
        "configured": auth.configured,
        "authenticated": not ENFORCE or auth.verify_token(session),
        "min_password_length": MIN_PASSWORD_LENGTH,
        "setup_allowed": _setup_allowed(request),
        # 桌面版应用内窗口：不需要口令，控制台不显示“退出登录”。
        "desktop_session": auth.is_desktop_session(session) and auth.verify_token(session),
    }


@router.get("/api/auth/desktop", include_in_schema=False)
async def auth_desktop(request: Request, token: str = "") -> Response:
    """桌面版应用内窗口带着外壳给的令牌打开这里，登录后进入控制台。只接受本机请求。"""
    auth = _auth(request)
    if _client(request) not in LOOPBACK or not auth.check_desktop_token(token):
        raise HTTPException(status_code=403, detail=msg("auth.desktopTokenInvalid"))
    response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
    _start_session(request, response, auth.issue_desktop_token())
    return response


@router.post("/api/auth/setup")
async def auth_setup(request: Request, response: Response, payload: NewPasswordPayload) -> dict:
    auth = _auth(request)
    if not _setup_allowed(request):
        raise HTTPException(status_code=403, detail=msg("auth.setupLocalOnly"))
    async with app_state(request).admin_auth_lock:
        if auth.configured:
            raise HTTPException(status_code=409, detail=msg("auth.alreadySet"))
        await asyncio.to_thread(auth.set_password, payload.password)
    _renew_session(request, response)
    return {"ok": True}


@router.post("/api/auth/login")
async def auth_login(request: Request, response: Response, payload: PasswordPayload) -> dict:
    auth = _auth(request)
    if not auth.configured:
        raise HTTPException(status_code=409, detail=msg("auth.notSet"))
    client = _client(request)
    if auth.blocked(client):
        raise HTTPException(status_code=429, detail=msg("auth.tooMany"))
    if not await asyncio.to_thread(auth.verify_password, payload.password):
        auth.record_failure(client)
        raise HTTPException(status_code=401, detail=msg("auth.wrong"))
    auth.clear_failures(client)
    _start_session(request, response)
    return {"ok": True}


@router.post("/api/auth/logout")
async def auth_logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/api/auth/password")
async def auth_change_password(
    request: Request, response: Response, payload: ChangePasswordPayload
) -> dict:
    auth = _auth(request)
    client = _client(request)
    if auth.blocked(client):
        raise HTTPException(status_code=429, detail=msg("auth.tooMany"))
    async with app_state(request).admin_auth_lock:
        if not await asyncio.to_thread(auth.verify_password, payload.current_password):
            auth.record_failure(client)
            raise HTTPException(status_code=403, detail=msg("auth.wrongCurrent"))
        auth.clear_failures(client)
        await asyncio.to_thread(auth.set_password, payload.new_password)
    # 签名密钥已更换，其他设备的会话失效；为当前浏览器重新发放。
    _renew_session(request, response)
    return {"ok": True}
