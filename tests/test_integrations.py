"""内置的 Emby / Jellyfin / Webhook 插件、配置接口，以及后台刷新与通知推送。"""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from conftest import prepare_server, raises_rendered
from fastapi.testclient import TestClient

from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.media_servers.emby import EmbyServer, JellyfinServer
from video_turbo.notifiers.base import Notice
from video_turbo.notifiers.webhook import WebhookNotifier, sign
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.integrations import MediaRefresher, NoticeService
from video_turbo.plugins import registry
from video_turbo.plugins.base import (
    SECRET_MASK,
    CredentialField,
    PluginCallError,
    PluginConfigError,
    masked_values,
    merge_values,
)
from video_turbo.plugins.connections import Connections
from video_turbo.plugins.settings import PluginSettings

pytestmark = pytest.mark.anyio


class Recorder:
    """按路径返回预设响应，并记录收到的请求。"""

    def __init__(self, routes: dict[str, httpx.Response | int]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get(f"{request.method} {request.url.path}", 404)
        return httpx.Response(response) if isinstance(response, int) else response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# ---- 配置合并 ----------------------------------------------------------------


def test_changing_address_requires_secrets_again() -> None:
    fields = EmbyServer.credential_fields
    saved = {"url": "http://emby:8096", "api_key": "secret"}
    # 只改其他字段、或地址只差末尾斜杠：沿用已保存的 Key。
    assert merge_values(fields, saved, {"url": "http://emby:8096/"})["api_key"] == "secret"
    assert merge_values(fields, saved, {})["api_key"] == "secret"
    # 地址变了而 Key 留空：拒绝，避免 Key 被发往新地址。
    with raises_rendered(PluginConfigError, "地址已变化，请重新填写：API Key"):
        merge_values(fields, saved, {"url": "http://evil:8096"})
    moved = merge_values(fields, saved, {"url": "http://evil:8096", "api_key": "new"})
    assert moved == {"url": "http://evil:8096", "api_key": "new"}
    with raises_rendered(PluginConfigError, "请填写：地址、API Key"):
        merge_values(fields, {}, {})


def test_emby_address_validation() -> None:
    assert EmbyServer.merge_values({}, {"url": " http://emby:8096/ ", "api_key": "k"})["url"] == (
        "http://emby:8096"
    )
    for bad in ("emby:8096", "ftp://emby", "http://u:p@emby:8096", "http://emby:8096/?x=1"):
        with pytest.raises(PluginConfigError):
            EmbyServer.merge_values({}, {"url": bad, "api_key": "k"})


def test_field_types_coerce_and_validate() -> None:
    fields = (
        CredentialField("cookie", "Cookie", type="textarea"),
        CredentialField("note", "备注", secret=False, required=False),
        CredentialField("port", "端口", type="number", min=1, max=65535),
        CredentialField("ratio", "比例", type="number", integer=False, required=False),
        CredentialField("region", "区域", type="select", options=(("cn", "国内"), ("intl", "海外"))),
        CredentialField("fast", "加速", type="switch", secret=True),
    )
    # 非文本字段一律不是密钥；开关不是必填。
    assert [field.secret for field in fields] == [True, False, False, False, False, False]
    assert fields[5].required is False and fields[5].default is False
    merged = merge_values(
        fields, {}, {"cookie": " a=1 ", "note": "x", "port": "8080", "ratio": 0.5, "region": "intl", "fast": "true"}
    )
    assert merged == {"cookie": "a=1", "note": "x", "port": 8080, "ratio": 0.5, "region": "intl", "fast": True}
    # 没提交沿用；密钥提交空值沿用；非密钥提交空值清空；开关可以关掉。
    again = merge_values(fields, merged, {"cookie": "", "note": "", "fast": False})
    assert again == {"cookie": "a=1", "port": 8080, "ratio": 0.5, "region": "intl", "fast": False}
    for values, message in (
        ({"port": "1.5"}, "端口必须是整数"),
        ({"port": 0}, "端口不能小于 1"),
        ({"port": "abc"}, "端口必须是数字"),
        ({"port": True}, "端口的值无效"),
        ({"region": "mars"}, "区域的选项无效"),
        ({"fast": "yes"}, "加速只能是开或关"),
        ({"port": None}, "请填写：端口"),
    ):
        with raises_rendered(PluginConfigError, message):
            merge_values(fields, merged, values)
    assert masked_values(fields, again) == {
        "cookie": SECRET_MASK, "port": 8080, "ratio": 0.5, "region": "intl", "fast": False,
    }
    with raises_rendered(ValueError, "类型"):
        CredentialField("x", "X", type="color")
    with raises_rendered(ValueError, "没有选项"):
        CredentialField("x", "X", type="select")


# ---- Emby / Jellyfin ---------------------------------------------------------


async def test_emby_refresh_and_test() -> None:
    recorder = Recorder(
        {
            "POST /emby/Library/Refresh": 204,
            "GET /emby/System/Info": httpx.Response(200, json={"ServerName": "家庭影院", "Version": "4.9"}),
        }
    )
    values = {"url": "http://emby:8096", "api_key": "emby-key"}
    async with EmbyServer(values, transport=recorder.transport) as server:
        await server.refresh()
        assert render(await server.test()) == "连接成功：家庭影院 4.9"
    assert [str(request.url) for request in recorder.requests] == [
        "http://emby:8096/emby/Library/Refresh",
        "http://emby:8096/emby/System/Info",
    ]
    assert all(request.headers["X-Emby-Token"] == "emby-key" for request in recorder.requests)


async def test_emby_falls_back_to_root_paths() -> None:
    recorder = Recorder({"POST /Library/Refresh": 204})
    async with EmbyServer({"url": "http://emby:8096", "api_key": "k"}, transport=recorder.transport) as server:
        await server.refresh()
    assert [request.url.path for request in recorder.requests] == ["/emby/Library/Refresh", "/Library/Refresh"]


async def test_jellyfin_uses_authorization_header() -> None:
    recorder = Recorder(
        {
            "POST /Library/Refresh": 204,
            "GET /System/Info": httpx.Response(200, json={"ServerName": "jf", "Version": "10.10.7"}),
        }
    )
    values = {"url": "http://jellyfin:8096", "api_key": "jf-key"}
    async with JellyfinServer(values, transport=recorder.transport) as server:
        await server.refresh()
        assert render(await server.test()) == "连接成功：jf 10.10.7"
    assert [request.url.path for request in recorder.requests] == ["/Library/Refresh", "/System/Info"]
    for request in recorder.requests:
        assert request.headers["Authorization"] == 'MediaBrowser Token="jf-key"'
        assert "X-Emby-Token" not in request.headers


async def test_media_server_errors_are_readable_and_redirects_not_followed() -> None:
    values = {"url": "http://emby:8096", "api_key": "k"}
    cases = {
        401: "API Key",
        500: "错误码 500",
    }
    for status, message in cases.items():
        recorder = Recorder({"POST /emby/Library/Refresh": status})
        async with EmbyServer(values, transport=recorder.transport) as server:
            with raises_rendered(PluginCallError, message):
                await server.refresh()
    # 带着 Key 的请求不跟随重定向。
    recorder = Recorder(
        {"POST /emby/Library/Refresh": httpx.Response(302, headers={"location": "http://evil/steal"})}
    )
    async with EmbyServer(values, transport=recorder.transport) as server:
        with raises_rendered(PluginCallError, "跳转"):
            await server.refresh()
    assert [request.url.host for request in recorder.requests] == ["emby"]
    # 不是 Emby 的地址：测试给出明确提示。
    recorder = Recorder({"GET /emby/System/Info": httpx.Response(200, text="<html>")})
    async with EmbyServer(values, transport=recorder.transport) as server:
        with raises_rendered(PluginCallError, "不是 Emby"):
            await server.test()


# ---- Webhook ---------------------------------------------------------------


async def test_webhook_payload_and_signature() -> None:
    recorder = Recorder({"POST /hooks/vt": 200})
    notice = Notice("warning", "云实例关机失败", "请检查余额", "2026-01-01T00:00:00+00:00", 7)
    values = {"url": "https://hooks.example.com/hooks/vt?token=abc", "secret": "s3cret"}
    async with WebhookNotifier(values, transport=recorder.transport) as notifier:
        await notifier.send(notice)
    request = recorder.requests[0]
    assert str(request.url) == values["url"]
    body = json.loads(request.content)
    assert body == {
        "event": "notice",
        "id": 7,
        "level": "warning",
        "title": "云实例关机失败",
        "message": "请检查余额",
        "created_at": "2026-01-01T00:00:00+00:00",
        "source": "video-turbo",
    }
    timestamp = request.headers["X-VideoTurbo-Timestamp"]
    expected = hmac.new(b"s3cret", timestamp.encode() + b"." + request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-VideoTurbo-Signature"] == f"sha256={expected}"
    assert sign("s3cret", timestamp, request.content) == f"sha256={expected}"

    # 不填签名密钥时不带签名头；接收端错误转成可读信息。
    recorder = Recorder({"POST /hooks/vt": 500})
    async with WebhookNotifier({"url": "https://hooks.example.com/hooks/vt"}, transport=recorder.transport) as notifier:
        with raises_rendered(PluginCallError, "错误码 500"):
            await notifier.send(notice)
    assert "X-VideoTurbo-Signature" not in recorder.requests[0].headers


def test_webhook_url_is_secret_and_summary_shows_host() -> None:
    fields = {field.key: field for field in WebhookNotifier.credential_fields}
    assert fields["url"].secret and fields["url"].binds_secrets
    values = WebhookNotifier.merge_values({}, {"url": "https://hooks.example.com/x?token=abc#frag"})
    assert values == {"url": "https://hooks.example.com/x?token=abc"}
    assert WebhookNotifier.summary(values) == "hooks.example.com"
    # 换地址时签名密钥必须重填。
    saved = {**values, "secret": "s"}
    with raises_rendered(PluginConfigError, "签名密钥"):
        WebhookNotifier.merge_values(saved, {"url": "https://other.example.com/x"})
    for bad in ("hooks.example.com", "https://u:p@hooks.example.com/x"):
        with pytest.raises(PluginConfigError):
            WebhookNotifier.merge_values({}, {"url": bad})


# ---- 后台刷新与推送 -----------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "app.db")
    database.initialize()
    return database


def _add(db: Database, kind: str, plugin_type: str, values: dict, **options) -> str:
    store = Connections(db, kind)
    _, record = store.prepare_new(plugin_type, options.pop("name", plugin_type), True, values, **options)
    return store.create(record)


async def test_refresher_isolates_failures_and_notifies_once(db: Database) -> None:
    good = _add(db, "media_server", "jellyfin", {"url": "http://jf:8096", "api_key": "k"}, name="Jellyfin")
    bad = _add(db, "media_server", "emby", {"url": "http://emby:8096", "api_key": "k"}, name="Emby")
    routes: dict[str, httpx.Response | int] = {"POST /Library/Refresh": 204}
    recorder = Recorder(routes)
    notices = NoticeService(db, deliver=False)
    refresher = MediaRefresher(db, notices, transport=recorder.transport)

    async def wait() -> None:
        while refresher._task and not refresher._task.done():
            await asyncio.sleep(0)

    # Emby 的两个路径都失败；Jellyfin 照常刷新。
    routes["POST /emby/Library/Refresh"] = 503
    refresher.request()
    refresher.request()  # 进行中的请求合并
    await wait()
    hosts = [request.url.host for request in recorder.requests if request.method == "POST"]
    assert hosts.count("jf") <= 2 and "jf" in hosts
    assert refresher.status.get(good)["last_error"] is None
    assert "错误码 503" in render(refresher.status.get(bad)["last_error"])
    assert [render(item["title"]) for item in db.list_notices()] == ["刷新媒体服务器“Emby”失败"]

    # 再次失败不重复提醒；恢复后提醒一次。
    refresher.request()
    await wait()
    assert len(db.list_notices()) == 1
    routes["POST /emby/Library/Refresh"] = 204
    refresher.request()
    await wait()
    assert render(db.list_notices()[0]["title"]) == "媒体服务器“Emby”已恢复"
    assert refresher.status.get(bad)["last_error"] is None


async def test_refresher_does_nothing_without_servers_or_when_disabled(db: Database) -> None:
    recorder = Recorder({})
    refresher = MediaRefresher(db, NoticeService(db, deliver=False), transport=recorder.transport)
    refresher.request()
    assert refresher._task is None
    _add(db, "media_server", "emby", {"url": "http://emby:8096", "api_key": "k"})
    preview = MediaRefresher(db, NoticeService(db, deliver=False), enabled=False, transport=recorder.transport)
    preview.request()
    assert preview._task is None and recorder.requests == []


async def test_notices_are_pushed_by_level(db: Database) -> None:
    all_levels = _add(db, "notifier", "webhook", {"url": "https://a.example/hook"}, name="全部", min_level="info")
    errors_only = _add(db, "notifier", "webhook", {"url": "https://b.example/hook"}, name="错误", min_level="error")
    routes: dict[str, httpx.Response | int] = {"POST /hook": 200}
    recorder = Recorder(routes)
    notices = NoticeService(db, transport=recorder.transport)
    notices.start()
    try:
        notices.notify("info", "已安装插件", "…")
        notices.notify("error", "关机失败", "…")
        for _ in range(100):
            if len(recorder.requests) >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await notices.close()
    sent = [(request.url.host, json.loads(request.content)["title"]) for request in recorder.requests]
    assert sent == [("a.example", "已安装插件"), ("a.example", "关机失败"), ("b.example", "关机失败")]
    assert notices.status.get(all_levels)["last_error"] is None
    assert notices.status.get(errors_only)["last_ok_at"]
    # 提醒照常写入数据库，推送失败不额外写提醒。
    assert len(db.list_notices()) == 2


async def test_notice_push_failure_is_recorded_without_new_notice(db: Database) -> None:
    connection = _add(db, "notifier", "webhook", {"url": "https://a.example/hook"}, min_level="info")
    notices = NoticeService(db, transport=Recorder({"POST /hook": 500}).transport)
    await notices.deliver_one(Notice("info", "t", "m", "2026-01-01T00:00:00+00:00", 1))
    assert "错误码 500" in render(notices.status.get(connection)["last_error"])
    assert db.list_notices() == []


# ---- 配置接口 ----------------------------------------------------------------


def test_connection_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = prepare_server(tmp_path, monkeypatch)
    recorder = Recorder(
        {
            "POST /emby/Library/Refresh": 204,
            "GET /emby/System/Info": httpx.Response(200, json={"ServerName": "家庭影院", "Version": "4.9"}),
            "POST /hook": 200,
        }
    )
    with TestClient(app) as client:
        app.state.plugin_transport = recorder.transport
        types = client.get("/api/media-servers/types").json()
        assert [item["id"] for item in types] == ["emby", "jellyfin"]
        assert types[0]["fields"][0]["binds_secrets"] is True
        assert [item["id"] for item in client.get("/api/notifiers/types").json()] == ["webhook"]
        assert client.get("/api/media-servers").json() == []

        # 保存前测试。
        values = {"url": "http://emby:8096", "api_key": "emby-secret-9999"}
        tested = client.post("/api/media-servers/types/emby/test", json={"values": values})
        assert tested.json() == {"ok": True, "message": "连接成功：家庭影院 4.9"}
        assert client.post("/api/media-servers/types/nope/test", json={"values": {}}).status_code == 404
        missing = client.post("/api/media-servers/types/emby/test", json={"values": {}})
        assert missing.status_code == 400 and "请填写" in render(missing.json()["detail"])

        created = client.post(
            "/api/media-servers", json={"type": "emby", "name": "客厅", "values": values}
        ).json()
        duplicate = client.post("/api/media-servers", json={"type": "emby", "name": "客厅", "values": values})
        assert duplicate.status_code == 400 and "名称已存在" in render(duplicate.json()["detail"])
        listed = client.get("/api/media-servers").json()
        assert listed[0]["summary"] == "http://emby:8096" and listed[0]["available"] is True
        # 密钥不回显。
        assert listed[0]["values"]["api_key"] == "••••••"
        assert "emby-secret" not in json.dumps(listed)

        # 编辑时改地址必须重填 Key；不改地址可以沿用。
        moved = client.put(
            f"/api/media-servers/{created['id']}",
            json={"name": "客厅", "values": {"url": "http://evil:8096"}},
        )
        assert moved.status_code == 400 and "重新填写" in render(moved.json()["detail"])
        kept = client.put(
            f"/api/media-servers/{created['id']}", json={"name": "客厅", "enabled": False, "values": {}}
        )
        assert kept.status_code == 200
        assert db.plugin_connections("media_server")[created["id"]]["values"]["api_key"] == "emby-secret-9999"
        assert client.get("/api/media-servers").json()[0]["enabled"] is False
        assert client.post(f"/api/media-servers/{created['id']}/test", json={"values": {}}).json()["ok"]

        # 通知配置带最低级别。
        hook = client.post(
            "/api/notifiers",
            json={"type": "webhook", "name": "家里", "values": {"url": "https://hooks.example.com/hook?t=1"}},
        ).json()
        notifier = client.get("/api/notifiers").json()[0]
        assert notifier["min_level"] == "warning" and notifier["summary"] == "hooks.example.com"
        assert "t=1" not in json.dumps(notifier)
        assert client.post(f"/api/notifiers/{hook['id']}/test", json={"values": {}}).json()["ok"]
        bad_level = client.put(
            f"/api/notifiers/{hook['id']}", json={"name": "家里", "min_level": "debug", "values": {}}
        )
        assert bad_level.status_code == 422

        assert client.delete(f"/api/media-servers/{created['id']}").json() == {"removed": True}
        assert client.delete(f"/api/media-servers/{created['id']}").status_code == 404
        assert client.put("/api/media-servers/nope", json={"name": "x", "values": {}}).status_code == 404


class _SignedWebhook(WebhookNotifier):
    """带插件设置的通知插件：所有配置共用的前缀写在插件设置里，登录令牌保存在插件状态里。"""

    id = "signed-webhook"
    label = "带前缀的 Webhook"
    settings_fields = (CredentialField("prefix", "标题前缀", secret=False, trim=False),)

    async def send(self, notice: Notice) -> None:
        self.save_state({"sent": self.state.get("sent", 0) + 1})
        await super().send(
            Notice(notice.level, self.settings["prefix"] + notice.title, notice.message, notice.created_at)
        )


async def test_notifier_receives_plugin_settings_and_saves_state(db: Database) -> None:
    registry.register("notifier", _SignedWebhook)
    try:
        store = PluginSettings(db)
        assert store.missing(_SignedWebhook) == ["标题前缀"]
        assert store.save(_SignedWebhook, store.merged([_SignedWebhook], {"prefix": "[家里] "}))
        _add(db, "notifier", "signed-webhook", {"url": "https://hooks.example.com/a"})
        recorder = Recorder({"POST /a": 204})
        notices = NoticeService(db, deliver=False, transport=recorder.transport)
        for _ in range(2):
            await notices.deliver_one(Notice("error", "出错了", "", "2026-01-01T00:00:00Z"))
        assert [json.loads(request.content)["title"] for request in recorder.requests] == ["[家里] 出错了"] * 2
        assert db.plugin_data("signed-webhook")["state"] == {"sent": 2}
        # 设置没变时保留状态，变了清空。
        assert not store.save(_SignedWebhook, store.merged([_SignedWebhook], {}))
        assert db.plugin_data("signed-webhook")["state"] == {"sent": 2}
        assert store.save(_SignedWebhook, store.merged([_SignedWebhook], {"prefix": "[公司] "}))
        assert db.plugin_data("signed-webhook")["state"] == {}
    finally:
        registry.unregister("notifier", "signed-webhook")


def test_plugin_settings_api(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch)
    registry.register("notifier", _SignedWebhook)
    try:
        with TestClient(app) as client:
            plugins = {item["id"]: item for item in client.get("/api/plugins").json()["plugins"]}
            assert plugins["signed-webhook"]["has_settings"] and plugins["signed-webhook"]["settings_missing"]
            assert not plugins["webhook"]["has_settings"]
            described = client.get("/api/plugins/signed-webhook/settings").json()
            assert described["testable"] is False and described["values"] == {}
            assert client.put("/api/plugins/signed-webhook/settings", json={"values": {}}).status_code == 400
            assert client.put(
                "/api/plugins/signed-webhook/settings", json={"values": {"prefix": "[x] "}}
            ).json() == {"saved": True, "changed": True}
            assert client.post("/api/plugins/signed-webhook/settings/test", json={"values": {}}).status_code == 400
            plugins = {item["id"]: item for item in client.get("/api/plugins").json()["plugins"]}
            assert not plugins["signed-webhook"]["settings_missing"]
            assert client.get("/api/plugins/webhook/settings").status_code == 404
    finally:
        registry.unregister("notifier", "signed-webhook")
