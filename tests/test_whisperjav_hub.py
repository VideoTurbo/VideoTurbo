import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import raises_rendered
from huggingface_hub.errors import EntryNotFoundError as _EntryNotFound
from huggingface_hub.errors import HfHubHTTPError as _HubHTTPError

from video_turbo.worker import whisperjav_hub as hub

WORKER = Path(__file__).resolve().parents[1] / "src/video_turbo/worker"


def hub_error(cls, message, status=None):
    # Constructors differ between huggingface_hub 0.x (requests) and 1.x (httpx).
    error = cls.__new__(cls)
    Exception.__init__(error, message)
    error.response = SimpleNamespace(status_code=status) if status else None
    return error


def EntryNotFoundError(message):
    return hub_error(_EntryNotFound, message)


def HfHubHTTPError(message, status):
    return hub_error(_HubHTTPError, message, status)


def chained(error, context):
    error.__context__ = context
    return error


@pytest.mark.parametrize(
    ("error", "missing"),
    [
        # Server log: an optional file's 404 raised while a network error was in flight.
        (chained(EntryNotFoundError("404 Client Error. Entry Not Found"), ConnectionError("reset")), True),
        (chained(HfHubHTTPError("404", 404), TimeoutError("timeout")), True),
        (chained(RuntimeError("wrapped"), EntryNotFoundError("404")), True),
        (HfHubHTTPError("429 too many requests", 429), False),
        (HfHubHTTPError("503 unavailable", 503), False),
        (TimeoutError("Connection to hf-mirror.com timed out"), False),
    ],
)
def test_is_missing_on_hub(error, missing):
    assert hub.is_missing_on_hub(error) is missing


@pytest.fixture
def model_loader(monkeypatch):
    package = ModuleType("whisperjav")
    utils = ModuleType("whisperjav.utils")
    loader = ModuleType("whisperjav.utils.model_loader")
    # Mimic WhisperJAV 1.9.5: any error mentioning a connection counts as network.
    loader._is_network_error = lambda error: "connection" in repr(error.__context__).lower()
    utils.model_loader = loader
    package.utils = utils
    monkeypatch.setitem(sys.modules, "whisperjav", package)
    monkeypatch.setitem(sys.modules, "whisperjav.utils", utils)
    monkeypatch.setitem(sys.modules, "whisperjav.utils.model_loader", loader)
    return loader


def test_patch_skips_mirror_retry_only_for_missing_files(model_loader):
    not_found = chained(EntryNotFoundError("404"), ConnectionError("Connection reset"))
    network = chained(OSError("download failed"), ConnectionError("Connection reset"))
    assert model_loader._is_network_error(not_found) is True
    hub.ignore_missing_hub_files()
    hub.ignore_missing_hub_files()  # Idempotent: no double wrapping.
    assert model_loader._is_network_error(not_found) is False
    assert model_loader._is_network_error(network) is True
    assert model_loader._is_network_error.__wrapped__.__name__ == "<lambda>"


def test_patch_is_noop_without_whisperjav(monkeypatch):
    monkeypatch.setitem(sys.modules, "whisperjav", None)
    hub.ignore_missing_hub_files()


def test_launcher_and_self_test_apply_fix():
    for script in ("whisperjav_launcher.py", "whisperjav_self_test.py"):
        text = (WORKER / script).read_text(encoding="utf-8")
        assert "load_hub_fixes().ignore_missing_hub_files()" in text, script
    launcher = (WORKER / "whisperjav_launcher.py").read_text(encoding="utf-8")
    # Spawned pass workers re-run the launcher as __mp_main__ and need the fix too.
    assert launcher.count("load_hub_fixes().ignore_missing_hub_files()") == 2


@pytest.fixture
def downloads(monkeypatch):
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    monkeypatch.delenv("VIDEO_TURBO_HF_ENDPOINT", raising=False)
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com")


