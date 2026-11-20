from pathlib import Path

import httpx
from conftest import prepare_server, raises_rendered
from fastapi.testclient import TestClient

from video_turbo.i18n import render
from video_turbo.llm_config import LlmNode, LlmSettings
from video_turbo.orchestrator import config_api
from video_turbo.orchestrator.app import app


def test_node_overrides_reach_pool_request_config(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch)
    with TestClient(app) as client:
        created = client.post(
            "/api/llm/nodes",
            json={"name": "主力", "pool": "translation", "protocol": "openai_chat", "base_url": "https://gw/v1/", "api_key": "secret-aaaa1111", "model": "grok-4", "concurrency": 3, "reasoning_effort": "low"},
        )
        assert created.status_code == 200, created.text
        node_id = created.json()["id"]
        node = created.json()["nodes"][0]
        assert node["base_url"] == "https://gw/v1"
        assert node["key_masked"] == "…1111" and node["key_configured"] is True
        assert "api_key" not in node
        assert "secret-aaaa1111" not in created.text

        # 热更新：无需重启即出现在节点池中。
        upstreams = client.get("/api/status").json()["translation"]["upstreams"]
        assert [item["name"] for item in upstreams] == ["主力"]
        assert upstreams[0]["api_mode"] == "chat_completions"
        assert upstreams[0]["concurrency"] == 3
        # 结构化输出不再由用户设置：始终请求，接口不支持时自动降级。
        assert upstreams[0]["structured_output"] is True
        assert upstreams[0]["reasoning_effort"] == "low"
        assert app.state.config.translation.ready

        # 同一网站的第二个 Key：复制为新节点，Key 留空时沿用来源节点的 Key，填写则使用新 Key。
        copied = client.post(
            "/api/llm/nodes",
            json={"name": "主力 副本", "protocol": "openai_chat", "base_url": "https://gw/v1", "copy_key_from": node_id, "model": "grok-4", "concurrency": 3},
        )
        assert copied.status_code == 200, copied.text
        second = client.post(
            "/api/llm/nodes",
            json={"name": "主力-2", "protocol": "openai_chat", "base_url": "https://gw/v1", "copy_key_from": node_id, "api_key": "secret-bbbb2222", "model": "grok-4", "concurrency": 3},
        )
        assert second.status_code == 200, second.text
        assert [item.api_key for item in app.state.config.translation.upstreams] == ["secret-aaaa1111", "secret-aaaa1111", "secret-bbbb2222"]
        # 总并发不单独设置：三个节点 × 并发 3。
        assert app.state.config.translation.concurrency == 9
        assert app.state.orchestrator.translation_request_semaphore._value == 9

        # 节点名称不能重复；没有审校池；OpenAI 兼容协议必须有接口地址。
        assert client.post("/api/llm/nodes", json={"name": "主力", "base_url": "https://gw/v1", "model": "m"}).status_code == 422
        assert client.post("/api/llm/nodes", json={"name": "审校", "pool": "review", "base_url": "https://gw/v1", "model": "gpt"}).status_code == 422
        assert client.post("/api/llm/nodes", json={"name": "无地址", "model": "gpt"}).status_code == 422

        # Key 留空（null）保留原值；空字符串清空。
        updated = client.put(
            f"/api/llm/nodes/{node_id}",
            json={"name": "主力", "protocol": "openai_responses", "base_url": "https://gw/v1", "model": "grok-4", "api_key": None},
        )
        assert updated.status_code == 200, updated.text
        assert app.state.config.translation.upstreams[0].api_key == "secret-aaaa1111"
        assert app.state.config.translation.upstreams[0].api_mode == "responses"

        disabled = client.put(
            f"/api/llm/nodes/{node_id}",
            json={"name": "主力", "base_url": "https://gw/v1", "model": "grok-4", "enabled": False},
        )
        assert disabled.status_code == 200
        assert [item["name"] for item in client.get("/api/status").json()["translation"]["upstreams"]] == ["主力 副本", "主力-2"]
        for item in client.get("/api/config").json()["llm"]["nodes"]:
            assert client.delete(f"/api/llm/nodes/{item['id']}").status_code == 200
        assert client.get("/api/config").json()["llm"] == {"nodes": []}


