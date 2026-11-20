"""下载 GitHub 发布中的 Linux NVIDIA Jasna 分卷；只依赖标准库和 aria2/wget。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from select_sources import SOURCES, github_prefix

PROBE_BYTES = 1024 * 1024


def log(message: str) -> None:
    print(f"[Jasna 下载] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None


def release_assets(release: dict) -> list[Asset]:
    tag = release.get("tag_name", "")
    if not re.fullmatch(r"v?[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("发布版本号无效")
    pattern = re.compile(rf"jasna-linux-nvidia-{re.escape(tag.removeprefix('v'))}\.tar\.zst\.part(\d{{3}})")
    parts = {}
    for item in release.get("assets", []):
        match = pattern.fullmatch(item.get("name", ""))
        if not match:
            continue
        name, url, size = item["name"], item["browser_download_url"], item["size"]
        expected = f"https://github.com/Kruk2/jasna/releases/download/{quote(tag)}/{name}"
        if url != expected or type(size) is not int or size <= 0:
            raise ValueError(f"分卷地址或大小无效：{name}")
        digest = item.get("digest")
        if digest is not None and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise ValueError(f"分卷 SHA-256 无效：{name}")
        index = int(match[1])
        if index in parts:
            raise ValueError(f"分卷重复：{name}")
        parts[index] = Asset(name, url, size, digest.split(":")[1].lower() if digest else None)
    if not parts or sorted(parts) != list(range(len(parts))):
        raise ValueError(f"发布 {tag} 的 Linux NVIDIA 分卷缺失或编号不连续")
    return [parts[index] for index in sorted(parts)]


def valid_file(path: Path, asset: Asset) -> bool:
    if not path.is_file() or path.stat().st_size != asset.size:
        return False
    if asset.sha256 is None:
        # 兼容没有发布 digest 的旧版；调用处明确告知只能验证大小。
        return not path.with_name(path.name + ".aria2").exists()
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == asset.sha256


def probe(url: str, size: int, seconds: float = 4) -> float | None:
    """读取实际分卷开头最多 1 MiB，拒绝代理返回的 HTML 和错误 Range。"""
    amount = min(PROBE_BYTES, size)
    request = urllib.request.Request(url, headers={
        "User-Agent": "video-turbo-worker", "Range": f"bytes=0-{amount - 1}",
    })
    start = time.monotonic()
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:
            if response.status not in (200, 206):
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            if any(value in content_type for value in ("text/", "json", "xml")):
                return None
            if response.status == 206:
                if response.headers.get("Content-Range") != f"bytes 0-{amount - 1}/{size}":
                    return None
            elif response.headers.get("Content-Length") != str(size):
                return None
            while received < amount and time.monotonic() - start < seconds:
                chunk = response.read1(min(64 * 1024, amount - received))
                if not chunk:
                    break
                received += len(chunk)
    except OSError:
        return None
    return (time.monotonic() - start) * amount / received if received else None


def rank_sources(asset: Asset, preset: str = "") -> list[str]:
    # 显式配置的源优先使用，失败时仍可回退直连。不要使用通用 Git 接口的测速结果。
    if preset:
        prefix = github_prefix(preset.strip())
        if prefix and (urlsplit(prefix).scheme != "https" or not prefix.endswith("/")):
            raise ValueError("VIDEO_TURBO_GITHUB_PROXY 必须是以 / 结尾的 HTTPS 前缀，或 direct")
        return list(dict.fromkeys([prefix, ""]))
    candidates = [github_prefix(value) for _, value in SOURCES["github"].candidates]
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        timings = list(pool.map(lambda prefix: probe(prefix + asset.url, asset.size), candidates))
    order = sorted(range(len(candidates)), key=lambda i: (
        timings[i] is None, timings[i] if timings[i] is not None else float("inf"),
    ))
    for prefix, elapsed in zip(candidates, timings):
        label = prefix or "GitHub 直连"
        log(f"实际分卷测速 {label}：" + (f"{elapsed:.2f}s" if elapsed is not None else "不可用"))
    return [candidates[index] for index in order]


def wget_download(command: list[str], path: Path, min_speed: int, window: int) -> int:
    """wget 没有低速中止参数，按连续窗口内的文件增长检测并切源。"""
    process = subprocess.Popen(command, stdout=sys.stderr)
    start = time.monotonic()
    previous = path.stat().st_size if path.exists() else 0
    try:
        while process.poll() is None:
            time.sleep(0.25)
            elapsed = time.monotonic() - start
            if elapsed < window:
                continue
            size = path.stat().st_size if path.exists() else 0
            if min_speed and (size - previous) / elapsed < min_speed:
                log("下载一直很慢，换一个下载地址（已下载的部分会保留）")
                return 1
            start, previous = time.monotonic(), size
        return process.returncode
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def download_asset(asset: Asset, directory: Path, sources: list[str], *, connections: int,
                   min_speed: int, slow_seconds: int, aria2: str | None) -> Path:
    path = directory / asset.name
    control = path.with_name(path.name + ".aria2")
    if asset.sha256 is None:
        log(f"{asset.name}：发布未提供 SHA-256，仅验证文件大小")
    if valid_file(path, asset):
        control.unlink(missing_ok=True)
        log(f"已验证，跳过下载：{asset.name}")
        return path
    # 完整大小但校验失败的文件不能被 -c 当作已经下载完；损坏文件重新下载。
    if path.exists() and path.stat().st_size >= asset.size and not control.exists():
        log(f"大小或 SHA-256 不符，重新下载：{asset.name}")
        path.unlink()
    if not aria2 and control.exists():
        # aria2 的分段文件可能有空洞，wget 只能恢复顺序下载的前缀。
        log(f"wget 无法恢复 aria2 分段断点，重新下载：{asset.name}")
        path.unlink(missing_ok=True)
        control.unlink()
    for prefix in sources:
        url = prefix + asset.url
        log(f"下载 {asset.name}，下载地址：{prefix or 'GitHub 直连'}")
        if aria2:
            command = [
                aria2, "--no-conf=true", "--continue=true", "--auto-file-renaming=false",
                "--allow-overwrite=true", "--file-allocation=none", "--max-concurrent-downloads=1",
                f"--max-connection-per-server={connections}", f"--split={connections}",
                "--min-split-size=8M", "--connect-timeout=10", f"--timeout={slow_seconds}",
                f"--lowest-speed-limit={max(1, min_speed // connections) if min_speed else 0}",
                "--max-tries=2", "--retry-wait=2", "--auto-save-interval=5",
                "--summary-interval=5", "--console-log-level=warn", "--download-result=hide",
                f"--dir={directory}", f"--out={asset.name}",
            ]
            if asset.sha256:
                command.extend(["--check-integrity=true", f"--checksum=sha-256={asset.sha256}"])
            result = subprocess.run([*command, url], stdout=sys.stderr, check=False).returncode
        else:
            command = [
                "wget", "--continue", "--tries=2", "--waitretry=2", "--connect-timeout=10",
                "--read-timeout=15", "--progress=dot:giga", "--output-document", str(path), url,
            ]
            result = wget_download(command, path, min_speed, slow_seconds)
        # 完成后独立验证，不能仅信任下载工具的退出状态或代理的 HTTP 200。
        if valid_file(path, asset):
            control.unlink(missing_ok=True)
            log(f"检查通过：{asset.name}")
            return path
        log(f"下载没完成或检查没通过（代码 {result}），换下一个下载地址：{asset.name}")
        if result == 0 or (path.exists() and path.stat().st_size >= asset.size and not control.exists()):
            path.unlink(missing_ok=True)
            control.unlink(missing_ok=True)
    raise RuntimeError(f"所有下载地址都失败了：{asset.name}；已下载的部分会保留，重新安装就能接着下")


def env_int(name: str, default: int, maximum: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 必须是 {minimum}–{maximum} 的整数") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum}–{maximum} 的整数")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--proxy", default="", help="用户指定的 GitHub 前缀；空值则测速选源")
    args = parser.parse_args()
    try:
        connections = env_int("VIDEO_TURBO_JASNA_CONNECTIONS", 4, 16)
        parallel = env_int("VIDEO_TURBO_JASNA_PARALLEL", 2, 3)
        min_speed = env_int("VIDEO_TURBO_JASNA_MIN_SPEED", 128 * 1024, 1024**3, minimum=0)
        slow_seconds = env_int("VIDEO_TURBO_JASNA_SLOW_SECONDS", 30, 600, minimum=5)
        assets = release_assets(json.load(sys.stdin))
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if any(asset.sha256 is None for asset in assets):
            log("该发布存在未提供 SHA-256 的分卷，这些文件仅验证大小")
        pending = [asset for asset in assets if not valid_file(args.output_dir / asset.name, asset)]
        if len(pending) < len(assets):
            log(f"已验证并复用 {len(assets) - len(pending)} 个分卷，跳过下载")
        sources = rank_sources(pending[0], args.proxy) if pending else []
        aria2 = shutil.which("aria2c")
        if pending and not aria2 and not shutil.which("wget"):
            raise RuntimeError("需要 aria2c 或 wget，请先安装其中一个下载工具")
        log(f"使用 {'aria2，每卷 ' + str(connections) + ' 个连接' if aria2 else 'wget 单连接'}，"
            f"同时下载 {parallel} 个分卷；支持断点续传")
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            list(pool.map(lambda asset: download_asset(
                asset, args.output_dir, sources, connections=connections, min_speed=min_speed,
                slow_seconds=slow_seconds, aria2=aria2,
            ), pending))
        # stdout 仅供 worker.sh 获取通过校验的分卷清单；进度和日志走 stderr。
        for asset in assets:
            print(args.output_dir / asset.name)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        parser.exit(1, f"[Jasna 下载] {exc}\n")


if __name__ == "__main__":
    main()
