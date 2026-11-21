"""桌面版的识别模型清单：按识别模式选模型、判断是否下载完整、下载进度、删除，以及只报告模型已下载的模式。"""

import json
from pathlib import Path

import pytest
from conftest import PROTOCOL, prepare_server
from fastapi.testclient import TestClient

from video_turbo.orchestrator.app import app
from video_turbo.worker import model_catalog
from video_turbo.worker.config import WorkerConfig
from video_turbo.worker.main import Worker


def _config(tmp_path: Path, **asr) -> WorkerConfig:
    python = tmp_path / ".venv-whisperjav" / "bin" / "python"
    return WorkerConfig(
        worker_token="token", cache_dir=tmp_path / "cache", idle={"enabled": False},
        asr={"download_root": tmp_path / "models", **asr}, whisperjav={"python_executable": python},
    )


def _cache(repo: model_catalog.Repo, files: dict[str, str] | None = None) -> None:
    """按 Hugging Face 缓存布局放一个下载完成的仓库：blobs + snapshots/<提交> 里的链接 + refs/main。"""
    snapshot = repo.folder / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (repo.folder / "blobs").mkdir()
    for name, content in (files or {"config.json": "{}"}).items():
        blob = repo.folder / "blobs" / name.replace("/", "_")
        blob.write_text(content)
        (snapshot / name).symlink_to(blob)
    (repo.folder / "refs").mkdir()
    (repo.folder / "refs" / "main").write_text("abc")


def _repos(config: WorkerConfig) -> dict[str, list[model_catalog.Repo]]:
    return {model.id: list(model.repos) for model in model_catalog.catalog(config)}


def test_each_recognition_mode_needs_its_own_models() -> None:
    assert model_catalog.needed("whisperjav", True) == ["qwen3-asr", "anime-whisper"]
    assert model_catalog.needed("whisperjav", False) == ["qwen3-asr"]
    assert model_catalog.needed("faster_whisper", False) == ["faster-whisper"]
    assert model_catalog.needed(None, False) == []


def test_catalog_uses_the_caches_recognition_reads(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repos = _repos(config)
    hub = tmp_path / "models" / "huggingface"
    assert [(repo.repo_id, repo.cache) for repo in repos["qwen3-asr"]] == [
        ("Qwen/Qwen3-ASR-1.7B", hub), ("FireRedTeam/FireRedVAD", hub),
    ]
    assert [(repo.repo_id, repo.cache) for repo in repos["anime-whisper"]] == [("litagin/anime-whisper", hub)]
    whisper = repos["faster-whisper"][0]
    assert (whisper.repo_id, whisper.cache) == ("Systran/faster-whisper-large-v3", tmp_path / "models")
    assert "model.bin" in whisper.patterns
    sizes = {model.id: model.size_mb() for model in model_catalog.catalog(config)}
    assert sizes == {"qwen3-asr": 4492, "anime-whisper": 2887, "faster-whisper": 2948}


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="需要符号链接")
def test_partial_downloads_do_not_count(tmp_path: Path) -> None:
    config = _config(tmp_path)
    anime = _repos(config)["anime-whisper"][0]
    assert not model_catalog.repo_downloaded(anime)
    _cache(anime)
    assert model_catalog.repo_downloaded(anime)
    (anime.folder / "blobs" / "next.incomplete").write_text("")
    assert not model_catalog.repo_downloaded(anime)
    (anime.folder / "blobs" / "next.incomplete").unlink()
    (anime.folder / "blobs" / "config.json").unlink()  # 快照里的链接指向缺失的文件
    assert not model_catalog.repo_downloaded(anime)


def test_status_marks_what_the_current_mode_still_needs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_file = tmp_path / "config" / "worker.yaml"
    config_file.parent.mkdir()
    for repo in _repos(config)["qwen3-asr"]:
        _cache(repo)
    # 连不上主程序、也没有缓存：不知道当前模式，什么都不算“需要”。
    result = model_catalog.status(config, config_file)
    assert result["mode"] is None and result["missing"] == []
    assert [item["downloaded"] for item in result["models"]] == [True, False, False]

    (config_file.parent / model_catalog.MODE_FILE).write_text(json.dumps({"mode": "whisperjav", "whisperjav_ensemble": True}))
    result = model_catalog.status(config, config_file)
    assert result["mode"] == {"mode": "whisperjav", "whisperjav_ensemble": True, "live": False}
    assert [item["id"] for item in result["models"] if item["needed"]] == ["qwen3-asr", "anime-whisper"]
    assert result["missing"] == ["anime-whisper"]


