import re
from pathlib import Path

import pytest
from conftest import PROTOCOL, prepare_server
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.orchestrator import auth, dashboard
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.auth import AdminAuth
from video_turbo.orchestrator.context import require_worker
from video_turbo.orchestrator.worker_gateway import is_worker_route
from video_turbo.protocol import PROTOCOL_HEADER, PROTOCOL_MISMATCH_STATUS, WORKER_PROTOCOL_VERSION


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    prepare_server(tmp_path, monkeypatch, worker_token="worker-secret")
    monkeypatch.setattr(auth, "ENFORCE", True)
    # 不依赖本地是否构建过前端。
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(dashboard, "DIST_DIR", dist)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def test_first_visit_requires_setup_then_grants_session(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    page = client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 303 and page.headers["location"] == "/login"
    blocked = client.get("/api/status")
    assert blocked.status_code == 401 and blocked.json()["code"] == "setup_required"
    assert client.put("/api/settings/ui", json={}).status_code == 401
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/api/auth/status").json()["configured"] is False

    assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 422
    response = client.post("/api/auth/setup", json={"password": "correct horse"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/status").status_code == 200
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 200
    assert client.get("/api/auth/status").json() == {
        "configured": True, "authenticated": True, "min_password_length": 8,
        "setup_allowed": True, "desktop_session": False,
    }
    # 已设定后不能再次走首次设定流程覆盖口令。
    assert client.post("/api/auth/setup", json={"password": "another one"}).status_code == 409


def test_login_logout_and_lockout(client: TestClient) -> None:
    client.post("/api/auth/setup", json={"password": "correct horse"})
    assert client.post("/api/auth/logout").status_code == 200
    blocked = client.get("/api/status")
    assert blocked.status_code == 401 and blocked.json()["code"] == "login_required"

    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.get("/api/status").status_code == 200

    client.post("/api/auth/logout")
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 429


def test_changing_password_invalidates_other_sessions(client: TestClient) -> None:
    client.post("/api/auth/setup", json={"password": "correct horse"})
    old_cookie = client.cookies.get(auth.COOKIE_NAME)
    wrong = client.post(
        "/api/auth/password",
        json={"current_password": "nope", "new_password": "battery staple"},
    )
    assert wrong.status_code == 403
    changed = client.post(
        "/api/auth/password",
        json={"current_password": "correct horse", "new_password": "battery staple"},
    )
    assert changed.status_code == 200
    assert client.get("/api/status").status_code == 200

    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, old_cookie)
    assert client.get("/api/status").status_code == 401
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "battery staple"}).status_code == 200


def test_worker_routes_use_worker_token_not_session(client: TestClient) -> None:
    client.post("/api/auth/setup", json={"password": "correct horse"})
    client.cookies.clear()
    payload = {"name": "gpu-01", "gpu_name": "RTX", "gpu_memory_mb": 1000}
    assert client.post("/api/workers/register", json=payload).status_code == 401
    registered = client.post(
        "/api/workers/register", json=payload, headers={**PROTOCOL, "X-Worker-Token": "worker-secret"}
    )
    assert registered.status_code in {200, 422}
    # Worker 密钥不能用来访问管理接口。
    assert client.get("/api/status", headers={**PROTOCOL, "X-Worker-Token": "worker-secret"}).status_code == 401


def _api_routes(routes=None):
    """包括 include_router 挂载的路由（这些在 app.routes 中是一个整体）。"""
    for route in app.routes if routes is None else routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _api_routes(route.original_router.routes)


def test_every_route_skipping_admin_auth_requires_worker_token() -> None:
    exempt = 0
    for route in _api_routes():
        sample = route.path.replace("{", "").replace("}", "")
        for method in route.methods:
            if is_worker_route(method, sample):
                exempt += 1
                assert require_worker in [d.call for d in route.dependant.dependencies], route.path
    assert exempt >= 12


