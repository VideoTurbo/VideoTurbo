from pathlib import Path

import pytest
import yaml
from conftest import prepare_server
from fastapi.testclient import TestClient
from test_configure_worker import load_tool, run_tool

from video_turbo import connection_string
from video_turbo.orchestrator import settings_api
from video_turbo.orchestrator.app import app


def test_round_trip_and_tolerates_copy_noise() -> None:
    code = connection_string.encode("http://192.168.1.10:8766/", " secret ")
    assert code.startswith("VT1:")
    assert connection_string.decode(code) == ("http://192.168.1.10:8766", "secret")
    # 复制时混进来的换行、空格和小写前缀都能认出。
    noisy = "  vt1:" + code[4:20] + "\n" + code[20:] + " "
    assert connection_string.decode(noisy) == ("http://192.168.1.10:8766", "secret")


@pytest.mark.parametrize(
    "text",
    ["", "http://nas:8766", "VT1:", "VT1:not-base64!", "VT1:" + "e30"],  # e30 = {}
)
def test_invalid_codes_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        connection_string.decode(text)


@pytest.mark.parametrize("url", ["ftp://nas:8766", "nas:8766", "http://nas:8766/api", "http://user:pw@nas:8766"])
def test_only_plain_http_addresses_are_encoded(url: str) -> None:
    with pytest.raises(ValueError):
        connection_string.encode(url, "secret")


def test_setup_tool_decodes_the_same_format() -> None:
    tool = load_tool()
    code = connection_string.encode("https://nas.example.com:8766", "tok")
    assert tool.decode_connection(code) == ("https://nas.example.com:8766", "tok")
    assert tool.decode_connection("http://nas:8766") is None
    assert tool.decode_connection("VT1:broken") is None


def test_setup_tool_accepts_a_code_instead_of_address_and_token(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "worker.yaml"
    code = connection_string.encode("http://192.168.1.10:8766", "tok-1")
    prompts = run_tool(monkeypatch, config, ["VT1:broken", code, ""])
    # 无效的接入串重新询问；有效的直接带出地址和密钥，不再问密钥。
    assert len(prompts) == 3
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert payload["server_url"] == "http://192.168.1.10:8766"
    assert payload["worker_token"] == "tok-1"


def test_console_offers_addresses_and_builds_codes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_api, "lan_addresses", lambda: ["192.168.1.10"])
    monkeypatch.delenv("VIDEO_TURBO_DEPLOYMENT", raising=False)
    prepare_server(tmp_path, monkeypatch, worker_token="worker-secret")
    with TestClient(app) as client:
        options = client.get("/api/settings/worker-connection").json()
        assert options["addresses"] == ["192.168.1.10"] and options["worker_port"] == 8766
        response = client.post("/api/settings/worker-connection", json={"url": "http://192.168.1.10:8766"})
        assert response.status_code == 200
        assert connection_string.decode(response.json()["code"]) == ("http://192.168.1.10:8766", "worker-secret")
        assert client.post("/api/settings/worker-connection", json={"url": "nas"}).status_code == 422
        # Docker 里的网卡地址是容器内部的，不提供。
        monkeypatch.setenv("VIDEO_TURBO_DEPLOYMENT", "docker")
        assert client.get("/api/settings/worker-connection").json()["addresses"] == []