def test_settings_sections_validate_and_hot_apply(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch)
    library = tmp_path / "lib"
    library.mkdir()
    with TestClient(app) as client:
        settings = client.get("/api/config").json()["settings"]

        saved = client.put("/api/config/scan", json={"schedule": "30 4 * * *", "settle_minutes": 10})
        assert saved.status_code == 200, saved.text
        assert app.state.config.scan.schedule == "30 4 * * *"
        assert app.state.config.scan.settle_minutes == 10

        # 内部参数不再开放：扩展名、排版、Worker 租约、影片修复参数、翻译批次都不能改。
        assert set(settings) == {"server", "media_libraries", "scan", "subtitles", "translation"}
        assert set(settings["translation"]) == {"enabled", "job_concurrency"}
        assert client.put("/api/config/scan", json={"video_extensions": ["mkv"]}).status_code == 422
        assert client.put("/api/config/subtitles", json={"max_lines": 3}).status_code == 422
        assert client.put("/api/config/translation", json={"batch_size": 24}).status_code == 422
        assert client.put("/api/config/workers", json={"lease_seconds": 60}).status_code == 404
        assert client.put("/api/config/restore", json={"lease_seconds": 60}).status_code == 404
        assert "rmvb" in app.state.config.scan.video_extensions
        assert client.put("/api/config/server", json={"port": 1}).status_code == 422
        assert client.put("/api/config/audio", json={}).status_code == 404

        # 媒体服务器改由插件配置，不再是配置段。
        assert client.put("/api/config/emby", json={}).status_code == 404

        # 媒体库改为逐个保存（/api/libraries），不再整段提交。
        assert client.put("/api/config/media_libraries", json=[]).status_code == 404
        table = client.get("/api/config").json()["languages"]
        assert [item["code"] for item in table] == ["ja", "en", "ko", "zh-CN", "zh-TW"]
        assert set(client.get("/api/config").json()["settings"]["subtitles"]) == {
            "skip_existing_target", "accept_other_chinese_variant",
            "unlabeled_embedded_subtitle_policy",
        }

        tuning = client.put("/api/config/translation", json={"job_concurrency": 2})
        assert tuning.status_code == 200
        assert app.state.config.translation.job_concurrency == 2

    # 重启后仍以数据库为准。
    with TestClient(app) as client:
        assert app.state.config.scan.schedule == "30 4 * * *"
        assert app.state.config.translation.job_concurrency == 2