def test_every_worker_token_route_is_reachable_by_workers() -> None:
    """反向检查：用 require_worker 的接口都必须在 WORKER_ROUTES 中，否则 GPU 机器端口和登录校验会拦下它。"""
    for route in _api_routes():
        if require_worker not in [d.call for d in route.dependant.dependencies]:
            continue
        sample = re.sub(r"\{[^}]+\}", "x", route.path.replace("{plugin_sha256}", "a" * 64))
        for method in route.methods:
            assert is_worker_route(method, sample), f"{method} {route.path}"


def test_worker_with_other_protocol_version_is_rejected(client: TestClient) -> None:
    payload = {"name": "gpu-01", "gpu_name": "RTX", "gpu_memory_mb": 1000}
    for protocol in ({}, {PROTOCOL_HEADER: str(WORKER_PROTOCOL_VERSION + 1)}):
        response = client.post(
            "/api/workers/register", json=payload, headers={**protocol, "X-Worker-Token": "worker-secret"}
        )
        assert response.status_code == PROTOCOL_MISMATCH_STATUS
        assert "升级到同一版本" in render(response.json()["detail"])
    # 密钥错误时先报密钥，不泄露协议版本。
    wrong = client.post("/api/workers/register", json=payload, headers={"X-Worker-Token": "wrong"})
    assert wrong.status_code == 401


def test_tokens_expire_and_reject_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    admin = AdminAuth(database)
    assert not admin.configured and not admin.verify_token("1.abc")
    admin.set_password("correct horse")
    token = admin.issue_token()
    assert admin.verify_token(token)
    expires, _, signature = token.partition(".")
    assert not admin.verify_token(f"{int(expires) + 60}.{signature}")
    assert not admin.verify_token(None) and not admin.verify_token("garbage")
    # 重启后从数据库读取同一份记录，已有会话继续有效，且数据库里没有明文口令。
    assert AdminAuth(database).verify_token(token)
    assert "correct horse" not in str(database.runtime_json(auth.SETTING_KEY))
    monkeypatch.setattr(auth.time, "time", lambda: int(expires) + 1)
    assert not admin.verify_token(token)


def test_oversized_bodies_are_rejected_before_being_read(client: TestClient) -> None:
    # 未登录：登录接口只收一个口令。
    big = "x" * (auth.PUBLIC_BODY_LIMIT + 1)
    assert client.post("/api/auth/login", json={"password": big}).status_code == 413
    # 不声明长度（分块传输）时在读取过程中截断。
    chunked = client.post("/api/auth/login", content=iter([big.encode()]))
    assert chunked.status_code == 413
    # Worker 接口先验密钥：密钥不对的大请求不读请求体。
    huge = b"x" * (auth.WORKER_BODY_LIMIT + 1)
    assert client.post("/api/workers/register", content=huge).status_code == 401
    token = {**PROTOCOL, "X-Worker-Token": "worker-secret"}
    assert client.post("/api/workers/register", content=huge, headers=token).status_code == 413
    # 影片修复回传是流式写盘，不受这个上限约束（由接口自己校验租约和分块大小）。
    upload = client.put("/api/restore/jobs/nope/upload?offset=0", content=huge[:1024], headers=token)
    assert upload.status_code == 409

    client.post("/api/auth/setup", json={"password": "correct horse"})
    oversized = client.put("/api/config/scan", content=b"x" * (auth.ADMIN_BODY_LIMIT + 1))
    assert oversized.status_code == 413
    assert client.put("/api/config/scan", json={"settle_minutes": 10}).status_code == 200


