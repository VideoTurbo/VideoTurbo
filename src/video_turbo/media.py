from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import languages
from .config import AudioConfig, SubtitleConfig
from .i18n import msg
from .languages import LanguagePair
from .media_sources.base import MediaInput, SourceEntry
from .models import SourceKind
from .naming import published_subtitle_path

TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
COMMENTARY_WORDS = {
    "commentary",
    "director",
    "description",
    "descriptive",
    "audio description",
    "解説",
    "コメンタリー",
    "副音声",
}


class MediaError(RuntimeError):
    pass


class MediaCancelled(MediaError):
    pass


@dataclass(slots=True)
class ProbeDecision:
    # process：需要生成字幕；done：已有目标字幕，无需处理；unusable：找不到可用的识别来源。
    action: Literal["process", "done", "unusable"]
    reason: str
    source_kind: SourceKind | None = None
    stream_index: int | None = None
    source_path: Path | None = None


def _input_args(source: Path | MediaInput, *, reconnect: bool = False) -> list[str]:
    """ffmpeg / ffprobe 的输入参数：本地文件或网盘地址（带请求头）。"""
    if isinstance(source, MediaInput):
        return source.ffmpeg_args(reconnect=reconnect)
    return ["-i", str(source)]


def _run_ffmpeg(
    command: list[str], timeout: int, cancelled: Callable[[], bool] | None
) -> None:
    """运行 ffmpeg；cancelled 返回 True 时结束进程并抛出 MediaCancelled。"""
    if cancelled is None:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
        return
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while True:
            try:
                _, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if cancelled():
                    raise MediaCancelled(msg("media.cancelled")) from None
                if time.monotonic() > deadline:
                    raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command, stderr=stderr)


def run_ffprobe(path: Path | MediaInput, timeout: int = 60) -> dict[str, Any]:
    """探测媒体信息。网盘地址只读取文件头，不会下载整部影片。"""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-print_format",
        "json",
        *(_input_args(path) if isinstance(path, MediaInput) else [str(path)]),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MediaError(msg("media.noFfprobe")) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(msg("media.ffprobeTimeout", name=path.name)) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or msg("media.unknownError")
        raise MediaError(msg("media.ffprobeFailed", detail=detail)) from exc
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(msg("media.unreadable", name=path.name)) from exc


