import io
import json
import logging
import zipfile
from pathlib import Path

from conftest import prepare_server
from fastapi.testclient import TestClient

from video_turbo import __version__
from video_turbo.config_store import LLM_KEY
from video_turbo.i18n import render
from video_turbo.llm_config import LlmNode, LlmSettings
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.diagnostics import REDACTED, redact


def test_redact_hides_secret_fields_and_known_secret_text() -> None:
    value = {
        "api_key": "sk-live-123",
        "key_configured": True,
        "nested": [{"password": "hunter2", "name": "node"}],
        "message": "token worker-secret leaked",
        "empty_token": "",
    }

    assert redact(value, ("worker-secret",)) == {
        "api_key": REDACTED,
        "key_configured": True,
        "nested": [{"password": REDACTED, "name": "node"}],
        "message": f"token {REDACTED} leaked",
        "empty_token": "",
    }


def test_diagnostics_archive_contains_info_and_logs_without_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    database = prepare_server(tmp_path, monkeypatch, worker_token="worker-secret-token")
    node = LlmNode(
        name="节点-1", pool="translation", base_url="http://node/v1", api_key="sk-abcdef123456", model="m"
    )
    database.set_runtime_json(LLM_KEY, LlmSettings(nodes=[node]).model_dump(mode="json"))

    with TestClient(app) as client:
        logging.getLogger("video_turbo.test").warning("worker token is worker-secret-token")
        response = client.get("/api/diagnostics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "videoturbo-diagnostics-" in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["diagnostics.json", "server.log"]

    raw = archive.read("diagnostics.json").decode()
    info = json.loads(raw)
    assert info["version"] == __version__
    assert info["database_schema"]["current"] == info["database_schema"]["supported"]
    assert render(info["llm"]["nodes"][0]["name"]) == "节点-1"
    assert info["llm"]["nodes"][0]["key_configured"] is True
    log = archive.read("server.log").decode()
    assert f"worker token is {REDACTED}" in log
    for secret in ("worker-secret-token", "sk-abcdef123456", "3456"):
        assert secret not in raw
        assert secret not in log
