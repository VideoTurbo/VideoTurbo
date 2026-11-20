"""主控提供控制台前端的构建产物；产物缺失时给出构建提示而不是报错。"""

from pathlib import Path

import pytest
from conftest import prepare_server
from fastapi.testclient import TestClient

from video_turbo import __version__
from video_turbo.orchestrator import dashboard
from video_turbo.orchestrator.app import app


def test_serves_built_index_without_caching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_server(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>VideoTurbo</title><div id=app></div>", encoding="utf-8")
    monkeypatch.setattr(dashboard, "DIST_DIR", dist)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-cache"
        assert "<title>VideoTurbo</title>" in response.text


def test_missing_build_explains_how_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_server(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "DIST_DIR", tmp_path / "missing")

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 503
        assert "npm --prefix web run build" in response.text
        # 页面缺失不影响接口。
        assert client.get("/health").json() == {"status": "ok", "version": __version__}