def _positive_duration(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _stream_duration(stream: dict[str, Any]) -> float | None:
    direct = _positive_duration(stream.get("duration"))
    if direct is not None:
        return direct

    duration_ts = _positive_duration(stream.get("duration_ts"))
    time_base = str(stream.get("time_base") or "")
    if duration_ts is not None and "/" in time_base:
        numerator, denominator = time_base.split("/", 1)
        numerator_value = _positive_duration(numerator)
        denominator_value = _positive_duration(denominator)
        if numerator_value is not None and denominator_value is not None:
            return duration_ts * numerator_value / denominator_value

    tagged = str(stream.get("tags", {}).get("DURATION") or "").strip()
    match = re.fullmatch(r"(\d+):([0-5]?\d):([0-5]?\d(?:\.\d+)?)", tagged)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return None


def duration_from_probe(
    probe: dict[str, Any], *, stream_index: int | None = None
) -> float | None:
    """Return a duration that does not truncate the selected source stream.

    Some malformed containers report a format duration shorter than their selected
    audio stream. ASR timestamps follow the decoded audio, so using only the format
    duration incorrectly rejects valid cues near the real end of those files.
    """
    candidates: list[float] = []
    format_duration = _positive_duration(probe.get("format", {}).get("duration"))
    if format_duration is not None:
        candidates.append(format_duration)

    if stream_index is not None:
        selected = next(
            (
                stream
                for stream in probe.get("streams", [])
                if stream.get("index") == stream_index
            ),
            None,
        )
        if selected is not None:
            selected_duration = _stream_duration(selected)
            if selected_duration is not None:
                candidates.append(selected_duration)
    return max(candidates) if candidates else None


def _stream_language(stream: dict[str, Any]) -> str:
    return str(stream.get("tags", {}).get("language", "")).strip().lower()


def _stream_title(stream: dict[str, Any]) -> str:
    return str(stream.get("tags", {}).get("title", "")).strip().lower()


def _stream_metadata_text(stream: dict[str, Any]) -> str:
    tags = stream.get("tags", {})
    if not isinstance(tags, dict):
        return ""
    return " ".join(str(value).strip().lower() for value in tags.values() if value is not None)


def _has_target_marker(stream: dict[str, Any], pair: LanguagePair, config: SubtitleConfig) -> bool:
    found = languages.match_metadata(f"{_stream_language(stream)} {_stream_metadata_text(stream)}")
    return any(
        languages.satisfies_target(
            code, pair.target, accept_other_variant=config.accept_other_chinese_variant
        )
        for code in found
    )


def _is_unlabeled_subtitle(stream: dict[str, Any]) -> bool:
    language = _stream_language(stream)
    return language in {"", "und", "unknown"} and not _stream_title(stream)


def _is_commentary(stream: dict[str, Any]) -> bool:
    title = _stream_title(stream)
    return any(word in title for word in COMMENTARY_WORDS)


EXTERNAL_SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt"}


def list_subtitle_files(directory: Path) -> list[Path]:
    """列出目录中的外挂字幕文件；DirEntry 自带类型信息，无需逐个 stat。"""
    files: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if os.path.splitext(entry.name)[1].lower() not in EXTERNAL_SUBTITLE_SUFFIXES:
                continue
            try:
                is_file = entry.is_file()
            except OSError:
                continue
            if is_file:
                files.append(Path(entry.path))
    return files


def subtitle_files_in(entries: Iterable[SourceEntry]) -> list[Path]:
    """来源目录列表中的外挂字幕文件。"""
    return [
        Path(entry.path)
        for entry in entries
        if not entry.is_dir and os.path.splitext(entry.name)[1].lower() in EXTERNAL_SUBTITLE_SUFFIXES
    ]


class SubtitleFileCache:
    """扫描期间缓存当前目录的外挂字幕列表。

    来源会连续产出同一目录的文件，因此只保留最近一个目录即可消除同目录
    多部影片重复列目录的开销，又不会让缓存内容在整次扫描中长期过期。
    lister 默认读取本地目录。
    """

    def __init__(self, lister: Callable[[Path], list[Path]] | None = None) -> None:
        self._lister = lister
        self._directory: Path | None = None
        self._files: list[Path] = []

    def files(self, directory: Path) -> list[Path]:
        if directory != self._directory:
            self._files = (self._lister or list_subtitle_files)(directory)
            self._directory = directory
        return self._files


def _external_subtitles(video: Path, subtitle_files: Iterable[Path] | None = None) -> list[Path]:
    if subtitle_files is None:
        subtitle_files = list_subtitle_files(video.parent)
    candidates: list[Path] = []
    prefix = video.stem.lower()
    for path in subtitle_files:
        if path.stem.lower() == prefix or path.name.lower().startswith(prefix + "."):
            candidates.append(path)
    return sorted(candidates)


def _subtitle_name_language(video: Path, subtitle: Path) -> str | None:
    return languages.match_filename(subtitle.name[len(video.stem) :].lower())


def _matches_burned_in_path(video: Path, patterns: Iterable[str]) -> bool:
    # 只检查文件主名和直接父目录，避免媒体库上层目录中的偶然字符造成误判。
    candidates = (video.stem, video.parent.name)
    return any(re.search(pattern, candidate) for pattern in patterns for candidate in candidates)


def _burned_in_patterns(config: SubtitleConfig, pair: LanguagePair) -> list[str]:
    return [*config.burned_in_target_path_patterns, *pair.target_language.burned_in_path_patterns]


def _external_subtitle_languages(
    video: Path, subtitle_files: Iterable[Path] | None
) -> list[tuple[Path, str | None]]:
    external = _external_subtitles(video, subtitle_files)
    return [(path, _subtitle_name_language(video, path)) for path in external]


def _skip_decision_from_files(
    video: Path,
    config: SubtitleConfig,
    pair: LanguagePair,
    external_languages: list[tuple[Path, str | None]],
    subtitle_files: Iterable[Path] | None = None,
) -> ProbeDecision | None:
    output = output_subtitle_path(video, pair.target, config)
    # 已列出目录时直接查列表，网盘来源不必为每部影片再发一次请求。
    output_exists = output in subtitle_files if subtitle_files is not None else output.is_file()
    if output_exists and not config.overwrite_existing:
        return ProbeDecision("done", msg("media.targetExists", name=output.name))

    target = pair.target_language
    if _matches_burned_in_path(video, _burned_in_patterns(config, pair)):
        return ProbeDecision("done", msg("media.hardsub", label=target.label))

    if config.skip_existing_target:
        for path, language in external_languages:
            # 与影片同名、没有任何语言段的「片名.srt」按目标语言字幕看待。
            if language is None and path.stem.lower() == video.stem.lower():
                return ProbeDecision("done", msg("media.unlabeledExists", name=path.name))
            if languages.satisfies_target(
                language, pair.target, accept_other_variant=config.accept_other_chinese_variant
            ):
                return ProbeDecision("done", msg("media.exists", label=target.label))
    return None


def decide_without_probe(
    video: Path,
    config: SubtitleConfig,
    pair: LanguagePair,
    subtitle_files: Iterable[Path] | None = None,
) -> ProbeDecision | None:
    """只根据文件系统判断影片是否已有目标字幕，避免对这些影片运行 ffprobe。

    这些规则在 choose_processing_source 中本就排在所有依赖 probe 的规则之前，
    因此提前判断不会改变最终结论。
    """
    if subtitle_files is not None:
        subtitle_files = list(subtitle_files)
    return _skip_decision_from_files(
        video, config, pair, _external_subtitle_languages(video, subtitle_files), subtitle_files
    )


def select_source(
    probe: dict[str, Any],
    pair: LanguagePair,
    external_languages: list[tuple[Path, str | None]],
) -> ProbeDecision:
    """按源语言选择翻译来源：优先文本字幕，其次音轨；不判断是否已有目标字幕。"""
    source = pair.source_language
    streams = probe.get("streams", [])
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    for path, language in external_languages:
        if language == pair.source:
            return ProbeDecision(
                "process",
                msg("media.preferExternal", label=source.label),
                SourceKind.EXTERNAL_TEXT,
                source_path=path,
            )
    for stream in subtitle_streams:
        if (
            languages.match_tag(_stream_language(stream)) == pair.source
            and str(stream.get("codec_name", "")).lower() in TEXT_SUBTITLE_CODECS
        ):
            return ProbeDecision(
                "process",
                msg("media.preferEmbedded", label=source.label),
                SourceKind.EMBEDDED_TEXT,
                stream_index=int(stream["index"]),
            )

    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    preferred_audio = [
        stream
        for stream in audio_streams
        if languages.match_tag(_stream_language(stream)) == pair.source
        and not _is_commentary(stream)
    ]
    if preferred_audio:
        default_streams = [
            stream
            for stream in preferred_audio
            if stream.get("disposition", {}).get("default") == 1
        ]
        selected = (default_streams or preferred_audio)[0]
        return ProbeDecision(
            "process",
            msg("media.useAudio", label=source.label),
            SourceKind.AUDIO,
            stream_index=int(selected["index"]),
        )

    non_commentary = [stream for stream in audio_streams if not _is_commentary(stream)]
    if len(non_commentary) == 1:
        # 只有一条音轨时语言标签常被封装工具随意填写，仍按源语言处理，并在原因里写明标签。
        tag = _stream_language(non_commentary[0])
        label = msg("media.noTag") if tag in {"", "und", "unknown"} else msg("media.tag", tag=tag)
        return ProbeDecision(
            "process",
            msg("media.onlyTrack", label=label, source=source.label),
            SourceKind.AUDIO,
            stream_index=int(non_commentary[0]["index"]),
        )
    return ProbeDecision("unusable", msg("media.noTrack", label=source.label))


def select_source_for_video(
    video: Path,
    probe: dict[str, Any],
    pair: LanguagePair,
    subtitle_files: Iterable[Path] | None = None,
) -> ProbeDecision:
    return select_source(probe, pair, _external_subtitle_languages(video, subtitle_files))


def choose_processing_source(
    video: Path,
    probe: dict[str, Any],
    config: SubtitleConfig,
    pair: LanguagePair,
    subtitle_files: Iterable[Path] | None = None,
) -> ProbeDecision:
    if subtitle_files is not None:
        subtitle_files = list(subtitle_files)
    external_languages = _external_subtitle_languages(video, subtitle_files)
    skipped = _skip_decision_from_files(video, config, pair, external_languages, subtitle_files)
    if skipped is not None:
        return skipped

    subtitle_streams = [
        stream for stream in probe.get("streams", []) if stream.get("codec_type") == "subtitle"
    ]
    if config.skip_existing_target:
        for stream in subtitle_streams:
            if _has_target_marker(stream, pair, config):
                return ProbeDecision(
                    "done", msg("media.hasTrack", label=pair.target_language.label)
                )

    unlabeled_streams = [stream for stream in subtitle_streams if _is_unlabeled_subtitle(stream)]
    if unlabeled_streams and config.unlabeled_embedded_subtitle_policy == "skip":
        return ProbeDecision("unusable", msg("media.unlabeledEmbedded"))
    return select_source(probe, pair, external_languages)


def is_settled(mtime_ns: int, settle_minutes: int) -> bool:
    """文件最后修改后是否已过了“新文件稳定等待”的时间。"""
    if settle_minutes <= 0:
        return True
    return time.time() - mtime_ns / 1e9 >= settle_minutes * 60


def extract_audio(
    video: Path | MediaInput,
    stream_index: int,
    output: Path,
    config: AudioConfig,
    timeout: int = 3600,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """提取一条音轨为 Opus。video 可以是网盘地址：GPU 机器边下载边提取，网络中断时自动重连。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".processing")
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        *_input_args(video, reconnect=True),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
        # Fill timestamp gaps with silence so later speech stays at its original time.
        "-af",
        "aresample=async=1:first_pts=0",
        "-ac",
        str(config.channels),
        "-ar",
        str(config.sample_rate),
    ]
    command.extend(["-c:a", "libopus", "-b:a", config.opus_bitrate, "-f", "ogg"])
    command.extend(["-y", str(temporary)])
    try:
        _run_ffmpeg(command, timeout, cancelled)
        temporary.replace(output)
    except MediaCancelled:
        temporary.unlink(missing_ok=True)
        raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        temporary.unlink(missing_ok=True)
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr
            if stderr.strip():
                # 不把网盘地址（可能带签名或令牌）写进错误信息。
                last = re.sub(r"https?://\S+", msg("media.driveUrl"), stderr.strip().splitlines()[-1])
                detail = msg("common.colon") + last[:200]
        raise MediaError(msg("media.extractFailed", name=video.name, detail=detail)) from exc


def extract_text_subtitle(
    video: Path | MediaInput, stream_index: int, output: Path, timeout: int = 600
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".processing.srt")
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        *_input_args(video),
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "srt",
        "-y",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
        temporary.replace(output)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise MediaError(msg("media.textFailed", name=video.name)) from exc


def convert_text_subtitle(source: Path, output: Path, timeout: int = 600) -> None:
    """将外部 ASS/SSA/VTT 字幕统一转成 UTF-8 SRT。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".processing.srt")
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-c:s",
        "srt",
        "-y",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
        temporary.replace(output)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise MediaError(msg("media.convertFailed", name=source.name)) from exc


def output_subtitle_path(video: Path, target_language: str, config: SubtitleConfig) -> Path:
    return published_subtitle_path(video, target_language, config.format)
