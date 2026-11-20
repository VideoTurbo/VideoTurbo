import pytest

from video_turbo import languages
from video_turbo.config import SubtitleConfig
from video_turbo.i18n import render
from video_turbo.models import Cue
from video_turbo.subtitles import (
    parse_srt_text,
    render_srt,
    repair_minor_overlaps,
    validate_cues,
    wrap_text,
)

ZH_LAYOUT = languages.get("zh-CN").layout


def test_srt_round_trip_preserves_timing() -> None:
    source = "1\n00:00:01,200 --> 00:00:03,400\nおはよう\n"
    cues = parse_srt_text(source)
    cues[0].target = "早上好"
    rendered = render_srt(cues, SubtitleConfig(), ZH_LAYOUT)
    assert "00:00:01,200 --> 00:00:03,400" in rendered
    assert "早上好" in rendered


def test_validation_detects_overlap() -> None:
    cues = [
        Cue(cue_id=1, start=0, end=2, source="a", target="甲"),
        Cue(cue_id=2, start=1.5, end=3, source="b", target="乙"),
    ]
    issues = validate_cues(cues, SubtitleConfig(), ZH_LAYOUT)
    assert any(issue.code == "overlap" and issue.severity == "error" for issue in issues)
    overlap = next(issue for issue in issues if issue.code == "overlap")
    assert "第 2 条与第 1 条" in render(overlap.message)
    assert "500 毫秒" in render(overlap.message)


def test_minor_overlap_is_repaired_before_validation() -> None:
    cues = [
        Cue(cue_id=812, start=4307.88, end=4309.42, source="さぁ"),
        Cue(cue_id=813, start=4309.38, end=4311.96, source="はぁい"),
    ]

    repairs = repair_minor_overlaps(cues)

    assert len(repairs) == 1
    assert repairs[0][0:2] == (812, 813)
    assert repairs[0][2] == pytest.approx(0.04)
    assert cues[0].end == 4309.38
    assert not any(issue.code == "overlap" for issue in validate_cues(cues, SubtitleConfig(), ZH_LAYOUT))


def test_large_overlap_is_not_automatically_repaired() -> None:
    cues = [
        Cue(cue_id=1, start=0, end=2, source="a"),
        Cue(cue_id=2, start=1.5, end=3, source="b"),
    ]

    assert repair_minor_overlaps(cues) == []
    assert cues[0].end == 2


def test_validation_rejects_cue_longer_than_limit() -> None:
    cues = [Cue(cue_id=1, start=310.1, end=7589.45, source="異常な長時間字幕")]

    issues = validate_cues(cues, SubtitleConfig(max_duration_ms=7000), ZH_LAYOUT)

    assert any(issue.code == "too_long" and issue.severity == "error" for issue in issues)


EN_LAYOUT = languages.get("en").layout


def test_english_wraps_between_words() -> None:
    text = "I never thought I would see you again, not after everything that happened."
    wrapped = wrap_text(text, EN_LAYOUT, 2).splitlines()
    assert len(wrapped) == 2
    assert all(len(line) <= EN_LAYOUT.max_chars_per_line for line in wrapped)
    assert " ".join(wrapped) == text
    assert wrapped[0].endswith(",")


def test_short_english_stays_on_one_line_and_keeps_spaces() -> None:
    assert wrap_text("  Hello   there!  ", EN_LAYOUT, 2) == "Hello there!"


def test_chinese_wrapping_is_unchanged() -> None:
    assert wrap_text("我从没想过还会再见到你，尤其是在发生了那些事情之后", ZH_LAYOUT, 2) == (
        "我从没想过还会再见到你，\n尤其是在发生了那些事情之后"
    )


def test_english_reading_speed_counts_spaces_and_uses_target_limits() -> None:
    cue = Cue(cue_id=1, start=0, end=2, source="你好", target="This is fine for two seconds.")
    assert not validate_cues([cue], SubtitleConfig(), EN_LAYOUT)
    fast = Cue(cue_id=1, start=0, end=1, source="你好", target="This line is far too long to read.")
    assert any(issue.code == "reading_speed" for issue in validate_cues([fast], SubtitleConfig(), EN_LAYOUT))
    rendered = render_srt([cue], SubtitleConfig(), EN_LAYOUT)
    assert "This is fine for two seconds." in rendered
