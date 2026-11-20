"""tools/select_sources.py：下载源测速与自动选择。"""

import importlib.util
import io
import json
import sys
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_turbo.i18n import render

TOOLS = Path(__file__).resolve().parents[1] / "tools"


@pytest.fixture
def module(monkeypatch):
    spec = importlib.util.spec_from_file_location("select_sources", TOOLS / "select_sources.py")
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "select_sources", loaded)
    spec.loader.exec_module(loaded)
    for source in loaded.SOURCES.values():
        monkeypatch.delenv(source.env, raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    return loaded


def test_every_source_has_an_official_address_and_a_mirror(module):
    for source in module.SOURCES.values():
        assert 2 <= len(source.candidates) <= 3
        assert any(label in {"官方", "直连"} for label, _ in source.candidates)


def test_fastest_reachable_candidate_wins(module, monkeypatch):
    timings = {"tuna": None, "pypi.org": 0.3, "hf-mirror": 0.2, "huggingface.co": 1.5}
    probed: list[str] = []

    def fake_probe(url, seconds):
        probed.append(url)
        return next(value for key, value in timings.items() if key in url)

    monkeypatch.setattr(module, "probe", fake_probe)
    monkeypatch.setattr(module, "probe_hf", lambda endpoint, files, seconds: fake_probe(endpoint, seconds))
    logs: list[str] = []
    chosen = module.select(["pypi", "hf"], log=logs.append)
    assert chosen == {
        "VIDEO_TURBO_PYPI_INDEX_URL": "https://pypi.org/simple",
        "VIDEO_TURBO_HF_ENDPOINT": "https://hf-mirror.com",
    }
    # 探测的是真实要下载的文件，而不只是首页。
    assert "https://pypi.org/simple/pip/" in probed
    assert "清华 不可用" in render(logs[0]) and "选用官方" in render(logs[0])


def test_preset_environment_skips_probe_and_all_failed_keeps_default(module, monkeypatch):
    monkeypatch.setenv("VIDEO_TURBO_HF_ENDPOINT", "https://hf.example")
    probed: list[str] = []
    monkeypatch.setattr(module, "probe", lambda url, seconds: probed.append(url))
    logs: list[str] = []
    chosen = module.select(["hf", "github"], log=logs.append)
    assert chosen["VIDEO_TURBO_HF_ENDPOINT"] == "https://hf.example"
    assert chosen["VIDEO_TURBO_GITHUB_PROXY"] == "direct"
    assert not any("hf.example" in url or "huggingface" in url for url in probed) and len(probed) == 3
    # 直连探测的是不带前缀的 GitHub 地址。
    assert any(url.startswith("https://github.com/") for url in probed)
    assert "全部不通" in render(logs[0])
    assert module.github_prefix("direct") == "" and module.github_prefix("https://p/") == "https://p/"


def test_cli_prints_shell_exports_and_json(module, monkeypatch, capsys):
    monkeypatch.setattr(module, "probe", lambda url, seconds: 0.1)
    module.main(["pypi"])
    assert capsys.readouterr().out == "export VIDEO_TURBO_PYPI_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple'\n"
    module.main(["torch", "--format", "json"])
    assert json.loads(capsys.readouterr().out) == {"VIDEO_TURBO_TORCH_INDEX_URL": "https://mirror.sjtu.edu.cn/pytorch-wheels"}


def test_github_stays_direct_unless_a_proxy_is_much_faster(module, monkeypatch):
    timings = {"ghfast": 0.5, "gh-proxy": 0.9}
    direct = [0.8]
    monkeypatch.setattr(
        module, "probe",
        lambda url, seconds: next((value for key, value in timings.items() if key in url), direct[0]),
    )
    assert module.select(["github"], log=lambda _: None) == {"VIDEO_TURBO_GITHUB_PROXY": "direct"}
    direct[0] = 3.0
    assert module.select(["github"], log=lambda _: None) == {"VIDEO_TURBO_GITHUB_PROXY": "https://ghfast.top/"}


def metadata_response(**overrides):
    response = io.BytesIO()
    response.code = 200
    response.headers = Message()
    headers = {"X-Repo-Commit": "a" * 40, "ETag": '"abc"', "Content-Length": "123"}
    headers.update(overrides)
    for key, value in headers.items():
        if value is not None:
            response.headers[key] = value
    return response


@pytest.mark.parametrize("headers", [
    {"X-Repo-Commit": None}, {"X-Repo-Commit": "not-a-commit"},
    {"ETag": None}, {"Content-Length": None}, {"Content-Length": "bad"},
    {"Content-Length": "0"}, {"Content-Type": "text/html; charset=utf-8"},
])
def test_hf_rejects_successful_http_without_valid_metadata(module, monkeypatch, headers):
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: SimpleNamespace(
        open=lambda *_, **__: metadata_response(**headers),
    ))
    assert module.probe_hf("https://mirror", ["Qwen/model/resolve/main/config.json"]) is None