def test_model_list_is_fetched_and_cached(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "b-model"}, {"id": "a-model"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        config_api.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with TestClient(app) as client:
        node_id = client.post(
            "/api/llm/nodes",
            json={"name": "n", "base_url": "https://gw/v1", "api_key": "saved-key-0000", "model": "m"},
        ).json()["id"]
        # 未保存的草稿也能拉取。
        draft = client.post("/api/llm/models", json={"protocol": "openai_responses", "base_url": "https://draft/v1", "api_key": "draft"})
        assert draft.json() == {"models": ["a-model", "b-model"]}
        assert str(seen[-1].url) == "https://draft/v1/models"
        assert seen[-1].headers["authorization"] == "Bearer draft"

        # 编辑中改了地址：已保存的 Key 不会发往新地址，必须重新填写；也不写入旧节点的缓存。
        requests_before = len(seen)
        refused = client.post("/api/llm/models", json={"node_id": node_id, "base_url": "https://other/v1"})
        assert refused.status_code == 422 and "重新填写" in render(refused.json()["detail"])
        node_input = {"name": "n", "base_url": "https://other/v1", "model": "m"}
        assert client.put(f"/api/llm/nodes/{node_id}", json=node_input).status_code == 422
        assert client.post("/api/llm/nodes", json={**node_input, "name": "副本", "copy_key_from": node_id}).status_code == 422
        assert len(seen) == requests_before
        edited = client.post("/api/llm/models", json={"node_id": node_id, "base_url": "https://other/v1", "api_key": "new-key"})
        assert edited.status_code == 200
        assert str(seen[-1].url) == "https://other/v1/models"
        assert seen[-1].headers["authorization"] == "Bearer new-key"
        assert client.get("/api/config").json()["llm"]["nodes"][0]["models"] == []

        cached = client.post("/api/llm/models", json={"node_id": node_id})
        assert cached.json()["models"] == ["a-model", "b-model"]
        assert str(seen[-1].url) == "https://gw/v1/models"
        node = client.get("/api/config").json()["llm"]["nodes"][0]
        assert node["models"] == ["a-model", "b-model"]
        assert node["models_fetched_at"]

        # 修改模型等不影响连接的字段时缓存保留；换地址后缓存作废。
        body = {"name": "n", "base_url": "https://gw/v1", "model": "a-model"}
        assert client.put(f"/api/llm/nodes/{node_id}", json=body).json()["nodes"][0]["models"] == ["a-model", "b-model"]
        assert client.put(f"/api/llm/nodes/{node_id}", json={**body, "base_url": "https://new/v1", "api_key": "saved-key-0000"}).json()["nodes"][0]["models"] == []


def test_llm_settings_reject_duplicate_node_names() -> None:
    with raises_rendered(ValueError, "重复"):
        LlmSettings(
            nodes=[
                LlmNode(name="n", pool="translation", base_url="https://x", model="m"),
                LlmNode(name="n", pool="fallback", base_url="https://x", model="m"),
            ],
        )
    with raises_rendered(ValueError, "接口地址"):
        LlmNode(name="n", model="m")
    assert LlmNode(name="v", protocol="vertex_ai", model="gemini").resolved_base_url().startswith("https://")


def test_vertex_service_account_node_saves_json_and_lists_models_with_bearer(tmp_path: Path, monkeypatch) -> None:
    from test_vertex_ai import _service_account_json

    from video_turbo import vertex_auth

    prepare_server(tmp_path, monkeypatch)
    monkeypatch.setattr(vertex_auth, "_token_cache", {})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == vertex_auth.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.token", "expires_in": 3600})
        return httpx.Response(200, json={"publisherModels": [{"name": "publishers/google/models/gemini-3-flash"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        config_api.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    credential = _service_account_json()
    node_input = {"name": "sa", "protocol": "vertex_ai", "auth": "service_account", "location": "us-central1", "model": "gemini-3-flash"}
    with TestClient(app) as client:
        # 草稿：凭据随请求提交。
        draft = client.post("/api/llm/models", json={"protocol": "vertex_ai", "auth": "service_account", "api_key": credential})
        assert draft.json() == {"models": ["gemini-3-flash"]}
        assert seen[-1].headers["authorization"] == "Bearer ya29.token"

        created = client.post("/api/llm/nodes", json={**node_input, "api_key": credential})
        assert created.status_code == 200
        node = client.get("/api/config").json()["llm"]["nodes"][0]
        assert node["auth"] == "service_account" and node["project"] == "demo-project"
        assert node["key_masked"] == "translator@demo-project.iam.gserviceaccount.com"
        assert "PRIVATE KEY" not in client.get("/api/config").text
        upstream = app.state.config.translation.upstreams[0]
        assert upstream.base_url == "https://us-central1-aiplatform.googleapis.com/v1/projects/demo-project/locations/us-central1"
        assert upstream.api_key == credential

        # 不重新上传文件时沿用已保存的 JSON；改成 API Key 认证却不填 Key 会被拒绝。
        assert client.put(f"/api/llm/nodes/{node['id']}", json=node_input).status_code == 200
        rejected = client.put(f"/api/llm/nodes/{node['id']}", json={**node_input, "auth": "api_key"})
        assert rejected.status_code == 422 and "认证方式" in render(rejected.text)
