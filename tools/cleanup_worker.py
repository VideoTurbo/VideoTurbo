"""清理 Worker 本机的中断残留：字幕任务临时目录、影片修复中断任务、模型下载残留和 pip/uv 缓存。

默认只列出将删除的内容和大小，加 ``--yes`` 才删除。检测到 Worker、WhisperJAV 或 Jasna
进程仍在运行时拒绝删除任务目录和模型残留（只清 pip/uv 缓存时不受影响）。
已下载完成的模型、配置、日志和 Jasna 许可证不会被删除。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from video_turbo.worker.config import WorkerConfig, load_worker_config

CATEGORIES = {
    "asr": "字幕任务临时目录",
    "restore": "影片修复中断任务目录",
    "models": "模型下载残留",
    "pip": "安装工具的下载缓存（其他 Python 程序也在用，删了它们重装时要重新下载）",
}
# huggingface_hub 把未下载完的文件存为 *.incomplete；最近仍在写入的可能属于共享模型盘上的其他机器，暂不删除。
PARTIAL_MIN_AGE_SECONDS = 30 * 60


@dataclass
class Target:
    category: str
    path: Path
    size: int
    # pip/uv 缓存通过各自的命令清理，而不是直接删除目录。
    command: list[str] | None = None


def tree_size(path: Path) -> int:
    if path.is_symlink() or not path.is_dir():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def load_config(path: Path) -> WorkerConfig:
    if path.is_file():
        return load_worker_config(path, require_token=False)
    print(f"未找到配置文件 {path}，按默认目录检查。")
    config = WorkerConfig(cache_dir=PROJECT / "cache")
    config.asr.download_root = PROJECT / "models"
    return config


def asr_targets(config: WorkerConfig) -> list[Target]:
    # 字幕任务目录：<缓存目录>/asr/<片名>-<短ID>。
    root = config.asr_dir
    found: list[Path] = []
    if root.is_dir():
        found.extend(
            entry for entry in sorted(root.iterdir()) if entry.is_dir() and not entry.is_symlink()
        )
    found.extend(sorted(Path(tempfile.gettempdir()).glob("whisperjav-test-*")))
    return [Target("asr", path, tree_size(path)) for path in found if path.is_dir()]


def restore_targets(config: WorkerConfig) -> list[Target]:
    root = config.restore_dir
    if not root.is_dir():
        return []
    return [
        Target("restore", entry, tree_size(entry))
        for entry in sorted(root.iterdir())
        if entry.is_dir() and not entry.is_symlink()
    ]


def model_targets(config: WorkerConfig, now: float | None = None) -> list[Target]:
    root = config.asr.download_root
    if not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - PARTIAL_MIN_AGE_SECONDS
    targets = []
    for path in sorted(root.rglob("*")):
        if not path.name.endswith(".incomplete") or not path.is_file() or path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            targets.append(Target("models", path, stat.st_size))
    return targets


def cache_env() -> dict[str, str]:
    env = dict(os.environ)
    # 安装脚本会禁用缓存；查询缓存目录时必须去掉，否则 pip cache 会报错。
    env.pop("PIP_NO_CACHE_DIR", None)
    env.pop("UV_NO_CACHE", None)
    return env


def query_dir(command: list[str]) -> Path | None:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False, env=cache_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = completed.stdout.strip().splitlines()
    if completed.returncode or not lines:
        return None
    path = Path(lines[-1].strip())
    return path if path.is_dir() else None


def pip_targets() -> list[Target]:
    targets = []
    pip_dir = query_dir([sys.executable, "-m", "pip", "cache", "dir"])
    if pip_dir is not None:
        targets.append(Target(
            "pip", pip_dir, tree_size(pip_dir), [sys.executable, "-m", "pip", "cache", "purge"],
        ))
    uv = shutil.which("uv")
    if uv:
        uv_dir = query_dir([uv, "cache", "dir"])
        if uv_dir is not None:
            targets.append(Target("pip", uv_dir, tree_size(uv_dir), [uv, "cache", "clean"]))
    return targets


def running_processes() -> list[str]:
    """本机仍在运行的 Worker、WhisperJAV 或 Jasna 进程。"""
    try:
        import psutil
    except ImportError:
        print("警告：未安装 psutil，无法检查程序是否在运行，请确认已停止所有服务。")
        return []
    found = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        if process.info["pid"] == os.getpid():
            continue
        argv = process.info.get("cmdline") or []
        line = " ".join(argv)
        jasna = bool(argv) and Path(argv[0]).name in {"jasna", "jasna.exe"} and "--input" in argv
        if "video_turbo.worker.main" in line or "whisperjav_launcher" in line or jasna:
            found.append(f"PID {process.info['pid']}：{line[:160]}")
    return found


def remove(target: Target) -> str | None:
    try:
        if target.command:
            completed = subprocess.run(
                target.command, capture_output=True, text=True, timeout=600, check=False,
                env=cache_env(),
            )
            if completed.returncode:
                return (completed.stderr or completed.stdout).strip() or "清理命令失败"
        elif target.path.is_dir() and not target.path.is_symlink():
            shutil.rmtree(target.path)
        else:
            target.path.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理本机上的中断残留和缓存")
    parser.add_argument("--config", type=Path, default=PROJECT / "config" / "worker.yaml")
    parser.add_argument("--yes", action="store_true", help="确认删除；不加时只列出将删除的内容")
    parser.add_argument(
        "--skip", action="append", choices=sorted(CATEGORIES), default=[],
        help="跳过某一类，可重复：asr 字幕任务、restore 影片修复任务、models 模型残留、pip 缓存",
    )
    args = parser.parse_args(argv)
    selected = [name for name in CATEGORIES if name not in args.skip]
    config = load_config(args.config)

    collectors = {
        "asr": lambda: asr_targets(config),
        "restore": lambda: restore_targets(config),
        "models": lambda: model_targets(config),
        "pip": pip_targets,
    }
    targets = [target for name in selected for target in collectors[name]()]
    for name in selected:
        items = [target for target in targets if target.category == name]
        print(f"\n[{CATEGORIES[name]}] {len(items)} 项，共 {human(sum(t.size for t in items))}")
        for target in items:
            note = f"（{' '.join(target.command[-2:])}）" if target.command else ""
            print(f"  {human(target.size):>9}  {target.path}{note}")
    total = sum(target.size for target in targets)
    print(f"\n合计 {human(total)}。")
    if "restore" in selected and any(t.category == "restore" for t in targets):
        print("注意：删除影片修复中断任务后，同一任务回到本机需要重新下载原片、重新修复。")
    if not targets:
        return 0
    running = running_processes() if any(t.category != "pip" for t in targets) else []
    if running:
        print("\n下面这些程序还在运行，请先停止所有服务再清理：", file=sys.stderr)
        for line in running:
            print(f"  {line}", file=sys.stderr)
    if not args.yes:
        print("当前只列出，未删除任何文件；确认后加 --yes 重新运行。")
        return 0
    if running:
        print("未删除任何文件。只清 pip/uv 缓存可加 --skip asr --skip restore --skip models。",
              file=sys.stderr)
        return 1

    failed = 0
    for target in targets:
        error = remove(target)
        if error:
            failed += 1
            print(f"删除失败：{target.path}：{error}", file=sys.stderr)
    print(f"\n清理完成，释放约 {human(total)}" + (f"，{failed} 项失败。" if failed else "。"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
