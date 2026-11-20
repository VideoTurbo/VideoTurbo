"""Linux Worker 部署包：开云机后由 NAS 通过 SSH 推送到实例。

只打包 Linux Worker 需要的源码、配置模板、安装脚本和工具，不含任何配置、密钥、环境或模型。
版本号是全部文件内容的 SHA-256 前 16 位：NAS 代码更新后版本随之变化，实例上会重新运行安装脚本。

源码目录按顺序查找：环境变量 ``VIDEO_TURBO_BUNDLE_ROOT``、项目源码目录（本地运行时）、
NAS 镜像中的 ``/app/bundle``（Dockerfile 复制）。
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

from ..i18n import msg

PATTERNS = (
    "pyproject.toml",
    "README.md",
    # AGPL 要求分发时附带许可证文本。
    "LICENSE",
    "src/**/*.py",
    "config/*.example.yaml",
    "deploy/worker/linux/*.sh",
    # 依赖清单：安装脚本按它安装与 CI 测试一致的版本。
    "deploy/worker/requirements/*.txt",
    "tools/*.py",
)
REQUIRED = (
    "pyproject.toml",
    "src/video_turbo/__init__.py",
    "deploy/worker/linux/worker.sh",
    "deploy/worker/requirements/asr.txt",
    "deploy/worker/requirements/restore.txt",
    "tools/configure_worker.py",
    "tools/select_sources.py",
)
CONTAINER_ROOT = Path("/app/bundle")


class BundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerBundle:
    version: str
    data: bytes
    file_count: int


def bundle_root() -> Path:
    configured = os.environ.get("VIDEO_TURBO_BUNDLE_ROOT", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates += [Path(__file__).resolve().parents[3], CONTAINER_ROOT]
    for candidate in candidates:
        if all((candidate / name).is_file() for name in REQUIRED):
            return candidate
    raise BundleError(
        msg("cloud.bundleMissing")
    )


def _files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in PATTERNS:
        for path in root.glob(pattern):
            if path.is_file() and "__pycache__" not in path.parts:
                found.add(path)
    return sorted(found, key=lambda path: path.relative_to(root).as_posix())


def build_bundle(root: Path | None = None) -> WorkerBundle:
    root = root or bundle_root()
    files = _files(root)
    digest = hashlib.sha256()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            content = path.read_bytes()
            digest.update(relative.encode() + b"\0" + content + b"\0")
            info = tarfile.TarInfo(relative)
            info.size = len(content)
            info.mode = 0o755 if relative.endswith(".sh") else 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return WorkerBundle(
        version=digest.hexdigest()[:16], data=buffer.getvalue(), file_count=len(files)
    )
