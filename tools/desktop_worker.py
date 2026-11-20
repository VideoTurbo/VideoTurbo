"""桌面版的识别运行环境：安装、写配置、查状态。只用标准库。

桌面外壳（desktop/crates/shell-core/src/worker_env.rs）用附带的 uv 准备好 Python 3.11，再用它运行本脚本。
源码目录（--bundle）就是安装包里的 worker-bundle（与开云机时推送的部署包相同）。运行环境放在 --root 下，
布局与源码部署一致（Worker 按 config 文件夹的上一级找 .venv-whisperjav、models、cache）::

    <root>/config/worker.yaml   服务配置
    <root>/.venv                识别程序
    <root>/.venv-whisperjav     默认识别模式的独立环境
    <root>/models、cache、logs
    <root>/mingit               Windows 上按需下载的 Git（安装 WhisperJAV 的依赖要用）
    <root>/state.json           已安装的版本，版本变化时外壳重新安装

进度以 ``@@VT `` 开头的 JSON 行输出到标准输出，其余输出原样写进安装日志。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import configure_worker
import select_sources

PROGRESS = "@@VT "
STATE_FILE = "state.json"
# Git for Windows 的精简版，只给 pip/uv 拉取 git+https 依赖用；按 SHA-256 校验。
MINGIT_URL = (
    "https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip"
)
MINGIT_SHA256 = "e3bf78351fe777fba47ddee5950082f8a98bb0f02604fdc7ba3ee2cb68ffa877"

STEPS = (
    ("sources", "选择下载源", "Choosing download sources"),
    ("git", "准备 Git", "Preparing Git"),
    ("venv", "创建运行环境", "Creating the program environment"),
    ("deps", "安装识别组件（第一次要下载显卡运行库，耗时较长）", "Installing recognition components (the first time downloads the GPU runtime and takes a while)"),
    ("program", "安装识别程序", "Installing the recognition program"),
    ("backend", "安装默认识别模式（下载 PyTorch，耗时较长）", "Installing the default recognition mode (downloads PyTorch, takes a while)"),
)


class SetupError(Exception):
    """安装不能继续；code 给外壳选择提示文字和下一步操作。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def emit(event: str, **fields: object) -> None:
    print(PROGRESS + json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


class Layout:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "config" / "worker.yaml"
        self.venv = root / ".venv"
        self.backend = root / ".venv-whisperjav"
        self.mingit = root / "mingit"
        self.logs = root / "logs"
        self.state = root / STATE_FILE

    @staticmethod
    def python(venv: Path) -> Path:
        return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def bundle_version(bundle: Path) -> str:
    text = (bundle / "src" / "video_turbo" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SetupError("bundle", f"无法读取版本号：{bundle}")
    return match.group(1)


def read_state(layout: Layout) -> dict:
    try:
        state = json.loads(layout.state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _download_mingit(layout: Layout) -> None:
    url = select_sources.github_prefix() + MINGIT_URL
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != MINGIT_SHA256:
        raise SetupError("download", f"MinGit 校验失败：应为 {MINGIT_SHA256}，实际 {digest}")
    shutil.rmtree(layout.mingit, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(layout.mingit)


def ensure_git(layout: Layout, env: dict[str, str]) -> None:
    """WhisperJAV 的部分依赖从 GitHub 用 git 拉取，必须有 git。"""
    if os.name == "nt":
        git_dir = layout.mingit / "cmd"
        if not (git_dir / "git.exe").is_file():
            _download_mingit(layout)
        env["PATH"] = str(git_dir) + os.pathsep + env["PATH"]
        return
    if sys.platform == "darwin":
        # /usr/bin/git 在没装命令行工具时只是个提示安装的占位程序，用 xcode-select 判断。
        probe = subprocess.run(["xcode-select", "-p"], capture_output=True, check=False)
        if probe.returncode != 0:
            raise SetupError("need_clt", "需要先安装 Apple 的命令行开发工具（含 Git）")
        return
    if not shutil.which("git", path=env["PATH"]):
        raise SetupError("need_git", "需要先安装 Git")


def _run(command: list[str], env: dict[str, str]) -> None:
    print("$ " + " ".join(command), flush=True)
    subprocess.run(command, env=env, check=True)


def install(args: argparse.Namespace) -> None:
    layout = Layout(args.root)
    bundle: Path = args.bundle
    version = bundle_version(bundle)
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.state.unlink(missing_ok=True)
    env = dict(os.environ)
    env["PATH"] = str(args.ffmpeg_dir) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    # 显卡运行库的 wheel 有数 GB，不留缓存。
    env["UV_NO_CACHE"] = "1"
    env["UV_LINK_MODE"] = "copy"
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_CONSTRAINT"):
        env.pop(key, None)
    total = len(STEPS)

    def step(index: int) -> None:
        key, zh, en = STEPS[index]
        emit("step", index=index + 1, total=total, key=key, zh=zh, en=en)

    step(0)
    os.environ.update(select_sources.select(["pypi", "torch", "github"]))
    env.update({key: value for key, value in os.environ.items() if key.startswith("VIDEO_TURBO_")})
    index_url = os.environ.get("VIDEO_TURBO_PYPI_INDEX_URL") or select_sources.SOURCES["pypi"].candidates[0][1]
    env["UV_DEFAULT_INDEX"] = env["PIP_INDEX_URL"] = index_url

    step(1)
    ensure_git(layout, env)

    step(2)
    python = Layout.python(layout.venv)
    if not python.is_file():
        _run([str(args.uv), "venv", "--python", str(args.python), "--seed", str(layout.venv)], env)

    step(3)
    requirements = bundle / "deploy" / "worker" / "requirements" / "asr.txt"
    _run([str(args.uv), "pip", "install", "--python", str(python), "--require-hashes", "-r", str(requirements)], env)

    step(4)
    # 版本号没变时也强制替换，避免继续用旧代码。
    _run([str(args.uv), "pip", "install", "--python", str(python), "--no-deps", "--reinstall", str(bundle)], env)

    step(5)
    _run([str(python), str(bundle / "tools" / "install_whisperjav.py"), "--venv", str(layout.backend),
          "--device", args.device], env)

    layout.state.write_text(
        json.dumps({"version": version, "device": args.device,
                    "installed_at": datetime.now(UTC).isoformat(timespec="seconds")}),
        encoding="utf-8",
    )
    emit("done", version=version)


def configure(args: argparse.Namespace) -> None:
    """写 config/worker.yaml：新建时从模板复制，已有时只改地址、密钥和模型目录。"""
    layout = Layout(args.root)
    if args.connection:
        decoded = configure_worker.decode_connection(args.connection)
        if decoded is None:
            raise SetupError("connection", "接入串无效")
        server_url, token = decoded
    else:
        server_url, token = args.server_url.rstrip("/"), args.token
    template = args.bundle / "config" / "worker.example.yaml"
    created = not layout.config.is_file()
    text = template.read_text(encoding="utf-8") if created else layout.config.read_text(encoding="utf-8")
    values = configure_worker.read_values(text)
    values.update(server_url=server_url, worker_token=token)
    if args.models:
        values["download_root"] = str(args.models)
    elif not values["download_root"]:
        values["download_root"] = "../models"
    text = configure_worker.write_values(text, values)
    if created and args.shared_machine:
        text = configure_worker.enable_idle_detection(text)
    layout.config.parent.mkdir(parents=True, exist_ok=True)
    temporary = layout.config.with_suffix(".yaml.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(layout.config)
    emit("configured", server_url=server_url)


def status(args: argparse.Namespace) -> None:
    layout = Layout(args.root)
    state = read_state(layout)
    values = configure_worker.read_values(layout.config.read_text(encoding="utf-8")) if layout.config.is_file() else {}
    installed = bool(state) and Layout.python(layout.venv).is_file() and Layout.python(layout.backend).is_file()
    print(json.dumps({
        "installed": installed,
        "version": state.get("version", ""),
        "current_version": bundle_version(args.bundle) if args.bundle else "",
        "configured": bool(values.get("server_url") and values.get("worker_token")),
        "server_url": values.get("server_url", ""),
    }, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="安装或更新识别运行环境")
    setup.add_argument("--root", type=Path, required=True)
    setup.add_argument("--bundle", type=Path, required=True)
    setup.add_argument("--uv", type=Path, required=True)
    setup.add_argument("--python", type=Path, required=True, help="uv 准备好的 Python 3.11")
    setup.add_argument("--ffmpeg-dir", type=Path, required=True)
    setup.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    config = commands.add_parser("configure", help="写入主程序地址和接入密钥")
    config.add_argument("--root", type=Path, required=True)
    config.add_argument("--bundle", type=Path, required=True)
    source = config.add_mutually_exclusive_group(required=True)
    source.add_argument("--connection", help="控制台“添加显卡电脑”给出的接入串")
    source.add_argument("--server-url")
    config.add_argument("--token", default="")
    config.add_argument("--models", type=Path, help="模型下载目录")
    config.add_argument("--shared-machine", action="store_true", help="新建配置时开启空闲检测")
    state = commands.add_parser("status", help="以 JSON 输出安装和配置状态")
    state.add_argument("--root", type=Path, required=True)
    state.add_argument("--bundle", type=Path)
    args = parser.parse_args(argv)
    if args.command == "configure" and args.server_url and not args.token:
        parser.error("--server-url 需要同时给出 --token")
    try:
        {"install": install, "configure": configure, "status": status}[args.command](args)
    except SetupError as exc:
        emit("error", code=exc.code, message=str(exc))
        return 2
    except subprocess.CalledProcessError as exc:
        emit("error", code="failed", message=f"命令失败（退出码 {exc.returncode}）：{exc.cmd[0]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