def test_mode_is_read_from_the_main_app_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare_server(tmp_path, monkeypatch, worker_token="token")
    with TestClient(app) as client:
        assert client.put("/api/settings/asr", json={"mode": "faster_whisper"}).status_code == 200
        assert client.get("/api/worker/asr-settings", headers=PROTOCOL).status_code == 401
        reply = client.get("/api/worker/asr-settings", headers={**PROTOCOL, "X-Worker-Token": "token"})
        assert reply.json() == {"mode": "faster_whisper", "whisperjav_ensemble": False}

        import httpx

        monkeypatch.setattr(httpx, "get", lambda url, headers, timeout: client.get(url.removeprefix("http://main"),
                                                                                    headers=headers))
        config = _config(tmp_path)
        config.server_url = "http://main"
        config_file = tmp_path / "worker" / "worker.yaml"
        config_file.parent.mkdir()
        assert model_catalog.fetch_mode(config, config_file) == {
            "mode": "faster_whisper", "whisperjav_ensemble": False, "live": True,
        }
    # 主程序关了：用上次读到的。
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert model_catalog.fetch_mode(config, config_file)["live"] is False


def test_download_reports_progress_and_delete_removes_the_cache(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    fetched = []

    def fetch(repo: model_catalog.Repo) -> None:
        fetched.append(repo.repo_id)
        _cache(repo, {"model.safetensors": "x" * 2048})

    model_catalog.download(config, ["anime-whisper", "anime-whisper", "unknown"], fetch=fetch, interval=0.01)
    assert fetched == ["litagin/anime-whisper"]
    events = [json.loads(line.removeprefix(model_catalog.PROGRESS)) for line in capsys.readouterr().out.splitlines()]
    assert events[0] == {"event": "model", "id": "anime-whisper", "zh": "anime-whisper", "en": "anime-whisper",
                         "index": 1, "count": 1, "done_mb": 0, "total_mb": events[0]["total_mb"]}
    assert [event["event"] for event in events[-2:]] == ["model_done", "done"]
    assert model_catalog.downloaded(next(m for m in model_catalog.catalog(config) if m.id == "anime-whisper"))

    # 已下载的不会再下载。
    model_catalog.download(config, ["anime-whisper"], fetch=fetch, interval=0.01)
    assert fetched == ["litagin/anime-whisper"]

    model_catalog.delete(config, "anime-whisper")
    assert not _repos(config)["anime-whisper"][0].folder.exists()


def test_desktop_worker_only_offers_modes_whose_models_are_downloaded(tmp_path: Path) -> None:
    config = _config(tmp_path, local_models_only=True)
    python = config.whisperjav.python_executable
    python.parent.mkdir(parents=True)
    python.write_text("")
    from video_turbo.worker.whisperjav import WHISPERJAV_VERSION

    (python.parent.parent / "video-turbo-whisperjav.json").write_text(
        json.dumps({"version": WHISPERJAV_VERSION, "ensemble": True}))
    worker = Worker(config)
    assert worker.supported_asr_modes() == []
    assert worker.supports_whisperjav_ensemble() is False

    repos = _repos(config)
    for repo in repos["qwen3-asr"]:
        _cache(repo)
    assert worker.supported_asr_modes() == ["whisperjav"]
    assert worker.supports_whisperjav_ensemble() is False
    _cache(repos["anime-whisper"][0])
    _cache(repos["faster-whisper"][0])
    assert worker.supported_asr_modes() == ["whisperjav", "faster_whisper"]
    assert worker.supports_whisperjav_ensemble() is True

    # 源码部署和 Docker 不受影响：模型在识别时下载。
    config.asr.local_models_only = False
    for model in ("qwen3-asr", "anime-whisper", "faster-whisper"):
        model_catalog.delete(config, model)
    assert worker.supported_asr_modes() == ["whisperjav", "faster_whisper"]
    assert worker.supports_whisperjav_ensemble() is True


def test_shell_defaults_match_the_catalog(tmp_path: Path) -> None:
    # 运行环境装好之前，外壳用这份默认清单让用户选模型。
    project = Path(__file__).resolve().parents[1]
    defaults = json.loads((project / "desktop" / "crates" / "shell-core" / "src" / "default_models.json").read_text(
        encoding="utf-8"))
    catalog = [
        {"id": m.id, "zh": m.zh, "en": m.en, "purpose_zh": m.purpose_zh, "purpose_en": m.purpose_en,
         "size_mb": m.size_mb()}
        for m in model_catalog.catalog(WorkerConfig())
    ]
    assert defaults == catalog
