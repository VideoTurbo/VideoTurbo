from __future__ import annotations

import csv
import io
import platform
import subprocess
from dataclasses import dataclass

from ..i18n import msg


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


def query_gpu(device_index: int = 0, device: str = "auto") -> GpuInfo:
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
