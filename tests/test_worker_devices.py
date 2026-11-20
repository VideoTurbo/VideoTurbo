import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import raises_rendered

from video_turbo.database import Database
from video_turbo.models import WorkerHeartbeat, WorkerRegistration
from video_turbo.worker import gpu
from video_turbo.worker import whisperjav as whisperjav_module
from video_turbo.worker.asr import Transcriber
from video_turbo.worker.config import WorkerConfig
from video_turbo.worker.whisperjav import WhisperJavTranscriber


@pytest.mark.parametrize(
    "requested,available,supported,expected",
    [
        ("auto", {"mps", "cpu"}, {"mps", "cpu"}, "mps"),
        ("auto", {"mps", "cpu"}, {"cuda", "cpu"}, "cpu"),
        ("mps", {"mps", "cpu"}, {"cuda", "cpu"}, "cpu"),
        ("auto", {"cuda", "cpu"}, {"cuda", "cpu"}, "cuda"),
        ("cuda", {"cpu"}, {"cuda", "cpu"}, "cpu"),
        ("cpu", {"cuda", "cpu"}, {"cuda", "cpu"}, "cpu"),
    ],
)
def test_backend_device_selection(requested, available, supported, expected):
    assert gpu.select_device(requested, available, supported) == expected


def test_mac_uses_system_metrics_without_nvidia(monkeypatch):
    import psutil

    monkeypatch.setattr(gpu.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gpu.platform, "processor", lambda: "Apple test CPU")
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: SimpleNamespace(total=16 * 1024**3, available=6 * 1024**3)
    )
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval: 23.4)

    def no_nvidia(*args):
        raise AssertionError("Mac 不应查询 nvidia-smi")

    monkeypatch.setattr(gpu, "_query_nvidia", no_nvidia)
    info = gpu.query_gpu()
    assert (info.device_type, info.memory_total_mb, info.memory_free_mb, info.utilization) == (
        "cpu",
        16384,
        6144,
        23,
    )


def test_missing_nvidia_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(gpu.platform, "system", lambda: "Linux")

    def missing(_index):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(gpu, "_query_nvidia", missing)
    monkeypatch.setattr(gpu, "query_cpu", lambda: SimpleNamespace(device_type="cpu"))
    assert gpu.query_gpu().device_type == "cpu"


@pytest.mark.parametrize("device,compute", [("cpu", "int8"), ("cuda", "float16")])
def test_faster_whisper_uses_supported_precision(monkeypatch, device, compute):
    calls = []
    package = ModuleType("faster_whisper")

    def model(path, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(feature_extractor=SimpleNamespace(sampling_rate=16000))

    package.WhisperModel = model
    audio = ModuleType("faster_whisper.audio")
    audio.decode_audio = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio)
    monkeypatch.setattr(
        "video_turbo.worker.asr._download_model_with_progress", lambda *_args: "cached-model"
    )
    monkeypatch.setattr(
        "video_turbo.worker.asr.query_gpu", lambda *_args: SimpleNamespace(device_type=device)
    )
    transcriber = Transcriber(WorkerConfig())
    transcriber.load()
    assert calls[0]["device"] == device
    assert calls[0]["compute_type"] == compute


def test_qwen_mps_request_uses_explicit_cpu_flags_and_float32(tmp_path, monkeypatch):
    config = WorkerConfig(device="mps", whisperjav={"python_executable": sys.executable})
    monkeypatch.setattr(
        "video_turbo.worker.whisperjav.query_gpu", lambda *_: SimpleNamespace(device_type="cpu", memory_total_mb=16384)
    )
    backend = WhisperJavTranscriber(config)
    backend.load()
    command = backend._command(tmp_path / "audio.opus", tmp_path / "output", tmp_path / "temp")
    assert command[command.index("--qwen-device") + 1] == "cpu"
    assert command[command.index("--device") + 1] == "cpu"
    assert command[command.index("--qwen-dtype") + 1] == "float32"
    assert "--accept-cpu-mode" in command
    assert backend._environment()["CUDA_VISIBLE_DEVICES"] == ""
    assert backend._environment()["VIDEO_TURBO_FIRERED_VAD_GPU"] == "0"


