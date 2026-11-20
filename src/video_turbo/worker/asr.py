from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..i18n import msg
from ..models import Cue
from .config import WorkerConfig
from .gpu import query_gpu, select_device

_SENTENCE_ENDINGS = frozenset("。！？!?…")
logger = logging.getLogger(__name__)


class TranscriptionCancelled(RuntimeError):
    """主控请求取消当前识别任务。"""


def _download_model_with_progress(model: str, download_root: Path) -> str:
    """下载远程模型并显示 Hugging Face 的文件大小、速度和 ETA。"""
    local_path = Path(model).expanduser()
    if local_path.is_dir():
        return str(local_path)

    from .whisperjav_hub import patch_hub_downloads

    patch_hub_downloads()
    from faster_whisper import utils as faster_whisper_utils
    from tqdm.auto import tqdm

    download_root.mkdir(parents=True, exist_ok=True)
    logger.info("检查/下载 Whisper 模型 %s，缓存目录：%s", model, download_root)

    # faster-whisper 默认把 snapshot_download 的 tqdm_class 替换成 disabled_tqdm。
    # 临时换回标准 tqdm；下载结束后恢复，避免影响依赖包的其他调用。
    disabled_tqdm = faster_whisper_utils.disabled_tqdm
    faster_whisper_utils.disabled_tqdm = tqdm
    try:
        model_path = faster_whisper_utils.download_model(
            model,
            cache_dir=str(download_root),
        )
    finally:
        faster_whisper_utils.disabled_tqdm = disabled_tqdm

    logger.info("Whisper 模型文件已就绪：%s", model_path)
    return model_path


def _visible_length(text: str) -> int:
    return len("".join(text.split()))


def _confidence(segment: Any) -> float | None:
    value = getattr(segment, "avg_logprob", None)
    return float(value) if value is not None else None


def _word_values(word: Any) -> tuple[float, float, str] | None:
    text = str(getattr(word, "word", ""))
    try:
        start = float(word.start)
        end = float(word.end)
    except (AttributeError, TypeError, ValueError):
        return None
    if not text.strip() or not math.isfinite(start) or not math.isfinite(end) or end <= start:
        return None
    return start, end, text


def _group_text(group: list[tuple[float, float, str]]) -> str:
    return "".join(item[2] for item in group).strip()


def _can_merge_groups(
    left: list[tuple[float, float, str]],
    right: list[tuple[float, float, str]],
    config: WorkerConfig,
) -> bool:
    left_text = _group_text(left)
    combined_text = left_text + _group_text(right)
    gap = right[0][0] - left[-1][1]
    duration = right[-1][1] - left[0][0]
    return (
        left_text[-1:] not in _SENTENCE_ENDINGS
        and gap <= config.asr.merge_short_cue_gap_seconds
        and duration <= config.asr.max_cue_duration_seconds
        and _visible_length(combined_text) <= config.asr.max_cue_chars
    )


def _polish_word_groups(
    groups: list[list[tuple[float, float, str]]], config: WorkerConfig
) -> list[list[tuple[float, float, str]]]:
    """合并短碎片，并把硬时长切分留下的短尾巴重新平衡。"""
    merged: list[list[tuple[float, float, str]]] = []
    for group in groups:
        if merged:
            left_is_short = _visible_length(_group_text(merged[-1])) < config.asr.min_cue_chars
            right_is_short = _visible_length(_group_text(group)) < config.asr.min_cue_chars
            if (left_is_short or right_is_short) and _can_merge_groups(merged[-1], group, config):
                merged[-1].extend(group)
                continue
        merged.append(list(group))

    # 如果达到最大时长/字符数后只剩一两个字，把前一条末尾的完整词移过来，
    # 避免产生“很长一条 + 两个字一条”的字幕，同时保留真实词级时间戳。
    for index in range(1, len(merged)):
        previous = merged[index - 1]
        current = merged[index]
        previous_text = _group_text(previous)
        gap = current[0][0] - previous[-1][1]
        if previous_text[-1:] in _SENTENCE_ENDINGS or gap > config.asr.merge_short_cue_gap_seconds:
            continue
        while (
            _visible_length(_group_text(current)) < config.asr.min_cue_chars and len(previous) > 1
        ):
            remaining = previous[:-1]
            candidate = [previous[-1], *current]
            if (
                _visible_length(_group_text(remaining)) < config.asr.min_cue_chars
                or _visible_length(_group_text(candidate)) > config.asr.max_cue_chars
                or candidate[-1][1] - candidate[0][0] > config.asr.max_cue_duration_seconds
            ):
                break
            previous[:] = remaining
            current[:] = candidate

    return merged


