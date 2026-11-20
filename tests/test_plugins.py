"""按地址或本地上传安装插件：下载、加载、注册、更新、卸载，重启后的重新加载，以及下载地址限制、安全模式和审计提醒。"""

import dataclasses
import sys
from pathlib import Path

import httpx
import pytest
from conftest import prepare_server, raises_rendered
from fastapi.testclient import TestClient

from video_turbo.database import Database
from video_turbo.i18n import render, render_data
from video_turbo.orchestrator.app import app
from video_turbo.plugins import installer, registry
from video_turbo.plugins.base import icon_uri

URL = "https://example.com/plugins/demo.py"


def _source(
    *, plugin_id: str = "demo", label: str = "演示云", api: object = 1, version: str = "1.0.0"
) -> str:
    return f'''
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from video_turbo.cloud.base import CAP_LIST, CloudInstance, CloudProvider, CredentialField

PLUGIN_API = {api!r}


@dataclass
class _Local:
    value: int = 1


class _Model(BaseModel):
    local: _Local | None = None


class DemoProvider(CloudProvider):
    id = "{plugin_id}"
    label = "{label}"
    version = "{version}"
    capabilities = frozenset({{CAP_LIST}})
    credential_fields = (CredentialField("api_key", "API Key"),)

    async def list_instances(self) -> list[CloudInstance]:
        _Model(local=_Local())
        return [CloudInstance(id="i-1", status="running")]
'''


def _media_source(plugin_id: str = "demo-media") -> str:
    return f'''
from video_turbo.media_servers.base import MediaServer
from video_turbo.plugins.base import CredentialField

PLUGIN_API = 1


class DemoMedia(MediaServer):
    id = "{plugin_id}"
    label = "演示媒体库"
    credential_fields = (CredentialField("url", "地址", secret=False),)

    async def refresh(self) -> None:
        return None

    async def test(self) -> str:
        return "ok"
'''