@pytest.mark.parametrize("primary,expected", [
    ("https://hf-mirror.com/", ["https://hf-mirror.com", "https://huggingface.co"]),
    ("https://huggingface.co", ["https://huggingface.co", "https://hf-mirror.com"]),
    ("https://custom", ["https://custom", "https://hf-mirror.com", "https://huggingface.co"]),
])
@pytest.mark.parametrize("name", ["hf_hub_download", "snapshot_download"])
def test_failed_metadata_switches_between_distinct_sources(downloads, monkeypatch, primary, expected, name):
    from huggingface_hub.errors import FileMetadataError

    monkeypatch.setenv("VIDEO_TURBO_HF_ENDPOINT", primary)
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        assert args == ("Qwen/model",)
        assert kwargs["cache_dir"] == "/models" and kwargs["revision"] == "pinned"
        if kwargs.get("local_files_only"):
            raise FileNotFoundError("not cached")
        if kwargs["endpoint"] == expected[-1]:
            return "model-path"
        raise FileMetadataError("Distant resource does not seem to be on huggingface.co")

    wrapper = hub.make_resilient_wrapper(original, name)
    assert wrapper("Qwen/model", cache_dir="/models", revision="pinned") == "model-path"
    assert [call["endpoint"] for call in calls if not call.get("local_files_only")] == expected
    assert sum(bool(call.get("local_files_only")) for call in calls) == (
        0 if name == "snapshot_download" else 1
    )


@pytest.mark.parametrize("alternate_available", [True, False])
def test_partial_snapshot_cache_does_not_prevent_endpoint_fallback(
    downloads, tmp_path, alternate_available,
):
    # Hub 0.x returns an existing snapshot directory in local-only mode even
    # when it only contains config/index files, without the requested shards.
    snapshot = tmp_path / "snapshots" / "commit"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    shards = ["model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"]
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            return str(snapshot)
        if kwargs["endpoint"] == "https://huggingface.co" and alternate_available:
            for shard in shards:
                (snapshot / shard).write_bytes(b"weights")
            return str(snapshot)
        raise TimeoutError("snapshot metadata unavailable")

    download = hub.make_resilient_wrapper(original, "snapshot_download")
    options = {"allow_patterns": shards, "cache_dir": str(tmp_path), "revision": "commit"}
    if alternate_available:
        assert download("Qwen/Qwen3-ASR-1.7B", **options) == str(snapshot)
        assert all((snapshot / shard).is_file() for shard in shards)
    else:
        with raises_rendered(TimeoutError, "snapshot metadata unavailable"):
            download("Qwen/Qwen3-ASR-1.7B", **options)
    assert [call["endpoint"] for call in calls] == [
        "https://hf-mirror.com", "https://huggingface.co",
    ]
    assert all(call == {**options, "endpoint": call["endpoint"]} for call in calls)


def test_cache_success_avoids_switching_sources(downloads):
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            return "cached"
        raise TimeoutError("timeout")

    assert hub.make_resilient_wrapper(original, "download")("repo") == "cached"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_http_errors_do_not_switch_sources(downloads, status):
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        raise HfHubHTTPError("error", status)

    with pytest.raises(_HubHTTPError):
        hub.make_resilient_wrapper(original, "download")("repo")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_http_errors_switch_sources(downloads, status):
    def original(*args, **kwargs):
        if kwargs["endpoint"] == "https://huggingface.co":
            return "ok"
        raise HfHubHTTPError("error", status)

    assert hub.make_resilient_wrapper(original, "download")("repo") == "ok"


@pytest.mark.parametrize("name", ["hf_hub_download", "snapshot_download"])
@pytest.mark.parametrize("offline,local", [(True, False), (False, True)])
def test_offline_and_local_only_never_add_network_attempts(downloads, monkeypatch, offline, local, name):
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", offline)
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        raise FileNotFoundError("cache miss")

    with pytest.raises(FileNotFoundError):
        hub.make_resilient_wrapper(original, name)("repo", local_files_only=local)
    assert calls == [{"local_files_only": local}]


