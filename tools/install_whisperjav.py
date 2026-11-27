"""Install the isolated, pinned ASR backend. Invoked by deploy/worker/linux/worker.sh and the desktop installer (tools/desktop_worker.py)."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "1.9.5"
REVISION = "8917f5a9ec5f63884827ed6bbda00bdb16e056cd"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_sources

# 没有测速结果时的地址；main() 会先测速，把选中的地址写进 VIDEO_TURBO_* 环境变量。
DEFAULT_PYPI_INDEX_URL = select_sources.SOURCES["pypi"].candidates[0][1]
DEFAULT_TORCH_INDEX_BASE_URL = select_sources.SOURCES["torch"].candidates[0][1]


def pypi_index_url() -> str:
    return os.environ.get("VIDEO_TURBO_PYPI_INDEX_URL") or DEFAULT_PYPI_INDEX_URL


def torch_index_base_url() -> str:
    return (os.environ.get("VIDEO_TURBO_TORCH_INDEX_URL") or DEFAULT_TORCH_INDEX_BASE_URL).rstrip("/")


# Jasna 的 Linux NVIDIA 基线。TorchAudio 2.11 使用稳定 ABI，
# torchaudio.load 在这个版本通过 TorchCodec 读取音频。
LINUX_CUDA_PACKAGES = (
    "torch==2.12.1+cu130", "torchvision==0.27.1+cu130",
    "torchaudio==2.11.0+cu130", "torchcodec==0.14.0+cu130",
)
# Windows 桌面版仍使用 PyTorch 2.8 的驱动兼容选项。
CUDA_INDEXES = ("cu128", "cu126")
DEFAULT_CUDA_INDEX = "cu128"
CU128_MIN_DRIVER = 570
CU126_MIN_DRIVER = 525
BLACKWELL_MIN_COMPUTE = 10.0


def torch_packages(
    system: str, device: str, has_nvidia: bool, cuda_index: str = DEFAULT_CUDA_INDEX
) -> list[str]:
    packages = ["torch==2.8.0", "torchaudio==2.8.0"]
    if system == "Linux" and device in {"auto", "cuda"} and has_nvidia:
        return [*LINUX_CUDA_PACKAGES, "--extra-index-url", pypi_index_url(),
                "--index-url", f"{torch_index_base_url()}/cu130"]
    if system == "Darwin":
        return packages + ["--index-url", pypi_index_url()]
    cuda = device in {"auto", "cuda"} and has_nvidia
    index = cuda_index if cuda else "cpu"
    return packages + ["--index-url", f"{torch_index_base_url()}/{index}"]


def _driver_major(version: str) -> int | None:
    head = version.strip().split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _compute_capability(value: str) -> float | None:
    try:
        return float(value.strip())
    except ValueError:
        return None


def query_nvidia_gpus() -> list[tuple[str, str]]:
    """Return (driver_version, compute_cap) per GPU; compute_cap may be empty."""

    def query(fields: str) -> list[list[str]] | None:
        try:
            completed = subprocess.run(
                ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode:
            return None
        rows = [[cell.strip() for cell in line.split(",")]
                for line in completed.stdout.splitlines() if line.strip()]
        return rows or None

    rows = query("driver_version,compute_cap")
    if rows and all(len(row) == 2 for row in rows):
        return [(row[0], row[1]) for row in rows]
    # 较旧驱动不支持 compute_cap 字段，此时只读取驱动版本。
    rows = query("driver_version")
    return [(row[0], "") for row in rows] if rows else []


def select_cuda_index(gpus: list[tuple[str, str]], system: str) -> tuple[str, str]:
    """Pick the PyTorch CUDA wheel index for the host driver; exit if unsupported."""
    override = (os.environ.get("VIDEO_TURBO_TORCH_CUDA") or "").strip().lower()
    drivers = [major for major in (_driver_major(driver) for driver, _ in gpus) if major]
    if system == "Linux":
        if override and override != "cu130":
            raise SystemExit("Linux 字幕使用 cu130，请取消 VIDEO_TURBO_TORCH_CUDA 或将它设为 cu130。")
        if not drivers or min(drivers) < 580:
            raise SystemExit("Linux 字幕的 CUDA 13.0 需要可检测的 NVIDIA 580 或更高版本驱动。")
        return "cu130", f"驱动 {min(drivers)}，使用 Jasna 的 PyTorch 2.12.1 + cu130"
    if override:
        if override not in CUDA_INDEXES:
            raise SystemExit(
                f"VIDEO_TURBO_TORCH_CUDA 只能是 {' 或 '.join(CUDA_INDEXES)}，当前为 {override}。"
            )
        return override, f"使用 VIDEO_TURBO_TORCH_CUDA 指定的 {override}"
    if not drivers:
        return DEFAULT_CUDA_INDEX, f"无法读取 NVIDIA 驱动版本，按默认使用 {DEFAULT_CUDA_INDEX}"
    driver = min(drivers)
    caps = [cap for cap in (_compute_capability(value) for _, value in gpus) if cap]
    blackwell = any(cap >= BLACKWELL_MIN_COMPUTE for cap in caps)
    if driver >= CU128_MIN_DRIVER:
        return "cu128", f"驱动 {driver} ≥ {CU128_MIN_DRIVER}，使用 cu128"
    if blackwell:
        raise SystemExit(
            f"检测到 Blackwell 显卡（如 RTX 5090），但驱动为 {driver}；"
            f"需要 {CU128_MIN_DRIVER} 或更高版本的 NVIDIA 驱动。"
            "请更换驱动较新的云主机或镜像；镜像里的 CUDA Toolkit 版本不影响本项目。"
        )
    if driver >= CU126_MIN_DRIVER:
        return "cu126", f"驱动 {driver} 低于 {CU128_MIN_DRIVER}，改用兼容的 cu126"
    raise SystemExit(
        f"NVIDIA 驱动 {driver} 过旧，至少需要 {CU126_MIN_DRIVER}（推荐 {CU128_MIN_DRIVER}+）。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="安装 WhisperJAV 独立环境")
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--device", choices=["auto", "cuda", "mps", "cpu"], default="auto")
    parser.add_argument(
        "--check-cuda", action="store_true",
        help="只检测 NVIDIA 驱动并输出将使用的 PyTorch CUDA 版本，不安装",
    )
    args = parser.parse_args()
    has_nvidia = bool(shutil.which("nvidia-smi"))
    cuda_index = DEFAULT_CUDA_INDEX
    if platform.system() == "Linux" and args.device == "cuda" and not has_nvidia:
        raise SystemExit("Linux CUDA 字幕安装需要通过 nvidia-smi 检测 NVIDIA 驱动。")
    if platform.system() != "Darwin" and args.device in {"auto", "cuda"} and has_nvidia:
        cuda_index, reason = select_cuda_index(query_nvidia_gpus(), platform.system())
        print(f"PyTorch CUDA 版本：{reason}。", flush=True)
    if args.check_cuda:
        return
    project = Path(__file__).resolve().parents[1]
    venv = (args.venv or project / ".venv-whisperjav").resolve()
    if not (3, 11) <= sys.version_info[:2] < (3, 13):
        raise SystemExit("请使用 Python 3.11 或 3.12 安装。")
    missing = [name for name in ("git", "ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise SystemExit("缺少安装依赖，请先安装并加入 PATH：" + ", ".join(missing))
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # 测速选出 PyPI、PyTorch、GitHub 的地址；安装脚本已选过时这里直接沿用。
    os.environ.update(select_sources.select(["pypi", "torch", "github"]))
    marker = venv / "video-turbo-whisperjav.json"
    marker.unlink(missing_ok=True)
    env = dict(os.environ)
    # Never resolve PyTorch from a CUDA index inherited from another environment.
    for key in (
        "PIP_EXTRA_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_INDEX",
        "PIP_CONSTRAINT", "PYTHONPATH", "PYTHONHOME",
    ):
        env.pop(key, None)
    env["PIP_INDEX_URL"] = pypi_index_url()
    env["UV_DEFAULT_INDEX"] = env["PIP_INDEX_URL"]
    # CUDA 版 PyTorch 及其 nvidia-* 依赖的 wheel 缓存会额外占用数 GB，不保留。
    env["PIP_NO_CACHE_DIR"] = "1"
    env["UV_NO_CACHE"] = "1"
    env["PYTHONUTF8"] = "1"

    def run(*command: str) -> None:
        subprocess.run(command, env=env, check=True)

    def check_linux_runtime(interpreter: str, check: str) -> None:
        # Match the Worker subprocess environment: system libraries must load
        # without LD_LIBRARY_PATH inherited from the cloud image.
        runtime_env = {key: value for key, value in env.items() if key != "LD_LIBRARY_PATH"}
        check_script = project / "src/video_turbo/worker/whisperjav_self_test.py"
        subprocess.run(
            [interpreter, "-I", "-c",
             f"import runpy, sys; runpy.run_path(sys.argv[1])[{check!r}]()",
             str(check_script)],
            env=runtime_env,
            check=True,
        )

    if platform.system() == "Linux":
        check_linux_runtime(sys.executable, "check_ten_vad_runtime")
    if not python.is_file():
        run(sys.executable, "-m", "venv", str(venv))
    probe = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode:
        run(str(python), "-m", "ensurepip", "--upgrade")
    run(str(python), "-m", "pip", "install", "--upgrade", "pip", "wheel")
    print("安装 WhisperJAV 的 PyTorch 环境（按平台选择，首次下载较大）…", flush=True)
    torch_args = torch_packages(platform.system(), args.device, has_nvidia, cuda_index)
    print(f"PyTorch / torchaudio 依赖源：{torch_args[-1]}", flush=True)
    run(
        str(python),
        "-m",
        "pip",
        "install",
        *torch_args,
    )
    run(
        str(python),
        "-m",
        "pip",
        "install",
        f"whisperjav[cli,qwen] @ git+{select_sources.github_prefix()}"
        f"https://github.com/meizhong986/WhisperJAV.git@{REVISION}",
        "qwen-asr==0.0.6",
        *(LINUX_CUDA_PACKAGES if cuda_index == "cu130" else ()),
    )
    run(str(python), "-m", "pip", "check")
    run(
        str(python),
        "-c",
        "from importlib.metadata import version; from importlib.util import find_spec; "
        f"assert version('whisperjav') == {VERSION!r}; "
        "assert all(find_spec(name) for name in ('qwen_asr', 'fireredvad', 'ten_vad', 'transformers'))",
    )
    if cuda_index == "cu130":
        check_linux_runtime(str(python), "check_linux_cuda_audio")
    marker.write_text(json.dumps({"version": VERSION, "revision": REVISION, "ensemble": True}), encoding="utf-8")
    print("WhisperJAV 独立环境安装完成。运行自检即可下载并验证双遍模型、VAD 和合并。")


if __name__ == "__main__":
    main()