def test_cross_site_requests_are_rejected(client: TestClient) -> None:
    client.post("/api/auth/setup", json={"password": "correct horse"})

    def pause(**headers: str) -> int:
        return client.post("/api/pipeline/pause", headers=headers).status_code

    # 同一台机器上另一个端口的网页带着会话 Cookie 发起请求。
    assert pause(Origin="http://testserver:8096") == 403
    assert pause(Origin="https://evil.example") == 403
    assert pause(**{"Sec-Fetch-Site": "same-site"}) == 403
    assert pause(**{"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"}) == 403
    assert client.post("/api/auth/login", json={"password": "x"}, headers={"Origin": "http://evil"}).status_code == 403
    assert client.get("/api/status").json()["pipeline_paused"] is False

    # 控制台自己的页面，以及不带这些请求头的非浏览器客户端。
    assert pause(Origin="http://testserver") == 200
    # HTTPS 反向代理后主控看到的 Host 可能不同，以浏览器的 Sec-Fetch-Site 为准。
    assert pause(**{"Sec-Fetch-Site": "same-origin", "Origin": "https://nas.example"}) == 200
    assert pause() == 200
    # 读取不受限制；Worker 接口不使用 Cookie，也不受限制。
    assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 200
    registered = client.post(
        "/api/workers/register",
        json={},
        headers={**PROTOCOL, "X-Worker-Token": "worker-secret", "Origin": "https://evil.example"},
    )
    assert registered.status_code == 422


def test_api_docs_are_disabled(client: TestClient) -> None:
    client.post("/api/auth/setup", json={"password": "correct horse"})
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


@pytest.fixture
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """桌面版：外壳给出令牌；返回按客户端地址创建 TestClient 的函数。"""
    monkeypatch.setenv("VIDEO_TURBO_DEPLOYMENT", "desktop")
    monkeypatch.setenv(auth.DESKTOP_TOKEN_ENV, "shell-token")
    prepare_server(tmp_path, monkeypatch, worker_token="worker-secret")
    monkeypatch.setattr(auth, "ENFORCE", True)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(dashboard, "DIST_DIR", dist)

    def make(host: str) -> TestClient:
        return TestClient(app, follow_redirects=False, client=(host, 50000))

    return make


def test_desktop_window_signs_in_without_password(desktop) -> None:
    with desktop("127.0.0.1") as local:
        assert local.get("/api/auth/desktop", params={"token": "wrong"}).status_code == 403
        response = local.get("/api/auth/desktop", params={"token": "shell-token"})
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert local.get("/api/status").status_code == 200
        status = local.get("/api/auth/status").json()
        assert status["configured"] is False and status["desktop_session"] is True
        assert status["setup_allowed"] is True
        # 在应用内窗口里设定口令（给其他设备用），窗口自己的会话保持不变。
        setup = local.post("/api/auth/setup", json={"password": "correct horse"})
        assert setup.status_code == 200 and "set-cookie" not in setup.headers
        assert local.get("/api/auth/status").json()["desktop_session"] is True
        assert local.get("/api/status").status_code == 200


def test_desktop_password_can_only_be_set_on_this_computer(desktop) -> None:
    with desktop("192.168.1.20") as remote:
        # 局域网里的设备拿不到外壳的令牌，也不能抢先设定口令。
        assert remote.get("/api/auth/desktop", params={"token": "shell-token"}).status_code == 403
        assert remote.get("/api/auth/status").json()["setup_allowed"] is False
        assert remote.post("/api/auth/setup", json={"password": "correct horse"}).status_code == 403
        assert remote.get("/api/status").status_code == 401


def test_desktop_sessions_end_when_the_main_app_restarts(desktop) -> None:
    with desktop("127.0.0.1") as local:
        local.get("/api/auth/desktop", params={"token": "shell-token"})
        cookie = local.cookies.get(auth.COOKIE_NAME)
    assert cookie and cookie.startswith(auth.DESKTOP_PREFIX)
    with desktop("127.0.0.1") as restarted:
        restarted.cookies.set(auth.COOKIE_NAME, cookie)
        assert restarted.get("/api/status").status_code == 401


def test_desktop_login_is_off_without_a_shell_token(client: TestClient) -> None:
    assert client.get("/api/auth/desktop", params={"token": ""}).status_code == 403
