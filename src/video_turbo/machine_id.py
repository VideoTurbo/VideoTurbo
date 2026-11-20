"""Worker 的机器标识：由硬件自动生成，不需要手工命名。

- NVIDIA 机器：GPU UUID 的哈希（``gpu-3f2a9c1d``）。镜像克隆不会带走 GPU UUID，
  同一镜像开出的多台云机、或在别的型号上运行的镜像都会得到各自正确的标识。
- Mac：IOPlatformUUID 的哈希（``mac-…``）；其他 CPU 机器：系统硬件 UUID（``cpu-…``）。

同一台机器上的多个 Worker 实例在标识后追加 ``-01``、``-02`` 或 ``-dc``，主控按去掉后缀的部分
把它们归为一台机器（云实例绑定、影片修复续传保留都按机器）。

本模块只用标准库，主控（开云机自动部署）和 Worker 共用。
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import uuid
from pathlib import Path

from .i18n import msg

_SUFFIX = re.compile(r"^(?:\d{1,3}|dc)$")


def _digest(value: str) -> str:
    return hashlib.sha1(value.strip().lower().encode("utf-8")).hexdigest()[:8]


def from_gpu_uuid(gpu_uuid: str) -> str:
    """nvidia-smi 报告的 GPU UUID（如 ``GPU-1f2e…``）→ 机器标识。"""
    if not gpu_uuid.strip():
        raise ValueError(msg("machine.noUuid"))
    return f"gpu-{_digest(gpu_uuid)}"


def nvidia_uuid_command(device_index: int = 0) -> list[str]:
    return ["nvidia-smi", f"--id={device_index}", "--query-gpu=uuid", "--format=csv,noheader"]


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command, check=True, capture_output=True, text=True, encoding="utf-8", timeout=15
    )
    return completed.stdout.strip()


def _host_uuid() -> tuple[str, str]:
    system = platform.system()
    if system == "Darwin":
        output = _run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
        if match:
            return "mac", match.group(1)
    elif system == "Windows":
        output = _run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"]
        )
        match = re.search(r"MachineGuid\s+REG_SZ\s+(\S+)", output)
        if match:
            return "cpu", match.group(1)
    else:
        for path in ("/sys/class/dmi/id/product_uuid", "/etc/machine-id"):
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                return "cpu", value
    return "cpu", f"{uuid.getnode():012x}"


def detect_machine_id(device: str = "auto", device_index: int = 0) -> str:
    """优先使用所选 GPU 的 UUID；没有 NVIDIA GPU 或使用 CPU/MPS 时改用主机硬件 UUID。

    环境变量 ``VIDEO_TURBO_MACHINE_ID`` 可覆盖（仅用于测试或特殊环境）。
    """
    override = os.environ.get("VIDEO_TURBO_MACHINE_ID", "").strip()
    if override:
        return override
    if device not in {"cpu", "mps"} and platform.system() != "Darwin":
        try:
            first = _run(nvidia_uuid_command(device_index)).splitlines()[0]
            return from_gpu_uuid(first)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
    prefix, value = _host_uuid()
    return f"{prefix}-{_digest(value)}"


def worker_name(machine_id: str, suffix: str | None = None) -> str:
    """机器标识 + 实例后缀（``01``、``02``…或 ``dc``）。"""
    suffix = (suffix or "").strip()
    if not suffix:
        return machine_id
    if not _SUFFIX.fullmatch(suffix):
        raise ValueError(msg("machine.badSuffix", suffix=suffix))
    return f"{machine_id}-{suffix}"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="输出本机的机器标识")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--device-index", type=int, default=0)
    args = parser.parse_args()
    print(detect_machine_id(args.device, args.device_index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
