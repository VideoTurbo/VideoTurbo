import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import raises_rendered

from video_turbo.i18n import render
from video_turbo.worker import whisperjav_self_test as checks
from video_turbo.worker.asr import TranscriptionCancelled
from video_turbo.worker.config import WorkerConfig
from video_turbo.worker.whisperjav import ANIME_MODEL, WhisperJavTranscriber


@pytest.fixture
def backend(tmp_path):
    config = WorkerConfig(
        asr={"download_root": tmp_path / "models"},
        whisperjav={"python_executable": sys.executable, "model": "custom/qwen"},
    )
    result = WhisperJavTranscriber(config)
    result.device = "cpu"
    return result


def option(command, name):
    return command[command.index(name) + 1]


def test_self_test_checks_complete_dual_stack_in_separate_processes(backend, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: calls.append((*args, kwargs)))
    with caplog.at_level(logging.INFO):
        backend.self_test()
    commands = [call[0] for call in calls]
    assert [option(command, "--check") for command in commands] == ["qwen", "vad", "anime", "vad", "merge"]
    # Self-test mirrors component output so model downloads show live progress.
    assert all(call[-1] == {"echo": True} for call in calls)
    assert option(commands[0], "--model") == "custom/qwen"
    assert option(commands[1], "--segmenter") == "firered-vad"
    assert option(commands[2], "--model") == ANIME_MODEL
    assert option(commands[3], "--segmenter") == "ten"
    assert all(option(command, "--device") == "cpu" for command in commands)
    assert all(command[1:4] == ["-I", "-X", "utf8"] for command in commands)
    assert all(Path(command[4]).is_file() for command in commands)
    assert all(not call[1].exists() for call in calls)
    assert backend._environment()["HF_HUB_CACHE"] == str(backend.config.asr.download_root / "huggingface")
    assert "自检全部通过" in render(caplog.text)
    assert all("--use-gpu" not in command for command in commands)


def test_self_test_respects_configured_primary_segmenter_without_repeating_ten(backend, monkeypatch):
    backend.config.whisperjav.speech_segmenter = "ten"
    commands = []
    monkeypatch.setattr(backend, "_run", lambda command, *_, **__: commands.append(command))
    backend.self_test()
    assert [option(command, "--segmenter") for command in commands if "--segmenter" in command] == ["ten"]
    backend.config.whisperjav.speech_segmenter = "whisperseg"
    commands.clear()
    backend.self_test()
    assert [option(command, "--segmenter") for command in commands if "--segmenter" in command] == ["whisperseg", "ten"]


def test_self_test_aborts_and_labels_second_model_failure(backend, monkeypatch, caplog):
    stages = []

    def run(command, *_, **__):
        stage = option(command, "--check")
        stages.append(stage)
        if stage == "anime":
            raise RuntimeError("model download failed")

    monkeypatch.setattr(backend, "_run", run)
    with caplog.at_level(logging.INFO), raises_rendered(RuntimeError, "第二遍 anime-whisper.*model download failed"):
        backend.self_test()
    assert stages == ["qwen", "vad", "anime"]
    assert "自检全部通过" not in render(caplog.text)


def test_self_test_stops_between_components_when_cancelled(backend, monkeypatch):
    commands = []
    monkeypatch.setattr(backend, "_run", lambda command, *_, **__: commands.append(command))
    with pytest.raises(TranscriptionCancelled):
        backend.self_test(cancel_check=lambda: bool(commands))
    assert len(commands) == 1


@pytest.mark.parametrize("device,dtype", [("cpu", "float32"), ("cuda", "float16")])
def test_model_checks_load_and_release_models_without_aligner(monkeypatch, device, dtype):
    events = []

    class Qwen:
        def __init__(self, **kwargs):
            assert kwargs["model_id"] == "custom/qwen"
            assert kwargs["device"] == device and kwargs["dtype"] == dtype
            assert kwargs["use_aligner"] is False and kwargs["timestamps"] == "none"
            events.append("qwen-init")

        def load_model_text_only(self):
            events.append("qwen-load")

        def unload_model(self):
            events.append("qwen-unload")

    class Anime:
        def __init__(self, **kwargs):
            assert kwargs == {"model_id": ANIME_MODEL, "device": device, "dtype": dtype}
            events.append("anime-init")

        def load(self):
            events.append("anime-load")

        def unload(self):
            events.append("anime-unload")

    qwen = ModuleType("whisperjav.modules.qwen_asr")
    qwen.QwenASR = Qwen
    anime = ModuleType("whisperjav.modules.subtitle_pipeline.generators.anime_whisper")
    anime.AnimeWhisperGenerator = Anime
    monkeypatch.setitem(sys.modules, qwen.__name__, qwen)
    monkeypatch.setitem(sys.modules, anime.__name__, anime)
    checks.check_qwen("custom/qwen", device)
    checks.check_anime(ANIME_MODEL, device)
    assert events == ["qwen-init", "qwen-load", "qwen-unload", "anime-init", "anime-load", "anime-unload"]


