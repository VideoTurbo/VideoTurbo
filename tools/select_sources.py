"""为 Worker 安装和运行挑选下载源：每类资源有官方源和镜像，用前花几秒测速后自动选择。

只依赖标准库，由 worker.sh、worker.ps1 和 install_whisperjav.py 调用。
普通下载源只读取前 256 KB；模型源用实际模型文件的 HEAD 响应验证元数据。
所有候选并行，总耗时不超过 PROBE_SECONDS 多一点。
设置对应的环境变量可固定地址并跳过测速（VIDEO_TURBO_GITHUB_PROXY=direct 表示直连 GitHub）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

PROBE_SECONDS = 4.0
PROBE_BYTES = 256 * 1024
GITHUB_DIRECT = "direct"


@dataclass(frozen=True)
class Source:
    label: str
    env: str
    # (名称, 地址)。全部不通时使用第一个。
    candidates: tuple[tuple[str, str], ...]
    # 探测地址模板：用真实要下载的文件确认“能连通、文件存在、速度如何”。
    probe: str
    # 该地址不比最快的慢一倍以上时优先用它（GitHub 能直连就不走第三方代理）。
    prefer: str = ""


SOURCES: dict[str, Source] = {
    "pypi": Source(
        "PyPI",
        "VIDEO_TURBO_PYPI_INDEX_URL",
        (
            ("清华", "https://pypi.tuna.tsinghua.edu.cn/simple"),
            ("官方", "https://pypi.org/simple"),
        ),
        "{url}/pip/",
    ),
    "torch": Source(
        "PyTorch",
        "VIDEO_TURBO_TORCH_INDEX_URL",
        (
            ("上海交大", "https://mirror.sjtu.edu.cn/pytorch-wheels"),
            ("南京大学", "https://mirror.nju.edu.cn/pytorch/whl"),
            ("官方", "https://download.pytorch.org/whl"),
        ),
        "{url}/cu128/torch/",
    ),
    "hf": Source(
        "Hugging Face",
        "VIDEO_TURBO_HF_ENDPOINT",
        (
            ("hf-mirror", "https://hf-mirror.com"),
            ("官方", "https://huggingface.co"),
        ),
        "{url}/Qwen/Qwen3-ASR-1.7B/resolve/main/config.json",
    ),
    "github": Source(
        "GitHub",
        "VIDEO_TURBO_GITHUB_PROXY",
        (
            ("直连", GITHUB_DIRECT),
            ("ghfast.top", "https://ghfast.top/"),
            ("gh-proxy.com", "https://gh-proxy.com/"),
        ),
        "{url}https://github.com/meizhong986/WhisperJAV.git/info/refs?service=git-upload-pack",
        prefer=GITHUB_DIRECT,
    ),
}


def github_prefix(value: str | None = None) -> str:
    """把 VIDEO_TURBO_GITHUB_PROXY 的值换成拼在 https://github.com/... 前面的前缀。"""
    value = os.environ.get("VIDEO_TURBO_GITHUB_PROXY", "") if value is None else value
    return "" if value in ("", GITHUB_DIRECT) else value


def probe(url: str, seconds: float = PROBE_SECONDS) -> float | None:
    """返回下载探测文件前 256 KB 的耗时（秒）；连不上、文件不存在或没有数据返回 None。"""
    start = time.monotonic()
    request = urllib.request.Request(
        url, headers={"User-Agent": "video-turbo-worker", "Range": f"bytes=0-{PROBE_BYTES - 1}"}
    )
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:
            if response.status not in (200, 206):
                return None
            while received < PROBE_BYTES:
                if time.monotonic() - start > seconds:
                    break
                chunk = response.read(min(32 * 1024, PROBE_BYTES - received))
                if not chunk:
                    return time.monotonic() - start if received else None
                received += len(chunk)
    except Exception:  # noqa: BLE001 - 任何网络错误都只表示该候选不可用
        return None
    elapsed = time.monotonic() - start
    if not received:
        return None
    # 限时内没下完：按已下载的比例折算成下完 256 KB 的时间，仍可与其他候选比较。
    return elapsed if received >= PROBE_BYTES else elapsed * PROBE_BYTES / received