@pytest.mark.parametrize("enabled", [True, False])
def test_cuda_whisperjav_runs_launcher_with_firered_vad_switch(tmp_path, monkeypatch, enabled):
    config = WorkerConfig(
        device="cuda",
        whisperjav={"python_executable": sys.executable, "firered_vad_gpu": enabled},
    )
    monkeypatch.setattr(
        "video_turbo.worker.whisperjav.query_gpu", lambda *_: SimpleNamespace(device_type="cuda", memory_total_mb=24576)
    )
    backend = WhisperJavTranscriber(config)
    backend.load()
    command = backend._command(tmp_path / "audio.opus", tmp_path / "output", tmp_path / "temp")
    assert Path(command[1]).name == "whisperjav_launcher.py" and Path(command[1]).is_file()
    env = backend._environment()
    assert env["VIDEO_TURBO_FIRERED_VAD_GPU"] == ("1" if enabled else "0")
    assert env["PYTHONSAFEPATH"] == "1"


def test_cpu_worker_device_is_preserved_in_database(tmp_path):
    db = Database(tmp_path / "app.db")
    db.initialize()
    db.register_worker(
        WorkerRegistration(
            name="mac-01",
            version="0.0.0",
            gpu_name="Mac",
            device_type="cpu",
            memory_total_mb=16000,
            model="large-v3",
            compute_type="int8", supported_asr_modes=["faster_whisper"],
        )
    )
    db.heartbeat_worker("mac-01", WorkerHeartbeat(asr_device="cpu"))
    worker = db.get_worker("mac-01")
    assert worker["device_type"] == worker["asr_device"] == "cpu"
    db.initialize()  # Upgrade/startup preserves the existing device.
    assert db.list_workers()[0]["device_type"] == "cpu"