def _segment_cues(segment: Any, config: WorkerConfig) -> list[Cue]:
    """把 Whisper segment 按词级时间戳拆成适合字幕显示的短 cue。"""
    text = str(getattr(segment, "text", "")).strip()
    start = float(segment.start)
    end = float(segment.end)
    if not text or end <= start:
        return []

    words = [
        values
        for word in (getattr(segment, "words", None) or [])
        if (values := _word_values(word)) is not None
    ]
    if not words:
        # 没有词级时间戳时不猜测对白在超长区间中的位置。NAS 的时长硬校验会
        # 阻止异常结果发布，并将任务标为需要检查。
        return [
            Cue(
                cue_id=0,
                start=start,
                end=end,
                source=text,
                confidence=_confidence(segment),
            )
        ]

    groups: list[list[tuple[float, float, str]]] = []
    group: list[tuple[float, float, str]] = []

    def flush() -> None:
        if not group:
            return
        if _group_text(group):
            groups.append(list(group))
        group.clear()

    for word in words:
        if group:
            silence = word[0] - group[-1][1]
            projected_duration = word[1] - group[0][0]
            group_text = _group_text(group)
            gap_is_boundary = silence >= config.asr.max_word_gap_seconds and (
                _visible_length(group_text) >= config.asr.min_cue_chars
                or group_text[-1:] in _SENTENCE_ENDINGS
                or silence >= config.asr.merge_short_cue_gap_seconds
            )
            if gap_is_boundary or projected_duration > config.asr.max_cue_duration_seconds:
                flush()

        group.append(word)
        group_text = _group_text(group)
        group_duration = group[-1][1] - group[0][0]
        if (
            _visible_length(group_text) >= config.asr.max_cue_chars
            or group_duration >= config.asr.max_cue_duration_seconds
            or (group_duration >= 0.8 and group_text[-1:] in _SENTENCE_ENDINGS)
        ):
            flush()

    flush()
    result: list[Cue] = []
    for polished_group in _polish_word_groups(groups, config):
        result.append(
            Cue(
                cue_id=0,
                start=polished_group[0][0],
                end=polished_group[-1][1],
                source=_group_text(polished_group),
                confidence=_confidence(segment),
            )
        )
    return result


def segments_to_cues(segments: Any, config: WorkerConfig) -> list[Cue]:
    cues: list[Cue] = []
    for segment in segments:
        cues.extend(_segment_cues(segment, config))
    for cue_id, cue in enumerate(cues, start=1):
        cue.cue_id = cue_id
    return cues


def _split_text(text: str, max_chars: int) -> list[str]:
    sentences = re.findall(r".+?[。！？!?…]+|.+$", text.strip())
    parts: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        while sentence:
            parts.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
    return parts


def _has_catastrophic_timeline_error(
    cues: list[Cue], duration: float, retry_threshold: float
) -> bool:
    """只识别需要整块重跑的严重错误，普通超长 cue 留给局部切分。"""
    return any(
        cue.start < -0.1
        or cue.end > duration + 0.5
        or cue.end <= cue.start
        or cue.end - cue.start >= retry_threshold
        for cue in cues
    )


def _normalize_chunk_cues(
    cues: list[Cue],
    *,
    offset: float,
    chunk_duration: float,
    config: WorkerConfig,
) -> tuple[list[Cue], int]:
    """限制 cue 在当前音频块内；极端退化时做局部近似切分。"""
    result: list[Cue] = []
    approximated = 0
    max_duration = config.asr.max_cue_duration_seconds
    for cue in cues:
        start = max(0.0, min(float(cue.start), chunk_duration))
        end = max(0.0, min(float(cue.end), chunk_duration))
        if end <= start:
            start, end = 0.0, chunk_duration
        if end - start <= max_duration:
            result.append(cue.model_copy(update={"start": start + offset, "end": end + offset}))
            continue

        # 固定小块重试后仍无可用词级时间戳时，只在这个最多 30 秒的小块内
        # 按文本长度估算显示位置，避免错误扩散到整部电影。
        approximated += 1
        cursor = start
        for part in _split_text(cue.source, config.asr.max_cue_chars):
            display_seconds = min(max_duration, max(0.8, _visible_length(part) / 4.0))
            if cursor >= end:
                break
            part_end = min(end, cursor + display_seconds)
            if part_end > cursor:
                result.append(
                    Cue(
                        cue_id=0,
                        start=cursor + offset,
                        end=part_end + offset,
                        source=part,
                        confidence=cue.confidence,
                    )
                )
            cursor = part_end
    return result, approximated


