"""Run one preflight check inside the isolated WhisperJAV interpreter.

Keep project imports out of this module: it is executed as a standalone script
by the backend interpreter, whose dependencies differ from the main Worker.
Each invocation checks one component so models never share a process or VRAM.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import importlib.metadata
import logging
import os
import platform
import shutil
from pathlib import Path


def check_ten_vad_runtime() -> None:
    if platform.system() != "Linux":
        return
    try:
        ctypes.CDLL("libc++.so.1")
    except OSError as exc:
        raise RuntimeError(
            "TEN VAD 无法加载 Linux C++ 运行库 libc++.so.1。"
            "Ubuntu/Debian 请执行 apt-get update && apt-get install -y libc++1，"
            f"然后重试。原始错误：{exc}"
        ) from exc


def load_hub_fixes():
    """Load the sibling whisperjav_hub.py by path (this script runs standalone)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisperjav_hub.py")
    spec = importlib.util.spec_from_file_location("video_turbo_whisperjav_hub", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def force_download_progress() -> None:
    """Show Hugging Face download bars although stderr is redirected to a log.

    huggingface_hub passes ``disable=None`` to tqdm, which hides bars whenever
    stderr is not a TTY; the Worker mirrors this log to the terminal during
    self-tests. HF_HUB_DISABLE_PROGRESS_BARS is still honoured by its tqdm class.
    """
    try:
        # `huggingface_hub.utils.tqdm` the attribute is the class; import the module.
        hf_tqdm = importlib.import_module("huggingface_hub.utils.tqdm")
    except ImportError:
        return
    if callable(getattr(hf_tqdm, "is_tqdm_disabled", None)):
        hf_tqdm.is_tqdm_disabled = lambda log_level: log_level == logging.NOTSET


def check_runtime(expected_version: str) -> None:
    actual = importlib.metadata.version("whisperjav")
    if actual != expected_version:
        raise RuntimeError(f"需要 WhisperJAV {expected_version}，实际为 {actual}，请重新运行安装脚本。")
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise RuntimeError("缺少运行依赖：" + ", ".join(missing))
    # Check before downloading/loading either ASR model or constructing TenVad,
    # whose destructor also fails if its native library could not be loaded.
    check_ten_vad_runtime()


def check_qwen(model_id: str, device: str) -> None:
    from whisperjav.modules.qwen_asr import QwenASR

    model = QwenASR(
        model_id=model_id,
        device=device,
        dtype="float16" if device == "cuda" else "float32",
        use_aligner=False,
        timestamps="none",
        attn_implementation="sdpa",
    )
    try:
        model.load_model_text_only()
    finally:
        model.unload_model()


def check_anime(model_id: str, device: str) -> None:
    from whisperjav.modules.subtitle_pipeline.generators.anime_whisper import AnimeWhisperGenerator

    model = AnimeWhisperGenerator(
        model_id=model_id,
        device=device,
        dtype="float16" if device == "cuda" else "float32",
    )
    try:
        model.load()
    finally:
        model.unload()


def check_vad(backend: str, use_gpu: bool = False) -> None:
    from whisperjav.modules.speech_segmentation import SpeechSegmenterFactory

    # Same switch the CLI launcher applies, so CUDA model loading is verified.
    segmenter = SpeechSegmenterFactory.create(backend, **({"use_gpu": True} if use_gpu else {}))
    try:
        # All three supported segmenters in the pinned 1.9.5 expose this lazy
        # loader. It downloads missing assets and checks native model loading.
        segmenter._ensure_model()
    finally:
        segmenter.cleanup()


def check_merge(directory: Path) -> None:
    from whisperjav.ensemble.merge import MergeEngine

    primary = directory / "self-test.pass1.srt"
    secondary = directory / "self-test.pass2.srt"
    merged = directory / "self-test.merged.srt"
    primary.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    secondary.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n別の結果\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\nありがとう\n",
        encoding="utf-8",
    )
    stats = MergeEngine().merge(primary, secondary, merged, strategy="pass1_primary")
    content = merged.read_text(encoding="utf-8")
    if (
        stats.get("merged_count") != 2
        or "こんにちは" not in content
        or "ありがとう" not in content
        or "別の結果" in content
    ):
        raise RuntimeError("pass1_primary 合并自检失败：主遍对白或第二遍补漏结果不符合预期。")


def main() -> None:
    parser = argparse.ArgumentParser(description="WhisperJAV 隔离环境组件自检")
    parser.add_argument("--check", choices=["qwen", "anime", "vad", "merge"], required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cpu")
    parser.add_argument("--model")
    parser.add_argument("--segmenter", choices=["firered-vad", "ten", "whisperseg"])
    parser.add_argument("--use-gpu", action="store_true", help="FireRedVAD 在 CUDA 上加载")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    check_runtime(args.expected_version)
    if args.check != "merge":
        # Match the normal CLI's download fallback and cache handling.
        load_hub_fixes().patch_hub_downloads()
        from whisperjav.utils.model_loader import patch_hf_hub_downloads

        patch_hf_hub_downloads()
        load_hub_fixes().ignore_missing_hub_files()
        force_download_progress()
    if args.check in {"qwen", "anime"}:
        if not args.model:
            parser.error("识别模型自检需要 --model。")
        if args.check == "qwen":
            check_qwen(args.model, args.device)
        else:
            check_anime(args.model, args.device)
    elif args.check == "vad":
        if not args.segmenter:
            parser.error("VAD 自检需要 --segmenter。")
        check_vad(args.segmenter, args.use_gpu)
    else:
        check_merge(Path.cwd())
    print(f"WhisperJAV {args.check} self-test OK", flush=True)


if __name__ == "__main__":
    main()