def test_hf_checks_every_actual_file_with_head_and_linked_metadata(module, monkeypatch):
    requested = []

    def open_request(request, timeout):
        requested.append(request)
        return metadata_response(**{
            "ETag": None, "Content-Length": None, "X-Linked-ETag": '"abc"', "X-Linked-Size": "1000",
        })

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    files = module.hf_files()
    assert module.probe_hf("https://mirror/", files) is not None
    assert sorted(r.full_url for r in requested) == sorted(f"https://mirror/{file}" for file in files)
    assert all(r.get_method() == "HEAD" for r in requested)
    assert "Qwen/Qwen3-ASR-1.7B/resolve/main/config.json" in files
    assert "litagin/anime-whisper/resolve/main/config.json" in files
    assert "FireRedTeam/FireRedVAD/resolve/main/VAD/model.pth.tar" in files


def test_hf_selects_official_when_mirror_metadata_is_invalid(module, monkeypatch):
    def open_request(request, timeout):
        return metadata_response(**{"X-Repo-Commit": None} if "hf-mirror" in request.full_url else {})

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    assert module.select(["hf"]) == {"VIDEO_TURBO_HF_ENDPOINT": "https://huggingface.co"}


def test_hf_preserves_head_on_redirect_and_does_not_follow_cdn(module):
    handler = module._MetadataRedirects()
    request = module.urllib.request.Request("https://mirror/model", method="HEAD")
    redirected = handler.redirect_request(request, None, 307, "", {}, "https://mirror/cache/model")
    assert redirected.get_method() == "HEAD"
    assert handler.redirect_request(request, None, 302, "", {}, "https://cdn/model") is None


def test_hf_accepts_external_redirect_metadata_without_downloading_cdn(module, monkeypatch):
    response = metadata_response(**{"X-Linked-ETag": '"large"', "X-Linked-Size": "5000000000"})

    def open_request(*_, **__):
        raise module.urllib.error.HTTPError("https://mirror/model", 302, "Found", response.headers, io.BytesIO())

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    assert module.probe_hf("https://mirror", ["model"]) is not None


def test_hf_uses_configured_model_and_skips_local_model(module, tmp_path):
    config = tmp_path / "worker.yaml"
    config.write_text("whisperjav:\n  model: custom/qwen\n  speech_segmenter: ten\n")
    assert module.hf_files(config) == [
        "custom/qwen/resolve/main/config.json", "litagin/anime-whisper/resolve/main/config.json",
    ]
    (tmp_path / "local-model").mkdir()
    config.write_text("whisperjav:\n  model: local-model\n")
    assert not any("local-model" in file for file in module.hf_files(config))


def test_standard_hf_endpoint_is_preserved_and_project_override_wins(module, monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", "https://custom")
    monkeypatch.setattr(module, "probe_hf", lambda *_: pytest.fail("manual source must not be probed"))
    assert module.select(["hf"]) == {"VIDEO_TURBO_HF_ENDPOINT": "https://custom"}
    monkeypatch.setenv("VIDEO_TURBO_HF_ENDPOINT", "https://project-custom")
    assert module.select(["hf"]) == {"VIDEO_TURBO_HF_ENDPOINT": "https://project-custom"}
