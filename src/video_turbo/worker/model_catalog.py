"""识别模型清单：桌面版的本机识别窗口和 Worker 应用用它列出、下载、删除模型。

单遍下载 Qwen3-ASR 和所选 VAD，双遍还需要 anime-whisper。
模型缓存在 <模型目录>/huggingface。Qwen 的模型选择写入 worker.yaml 的 whisperjav.model。

命令行（外壳在识别程序的环境里运行）::

    python -m video_turbo.worker.model_catalog --config <worker.yaml> status
    python -m video_turbo.worker.model_catalog --config <worker.yaml> download qwen3-asr anime-whisper
    python -m video_turbo.worker.model_catalog --config <worker.yaml> select qwen3-asr Qwen/Qwen3-ASR-0.6B
    python -m video_turbo.worker.model_catalog --config <worker.yaml> delete anime-whisper

``download`` 的 ``模型@版本`` 先改用这个版本再下载。

``status`` 输出一行 JSON；``download`` 以 ``@@VT `` 开头的 JSON 行输出进度。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import WorkerConfig, load_worker_config

logger = logging.getLogger(__name__)
PROGRESS = "@@VT "
QWEN = "qwen3-asr"
ANIME = "anime-whisper"
ANIME_REPO = "litagin/anime-whisper"
FIRERED_VAD_REPO = "FireRedTeam/FireRedVAD"
# 默认模型的大小（MB），连不上 Hugging Face 时用来显示；能连上时以实际文件大小为准。
KNOWN_SIZES_MB = {
    "Qwen/Qwen3-ASR-1.7B": 4485,
    "Qwen/Qwen3-ASR-0.6B": 1793,
    ANIME_REPO: 2887,
    FIRERED_VAD_REPO: 7,
}
# 主程序的识别模式缓存在配置旁边：连不上主程序时用上次的结果。
MODE_FILE = "asr-mode.json"
SERVER_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Repo:
    repo_id: str
    cache: Path

    @property
    def folder(self) -> Path:
        return self.cache / ("models--" + self.repo_id.replace("/", "--"))


@dataclass(frozen=True)
class Variant:
    """模型的一个版本：value 是写进 worker.yaml 的值。"""

    value: str
    label: str


# 第一个是默认版本。
VARIANTS = {
    QWEN: (Variant("Qwen/Qwen3-ASR-1.7B", "1.7B"), Variant("Qwen/Qwen3-ASR-0.6B", "0.6B")),
}
# 版本写在 worker.yaml 的哪一节、哪个键。
SETTINGS = {QWEN: ("whisperjav", "model")}


@dataclass(frozen=True)
class Model:
    id: str
    zh: str
    en: str
    purpose_zh: str
    purpose_en: str
    repos: tuple[Repo, ...]
    modes: tuple[str, ...] = field(default=())
    # 当前用的版本（worker.yaml 里的值）和可选的版本；没有版本可选时为空。
    variant: str | None = None
    variants: tuple[Variant, ...] = ()

    def size_mb(self) -> int | None:
        return _size_mb(self.repos)


def repos(config: WorkerConfig, model_id: str, value: str | None = None) -> tuple[Repo, ...]:
    """模型（某个版本，默认当前版本）要下载的仓库。"""
    hub = config.asr.download_root / "huggingface"
    if model_id == QWEN:
        found = [Repo(value or config.whisperjav.model, hub)]
        if config.whisperjav.speech_segmenter == "firered-vad":
            found.append(Repo(FIRERED_VAD_REPO, hub))
        return tuple(found)
    if model_id == ANIME:
        return (Repo(ANIME_REPO, hub),)
    return ()


def _variants(model_id: str, current: str) -> tuple[Variant, ...]:
    known = VARIANTS.get(model_id, ())
    # worker.yaml 里手动写的其他模型也列出来。
    return known if any(item.value == current for item in known) else (*known, Variant(current, current))


def catalog(config: WorkerConfig) -> list[Model]:
    qwen = config.whisperjav.model
    return [
        Model(QWEN, "Qwen3-ASR", "Qwen3-ASR", "WhisperJAV 模式（第一遍）", "WhisperJAV mode (pass 1)",
              repos(config, QWEN), ("whisperjav",), qwen, _variants(QWEN, qwen)),
        Model(ANIME, "anime-whisper", "anime-whisper", "WhisperJAV 双遍（第二遍）", "WhisperJAV two-pass (pass 2)",
              repos(config, ANIME), ("whisperjav_ensemble",)),
    ]


def _size_mb(found: Iterable[Repo]) -> int | None:
    sizes = [KNOWN_SIZES_MB.get(repo.repo_id) for repo in found]
    return None if any(size is None for size in sizes) else sum(size or 0 for size in sizes)


def repo_downloaded(repo: Repo) -> bool:
    """缓存里是否有完整下载的主分支版本（本地文件夹路径也算）。"""
    if Path(repo.repo_id).expanduser().is_dir():
        return True
    try:
        commit = (repo.folder / "refs" / "main").read_text().strip()
    except OSError:
        return False
    files = [path for path in (repo.folder / "snapshots" / commit).rglob("*") if not path.is_dir()]
    # exists() 跟随快照里的链接：指向缺失文件的链接说明没下载完。
    return bool(files) and all(path.exists() for path in files) and not any(repo.folder.glob("blobs/*.incomplete"))


def downloaded(model: Model) -> bool:
    return all(repo_downloaded(repo) for repo in model.repos)


def needed(mode: str | None, ensemble: bool) -> list[str]:
    """识别模式需要的模型。"""
    if mode == "whisperjav":
        return [QWEN, ANIME] if ensemble else [QWEN]
    return []


def supported_modes(config: WorkerConfig) -> tuple[set[str], bool]:
    """已下载的模型支持哪些识别模式，以及 WhisperJAV 能否双遍。"""
    ready = {model.id for model in catalog(config) if downloaded(model)}
    return ({"whisperjav"} if QWEN in ready else set()), QWEN in ready and ANIME in ready


def fetch_mode(config: WorkerConfig, config_file: Path) -> dict | None:
    """从主程序读取当前识别模式；连不上时用上次读到的。"""
    cached = config_file.parent / MODE_FILE
    if config.server_url and config.worker_token:
        try:
            import httpx

            from ..protocol import PROTOCOL_HEADER, WORKER_PROTOCOL_VERSION

            response = httpx.get(
                f"{config.server_url.rstrip('/')}/api/worker/asr-settings",
                headers={"X-Worker-Token": config.worker_token, PROTOCOL_HEADER: str(WORKER_PROTOCOL_VERSION)},
                timeout=SERVER_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            mode = response.json()
            result = {"mode": str(mode["mode"]), "whisperjav_ensemble": bool(mode.get("whisperjav_ensemble"))}
            try:
                cached.write_text(json.dumps(result), encoding="utf-8")
            except OSError:
                pass
            return {**result, "live": True}
        except Exception as exc:  # noqa: BLE001 - 主程序暂时连不上时退回缓存
            logger.debug("读取主程序的识别模式失败：%s", exc)
    try:
        result = json.loads(cached.read_text(encoding="utf-8"))
        if result["mode"] == "faster_whisper":
            return {"mode": "whisperjav", "whisperjav_ensemble": False, "live": False}
        return {"mode": str(result["mode"]), "whisperjav_ensemble": bool(result.get("whisperjav_ensemble")), "live": False}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def status(config: WorkerConfig, config_file: Path) -> dict:
    mode = fetch_mode(config, config_file)
    wanted = needed(mode["mode"], mode["whisperjav_ensemble"]) if mode else []
    models = []
    for model in catalog(config):
        models.append({
            "id": model.id,
            "zh": model.zh,
            "en": model.en,
            "purpose_zh": model.purpose_zh,
            "purpose_en": model.purpose_en,
            "size_mb": model.size_mb(),
            "downloaded": downloaded(model),
            "needed": model.id in wanted,
            "variant": model.variant,
            "variants": [
                {
                    "value": item.value,
                    "label": item.label,
                    "size_mb": _size_mb(repos(config, model.id, item.value)),
                    "downloaded": all(repo_downloaded(repo) for repo in repos(config, model.id, item.value)),
                }
                for item in model.variants
            ],
        })
    return {
        "mode": mode,
        "models": models,
        # 当前识别模式需要、还没下载的模型。
        "missing": [item["id"] for item in models if item["needed"] and not item["downloaded"]],
    }


def emit(event: str, **fields: object) -> None:
    print(PROGRESS + json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _remote_size(repo: Repo) -> int | None:
    """仓库里要下载的文件总大小（字节）；连不上时返回 None。"""
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo.repo_id, files_metadata=True, timeout=SERVER_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 只用于显示进度
        return None
    total = 0
    for sibling in info.siblings or []:
        total += sibling.size or 0
    return total or None


def _folder_bytes(folder: Path) -> int:
    total = 0
    for path in folder.glob("blobs/*"):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _fetch(repo: Repo) -> None:
    from .whisperjav_hub import patch_hub_downloads

    patch_hub_downloads()
    import huggingface_hub

    repo.cache.mkdir(parents=True, exist_ok=True)
    huggingface_hub.snapshot_download(repo.repo_id, cache_dir=str(repo.cache))


def download(
    config: WorkerConfig,
    ids: Iterable[str],
    fetch: Callable[[Repo], None] = _fetch,
    interval: float = 1.0,
) -> None:
    """依次下载模型；每秒按缓存文件夹的大小报告进度（不依赖下载库的进度条）。"""
    models = {model.id: model for model in catalog(config)}
    selected = [models[item] for item in dict.fromkeys(ids) if item in models]
    for index, model in enumerate(selected, 1):
        repos = [repo for repo in model.repos if not repo_downloaded(repo)]
        sizes = [_remote_size(repo) or KNOWN_SIZES_MB.get(repo.repo_id, 0) * 1024 * 1024 for repo in repos]
        total = sum(sizes)
        start = sum(_folder_bytes(repo.folder) for repo in repos)
        emit("model", id=model.id, zh=model.zh, en=model.en, index=index, count=len(selected),
             done_mb=0, total_mb=round(total / 1048576))
        finished = threading.Event()

        def report(model: Model = model, repos: list[Repo] = repos, index: int = index, total: int = total,
                   start: int = start, finished: threading.Event = finished) -> None:
            while not finished.wait(interval):
                done = max(0, sum(_folder_bytes(repo.folder) for repo in repos) - start)
                emit("model", id=model.id, zh=model.zh, en=model.en, index=index, count=len(selected),
                     done_mb=round(min(done, total or done) / 1048576), total_mb=round(total / 1048576))

        reporter = threading.Thread(target=report, daemon=True)
        reporter.start()
        try:
            for repo in repos:
                fetch(repo)
        finally:
            finished.set()
            reporter.join()
        emit("model_done", id=model.id)
    emit("done")


def select(config_file: Path, model_id: str, value: str) -> None:
    """改用模型的另一个版本：写进 worker.yaml（保留注释和其他设置）。"""
    if model_id not in SETTINGS:
        raise SystemExit(f"这个模型没有版本可选：{model_id}")
    section, key = SETTINGS[model_id]
    text = config_file.read_text(encoding="utf-8")
    line = f"  {key}: {json.dumps(value, ensure_ascii=False)}"
    header = re.search(rf"^{section}:[ \t]*$", text, re.MULTILINE)
    if header is None:
        text = text.rstrip("\n") + f"\n{section}:\n{line}\n"
    else:
        # 这一节到下一个顶层键为止。
        end = re.search(r"^\S", text[header.end() + 1:], re.MULTILINE)
        stop = header.end() + 1 + end.start() if end else len(text)
        body = text[header.end():stop]
        pattern = re.compile(rf"^  {key}:.*$", re.MULTILINE)
        body = pattern.sub(lambda _: line, body, count=1) if pattern.search(body) else f"\n{line}{body}"
        text = text[:header.end()] + body + text[stop:]
    temporary = config_file.with_suffix(".yaml.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(config_file)
    emit("selected", id=model_id, value=value)


def delete(config: WorkerConfig, model_id: str) -> None:
    for model in catalog(config):
        if model.id != model_id:
            continue
        for repo in model.repos:
            # 本地文件夹形式的模型是用户自己放的，不删。
            if not Path(repo.repo_id).expanduser().is_dir():
                shutil.rmtree(repo.folder, ignore_errors=True)
        emit("deleted", id=model_id)
        return
    raise SystemExit(f"没有这个模型：{model_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="识别模型清单")
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="以 JSON 输出模型清单和当前识别模式")
    fetch = commands.add_parser("download", help="下载模型；模型@版本 先改用这个版本")
    fetch.add_argument("ids", nargs="+")
    choose = commands.add_parser("select", help="改用模型的另一个版本")
    choose.add_argument("id")
    choose.add_argument("value")
    remove = commands.add_parser("delete", help="删除模型")
    remove.add_argument("id")
    args = parser.parse_args(argv)
    config = load_worker_config(args.config, require_token=False)
    if args.command == "status":
        print(json.dumps(status(config, args.config), ensure_ascii=False), flush=True)
    elif args.command == "select":
        select(args.config, args.id, args.value)
    elif args.command == "download":
        try:
            ids = []
            for item in args.ids:
                model_id, _, value = item.partition("@")
                if value:
                    select(args.config, model_id, value)
                ids.append(model_id)
            config = load_worker_config(args.config, require_token=False)
            download(config, ids)
        except Exception as exc:  # noqa: BLE001 - 原因交给外壳显示
            emit("error", message=f"{type(exc).__name__}: {exc}")
            return 1
    else:
        delete(config, args.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
