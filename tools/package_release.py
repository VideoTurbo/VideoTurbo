"""Build a source deployment directory using only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "release-manifest.json"
GENERATOR = "video-turbo/package_release.py"
REQUIRED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    ".dockerignore",
    "src/video_turbo/__init__.py",
    "src/video_turbo/orchestrator/static/login.html",
    "web/package.json",
    "web/package-lock.json",
    "web/index.html",
    "web/vite.config.ts",
    "web/tsconfig.json",
    "web/src/main.ts",
    "config/worker.example.yaml",
    "deploy/server/Dockerfile",
    "deploy/server/docker-compose.example.yml",
    "deploy/worker/linux/worker.sh",
    "deploy/worker/requirements/asr.txt",
    "deploy/worker/requirements/restore.txt",
    "tools/install_whisperjav.py",
    "tools/select_sources.py",
    "tools/download_jasna.py",
    "tools/configure_worker.py",
    "tools/configure_jasna.py",
    "tools/cleanup_worker.py",
)
# Explicit source types prevent local secrets/runtime files from entering the
# bundle even if a developer accidentally adds them to Git or changes .gitignore.
SOURCE_PATTERNS = (
    "src/**/*.py",
    # 登录页：auth.py 在导入时读取，缺少时主控无法启动。
    "src/video_turbo/orchestrator/static/*.html",
    # 控制台前端源码：主控镜像构建时用它生成页面，不打包本机的 node_modules 和构建产物。
    "web/src/**/*.ts",
    "web/src/**/*.vue",
    "web/src/**/*.css",
    "config/*.example.yaml",
    "deploy/worker/linux/*.sh",
    "deploy/worker/requirements/*.txt",
    "deploy/server/*.example.yml",
    "deploy/server/*.example.yaml",
    "README*.md",
    "tools/*.py",
)
EXCLUDED_DIRECTORIES = {
    "__pycache__", "build", "dist", "data", "logs", "work", "cache",
    "backups", "models", "runtime", "venv", "env", "jasna", "node_modules",
}

UPDATE_GUIDE = """video-turbo 更新说明

本目录是 Linux 显卡电脑上服务的安装与更新包（主程序使用 Docker 镜像或桌面版安装包，Windows 显卡电脑使用
VideoTurbo Worker 安装包，都不需要本目录），不含配置、密钥、数据库、模型和日志。

1. 等正在处理的任务结束，停止显卡电脑上的服务。
2. 把本目录的内容合并覆盖到原项目文件夹（不要先删除原文件夹，也不要再嵌套一层 video-turbo）。
   也可以在项目的上级目录解压压缩包，例如：
     cd ~ && unzip -o video-turbo.zip
     cd ~ && tar -xzf video-turbo.tar.gz
3. 让新代码生效：

主程序：在 docker-compose.yml 所在的文件夹执行
  docker compose pull && docker compose up -d

Windows 显卡电脑：下载并安装新版 VideoTurbo Worker，它会自动更新运行环境。

Linux 显卡电脑：在项目文件夹中执行
  bash deploy/worker/linux/worker.sh install asr      （装了影片修复时用 install all）
  bash deploy/worker/linux/worker.sh start