def hf_files(config: Path | None = None) -> list[str]:
    """Files loaded by the dual-pass self-test; honour a configured/local Qwen model.

    Fresh installers may not have PyYAML yet, so probing the default stack does
    not depend on it. Runtime selection uses the installed Worker's YAML parser.
    """
    options = {}
    if config and config.is_file():
        try:
            import yaml
        except ImportError:
            print("[下载源] PyYAML 尚未安装，先验证默认识别模型。", file=sys.stderr)
        else:
            options = (yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("whisperjav", {})
    model = options.get("model", "Qwen/Qwen3-ASR-1.7B")
    local = Path(model).expanduser()
    if config and not local.is_absolute():
        local = config.parent / local
    files = [] if local.is_dir() else [f"{model}/resolve/main/config.json"]
    files.append("litagin/anime-whisper/resolve/main/config.json")
    if options.get("speech_segmenter", "firered-vad") == "firered-vad":
        files.extend(f"FireRedTeam/FireRedVAD/resolve/main/VAD/{name}" for name in ("model.pth.tar", "cmvn.ark"))
    return files


class _MetadataRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Like huggingface_hub: resolve same-host redirects, keep CDN metadata
        # from the original response instead of following a signed download URL.
        if urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.method = req.get_method()
        return redirected


def _hf_metadata_valid(endpoint: str, filename: str, seconds: float) -> bool:
    opener = urllib.request.build_opener(_MetadataRedirects())
    try:
        request = urllib.request.Request(
            f"{endpoint.rstrip('/')}/{quote(filename, safe='/')}",
            method="HEAD", headers={"User-Agent": "video-turbo-worker"},
        )
        try:
            response = opener.open(request, timeout=seconds)
        except urllib.error.HTTPError as exc:
            if not 300 <= exc.code < 400:
                raise
            response = exc
        with response:
            headers = response.headers
            size = headers.get("X-Linked-Size") or headers.get("Content-Length", "")
            return (
                response.code in (200, 301, 302, 303, 307, 308)
                and bool(re.fullmatch(r"[0-9a-fA-F]{40}", headers.get("X-Repo-Commit", "")))
                and bool(headers.get("X-Linked-ETag") or headers.get("ETag"))
                and size.isdigit() and int(size) > 0
                and "text/html" not in headers.get("Content-Type", "").lower()
            )
    except Exception:  # noqa: BLE001 - unusable metadata/network rejects this candidate
        return False


def probe_hf(endpoint: str, files: list[str], seconds: float = PROBE_SECONDS) -> float | None:
    # Check files concurrently: multiple sequential HEADs would otherwise use
    # up the entire probe budget even on a healthy endpoint.
    start = time.monotonic()
    valid = [False] * len(files)
    threads = []
    for index, filename in enumerate(files):
        def run(index=index, filename=filename):
            valid[index] = _hf_metadata_valid(endpoint, filename, seconds)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(max(0.0, seconds - (time.monotonic() - start)))
    elapsed = time.monotonic() - start
    return elapsed if all(valid) and elapsed <= seconds else None


def select(names: list[str] | None = None, *, seconds: float = PROBE_SECONDS, log=None,
           files: list[str] | None = None) -> dict[str, str]:
    """返回 {环境变量名: 选中的地址}。已设置环境变量的资源直接沿用，不测速。"""
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    chosen: dict[str, str] = {}
    results: dict[tuple[str, str], float | None] = {}
    threads: list[threading.Thread] = []
    pending: list[str] = []
    for name in names or list(SOURCES):
        source = SOURCES[name]
        preset = os.environ.get(source.env, "").strip()
        if name == "hf" and not preset:
            preset = os.environ.get("HF_ENDPOINT", "").strip()
        if preset:
            chosen[source.env] = preset
            continue
        pending.append(name)
        for _, url in source.candidates:
            target = source.probe.format(url="" if url == GITHUB_DIRECT else url)

            def run(key=(name, url), target=target) -> None:
                results[key] = (probe_hf(key[1], files if files is not None else hf_files(), seconds)
                                if key[0] == "hf" else probe(target, seconds))

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            threads.append(thread)
    deadline = time.monotonic() + seconds + 1
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    for name in pending:
        source = SOURCES[name]
        timings = [(results.get((name, url)), label, url) for label, url in source.candidates]
        reachable = sorted((item for item in timings if item[0] is not None), key=lambda item: item[0])
        detail = "，".join(
            f"{label} {elapsed:.1f}s" if elapsed is not None else f"{label} 不可用" for elapsed, label, _ in timings
        )
        if reachable:
            best = reachable[0][0]
            _, label, url = next(
                (item for item in reachable if item[2] == source.prefer and item[0] <= best * 2), reachable[0]
            )
            log(f"[下载源] {source.label}：{detail} → 选用{label}")
        else:
            label, url = source.candidates[0]
            log(f"[下载源] {source.label}：{detail} → 全部不通，仍使用{label}；可用 {source.env} 指定地址")
        chosen[source.env] = url
    return chosen


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", choices=list(SOURCES), help="要选择的资源，默认全部")
    parser.add_argument("--format", choices=("sh", "json"), default="sh")
    parser.add_argument("--seconds", type=float, default=PROBE_SECONDS, help="每个候选的测速时限")
    parser.add_argument("--config", type=Path, help="本机的服务配置文件，用实际配置的模型文件验证 Hugging Face 源")
    args = parser.parse_args(argv)
    chosen = select(args.names or None, seconds=args.seconds, files=hf_files(args.config))
    if args.format == "json":
        print(json.dumps(chosen))
        return
    for key, value in chosen.items():
        print("export {}='{}'".format(key, value.replace("'", "'\\''")))


if __name__ == "__main__":
    main()
