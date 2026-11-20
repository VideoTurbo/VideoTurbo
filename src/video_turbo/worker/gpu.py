from __future__ import annotations

import csv
import importlib.util
import io
import os
import platform
import site
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..i18n import msg

_DLL_DIRECTORY_HANDLES: list[object] = []


def nvidia_runtime_library_paths() -> list[Path]:
    """定位 pip 安装的 NVIDIA 动态库目录，兼容普通包和 namespace package。"""
    result: list[Path] = []
    missing: list[str] = []
    for module_name in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ModuleNotFoundError):
            spec = None
        candidates: list[Path] = []
        if spec is not None:
            if spec.submodule_search_locations:
                candidates.extend(Path(value) for value in spec.submodule_search_locations)
            elif spec.origin:
                candidates.append(Path(spec.origin).parent)
        existing = next((path for path in candidates if path.is_dir()), None)
        if existing is None:
            missing.append(module_name)
        elif existing not in result:
            result.append(existing)
    if missing:
        raise RuntimeError(msg("worker.gpu.libsMissing", paths=list(missing)))
    return result


@dataclass(slots=True)
class GpuInfo:
    name: str
    driver_version: str | None
    memory_total_mb: int
    memory_free_mb: int
    utilization: int
    device_type: str = "cuda"


def select_device(requested: str, available: set[str], supported: set[str]) -> str:
    """Choose only devices supported by both this backend and this machine."""
    candidates = ("cuda", "mps", "cpu") if requested == "auto" else (requested, "cpu")
    return next((device for device in candidates if device in available & supported), "cpu")


def query_cpu() -> GpuInfo:
    import psutil

    memory = psutil.virtual_memory()
    return GpuInfo(
        name=platform.processor() or platform.machine() or "CPU",
        driver_version=None,
        memory_total_mb=memory.total // (1024 * 1024),
        memory_free_mb=memory.available // (1024 * 1024),
        utilization=round(psutil.cpu_percent(interval=0.1)),
        device_type="cpu",
    )


def configure_cuda_dll_paths() -> list[Path]:
    """把 pip 安装的 CUDA 12 cuBLAS/cuDNN DLL 加入当前进程搜索路径。"""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return []
    roots = [Path(value) for value in site.getsitepackages()]
    user_site = site.getusersitepackages()
    if user_site:
        roots.append(Path(user_site))
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(
            [
                root / "nvidia" / "cublas" / "bin",
                root / "nvidia" / "cudnn" / "bin",
            ]
        )
    added: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir():
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(candidate)))
            os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
            added.append(candidate)
    return added


def query_gpu(device_index: int = 0, device: str = "auto") -> GpuInfo:
    # Both currently integrated engines support CUDA and CPU, not MPS.
    # Keep macOS on the same models and use CPU until a backend supports MPS.
    if device in {"cpu", "mps"} or platform.system() == "Darwin":
        return query_cpu()
    try:
        return _query_nvidia(device_index)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, IndexError):
        return query_cpu()


def _query_nvidia(device_index: int = 0) -> GpuInfo:
    command = [
        "nvidia-smi",
        f"--id={device_index}",
        "--query-gpu=name,driver_version,memory.total,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, encoding="utf-8", timeout=15
        )
    except FileNotFoundError as exc:
        raise RuntimeError(msg("worker.gpu.noSmi")) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(msg("worker.gpu.smiFailed", error=exc.stderr.strip())) from exc
    row = next(csv.reader(io.StringIO(completed.stdout.strip())))
    return GpuInfo(
        name=row[0].strip(),
        driver_version=row[1].strip(),
        memory_total_mb=int(row[2].strip()),
        memory_free_mb=int(row[3].strip()),
        utilization=int(row[4].strip()),
    )
