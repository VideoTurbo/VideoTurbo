from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from .config import SubtitleConfig
from .i18n import msg
from .languages import Layout
from .models import Cue

TIMESTAMP_RE = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})")
MINOR_OVERLAP_MAX_SECONDS = 0.2


@dataclass(slots=True)
class QualityIssue:
    cue_id: int | None
    code: str
    message: str
    severity: str = "warning"


def parse_timestamp(value: str) -> float:
    match = TIMESTAMP_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(msg("subtitles.badTimestamp", value=value))
    return int(match["h"]) * 3600 + int(match["m"]) * 60 + int(match["s"]) + int(match["ms"]) / 1000


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def parse_srt_text(text: str) -> list[Cue]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    cues: list[Cue] = []
    for block in re.split(r"\n{2,}", normalized):
        lines = [line.rstrip() for line in block.split("\n")]
        if not lines:
            continue
        timing_index = 1 if lines[0].strip().isdigit() else 0
        if timing_index >= len(lines) or "-->" not in lines[timing_index]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[timing_index].split("-->", 1)]
        end_raw = end_raw.split()[0]
        dialogue = " ".join(line.strip() for line in lines[timing_index + 1 :] if line.strip())
        dialogue = re.sub(r"<[^>]+>", "", dialogue).strip()
        if not dialogue:
            continue
        cues.append(
            Cue(
                cue_id=len(cues) + 1,
                start=parse_timestamp(start_raw),
                end=parse_timestamp(end_raw),
                source=dialogue,
            )
        )
    return cues


def parse_srt(path: Path, legacy_encodings: Iterable[str] = ()) -> list[Cue]:
    """legacy_encodings 是源语言常见的本地编码，排在 UTF-8/UTF-16 之后尝试。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", *legacy_encodings):
        try:
            return parse_srt_text(raw.decode(encoding))
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError(msg("subtitles.unreadable", path=path))


def _visible_length(text: str, layout: Layout) -> int:
    # 中日文字幕不计空白；按词分隔的语言把连续空白算作一个字符。
    if layout.kind == "cjk":
        return len(re.sub(r"\s+", "", text))
    return len(re.sub(r"\s+", " ", text.strip()))


_CJK_PUNCTUATION = set("，。！？；：、…—）】》」』")
_SPACED_PUNCTUATION = set(",.!?;:…—)]}\"'")


def _split_cjk(text: str, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", "", text.strip())
    if len(text) <= max_chars:
        return [text]
    target = min(max_chars, len(text) // 2 + 1)
    candidates = [index for index in range(1, min(len(text), max_chars + 1))]
    split_at = min(
        candidates,
        key=lambda index: (
            0 if text[index - 1] in _CJK_PUNCTUATION else 1,
            abs(index - target),
        ),
    )
    return [text[:split_at], text[split_at:]]


def _split_spaced(text: str, max_chars: int) -> list[str]:
    words = text.split()
    joined = " ".join(words)
    if len(joined) <= max_chars or len(words) < 2:
        return [joined]
    # 只在词与词之间断开；首行不超过上限的位置里，优先标点之后，其次两行长度接近。
    candidates = [
        index for index in range(1, len(words)) if len(" ".join(words[:index])) <= max_chars
    ] or [1]
    half = len(joined) / 2
    split_at = min(
        candidates,
        key=lambda index: (
            0 if words[index - 1][-1] in _SPACED_PUNCTUATION else 1,
            abs(len(" ".join(words[:index])) - half),
        ),
    )
    return [" ".join(words[:split_at]), " ".join(words[split_at:])]


def wrap_text(text: str, layout: Layout, max_lines: int) -> str:
    text = text.strip()
    if not text:
        return ""
    if layout.kind == "cjk":
        lines = _split_cjk(text, layout.max_chars_per_line)
    else:
        lines = _split_spaced(text, layout.max_chars_per_line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)


def repair_minor_overlaps(
    cues: list[Cue], max_overlap_seconds: float = MINOR_OVERLAP_MAX_SECONDS
) -> list[tuple[int, int, float]]:
    """收紧前一条字幕的结束时间，消除 Whisper 产生的轻微相邻重叠。"""
    repairs: list[tuple[int, int, float]] = []
    for previous, current in pairwise(cues):
        overlap = previous.end - current.start
        if 0 < overlap <= max_overlap_seconds and current.start > previous.start:
            previous.end = current.start
            repairs.append((previous.cue_id, current.cue_id, overlap))
    return repairs


def validate_cues(
    cues: Iterable[Cue],
    config: SubtitleConfig,
    layout: Layout,
    media_duration: float | None = None,
) -> list[QualityIssue]:
    """layout 是待检查文本所用语言的排版：有译文时用目标语言，只有原文时用源语言。"""
    issues: list[QualityIssue] = []
    previous_end = 0.0
    previous_end_cue_id: int | None = None
    for cue in cues:
        text = (cue.target or cue.source).strip()
        duration = cue.end - cue.start
        if cue.end <= cue.start:
            issues.append(
                QualityIssue(cue.cue_id, "invalid_timing", msg("subtitles.invalidTiming"), "error")
            )
            continue
        if cue.start < previous_end - 0.01:
            overlap_ms = (previous_end - cue.start) * 1000
            issues.append(
                QualityIssue(
                    cue.cue_id,
                    "overlap",
                    msg("subtitles.overlap", cue_id=cue.cue_id, previous_end_cue_id=previous_end_cue_id, overlap_ms=f"{overlap_ms:.0f}"),
                    "error",
                )
            )
        if duration * 1000 < config.min_duration_ms:
            issues.append(QualityIssue(cue.cue_id, "too_short", msg("subtitles.tooShort")))
        if duration * 1000 > config.max_duration_ms:
            issues.append(
                QualityIssue(
                    cue.cue_id,
                    "too_long",
                    msg("subtitles.tooLong", cue_id=cue.cue_id, duration=f"{duration:.1f}", limit=f"{config.max_duration_ms / 1000:.1f}"),
                    "error",
                )
            )
        if media_duration is not None and cue.end > media_duration + 0.5:
            issues.append(QualityIssue(cue.cue_id, "past_media_end", msg("subtitles.pastEnd"), "error"))
        if not text:
            issues.append(QualityIssue(cue.cue_id, "empty", msg("subtitles.empty"), "error"))
        elif (
            duration > 0
            and _visible_length(text, layout) / duration > layout.max_chars_per_second
        ):
            issues.append(QualityIssue(cue.cue_id, "reading_speed", msg("subtitles.readingSpeed")))
        if cue.target:
            wrapped = wrap_text(cue.target, layout, config.max_lines).splitlines()
            if any(len(line) > layout.max_chars_per_line for line in wrapped):
                issues.append(QualityIssue(cue.cue_id, "line_length", msg("subtitles.lineLength")))
        if cue.end > previous_end:
            previous_end = cue.end
            previous_end_cue_id = cue.cue_id
    return issues


def render_srt(cues: Iterable[Cue], config: SubtitleConfig, layout: Layout) -> str:
    """layout 是目标语言的排版；没有译文的 cue 原样输出原文。"""
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        text = cue.target if cue.target is not None else cue.source
        blocks.append(
            f"{index}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            f"{wrap_text(text, layout, config.max_lines) if cue.target is not None else text.strip()}"
        )
    return "\n\n".join(blocks) + "\n"