def test_explicit_endpoint_wins_and_force_download_does_not_use_cache(downloads, monkeypatch):
    monkeypatch.setenv("VIDEO_TURBO_HF_ENDPOINT", "https://manual")
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["endpoint"] == "https://hf-mirror.com":
            return "ok"
        raise ConnectionError("reset")

    assert hub.make_resilient_wrapper(original, "download")(
        "repo", endpoint="https://explicit", force_download=True,
    ) == "ok"
    assert [call["endpoint"] for call in calls] == ["https://explicit", "https://hf-mirror.com"]
    assert all(call["force_download"] and not call.get("local_files_only") for call in calls)


def test_all_sources_fail_preserves_original_error_and_logs_endpoints(downloads, caplog):
    error = TimeoutError("first source timed out")
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise error
        raise ConnectionError("failed")

    with pytest.raises(TimeoutError) as caught:
        hub.make_resilient_wrapper(original, "download")("repo")
    assert caught.value is error
    assert len(calls) == 3  # primary, local cache, alternate
    assert "https://hf-mirror.com" in caplog.text and "https://huggingface.co" in caplog.text


def test_hub_0x_local_entry_error_is_not_a_remote_404(downloads, monkeypatch):
    from huggingface_hub import errors

    class OldLocalEntryError(_EntryNotFound):
        pass

    monkeypatch.setattr(errors, "LocalEntryNotFoundError", OldLocalEntryError)
    error = hub_error(OldLocalEntryError, "not in cache")
    assert not hub.is_missing_on_hub(error)
    assert hub._retryable(error)


def test_patch_prevents_upstream_from_nesting_download_wrappers(downloads, model_loader, monkeypatch):
    import huggingface_hub

    for name in ("hf_hub_download", "snapshot_download"):
        monkeypatch.setattr(huggingface_hub, name, lambda *_, **__: "ok")
    hub.patch_hub_downloads()
    first = huggingface_hub.hf_hub_download
    hub.patch_hub_downloads()
    assert huggingface_hub.hf_hub_download is first
    assert model_loader._make_resilient_wrapper(first, "download") is first


def test_download_patches_run_before_upstream_imports():
    launcher = (WORKER / "whisperjav_launcher.py").read_text(encoding="utf-8")
    assert launcher.count("load_hub_fixes().patch_hub_downloads()") == 2
    assert launcher.index("load_hub_fixes().patch_hub_downloads()") < launcher.index("from whisperjav.main import main")
    checks = (WORKER / "whisperjav_self_test.py").read_text(encoding="utf-8")
    assert checks.index("load_hub_fixes().patch_hub_downloads()") < checks.index(
        "from whisperjav.utils.model_loader import patch_hf_hub_downloads",
    )


def test_faster_whisper_uses_same_fallback(downloads, monkeypatch, tmp_path):
    import huggingface_hub
    from faster_whisper import utils

    from video_turbo.worker.asr import _download_model_with_progress

    calls = []

    def snapshot(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["endpoint"] == "https://huggingface.co":
            return "downloaded"
        raise TimeoutError("timeout")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda *_, **__: None)
    monkeypatch.setattr(utils, "download_model", lambda model, **kwargs: huggingface_hub.snapshot_download(model, **kwargs))
    assert _download_model_with_progress("repo/model", tmp_path) == "downloaded"
    assert [call["endpoint"] for call in calls if not call.get("local_files_only")] == [
        "https://hf-mirror.com", "https://huggingface.co",
    ]


def test_missing_optional_file_after_switch_is_propagated_without_more_retries(downloads):
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["endpoint"] == "https://huggingface.co":
            raise EntryNotFoundError("optional file absent")
        raise TimeoutError("timeout")

    with pytest.raises(_EntryNotFound):
        hub.make_resilient_wrapper(original, "download")("repo")
    assert len(calls) == 3
