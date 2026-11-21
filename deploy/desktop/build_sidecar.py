"""准备桌面版安装包里的资源：打包好的主程序、FFmpeg 和云机部署包。

在要出包的系统上运行（PyInstaller 不能跨平台打包）：

    uv run --extra orchestrator --extra desktop python deploy/desktop/build_sidecar.py

产物放在 desktop/apps/videoturbo/resources/（不入库），Tauri 打包时整个目录作为资源带上：

- server/：PyInstaller 打包的主程序文件夹，入口是 videoturbo-server(.exe)
- ffmpeg/：ffmpeg、ffprobe 及其许可证
- worker-bundle/：识别程序的源码。开云机时推送到实例，本机识别和识别机应用也从这里安装；内容与 Docker 镜像里的 /app/bundle 相同
- uv/：安装本机识别运行环境用的 uv
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "deploy" / "desktop" / "videoturbo-server.spec"
RESOURCES = ROOT / "desktop" / "apps" / "videoturbo" / "resources"
CACHE = ROOT / "build" / "desktop"

# FFmpeg 6.1.1 静态构建（GPLv3），来自 ffmpeg-static 项目的 GitHub 发布；按 SHA-256 校验。
FFMPEG_RELEASE = "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1"
FFMPEG_FILES = {
    "darwin-arm64": {
        "ffmpeg-darwin-arm64.gz": "8923876afa8db5585022d7860ec7e589af192f441c56793971276d450ed3bbfa",
        "ffprobe-darwin-arm64.gz": "d986a8ec7b030899fe66a8a288ed809a3543338705a3ce178cfb85869c5d80be",
        "darwin-arm64.LICENSE": "cb48bf09a11f5fb576cddb0431c8f5ed0a60157a9ec942adffc13907cbe083f2",
        "darwin-arm64.README": "05ba4b92c96605434b1aaae3eedf5a2c280c9607bf78ffca9a5b536d9af2dc6a",
    },
    "win32-x64": {
        "ffmpeg-win32-x64.gz": "8883a3dffbd0a16cf4ef95206ea05283f78908dbfb118f73c83f4951dcc06d77",
        "ffprobe-win32-x64.gz": "f309e6223ad89d2fe54bccd420a7709b66fd27540674e92309578ed491a43c8d",
        "win32-x64.LICENSE": "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    },
}


# uv 0.13.6 的发布文件，按 SHA-256 校验。本机识别用它下载 Python 3.11 和安装依赖。
UV_RELEASE = "https://github.com/astral-sh/uv/releases/download/0.13.6"
UV_FILES = {
    "darwin-arm64": ("uv-aarch64-apple-darwin.tar.gz", "60fbd5c8a1ed681845eea28d9d622fcde03c51e91a43465b64a1e17db1ed3472"),
    "win32-x64": ("uv-x86_64-pc-windows-msvc.zip", "a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f"),
}


def log(message: str) -> None:
    print(f"[desktop] {message}", flush=True)


def target() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine == "arm64":
        return "darwin-arm64"
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return "win32-x64"
    raise SystemExit(f"桌面版只支持 Apple 芯片的 macOS 和 64 位 Windows，当前是 {sys.platform} {machine}。")


def build_dashboard() -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("找不到 npm，无法构建控制台前端。已构建过时可加 --skip-web。")
    log("构建控制台前端")
    subprocess.run([npm, "ci"], cwd=ROOT / "web", check=True)
    subprocess.run([npm, "run", "build"], cwd=ROOT / "web", check=True)


def build_server(output: Path) -> None:
    dashboard = ROOT / "src" / "video_turbo" / "orchestrator" / "static" / "dashboard" / "index.html"
    if not dashboard.is_file():
        raise SystemExit("控制台前端还没有构建：去掉 --skip-web 重新运行。")
    log("用 PyInstaller 打包主程序")
    work = CACHE / "pyinstaller"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(work / "dist"), "--workpath", str(work / "build"), str(SPEC),
        ],
        cwd=ROOT,
        check=True,
    )
    shutil.rmtree(output, ignore_errors=True)
    shutil.copytree(work / "dist" / "videoturbo-server", output, symlinks=True)


def _download(name: str, expected: str, release: str = FFMPEG_RELEASE) -> Path:
    path = CACHE / "downloads" / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        log(f"下载 {name}")
        with urllib.request.urlopen(f"{release}/{name}", timeout=120) as response:
            data = response.read()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise SystemExit(f"{name} 的 SHA-256 不符：应为 {expected}，实际 {actual}。")
        path.write_bytes(data)
    return path


def fetch_ffmpeg(platform_key: str, output: Path) -> None:
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    suffix = ".exe" if platform_key.startswith("win32") else ""
    for name, digest in FFMPEG_FILES[platform_key].items():
        source = _download(name, digest)
        if name.endswith(".gz"):
            tool = name.split("-", 1)[0]
            target_path = output / f"{tool}{suffix}"
            target_path.write_bytes(gzip.decompress(source.read_bytes()))
            target_path.chmod(0o755)
        else:
            shutil.copyfile(source, output / ("FFMPEG-" + name.split(".", 1)[1]))


def fetch_uv(platform_key: str, output: Path) -> None:
    name, digest = UV_FILES[platform_key]
    archive = _download(name, digest, UV_RELEASE)
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    wanted = {"uv", "uv.exe", "uvx", "uvx.exe"}
    data = archive.read_bytes()
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            for member in bundle.namelist():
                if Path(member).name in wanted:
                    (output / Path(member).name).write_bytes(bundle.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
            for member in bundle.getmembers():
                if member.isfile() and Path(member.name).name in wanted:
                    target = output / Path(member.name).name
                    target.write_bytes(bundle.extractfile(member).read())
                    target.chmod(0o755)
    if not any((output / name).is_file() for name in ("uv", "uv.exe")):
        raise SystemExit(f"{name} 里没有找到 uv。")


def copy_worker_bundle(output: Path) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from video_turbo.orchestrator.worker_bundle import PATTERNS, REQUIRED

    shutil.rmtree(output, ignore_errors=True)
    files = {
        path for pattern in PATTERNS for path in ROOT.glob(pattern)
        if path.is_file() and "__pycache__" not in path.parts
    }
    for path in sorted(files):
        relative = path.relative_to(ROOT)
        (output / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output / relative)
    missing = [name for name in REQUIRED if not (output / name).is_file()]
    if missing:
        raise SystemExit(f"云机部署包缺少文件：{', '.join(missing)}")
    log(f"云机部署包：{len(files)} 个文件")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-web", action="store_true", help="不重新构建控制台前端")
    parser.add_argument("--skip-server", action="store_true", help="不重新打包主程序")
    args = parser.parse_args()
    platform_key = target()
    RESOURCES.mkdir(parents=True, exist_ok=True)
    if not args.skip_server:
        if not args.skip_web:
            build_dashboard()
        build_server(RESOURCES / "server")
    fetch_ffmpeg(platform_key, RESOURCES / "ffmpeg")
    fetch_uv(platform_key, RESOURCES / "uv")
    copy_worker_bundle(RESOURCES / "worker-bundle")
    log(f"完成：{RESOURCES}")


if __name__ == "__main__":
    main()
