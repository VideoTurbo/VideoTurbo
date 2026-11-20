"""插件仓库：仓库地址换算、plugins.json 解析，以及添加、列出、删除仓库的接口。"""

import json
from pathlib import Path

import httpx
import pytest
from conftest import prepare_server
from fastapi.testclient import TestClient

from video_turbo.i18n import render, render_data
from video_turbo.orchestrator.app import app
from video_turbo.plugins import installer, repositories

INDEX = "https://raw.githubusercontent.com/o/r/HEAD/plugins.json"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, _port: int) -> list[str]:
        return ["10.0.0.1"] if host == "intranet.example" else ["93.184.215.14"]

    monkeypatch.setattr(installer, "_resolve", resolve)


def test_index_url() -> None:
    assert repositories.index_url("https://github.com/o/r") == INDEX
    assert repositories.index_url(" https://github.com/o/r.git/ ") == INDEX
    assert repositories.index_url("https://github.com/o/r/tree/dev") == (
        "https://raw.githubusercontent.com/o/r/dev/plugins.json"
    )
    assert repositories.index_url("https://github.com/o/r/tree/dev/sub/dir") == (
        "https://raw.githubusercontent.com/o/r/dev/sub/dir/plugins.json"
    )
    assert repositories.index_url("https://github.com/o/r/blob/main/plugins.json") == (
        "https://raw.githubusercontent.com/o/r/main/plugins.json"
    )
    assert repositories.index_url("https://cdn.example.com/x/index.json") == (
        "https://cdn.example.com/x/index.json"
    )
    for bad in ("http://github.com/o/r", "https://example.com/o/r", "https://u:p@github.com/o/r", "x"):
        with pytest.raises(installer.PluginError):
            repositories.index_url(bad)


def _index(**extra: object) -> str:
    return json.dumps(
        {
            "name": "官方插件",
            "plugins": [
                {
                    "id": "chenyu",
                    "name": "晨羽智云",
                    "version": "1.0.0",
                    "description": "管理晨羽智云的 GPU 机器",
                    "kinds": ["cloud"],
                    "path": "cloud_gpu/chenyu.py",
                    "sha256": "A" * 64,
                    "icon": "<svg xmlns='http://www.w3.org/2000/svg'/>",
                    "homepage": "https://chenyu.example",
                },
                {
                    "id": "bundle",
                    "name": "全家桶",
                    "kinds": ["media_source", "notifier", "future_kind"],
                    "url": "https://github.com/x/y/blob/main/bundle.py",
                    "icon": "https://img.example/a.png",
                },
                # 不合格的条目跳过。
                {"id": "Bad Id", "kinds": ["cloud"], "path": "a.py"},
                {"id": "nokind", "kinds": ["future_kind"], "path": "a.py"},
                {"id": "nopath", "kinds": ["cloud"]},
                {"id": "insecure", "kinds": ["cloud"], "url": "http://example.com/a.py"},
                {"id": "chenyu", "kinds": ["cloud"], "path": "dup.py"},
                "not an object",
            ],
            **extra,
        }
    )


def test_parse_index() -> None:
    index = repositories.parse_index(_index(), INDEX)
    assert render(index["name"]) == "官方插件" and index["skipped"] == 6
    first, second = index["plugins"]
    assert first["url"] == "https://raw.githubusercontent.com/o/r/HEAD/cloud_gpu/chenyu.py"
    assert first["sha256"] == "a" * 64 and render_data(first["kinds"]) == [{"id": "cloud", "label": "云平台"}]
    assert first["icon"].startswith("data:image/svg+xml;base64,") and first["homepage"]
    assert second["url"] == "https://raw.githubusercontent.com/x/y/main/bundle.py"
    # 列表里只认内联图标，不访问图片地址；不认识的类型忽略。
    assert second["icon"] == "" and [kind["id"] for kind in second["kinds"]] == ["media_source", "notifier"]
    for bad in ("not json", "[]", '{"plugins": {}}'):
        with pytest.raises(installer.PluginError):
            repositories.parse_index(bad, INDEX)


def _serve(files: dict[str, str | httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return body if isinstance(body, httpx.Response) else httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


def test_repository_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = prepare_server(tmp_path, monkeypatch)
    other = "https://raw.githubusercontent.com/o/broken/HEAD/plugins.json"
    files: dict[str, str | httpx.Response] = {INDEX: _index(), other: "{}"}
    with TestClient(app) as client:
        app.state.plugin_transport = _serve(files)
        assert client.get("/api/plugin-repositories").json() == {"repositories": []}
        # 不是插件仓库、地址不对、内网地址都不保存。
        assert client.post("/api/plugin-repositories", json={"url": "https://github.com/o/broken"}).status_code == 400
        assert client.post("/api/plugin-repositories", json={"url": "https://example.com/a"}).status_code == 400
        assert (
            client.post("/api/plugin-repositories", json={"url": "https://intranet.example/plugins.json"}).status_code
            == 400
        )
        added = client.post("/api/plugin-repositories", json={"url": "https://github.com/o/r"}).json()
        assert render(added["name"]) == "官方插件" and added["index_url"] == INDEX
        assert [item["id"] for item in added["plugins"]] == ["chenyu", "bundle"]
        assert client.post("/api/plugin-repositories", json={"url": "https://github.com/o/r/"}).status_code == 409
        assert db.plugin_repositories()[0]["url"] == "https://github.com/o/r"

        # 每次列出都重新下载；下载失败时带上原因。
        files[INDEX] = httpx.Response(500)
        listed = client.get("/api/plugin-repositories").json()["repositories"]
        assert render(listed[0]["name"]) == "官方插件" and "500" in listed[0]["error"] and listed[0]["plugins"] == []

        assert client.delete(f"/api/plugin-repositories/{added['id']}").json() == {"removed": True}
        assert client.delete(f"/api/plugin-repositories/{added['id']}").status_code == 404
        assert db.plugin_repositories() == []