主程序和显卡电脑上的服务请一起更新。更多说明见官网文档：https://videoturbo.github.io/guide/update/
"""


def source_files(project: Path) -> list[Path]:
    paths = {project / name for name in REQUIRED_FILES}
    for pattern in SOURCE_PATTERNS:
        paths.update(
            path for path in project.glob(pattern)
            if path.is_file() and not any(
                part.startswith(".") or part in EXCLUDED_DIRECTORIES or part.endswith(".egg-info")
                for part in path.relative_to(project).parts
            )
        )
    for path in paths:
        if not path.is_file():
            raise ValueError(f"缺少安装所需文件：{path.relative_to(project)}")
        if path.resolve() != path:
            raise ValueError(f"安装文件不能是符号链接或经过符号链接目录：{path}")
    return sorted(paths)


def validate_output(project: Path, output: Path) -> None:
    if output.is_symlink():
        raise ValueError("输出目录不能是符号链接。")
    resolved = output.resolve()
    if project.is_relative_to(resolved):
        raise ValueError("输出目录不能覆盖项目或项目的父目录。")
    if resolved.is_relative_to(project) and resolved.relative_to(project).parts[0] != "dist":
        raise ValueError("项目内部的输出目录必须位于 dist/ 下，避免覆盖源码或实际配置。")
    if not output.exists():
        return
    try:
        manifest = json.loads((output / MANIFEST).read_text(encoding="utf-8"))
        if manifest["generator"] != GENERATOR:
            raise ValueError("不是本脚本生成的目录")
        allowed = {Path(MANIFEST)}
        for item in manifest["files"]:
            path = Path(item["path"])
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("文件清单含有无效路径")
            allowed.add(path)
            allowed.update(path.parents)
        for path in output.rglob("*"):
            if path.is_symlink() or path.relative_to(output) not in allowed:
                raise ValueError(f"目录中有非打包文件：{path.relative_to(output)}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"拒绝覆盖现有目录 {output}：{exc}。请另选输出目录。") from exc


def project_version(project: Path) -> str:
    """读取 src/video_turbo/__init__.py 中的 __version__，这是项目唯一的版本号来源。"""
    source = (project / "src" / "video_turbo" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        raise ValueError("src/video_turbo/__init__.py 中缺少 __version__。")
    return match.group(1)


def build_release(project: Path, output: Path) -> dict:
    project = project.resolve()
    output = output.absolute()
    validate_output(project, output)
    files = source_files(project)
    version = project_version(project)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build completely before replacing a previous package. A failed copy leaves
    # the last successful package intact, and every build removes stale sources.
    with tempfile.TemporaryDirectory(prefix=".video-turbo-package-", dir=output.parent) as temp:
        stage = Path(temp) / "new"
        stage.mkdir()
        for source in files:
            target = stage / source.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (stage / "UPDATE.txt").write_text(UPDATE_GUIDE, encoding="utf-8-sig")
        entries = [
            {
                "path": path.relative_to(stage).as_posix(),
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(stage.rglob("*")) if path.is_file()
        ]
        manifest = {
            "generator": GENERATOR,
            "version": version,
            "generated_at": datetime.now(UTC).isoformat(),
            "files": entries,
        }
        (stage / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        previous = Path(temp) / "previous"
        try:
            validate_output(project, output)
            if output.exists():
                output.rename(previous)
            stage.rename(output)
        except BaseException:
            if previous.exists() and not output.exists():
                previous.rename(output)
            raise
    return manifest


def archive_paths(output: Path) -> list[Path]:
    output = output.absolute()
    return [output.with_name(output.name + suffix) for suffix in (".zip", ".tar.gz")]


def build_archives(output: Path) -> list[Path]:
    """Pack the release directory as .zip and .tar.gz next to it.

    Members sit under one top-level folder named like the release directory
    (video-turbo/), so `unzip video-turbo.zip` in ~ yields ~/video-turbo and
    merges into an existing deployment there.
    """
    output = output.absolute()
    root = output.name
    files = sorted(path for path in output.rglob("*") if path.is_file())
    targets = archive_paths(output)
    for target in targets:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"拒绝覆盖 {target}：不是普通文件。")

    def reset_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    built = []
    with tempfile.TemporaryDirectory(prefix=".video-turbo-archive-", dir=output.parent) as temp:
        stage = Path(temp)
        zip_path, tar_path = stage / targets[0].name, stage / targets[1].name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                info = zipfile.ZipInfo.from_file(path, f"{root}/{path.relative_to(output).as_posix()}")
                info.compress_type = zipfile.ZIP_DEFLATED
                # Keep Unix permissions so shell scripts stay executable after unzip.
                info.external_attr = (path.stat().st_mode & 0o777 | 0o100000) << 16
                archive.writestr(info, path.read_bytes())
        with tarfile.open(tar_path, "w:gz") as archive:
            for path in files:
                archive.add(path, arcname=f"{root}/{path.relative_to(output).as_posix()}",
                            recursive=False, filter=reset_owner)
        for source, target in zip((zip_path, tar_path), targets):
            os.replace(source, target)
            built.append(target)
    return built


def main() -> None:
    parser = argparse.ArgumentParser(description="生成可上传覆盖的 video-turbo 安装目录（无需 gh/Git）")
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / "dist" / "video-turbo",
        help="输出目录，默认项目根目录下的 dist/video-turbo",
    )
    args = parser.parse_args()
    try:
        manifest = build_release(PROJECT_ROOT, args.output)
        archives = build_archives(args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"打包失败：{exc}\n")
    print(f"打包完成：{args.output.resolve()}")
    print(f"版本：{manifest['version']}，文件数：{len(manifest['files']) + 1}")
    for archive in archives:
        print(f"压缩包：{archive.resolve()}（{archive.stat().st_size / 1024 / 1024:.1f} MB）")
    print("将该目录或压缩包的内容合并覆盖到线上原项目目录；后续命令见包内 UPDATE.txt。")
    print(f"压缩包内含一层 {args.output.name}/ 目录；在项目的上级目录解压即可合并覆盖，例如：")
    print(f"  cd ~ && unzip -o {archives[0].name}")
    print(f"  cd ~ && tar -xzf {archives[1].name}")


if __name__ == "__main__":
    main()