def _serve(files: dict[str, str | httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return body if isinstance(body, httpx.Response) else httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试不访问网络：域名解析到公网地址，个别用例再改成内网地址。"""

    async def resolve(host: str, _port: int) -> list[str]:
        return {"intranet.example": ["192.168.1.5"], "mixed.example": ["93.184.215.14", "10.0.0.1"]}.get(
            host, ["93.184.215.14"]
        )

    monkeypatch.setattr(installer, "_resolve", resolve)
    monkeypatch.delenv(installer.SAFE_MODE_ENV, raising=False)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "app.db")
    database.initialize()
    yield database
    for plugin_id in list(database.installed_plugins()):
        installer.uninstall(database, plugin_id)


async def _install(db: Database, url: str, transport: httpx.MockTransport) -> dict:
    """两步安装：先取 SHA-256（不执行），再带着它确认。"""
    preview = await installer.preview(url, transport)
    return await installer.install(db, url, preview["sha256"], transport)


async def _update(db: Database, plugin_id: str, transport: httpx.MockTransport) -> dict:
    check = await installer.check_update(db, plugin_id, transport)
    return await installer.update(db, plugin_id, check["sha256"], transport)


def _plugin_modules() -> list[str]:
    return [name for name in sys.modules if name.startswith("video_turbo_plugin_")]


@pytest.mark.anyio
async def test_install_update_uninstall(db: Database) -> None:
    result = await _install(db, URL, _serve({URL: _source()}))
    assert result["id"] == "demo" and render_data(result["kinds"]) == [{"id": "cloud", "label": "云平台"}] and result["changed"]
    provider = registry.get("cloud", "demo")
    async with provider({"api_key": "k"}) as client:
        assert [item.id for item in await client.list_instances()] == ["i-1"]
    saved = db.installed_plugins()["demo"]
    assert saved["kinds"] == ["cloud"] and saved["members"] == [["cloud", "demo"]] and saved["url"] == URL and saved["sha256"] == result["sha256"]
    assert result["version"] == saved["version"] == "1.0.0"

    # 源码没有变化：不重新加载。
    assert (await _update(db, "demo", _serve({URL: _source()})))["changed"] is False
    assert registry.get("cloud", "demo") is provider

    # 检查更新时不执行代码，静态读出新版本号。
    newer = _serve({URL: _source(label="演示云 2", version="1.1.0")})
    check = await installer.check_update(db, "demo", newer)
    assert check["changed"] and check["version"] == "1.1.0" and render(check["label"]) == "演示云 2"
    assert render(registry.get("cloud", "demo").label) == "演示云"
    updated = await _update(db, "demo", newer)
    assert updated["changed"] and render(registry.get("cloud", "demo").label) == "演示云 2"
    assert updated["version"] == db.installed_plugins()["demo"]["version"] == "1.1.0"
    assert installer.describe(db)[0]["version"] == "1.1.0"
    assert db.installed_plugins()["demo"]["installed_at"] == saved["installed_at"]
    assert len(_plugin_modules()) == 1

    # 更新失败时保留原来的版本。
    for broken in (_source(plugin_id="other"), "def broken(:", httpx.Response(500)):
        with pytest.raises(installer.PluginError):
            await _update(db, "demo", _serve({URL: broken}))
        assert render(registry.get("cloud", "demo").label) == "演示云 2"
        assert registry.get("media_server", "demo") is None
        assert db.installed_plugins()["demo"]["sha256"] == updated["sha256"]

    removed = installer.uninstall(db, "demo")
    assert removed["url"] == URL and "source" not in removed
    assert registry.get("cloud", "demo") is None and db.installed_plugins() == {}
    assert _plugin_modules() == []


@pytest.mark.anyio
async def test_plugin_kind_follows_base_class(db: Database) -> None:
    url = "https://example.com/media.py"
    result = await _install(db, url, _serve({url: _media_source()}))
    assert render_data(result["kinds"]) == [{"id": "media_server", "label": "媒体服务器"}]
    assert render(registry.get("media_server", "demo-media").label) == "演示媒体库"
    assert registry.get("cloud", "demo-media") is None
    assert [render_data(item["kinds"]) for item in installer.describe(db)] == [[{"id": "media_server", "label": "媒体服务器"}]]


@pytest.mark.anyio
async def test_code_runs_only_after_sha256_is_confirmed(db: Database, tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    source = _source() + f"\nopen({str(marker)!r}, 'w').close()\n"
    transport = _serve({URL: source})

    # 第一步只下载和静态检查，不执行。
    preview = await installer.preview(URL, transport)
    assert preview["url"] == URL and len(preview["sha256"]) == 64
    assert preview["size"] == len(source.encode())
    assert not marker.exists() and _plugin_modules() == [] and db.installed_plugins() == {}

    # 确认之后地址上的内容被换掉：不执行。
    swapped = _serve({URL: source + "# changed\n"})
    with raises_rendered(installer.PluginError, "发生了变化") as caught:
        await installer.install(db, URL, preview["sha256"], swapped)
    assert caught.value.conflict and caught.value.mismatch
    assert not marker.exists() and db.installed_plugins() == {}

    await installer.install(db, URL, preview["sha256"].upper(), transport)
    assert marker.exists()

    # 更新同样先核对：检查不执行，内容与确认的不一致时保留原版本。
    marker.unlink()
    newer = _serve({URL: source + "# v2\n"})
    check = await installer.check_update(db, "demo", newer)
    assert check["changed"] and check["sha256"] != preview["sha256"] and not marker.exists()
    with raises_rendered(installer.PluginError, "发生了变化"):
        await installer.update(db, "demo", check["sha256"], swapped)
    assert not marker.exists() and db.installed_plugins()["demo"]["sha256"] == preview["sha256"]
    assert (await installer.update(db, "demo", check["sha256"], newer))["changed"] and marker.exists()


@pytest.mark.anyio
async def test_installed_plugins_reload_on_startup(db: Database) -> None:
    await _install(db, URL, _serve({URL: _source()}))
    registry.unregister("cloud", "demo")  # 模拟进程重启后的空注册表
    assert installer.load_installed(db) == []
    assert render(registry.get("cloud", "demo").label) == "演示云"

    # 保存的源码已不适配当前接口：跳过并报告原因，不影响主控启动。
    db.set_installed_plugin("demo", {**db.installed_plugins()["demo"], "source": _source(api=0)})
    failures = installer.load_installed(db)
    assert registry.get("cloud", "demo") is None
    assert failures[0][0] == "demo" and "版本标记" in render(failures[0][1])
    assert "版本标记" in render(installer.describe(db)[0]["error"])


@pytest.mark.anyio
async def test_safe_mode_skips_installed_plugins(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    await _install(db, URL, _serve({URL: _source()}))
    monkeypatch.setenv(installer.SAFE_MODE_ENV, "1")
    failures = installer.load_installed(db)
    assert registry.get("cloud", "demo") is None
    assert [(plugin_id, render(reason)) for plugin_id, reason in failures] == [("demo", "安全模式（VIDEO_TURBO_SAFE_MODE=1）：未加载。")]
    # 安全模式下不能安装或更新（不会执行任何插件代码），但可以卸载。
    for call in (
        installer.preview(URL, _serve({URL: _source()})),
        installer.check_update(db, "demo", _serve({URL: _source()})),
    ):
        with raises_rendered(installer.PluginError, "安全模式"):
            await call
    installer.uninstall(db, "demo")
    assert db.installed_plugins() == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_source(api=None).replace("PLUGIN_API = None\n", ""), "缺少 PLUGIN_API"),
        (_source(api=99), "版本标记"),
        (_source(api=True), "版本标记"),
        (_source(api=[1]), "版本标记"),
        (_source(plugin_id="Bad Id"), "id 只能"),
        (_source(plugin_id="ssh"), "同名的内置插件"),
        # id 在所有类型中唯一：云平台插件不能占用内置媒体服务器的 id。
        (_source(plugin_id="emby"), "同名的内置插件"),
        ("PLUGIN_API = 1\n", "没有插件类"),
        ("PLUGIN_API = 1\nraise RuntimeError('boom')\n", "boom"),
        # 不是插件或版本不匹配的文件不会被执行。
        ("raise RuntimeError('boom')\n", "缺少 PLUGIN_API"),
        ("PLUGIN_API = 99\nraise RuntimeError('boom')\n", "版本标记"),
        (
            (
                "from video_turbo.media_sources.base import MediaSourcePlugin\n"
                "from video_turbo.plugins.base import CredentialField\n"
                "PLUGIN_API = 2\n"
                "class Drive(MediaSourcePlugin):\n"
                "    id = 'demo'\n"
                "    label = 'Drive'\n"
                "    credential_fields = (CredentialField('token', 'Token'),)\n"
                "    list_dir = stat = read_bytes = write_bytes = rename = media_input = test = None\n"
            ),
            "settings_fields",
        ),
        ("x" * (installer.MAX_SOURCE_BYTES + 1), "超过 1 MB"),
    ],
)
async def test_install_rejects_invalid_plugins(db: Database, source: str, message: str) -> None:
    with raises_rendered(installer.PluginError, message):
        await _install(db, URL, _serve({URL: source}))
    assert db.installed_plugins() == {} and _plugin_modules() == []
    assert render(registry.get("cloud", "ssh").label) != "演示云"
    assert registry.get("media_server", "emby").label == "Emby"


@pytest.mark.anyio
async def test_install_rejects_same_id_from_another_url(db: Database) -> None:
    other = "https://example.com/other.py"
    transport = _serve({URL: _source(), other: _source(label="冒名")})
    await _install(db, URL, transport)
    with pytest.raises(installer.PluginError) as caught:
        await _install(db, other, transport)
    assert caught.value.conflict and render(registry.get("cloud", "demo").label) == "演示云"
    # 同一地址再次安装等同于更新。
    assert (await _install(db, URL, transport))["id"] == "demo"


async def _upload(db: Database, source: str, filename: str = "demo.py") -> dict:
    preview = await installer.preview_upload(filename, source)
    return await installer.install_upload(db, filename, source, preview["sha256"])


@pytest.mark.anyio
async def test_upload_install_update_uninstall(db: Database, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = _source() + f"\nopen({str(marker)!r}, 'w').close()\n"
    # 静态检查不执行代码；文件名只保留最后一段。
    preview = await installer.preview_upload("C:\\plugins\\demo.py", source)
    assert preview["filename"] == "demo.py" and preview["url"] == ""
    assert (preview["id"], preview["kinds"][0]["id"], preview["version"]) == ("demo", "cloud", "1.0.0")
    assert not marker.exists() and db.installed_plugins() == {}
    with pytest.raises(installer.PluginError) as caught:
        await installer.install_upload(db, "demo.py", source + "# x\n", preview["sha256"])
    assert caught.value.mismatch and not marker.exists()

    result = await installer.install_upload(db, "demo.py", source, preview["sha256"])
    assert marker.exists() and result["changed"] and not result["updated"]
    saved = db.installed_plugins()["demo"]
    assert saved["url"] == "" and saved["filename"] == "demo.py"
    listed = installer.describe(db)[0]
    assert listed["source"] == "upload" and listed["filename"] == "demo.py"

    # 重新上传同一插件的新文件即为更新；本地上传的插件不能在线检查更新。
    updated = await _upload(db, _source(label="演示云 2", version="1.1.0"), "demo-1.1.py")
    assert updated["updated"] and render(registry.get("cloud", "demo").label) == "演示云 2"
    assert db.installed_plugins()["demo"]["filename"] == "demo-1.1.py"
    assert db.installed_plugins()["demo"]["installed_at"] == saved["installed_at"]
    assert len(_plugin_modules()) == 1
    with raises_rendered(installer.PluginError, "重新上传"):
        await installer.check_update(db, "demo")

    # 重启后从数据库加载。
    registry.unregister("cloud", "demo")
    assert installer.load_installed(db) == []
    assert render(registry.get("cloud", "demo").label) == "演示云 2"

    installer.uninstall(db, "demo")
    assert registry.get("cloud", "demo") is None and _plugin_modules() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("filename", "source", "message"),
    [
        ("demo.txt", _source(), "请上传插件文件"),
        (".py", _source(), "请上传插件文件"),
        ("demo.py", "PLUGIN_API = 99\n", "版本标记"),
        ("demo.py", "x" * (installer.MAX_SOURCE_BYTES + 1), "超过 1 MB"),
    ],
)
async def test_upload_rejects_invalid_files(db: Database, filename: str, source: str, message: str) -> None:
    with raises_rendered(installer.PluginError, message):
        await _upload(db, source, filename)
    assert db.installed_plugins() == {}


@pytest.mark.anyio
async def test_upload_and_url_sources_do_not_replace_each_other(db: Database) -> None:
    await _install(db, URL, _serve({URL: _source()}))
    with raises_rendered(installer.PluginError, "其他地址") as caught:
        await _upload(db, _source(label="冒名"))
    assert caught.value.conflict and render(registry.get("cloud", "demo").label) == "演示云"
    installer.uninstall(db, "demo")

    await _upload(db, _source())
    with raises_rendered(installer.PluginError, "本地上传"):
        await _install(db, URL, _serve({URL: _source(label="冒名")}))
    assert render(registry.get("cloud", "demo").label) == "演示云"


def test_icon_uri() -> None:
    svg = icon_uri('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert svg.startswith("data:image/svg+xml;base64,")
    assert icon_uri("data:image/png;base64,AAAA") == "data:image/png;base64,AAAA"
    # 远程地址、非图片和过大的图标都忽略。
    assert icon_uri("https://example.com/a.png") == ""
    assert icon_uri("data:text/html,<script>") == ""
    assert icon_uri("data:image/png;base64," + "A" * 70000) == ""
    assert icon_uri(None) == ""


def test_normalize_url() -> None:
    assert installer.normalize_url(" https://github.com/o/r/blob/main/dir/a.py ") == (
        "https://raw.githubusercontent.com/o/r/main/dir/a.py"
    )
    assert installer.normalize_url(URL) == URL
    for bad in ("http://example.com/a.py", "file:///etc/passwd", "a.py", "https://u:p@example.com/a.py"):
        with pytest.raises(installer.PluginError):
            installer.normalize_url(bad)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.py",
        "https://10.1.2.3/a.py",
        "https://[::1]/a.py",
        "https://[::ffff:192.168.1.1]/a.py",
        "https://169.254.169.254/latest/meta-data",
        "https://100.64.0.1/a.py",
        "https://intranet.example/a.py",
        # 只要有一个解析结果是内网地址就拒绝。
        "https://mixed.example/a.py",
    ],
)
async def test_download_rejects_private_addresses(url: str) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_source())

    with raises_rendered(installer.PluginError, "局域网"):
        await installer.preview(url, httpx.MockTransport(handler))
    assert requested == []


@pytest.mark.anyio
async def test_download_checks_every_redirect() -> None:
    requested: list[str] = []
    targets = {
        URL: httpx.Response(302, headers={"location": "https://cdn.example.com/demo.py"}),
        "https://cdn.example.com/demo.py": httpx.Response(200, text=_source()),
        "https://example.com/to-intranet.py": httpx.Response(
            301, headers={"location": "https://intranet.example/demo.py"}
        ),
        "https://example.com/to-http.py": httpx.Response(
            302, headers={"location": "http://example.com/demo.py"}
        ),
        "https://example.com/loop.py": httpx.Response(302, headers={"location": "/loop.py"}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return targets[str(request.url)]

    transport = httpx.MockTransport(handler)
    preview = await installer.preview(URL, transport)
    assert preview["url"] == URL and requested == [URL, "https://cdn.example.com/demo.py"]

    requested.clear()
    with raises_rendered(installer.PluginError, "局域网"):
        await installer.preview("https://example.com/to-intranet.py", transport)
    assert requested == ["https://example.com/to-intranet.py"]
    with raises_rendered(installer.PluginError, "https"):
        await installer.preview("https://example.com/to-http.py", transport)
    with raises_rendered(installer.PluginError, "跳转次数太多"):
        await installer.preview("https://example.com/loop.py", transport)


@pytest.mark.anyio
async def test_download_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "RETRY_DELAY_SECONDS", 0)
    failures: list[Exception] = [httpx.ConnectTimeout(""), httpx.ReadError("")]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if failures:
            raise failures.pop(0)
        return httpx.Response(200, text=_source())

    # 超时、连接中断后重试，最终成功。
    assert (await installer.preview(URL, httpx.MockTransport(handler)))["url"] == URL
    assert len(requested) == installer.DOWNLOAD_ATTEMPTS

    # 一直超时：用完重试次数后报错，错误文字为空时也有说明。
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    with raises_rendered(installer.PluginError, "下载插件失败：连接超时"):
        await installer.preview(URL, httpx.MockTransport(timeout))

    # 服务器明确返回的错误不重试。
    requested.clear()

    def not_found(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(404)

    with raises_rendered(installer.PluginError, "404"):
        await installer.preview(URL, httpx.MockTransport(not_found))
    assert len(requested) == 1


def test_http_error_text_never_empty() -> None:
    assert render(installer._http_error_text(httpx.ConnectError(""))) == (
        "无法连接到服务器，请检查网络或代理设置。"
    )
    assert installer._http_error_text(httpx.RemoteProtocolError("")) == "RemoteProtocolError"
    assert installer._http_error_text(httpx.ReadError("reset")) == "reset"


def _notices(db: Database) -> list[tuple[str, str]]:
    return [(item["level"], render(item["title"])) for item in reversed(db.list_notices())]


def test_plugin_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = prepare_server(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.state.plugin_transport = _serve({URL: _source()})
        listed = client.get("/api/plugins").json()
        assert listed["safe_mode"] is False
        # 内置插件也列出来，带图标；不能卸载。
        builtin = listed["plugins"]
        assert [(item["id"], item["source"]) for item in builtin] == [
            ("ssh", "builtin"), ("emby", "builtin"), ("jellyfin", "builtin"), ("webhook", "builtin")
        ]
        assert all(item["icon"].startswith("data:image/svg+xml;base64,") for item in builtin)
        assert client.delete("/api/plugins/emby").status_code == 400
        assert [kind["id"] for kind in listed["kinds"]] == ["cloud", "media_server", "media_source", "notifier"]
        assert [item["id"] for item in client.get("/api/cloud/provider-types").json()] == ["ssh"]
        assert client.post("/api/plugins/preview", json={"url": "http://x/a.py"}).status_code == 400
        assert client.post("/api/plugins/preview", json={"url": URL + "x"}).status_code == 400
        assert client.post("/api/plugins/preview", json={"url": "https://10.0.0.2/a.py"}).status_code == 400
        # 没有核对过的 SHA-256 不能安装；内容不一致时写入警告提醒。
        assert client.post("/api/plugins", json={"url": URL}).status_code == 422
        assert client.post("/api/plugins", json={"url": URL, "sha256": "0" * 64}).status_code == 409
        assert client.get("/api/plugins").json()["plugins"] == builtin
        assert _notices(db) == [("warning", "插件文件在确认后发生了变化，已取消")]

        preview = client.post("/api/plugins/preview", json={"url": URL}).json()
        assert preview["version"] == "1.0.0"
        assert (preview["id"], preview["label"], preview["kinds"]) == (
            "demo",
            "演示云",
            [{"id": "cloud", "label": "云平台"}],
        )
        confirmed = {"url": URL, "sha256": preview["sha256"]}
        installed = client.post("/api/plugins", json=confirmed).json()
        assert installed["id"] == "demo" and installed["sha256"] == preview["sha256"]
        # 安装写入提醒，含来源和 SHA-256。
        latest = db.list_notices()[0]
        assert render(latest["title"]) == "已安装插件：演示云" and "版本：1.0.0" in render(latest["message"])
        assert URL in latest["message"] and preview["sha256"] in latest["message"]
        # 安装后不重启即可使用。
        types = [item["id"] for item in client.get("/api/cloud/provider-types").json()]
        assert types == ["ssh", "demo"]
        created = client.post(
            "/api/cloud/providers",
            json={"type": "demo", "name": "演示账号", "values": {"api_key": "k"}},
        ).json()
        listed = client.get("/api/plugins").json()["plugins"][len(builtin):]
        assert render(listed[0]["label"]) == "演示云" and listed[0]["usage"] == 1
        assert listed[0]["kinds"] == [{"id": "cloud", "label": "云平台"}] and listed[0]["source"] == "url"
        assert listed[0]["sha256"] == installed["sha256"] and listed[0]["icon"] == ""
        assert client.post("/api/plugins/demo/update/check").json()["changed"] is False
        assert client.post("/api/plugins/demo/update").status_code == 422
        unchanged = client.post("/api/plugins/demo/update", json={"sha256": preview["sha256"]})
        assert unchanged.json()["changed"] is False
        assert client.post("/api/plugins/nope/update/check").status_code == 400

        # 卸载后配置保留，标记为缺少插件；重新安装即恢复。
        assert client.delete("/api/plugins/demo").json() == {"removed": True}
        assert render(db.list_notices()[0]["title"]) == "已卸载插件：演示云"
        assert client.get("/api/cloud/providers").json() == []
        assert client.get("/api/cloud/orphan-providers").json() == [
            {"id": created["id"], "label": "演示账号", "type": "demo"}
        ]
        assert client.get("/api/status").status_code == 200
        client.post("/api/plugins", json=confirmed)
        assert [item["label"] for item in client.get("/api/cloud/providers").json()] == ["演示账号"]

        # 缺少插件的配置可以直接删除。
        client.delete("/api/plugins/demo")
        assert client.delete(f"/api/cloud/providers/{created['id']}").json() == {"removed": True}
        assert db.cloud_profiles() == {} and db.cloud_credentials(created["id"]) == {}

        # 本地上传：先静态检查，再带着 SHA-256 安装；写入提醒，来源是文件名。
        media = {"filename": "media.py", "source": _media_source()}
        assert client.post("/api/plugins/upload/preview", json={**media, "filename": "a.txt"}).status_code == 400
        checked = client.post("/api/plugins/upload/preview", json=media).json()
        assert checked["id"] == "demo-media" and checked["filename"] == "media.py"
        assert client.post("/api/plugins/upload", json={**media, "sha256": "0" * 64}).status_code == 409
        uploaded = client.post("/api/plugins/upload", json={**media, "sha256": checked["sha256"]}).json()
        assert uploaded["id"] == "demo-media" and not uploaded["updated"]
        latest = db.list_notices()[0]
        assert render(latest["title"]) == "已安装插件：演示媒体库" and "本地上传 media.py" in render(latest["message"])
        assert client.post("/api/plugins/demo-media/update/check").status_code == 400
        assert client.delete("/api/plugins/demo-media").json() == {"removed": True}
        assert "本地上传 media.py" in render(db.list_notices()[0]["message"])

        client.post("/api/plugins", json=confirmed)
    # 重启主控：从数据库重新加载。
    registry.unregister("cloud", "demo")
    with TestClient(app) as client:
        assert [item["id"] for item in client.get("/api/cloud/provider-types").json()] == ["ssh", "demo"]
        client.delete("/api/plugins/demo")


def test_startup_reports_unloaded_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = prepare_server(tmp_path, monkeypatch)
    record = {"kind": "cloud", "url": URL, "sha256": "x", "source": _source(api=0)}
    db.set_installed_plugin("demo", record)
    with TestClient(app) as client:
        assert render(db.list_notices()[0]["title"]) == "插件未加载：demo"
        assert "版本标记" in render(client.get("/api/plugins").json()["plugins"][-1]["error"])

    db.set_installed_plugin("demo", {**record, "source": _source()})
    monkeypatch.setenv(installer.SAFE_MODE_ENV, "1")
    with TestClient(app) as client:
        assert registry.get("cloud", "demo") is None
        assert render(db.list_notices()[0]["title"]) == "安全模式：未加载按地址安装的插件"
        assert client.get("/api/plugins").json()["safe_mode"] is True
        app.state.plugin_transport = _serve({URL: _source()})
        refused = client.post("/api/plugins/preview", json={"url": URL})
        assert refused.status_code == 409 and "安全模式" in render(refused.json()["detail"])
        assert client.delete("/api/plugins/demo").json() == {"removed": True}


@pytest.mark.anyio
async def test_preview_reads_plugin_info_without_running_it(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    source = f'''
from video_turbo.notifiers import base

PLUGIN_API = 1
ICON = "<svg xmlns='http://www.w3.org/2000/svg'/>"


class _Helper:
    label = "不是插件"
    version = "9.9.9"


class DemoNotifier(base.Notifier):
    id = "demo-notify"
    label = "演示通知"
    version = "2.0"
    homepage = "https://example.com/demo"
    icon = ICON

open({str(marker)!r}, "w").close()
'''
    preview = await installer.preview(URL, _serve({URL: source}))
    assert not marker.exists()
    assert render_data({key: preview[key] for key in ("id", "label", "kinds", "version", "homepage")}) == {
        "id": "demo-notify",
        "label": "演示通知",
        "kinds": [{"id": "notifier", "label": "通知"}],
        "version": "2.0",
        "homepage": "https://example.com/demo",
    }
    assert preview["icon"] == icon_uri("<svg xmlns='http://www.w3.org/2000/svg'/>")


# ---- 一个文件提供多种类型 ----

_BUNDLE = '''
from video_turbo.media_servers.base import MediaServer
from video_turbo.notifiers.base import Notifier
from video_turbo.plugins.base import CredentialField

PLUGIN_API = {api}
PLUGIN_ID = "bundle"
PLUGIN_NAME = "全家桶"
PLUGIN_VERSION = "3.0"
PLUGIN_ICON = {icon!r}
PLUGIN_SETTINGS = (CredentialField("token", "令牌"),)


class BundleServer(MediaServer):
    id = "bundle-server"
    label = "全家桶媒体服务器"

    async def refresh(self) -> None:
        return None

    async def test(self) -> str:
        return self.settings["token"]


class BundleNotifier(Notifier):
    id = "bundle-notify"
    label = "全家桶通知"
{extra}
    async def send(self, notice) -> None:
        return None

    async def test(self) -> str:
        return "ok"
'''


def _bundle(
    *, api: str = '{"media_server": 1, "notifier": 1}', icon: str = "", extra: str = ""
) -> str:
    return _BUNDLE.format(api=api, icon=icon, extra=extra)


@pytest.mark.anyio
async def test_one_file_provides_several_kinds(db: Database) -> None:
    url = "https://example.com/bundle.py"
    transport = _serve({url: _bundle()})
    preview = await installer.preview(url, transport)
    assert (preview["id"], preview["label"], preview["version"]) == ("bundle", "全家桶", "3.0")
    assert [kind["id"] for kind in preview["kinds"]] == ["media_server", "notifier"]
    assert [member["id"] for member in preview["members"]] == ["bundle-server", "bundle-notify"]

    result = await installer.install(db, url, preview["sha256"], transport)
    assert result["id"] == "bundle" and [kind["id"] for kind in result["kinds"]] == [
        "media_server",
        "notifier",
    ]
    server = registry.get("media_server", "bundle-server")
    notifier = registry.get("notifier", "bundle-notify")
    # 两个插件类共用模块级插件设置，按插件 id 保存。
    assert server.plugin_key == notifier.plugin_key == "bundle"
    assert server.settings_fields == notifier.settings_fields and server.settings_fields[0].key == "token"
    listed = installer.describe(db)
    assert len(listed) == 1 and render(listed[0]["label"]) == "全家桶"
    assert [member["kind"] for member in listed[0]["members"]] == ["media_server", "notifier"]
    assert [item for item, _ in installer.members("bundle")] == ["media_server", "notifier"]

    # 新版本少了一个类型：旧的插件类随之注销。
    newer = _serve({url: _source(plugin_id="bundle", label="全家桶")})
    await _update(db, "bundle", newer)
    assert registry.get("notifier", "bundle-notify") is None
    assert registry.get("cloud", "bundle") is not None

    installer.uninstall(db, "bundle")
    assert registry.get("cloud", "bundle") is None and _plugin_modules() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_bundle().replace('PLUGIN_ID = "bundle"\n', ""), "PLUGIN_ID"),
        (_bundle(api="1").replace("PLUGIN_SETTINGS", "_UNUSED").replace(
            '    label = "全家桶通知"\n', '    label = "全家桶通知"\n    settings_fields = (CredentialField("x", "X"),)\n'
        ), "PLUGIN_SETTINGS"),
        (_bundle(api='{"media_server": 1}'), "没有声明通知插件"),
        (_bundle(api='{"media_server": 1, "notifier": 7}'), "版本标记"),
        (_bundle(api='{"nope": 1}'), "类型 'nope' 不存在"),
        (_bundle().replace('"bundle-server"', '"emby"'), "同名的内置插件"),
    ],
)
async def test_multi_kind_file_rules(db: Database, source: str, message: str) -> None:
    with raises_rendered(installer.PluginError, message):
        await _install(db, URL, _serve({URL: source}))
    assert db.installed_plugins() == {} and _plugin_modules() == []
    assert registry.get("media_server", "bundle-server") is None


def test_shared_plugin_settings_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = prepare_server(tmp_path, monkeypatch)
    url = "https://example.com/bundle.py"
    with TestClient(app) as client:
        app.state.plugin_transport = _serve({url: _bundle()})
        preview = client.post("/api/plugins/preview", json={"url": url}).json()
        client.post("/api/plugins", json={"url": url, "sha256": preview["sha256"]})
        item = next(p for p in client.get("/api/plugins").json()["plugins"] if p["id"] == "bundle")
        assert item["has_settings"] and item["settings_missing"]
        info = client.get("/api/plugins/bundle/settings").json()
        assert render(info["label"]) == "全家桶" and [f["key"] for f in info["fields"]] == ["token"]
        assert client.put("/api/plugins/bundle/settings", json={"values": {"token": "t"}}).json()["saved"]
        assert db.plugin_data("bundle")["settings"] == {"token": "t"}
        # 媒体服务器用的是共用的设置。
        created = client.post(
            "/api/media-servers", json={"type": "bundle-server", "name": "家里", "values": {}}
        ).json()
        tested = client.post(f"/api/media-servers/{created['id']}/test", json={"values": {}})
        assert tested.json()["message"] == "t"
        assert client.delete("/api/plugins/bundle").json() == {"removed": True}


# ---- 远程图标 ----

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ICON_URL = "https://img.example.com/icon.png"


@pytest.mark.anyio
async def test_remote_icon_is_downloaded_checked_and_stored(db: Database) -> None:
    url = "https://example.com/bundle.py"
    files = {url: _bundle(icon=ICON_URL), ICON_URL: httpx.Response(200, content=_PNG)}
    preview = await installer.preview(url, _serve(files))
    assert preview["icon"].startswith("data:image/png;base64,")
    assert preview["icon_url"] == ICON_URL and len(preview["icon_sha256"]) == 64

    # 确认后图标被替换：取消安装。
    swapped = _serve({**files, ICON_URL: httpx.Response(200, content=_PNG + b"x")})
    with raises_rendered(installer.PluginError, "图标") as caught:
        await installer.install(db, url, preview["sha256"], swapped, icon_sha256=preview["icon_sha256"])
    assert caught.value.mismatch and db.installed_plugins() == {} and _plugin_modules() == []

    await installer.install(db, url, preview["sha256"], _serve(files), icon_sha256=preview["icon_sha256"])
    saved = db.installed_plugins()["bundle"]
    assert saved["icon"] == preview["icon"] and saved["icon_sha256"] == preview["icon_sha256"]
    assert installer.describe(db)[0]["icon"] == preview["icon"]
    # 没有自己图标的插件类用插件的图标。
    assert registry.get("notifier", "bundle-notify").icon == preview["icon"]
    registry.unregister("notifier", "bundle-notify")
    installer.load_installed(db)
    assert registry.get("notifier", "bundle-notify").icon == preview["icon"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("icon", "message"),
    [
        (httpx.Response(200, content=b"<html>hi</html>"), "不是 SVG"),
        (httpx.Response(200, content=b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'), "脚本"),
        (httpx.Response(200, content=b'<svg><image href="https://t.example/p.png"/></svg>'), "外部引用"),
        (httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 60000), "超过"),
        (httpx.Response(404), "404"),
    ],
)
async def test_bad_remote_icon_is_skipped(db: Database, icon: httpx.Response, message: str) -> None:
    url = "https://example.com/bundle.py"
    files = {url: _bundle(icon=ICON_URL), ICON_URL: icon}
    preview = await installer.preview(url, _serve(files))
    assert preview["icon"] == "" and preview["icon_sha256"] == "" and message in render(preview["icon_error"])
    # 图标有问题不影响安装，只是没有图标。
    await installer.install(db, url, preview["sha256"], _serve(files))
    assert db.installed_plugins()["bundle"]["icon"] == ""


@pytest.mark.anyio
async def test_remote_icon_must_be_public(db: Database) -> None:
    url = "https://example.com/bundle.py"
    files = {url: _bundle(icon="https://intranet.example/icon.png")}
    preview = await installer.preview(url, _serve(files))
    assert "局域网" in render(preview["icon_error"])


def test_icon_uri_rejects_unsafe_svg() -> None:
    assert icon_uri('<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(#g)"/></svg>')
    assert icon_uri('<svg><use href="#a"/></svg>')
    for unsafe in (
        "<svg><script>alert(1)</script></svg>",
        '<svg onload="alert(1)"/>',
        "<svg><foreignObject/></svg>",
        '<svg><image xlink:href="https://t.example/x.png"/></svg>',
        '<svg><rect style="fill:url(https://t.example/x)"/></svg>',
    ):
        assert icon_uri(unsafe) == "", unsafe
    import base64

    encoded = base64.b64encode(b"<svg onload='x'/>").decode()
    assert icon_uri("data:image/svg+xml;base64," + encoded) == ""


def test_svg_icon_check_is_not_slowed_by_crafted_comments() -> None:
    import time

    from video_turbo.plugins.base import icon_from_bytes

    assert icon_from_bytes(b'<?xml version="1.0"?>\n<!-- a --> <!-- b -- c --><svg/>').startswith(
        "data:image/svg+xml;base64,"
    )
    # 修复前，这段输入的回溯次数随重复次数指数增长（25 次约 5 秒，每多一次翻倍）。
    crafted = b"<!--" + b"--><!--" * 25
    started = time.perf_counter()
    with pytest.raises(ValueError):
        icon_from_bytes(crafted)
    assert time.perf_counter() - started < 0.5


@pytest.mark.anyio
async def test_install_accepts_plugins_within_supported_api_range(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 只新增接口时 PLUGIN_API_VERSION 加一、MIN 不变：适配旧版本的插件仍能安装。
    cloud = dataclasses.replace(registry.KINDS["cloud"], api_version=2, min_api_version=1)
    monkeypatch.setitem(registry.KINDS, "cloud", cloud)
    with raises_rendered(installer.PluginError, "版本标记 3"):
        await _install(db, URL, _serve({URL: _source(api=3)}))
    await _install(db, URL, _serve({URL: _source(api=1)}))
    assert registry.get("cloud", "demo") is not None