@pytest.mark.parametrize("enabled", [True, False])
def test_self_test_loads_firered_vad_on_gpu_only_for_cuda(backend, monkeypatch, enabled):
    backend.device = "cuda"
    backend.config.whisperjav.firered_vad_gpu = enabled
    calls = []
    monkeypatch.setattr(backend, "_run", lambda *args, **_: calls.append(args[0]))
    backend.self_test()
    vad = [command for command in calls if option(command, "--check") == "vad"]
    assert [option(command, "--segmenter") for command in vad] == ["firered-vad", "ten"]
    assert ("--use-gpu" in vad[0]) is enabled
    assert "--use-gpu" not in vad[1]


def test_vad_check_passes_gpu_switch_to_factory(monkeypatch):
    created = []
    segmenter = SimpleNamespace(_ensure_model=lambda: None, cleanup=lambda: None)
    module = ModuleType("whisperjav.modules.speech_segmentation")
    module.SpeechSegmenterFactory = SimpleNamespace(
        create=lambda name, **kwargs: created.append((name, kwargs)) or segmenter
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    checks.check_vad("firered-vad", use_gpu=True)
    checks.check_vad("ten")
    assert created == [("firered-vad", {"use_gpu": True}), ("ten", {})]


def test_vad_check_loads_native_model_and_always_cleans_up(monkeypatch):
    events = []

    def load():
        events.append("load")
        raise RuntimeError("native library unavailable")

    segmenter = SimpleNamespace(_ensure_model=load, cleanup=lambda: events.append("cleanup"))
    module = ModuleType("whisperjav.modules.speech_segmentation")

    def create(name):
        assert name == "ten"
        return segmenter

    module.SpeechSegmenterFactory = SimpleNamespace(create=create)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with raises_rendered(RuntimeError, "native library unavailable"):
        checks.check_vad("ten")
    assert events == ["load", "cleanup"]


def test_runtime_check_rejects_wrong_version_or_missing_ffprobe(monkeypatch):
    monkeypatch.setattr(checks.importlib.metadata, "version", lambda _: "1.9.4")
    with raises_rendered(RuntimeError, "需要 WhisperJAV 1.9.5"):
        checks.check_runtime("1.9.5")
    monkeypatch.setattr(checks.importlib.metadata, "version", lambda _: "1.9.5")
    monkeypatch.setattr(checks.shutil, "which", lambda name: "/bin/ffmpeg" if name == "ffmpeg" else None)
    with raises_rendered(RuntimeError, "ffprobe"):
        checks.check_runtime("1.9.5")


def test_runtime_check_reports_missing_linux_library_before_model_imports(monkeypatch):
    monkeypatch.setattr(checks.importlib.metadata, "version", lambda _: "1.9.5")
    monkeypatch.setattr(checks.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(checks.platform, "system", lambda: "Linux")

    def load(name):
        assert name == "libc++.so.1"
        raise OSError("libc++.so.1: cannot open shared object file")

    monkeypatch.setattr(checks.ctypes, "CDLL", load)
    with raises_rendered(RuntimeError, r"apt-get install -y libc\+\+1"):
        checks.check_runtime("1.9.5")


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_ten_runtime_check_only_loads_library_on_linux(monkeypatch, system):
    monkeypatch.setattr(checks.platform, "system", lambda: system)
    libraries = []
    monkeypatch.setattr(checks.ctypes, "CDLL", libraries.append)
    checks.check_ten_vad_runtime()
    assert libraries == (["libc++.so.1"] if system == "Linux" else [])


@pytest.mark.parametrize("echo", [True, False])
def test_run_mirrors_download_progress_and_splits_carriage_returns(backend, tmp_path, capsys, echo):
    script = tmp_path / "progress.py"
    script.write_text(
        "import sys, time\n"
        "for percent in (10, 55, 100):\n"
        "    sys.stderr.write(f'\\rmodel.safetensors: {percent}%|##| 4.06G/4.06G')\n"
        "    sys.stderr.flush(); time.sleep(0.25)\n"
        "sys.stderr.write('\\nloaded\\nfailed at the end')\n"
        "sys.exit(3)\n"
    )
    with pytest.raises(RuntimeError) as error:
        backend._run([sys.executable, str(script)], tmp_path, None, echo=echo)
    message = str(error.value)
    # Each tqdm redraw is its own tail line, and the unterminated last line is kept.
    assert "model.safetensors: 55%" in message
    assert message.endswith("loaded\nfailed at the end")
    err = capsys.readouterr().err
    if echo:
        assert "\rmodel.safetensors: 10%" in err and "\rmodel.safetensors: 100%" in err
    else:
        assert "model.safetensors" not in err


def test_force_download_progress_enables_bars_without_tty(monkeypatch):
    package = ModuleType("huggingface_hub")
    utils = ModuleType("huggingface_hub.utils")
    hf_tqdm = ModuleType("huggingface_hub.utils.tqdm")
    hf_tqdm.is_tqdm_disabled = lambda log_level: None  # None hides bars on non-TTY
    utils.tqdm = type("tqdm", (), {})  # The package re-exports the class under the same name.
    package.utils = utils
    monkeypatch.setitem(sys.modules, "huggingface_hub", package)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", utils)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils.tqdm", hf_tqdm)
    checks.force_download_progress()
    assert hf_tqdm.is_tqdm_disabled(logging.INFO) is False
    assert hf_tqdm.is_tqdm_disabled(logging.NOTSET) is True