def test_mac_installer_uses_native_pytorch_index(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "tools/install_whisperjav.py"
    spec = importlib.util.spec_from_file_location("install_whisperjav", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("VIDEO_TURBO_PYPI_INDEX_URL", raising=False)
    assert module.torch_packages("Darwin", "auto", False)[-1] == (
        "https://pypi.tuna.tsinghua.edu.cn/simple"
    )
    assert module.torch_packages("Linux", "cpu", True)[-1].endswith("/cpu")
    assert module.torch_packages("Linux", "auto", True)[-1].endswith("/cu128")


def load_installer(name="cuda_installer"):
    path = Path(__file__).resolve().parents[1] / "tools/install_whisperjav.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("gpus", "expected"),
    [
        ([("575.51.03", "12.0")], "cu128"),  # RTX 5090 on R575
        ([("570.86.10", "8.9")], "cu128"),
        ([("550.54.15", "8.6")], "cu126"),  # CUDA 12.4 era driver on RTX 3090
        ([("535.183.01", "")], "cu126"),  # Driver too old for compute_cap query
        ([("525.60.13", "8.9")], "cu126"),
        ([], "cu128"),  # Unknown driver keeps the historical default
        ([("575.51", "8.6"), ("550.54", "8.9")], "cu126"),  # Lowest driver wins
    ],
)
def test_torch_cuda_index_follows_host_driver(monkeypatch, gpus, expected):
    module = load_installer()
    monkeypatch.delenv("VIDEO_TURBO_TORCH_CUDA", raising=False)
    assert module.select_cuda_index(gpus)[0] == expected
    assert module.torch_packages("Linux", "auto", True, expected)[-1].endswith("/" + expected)


@pytest.mark.parametrize(
    ("gpus", "message"),
    [
        ([("560.35.03", "12.0")], "Blackwell"),
        ([("565.57", "10.0")], "Blackwell"),
        ([("520.61.05", "8.6")], "过旧"),
    ],
)
def test_torch_cuda_index_rejects_unsupported_drivers(monkeypatch, gpus, message):
    module = load_installer()
    monkeypatch.delenv("VIDEO_TURBO_TORCH_CUDA", raising=False)
    with raises_rendered(SystemExit, message):
        module.select_cuda_index(gpus)


def test_torch_cuda_index_can_be_overridden(monkeypatch):
    module = load_installer()
    monkeypatch.setenv("VIDEO_TURBO_TORCH_CUDA", "CU126")
    assert module.select_cuda_index([("575.51", "12.0")])[0] == "cu126"
    monkeypatch.setenv("VIDEO_TURBO_TORCH_CUDA", "cu124")
    with raises_rendered(SystemExit, "VIDEO_TURBO_TORCH_CUDA"):
        module.select_cuda_index([])


def test_query_nvidia_gpus_falls_back_without_compute_cap(monkeypatch):
    module = load_installer()

    def run(command, **kwargs):
        if "compute_cap" in command[1]:
            return SimpleNamespace(returncode=2, stdout="Field \"compute_cap\" is not a valid field\n")
        return SimpleNamespace(returncode=0, stdout="535.183.01\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.query_nvidia_gpus() == [("535.183.01", "")]


def test_check_cuda_only_reports_selection(monkeypatch, capsys):
    module = load_installer()
    monkeypatch.delenv("VIDEO_TURBO_TORCH_CUDA", raising=False)
    monkeypatch.setattr(sys, "argv", ["install_whisperjav.py", "--check-cuda"])
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module, "query_nvidia_gpus", lambda: [("550.54.15", "8.6")])

    def forbidden(*args, **kwargs):
        raise AssertionError("--check-cuda 不应执行安装命令")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    module.main()
    assert "cu126" in capsys.readouterr().out


def test_installer_repairs_existing_environment_without_pip(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "tools/install_whisperjav.py"
    spec = importlib.util.spec_from_file_location("repair_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    venv = tmp_path / "isolated"
    executable = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(sys, "argv", [str(path), "--venv", str(venv)])
    monkeypatch.setattr(module.shutil, "which", lambda name: str(tmp_path / name))
    monkeypatch.setattr(module, "query_nvidia_gpus", lambda: [("575.51", "8.6")])
    calls = []
    repaired = False

    def run(command, **kwargs):
        nonlocal repaired
        calls.append(command)
        if command[1:] == ["-m", "pip", "--version"]:
            return SimpleNamespace(returncode=1)
        if command[1:3] == ("-m", "ensurepip"):
            repaired = True
        if command[1:4] == ("-m", "pip", "install"):
            assert repaired, "必须在安装包之前补齐 pip"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    assert repaired
    marker = json.loads((venv / "video-turbo-whisperjav.json").read_text())
    assert marker["ensemble"] is True


def test_linux_installer_checks_native_runtime_before_installing_packages(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "tools/install_whisperjav.py"
    spec = importlib.util.spec_from_file_location("native_runtime_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    venv = tmp_path / "isolated"
    monkeypatch.setattr(sys, "argv", [str(path), "--venv", str(venv)])
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module, "query_nvidia_gpus", lambda: [("575.51", "8.6")])
    monkeypatch.setenv("LD_LIBRARY_PATH", "/cloud/image/lib")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[1:3] == ["-I", "-c"]
        assert "LD_LIBRARY_PATH" not in kwargs["env"]
        assert Path(command[-1]).is_file()
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    assert len(calls) == 1
    assert not (venv / "video-turbo-whisperjav.json").exists()


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"cpu.max": "1500000 100000\n"}, 15.0),
        ({"cpu.max": "max 100000\n"}, None),
        ({"cpu/cpu.cfs_quota_us": "400000\n", "cpu/cpu.cfs_period_us": "100000\n"}, 4.0),
        ({"cpu/cpu.cfs_quota_us": "-1\n", "cpu/cpu.cfs_period_us": "100000\n"}, None),
        ({}, None),
    ],
)
def test_available_cpus_honours_container_quota(tmp_path, monkeypatch, files, expected):
    monkeypatch.setattr("os.sched_getaffinity", lambda _pid: set(range(128)), raising=False)
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    assert whisperjav_module.available_cpus(tmp_path) == (expected or 128.0)