class Transcriber:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self._model = None
        self._decode_audio = None
        self._sampling_rate = 16000
        self.device: str | None = None
        self.compute_type: str | None = None
        # 由 claim 的 language 设置，按任务源语言识别。
        self.language = "ja"

    def unload(self) -> None:
        self._model = None
        self._decode_audio = None

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio

        if self.device is None:
            hardware = query_gpu(self.config.device_index, self.config.device)
            self.device = select_device(
                self.config.device, {hardware.device_type, "cpu"}, {"cuda", "cpu"}
            )
        self.compute_type = self.config.asr.compute_type if self.device == "cuda" else "int8"

        model_path = _download_model_with_progress(
            self.config.asr.model,
            self.config.asr.download_root,
        )
        self._model = WhisperModel(
            model_path,
            device=self.device,
            device_index=self.config.device_index,
            compute_type=self.compute_type,
        )
        self._decode_audio = decode_audio
        self._sampling_rate = int(self._model.feature_extractor.sampling_rate)

    def _transcribe_once(self, audio: Any, *, vad_filter: bool) -> tuple[list[Cue], Any]:
        vad_parameters = None
        if vad_filter:
            vad_parameters = {
                "min_silence_duration_ms": self.config.asr.vad_min_silence_duration_ms,
                "speech_pad_ms": 200,
            }
        segments, info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=self.config.asr.beam_size,
            vad_filter=vad_filter,
            vad_parameters=vad_parameters,
            word_timestamps=True,
            condition_on_previous_text=self.config.asr.condition_on_previous_text,
            hallucination_silence_threshold=self.config.asr.hallucination_silence_threshold,
        )
        return segments_to_cues(segments, self.config), info

    def transcribe(
        self,
        audio_path: Path,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[list[Cue], dict]:
        def raise_if_cancelled() -> None:
            if cancel_check and cancel_check():
                raise TranscriptionCancelled(msg("worker.cancelledByServer"))

        raise_if_cancelled()
        self.load()
        raise_if_cancelled()
        audio = self._decode_audio(str(audio_path), sampling_rate=self._sampling_rate)
        raise_if_cancelled()
        total_samples = len(audio)
        duration = total_samples / self._sampling_rate
        chunk_samples = self.config.asr.chunk_seconds * self._sampling_rate
        retry_samples = self.config.asr.retry_chunk_seconds * self._sampling_rate
        chunk_count = max(1, math.ceil(total_samples / chunk_samples))
        cues: list[Cue] = []
        language_probability = None
        fallback_chunks = 0
        approximated_cues = 0
        if progress_callback:
            progress_callback(0, chunk_count)

        for chunk_index, chunk_start in enumerate(range(0, total_samples, chunk_samples), start=1):
            raise_if_cancelled()
            chunk = audio[chunk_start : chunk_start + chunk_samples]
            chunk_duration = len(chunk) / self._sampling_rate
            offset = chunk_start / self._sampling_rate
            logger.info("识别音频块 %d/%d（%.1f 秒处）。", chunk_index, chunk_count, offset)
            chunk_cues, info = self._transcribe_once(
                chunk,
                vad_filter=self.config.asr.vad_filter,
            )
            raise_if_cancelled()
            if language_probability is None:
                language_probability = getattr(info, "language_probability", None)

            retry_threshold = max(
                float(self.config.asr.retry_chunk_seconds),
                self.config.asr.max_cue_duration_seconds * 4,
            )
            if _has_catastrophic_timeline_error(
                chunk_cues,
                chunk_duration,
                retry_threshold,
            ):
                fallback_chunks += 1
                logger.warning(
                    "%.1f 秒处的音频块时间轴异常，改用 %d 秒固定小块重试。",
                    offset,
                    self.config.asr.retry_chunk_seconds,
                )
                for retry_start in range(0, len(chunk), retry_samples):
                    raise_if_cancelled()
                    retry_audio = chunk[retry_start : retry_start + retry_samples]
                    retry_duration = len(retry_audio) / self._sampling_rate
                    retry_cues, _ = self._transcribe_once(retry_audio, vad_filter=False)
                    raise_if_cancelled()
                    normalized, approximate_count = _normalize_chunk_cues(
                        retry_cues,
                        offset=offset + retry_start / self._sampling_rate,
                        chunk_duration=retry_duration,
                        config=self.config,
                    )
                    cues.extend(normalized)
                    approximated_cues += approximate_count
            else:
                normalized, approximate_count = _normalize_chunk_cues(
                    chunk_cues,
                    offset=offset,
                    chunk_duration=chunk_duration,
                    config=self.config,
                )
                cues.extend(normalized)
                approximated_cues += approximate_count
            if progress_callback:
                progress_callback(chunk_index, chunk_count)

        cues.sort(key=lambda cue: (cue.start, cue.end))
        for cue_id, cue in enumerate(cues, start=1):
            cue.cue_id = cue_id
        metadata = {
            "asr_engine": "fixed_chunks",
            "asr_mode": "faster_whisper",
            "model": self.config.asr.model,
            "compute_type": self.compute_type or self.config.asr.compute_type,
            "device": self.device or "cpu",
            "vad_filter": self.config.asr.vad_filter,
            "chunk_seconds": self.config.asr.chunk_seconds,
            "retry_chunk_seconds": self.config.asr.retry_chunk_seconds,
            "fallback_chunk_count": fallback_chunks,
            "approximated_cue_count": approximated_cues,
            "language_probability": language_probability,
            "duration_seconds": duration,
            "duration_after_vad_seconds": None,
        }
        return cues, metadata