def test_whisperjav_threads_split_quota_between_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(whisperjav_module, "available_cpus", lambda: 15.0)
    monkeypatch.setenv("VIDEO_TURBO_WORKER_COUNT", "3")
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setenv("MKL_NUM_THREADS", "2")
    backend = WhisperJavTranscriber(WorkerConfig(whisperjav={"python_executable": sys.executable}))
    backend.device = "cuda"
    env = backend._environment()
    assert env["OMP_NUM_THREADS"] == env["NUMBA_NUM_THREADS"] == "5"
    assert env["MKL_NUM_THREADS"] == "2"  # explicit operator setting wins
    monkeypatch.setenv("VIDEO_TURBO_WORKER_COUNT", "32")
    assert whisperjav_module.cpu_thread_budget() == 1


def cache_repo(cache: Path, repo_id: str, *, dangling: bool = False, incomplete: bool = False) -> None:
    repo = cache / ("models--" + repo_id.replace("/", "--"))
    snapshot = repo / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (repo / "blobs").mkdir()
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc123")
    blob = repo / "blobs" / "f00d"
    if not dangling:
        blob.write_text("{}")
    (snapshot / "config.json").symlink_to(blob)
    if incomplete:
        (repo / "blobs" / "beef.incomplete").write_text("")


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, True),
        ({"dangling": True}, False),
        ({"incomplete": True}, False),
    ],
)
def test_hf_repo_cached_requires_finished_snapshot(tmp_path, options, expected):
    cache_repo(tmp_path, "Qwen/Qwen3-ASR-1.7B", **options)
    assert whisperjav_module.hf_repo_cached(tmp_path, "Qwen/Qwen3-ASR-1.7B") is expected
    assert whisperjav_module.hf_repo_cached(tmp_path, "litagin/anime-whisper") is False


def test_whisperjav_runs_offline_only_when_job_models_are_cached(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    config = WorkerConfig(
        asr={"download_root": tmp_path},
        whisperjav={"python_executable": sys.executable, "speech_segmenter": "firered-vad"},
    )
    cache = tmp_path / "huggingface"
    cache_repo(cache, config.whisperjav.model)
    backend = WhisperJavTranscriber(config)
    assert backend._uncached_models() == ["FireRedTeam/FireRedVAD"]
    cache_repo(cache, "FireRedTeam/FireRedVAD")
    assert backend._uncached_models() == []
    backend.ensemble = True  # second pass adds anime-whisper; TEN VAD is bundled
    assert backend._uncached_models() == ["litagin/anime-whisper"]
    assert "HF_HUB_OFFLINE" not in backend._environment()
    assert backend._environment(offline=True)["HF_HUB_OFFLINE"] == "1"
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    assert backend._environment(offline=True)["HF_HUB_OFFLINE"] == "0"
    config.whisperjav.speech_segmenter = "whisperseg"
    assert "whisperseg" in backend._uncached_models()


@pytest.mark.parametrize(
    ("ensemble", "device", "memory", "enabled", "expected"),
    [
        (True, "cuda", 24576, True, "1"),
        (True, "cuda", 8192, True, "0"),  # CMP 40HX: both models do not fit at once
        (True, "cuda", 24576, False, "0"),
        (True, "cpu", 24576, True, "0"),
        (False, "cuda", 24576, True, "0"),
    ],
)
def test_parallel_passes_need_ensemble_cuda_and_memory(ensemble, device, memory, enabled, expected):
    backend = WhisperJavTranscriber(
        WorkerConfig(whisperjav={"python_executable": sys.executable, "parallel_passes": enabled})
    )
    backend.ensemble, backend.device, backend.memory_total_mb = ensemble, device, memory
    assert backend._environment()["VIDEO_TURBO_WHISPERJAV_PARALLEL_PASSES"] == expected
