import os
import shutil
import subprocess
import sys
import time
from array import array
from pathlib import Path

import pytest

from video_turbo.config import AudioConfig, SubtitleConfig
from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.media import (
    choose_processing_source,
    duration_from_probe,
    extract_audio,
    is_settled,
    output_subtitle_path,
)
from video_turbo.models import SourceKind

JA_ZH = LanguagePair()


def _probe(*streams: dict) -> dict:
    return {"streams": list(streams), "format": {"duration": "7200"}}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
@pytest.mark.parametrize("with_gap", [False, True])
def test_audio_extraction_keeps_speech_timing_and_fills_gaps(tmp_path: Path, with_gap: bool):
    # Shift everything after 1 s by 2 s, leaving a timestamp gap like a damaged audio track.
    # (aselect would be simpler, but it keeps every frame in FFmpeg 8.0.)
    source = tmp_path / "source.mka"
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        f"sine=frequency=1000:sample_rate=16000:duration={2 if with_gap else 4}",
    ]
    if with_gap:
        command += ["-af", "asetpts='PTS+gte(T,1)*2/TB'"]
    subprocess.run(command + ["-c:a", "pcm_s16le", str(source)],
                   check=True, capture_output=True, timeout=15)
    output = tmp_path / "audio.opus"
    extract_audio(source, 0, output, AudioConfig(), timeout=15)
    decoded = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(output),
         "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        check=True, capture_output=True, timeout=15,
    )
    samples = array("f")
    samples.frombytes(decoded.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    # Check decoded samples, since container duration alone can conceal a time jump.
    assert len(samples) / 16000 == pytest.approx(4, abs=0.05)

    def peak(start: float, end: float) -> float:
        return max(abs(value) for value in samples[int(start * 16000):int(end * 16000)])

    assert peak(0.2, 0.8) > 0.05
    assert peak(3.2, 3.8) > 0.05
    assert peak(1.3, 2.7) < 0.001 if with_gap else peak(1.3, 2.7) > 0.05
    assert not output.with_suffix(".opus.processing").exists()


def test_zero_settle_minutes_does_not_wait_for_mtime(tmp_path: Path) -> None:
    video = tmp_path / "new.mkv"
    video.touch()
    future = time.time() + 60
    os.utime(video, (future, future))

    mtime_ns = video.stat().st_mtime_ns
    assert is_settled(mtime_ns, 0) is True
    assert is_settled(mtime_ns, 30) is False


def test_duration_uses_longer_selected_audio_stream() -> None:
    probe = {
        "format": {"duration": "8253.432"},
        "streams": [
            {"index": 0, "codec_type": "video", "duration": "8253.432"},
            {"index": 1, "codec_type": "audio", "duration": "8974.2"},
            {"index": 2, "codec_type": "audio", "duration": "9999"},
        ],
    }

    assert duration_from_probe(probe, stream_index=1) == pytest.approx(8974.2)


def test_duration_ignores_shorter_selected_stream_and_unselected_streams() -> None:
    probe = {
        "format": {"duration": "7200"},
        "streams": [
            {"index": 1, "codec_type": "audio", "duration": "7190"},
            {"index": 2, "codec_type": "audio", "duration": "9000"},
        ],
    }

    assert duration_from_probe(probe, stream_index=1) == pytest.approx(7200)


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ({"index": 1, "duration_ts": 8974200, "time_base": "1/1000"}, 8974.2),
        ({"index": 1, "tags": {"DURATION": "02:29:34.200000"}}, 8974.2),
    ],
)
def test_duration_supports_stream_duration_fallbacks(stream: dict, expected: float) -> None:
    probe = {"format": {"duration": "8253.432"}, "streams": [stream]}

    assert duration_from_probe(probe, stream_index=1) == pytest.approx(expected)


def test_prefers_embedded_japanese_text_subtitle(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    decision = choose_processing_source(
        video,
        _probe(
            {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "ass",
                "tags": {"language": "jpn"},
            },
        ),
        SubtitleConfig(), JA_ZH,
    )
    assert decision.source_kind == SourceKind.EMBEDDED_TEXT
    assert decision.stream_index == 2


def test_existing_simplified_chinese_is_skipped(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    (tmp_path / "movie.zh-CN.srt").write_text("x", encoding="utf-8")
    decision = choose_processing_source(video, _probe(), SubtitleConfig(), JA_ZH)
    assert decision.action == "done"


def test_output_uses_chinese_default_suffix(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    assert output_subtitle_path(video, "zh-CN", SubtitleConfig()).name == "movie.zh.default.srt"


def test_existing_output_subtitle_is_skipped(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    (tmp_path / "movie.zh.default.srt").write_text("已有字幕", encoding="utf-8")
    decision = choose_processing_source(video, _probe(), SubtitleConfig(), JA_ZH)
    assert decision.action == "done"
    assert "movie.zh.default.srt" in decision.reason


@pytest.mark.parametrize(
    "tags",
    [
        {"language": "zh-Hans"},
        {"language": "cmn"},
        {"language": "und", "title": "简中字幕"},
        {"language": "und", "title": "繁中"},
        {"language": "und", "title": "中字"},
        {"language": "und", "handler_name": "Chinese Simplified (CHS)"},
    ],
)
def test_embedded_chinese_metadata_variants_are_skipped(tmp_path: Path, tags: dict) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    decision = choose_processing_source(
        video,
        _probe(
            {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "ass", "tags": tags},
        ),
        SubtitleConfig(), JA_ZH,
    )

    assert decision.action == "done"
    assert "中文字幕轨" in render(decision.reason)


def test_unlabeled_embedded_subtitle_can_skip_asr(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    probe = _probe(
        {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "ass", "tags": {}},
    )

    ignored = choose_processing_source(video, probe, SubtitleConfig(), JA_ZH)
    skipped = choose_processing_source(
        video,
        probe,
        SubtitleConfig(unlabeled_embedded_subtitle_policy="skip"), JA_ZH,
    )

    assert ignored.source_kind == SourceKind.AUDIO
    assert skipped.action == "unusable"
    assert "无语言标签" in render(skipped.reason)


def test_ambiguous_audio_tracks_are_skipped(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    probe = _probe(
        {"index": 1, "codec_type": "audio", "tags": {}},
        {"index": 2, "codec_type": "audio", "tags": {}},
    )

    decision = choose_processing_source(video, probe, SubtitleConfig(), JA_ZH)

    assert decision.action == "unusable"
    assert decision.source_kind is None
    assert "无法可靠确定日语主音轨" in render(decision.reason)


def test_burned_in_chinese_suffix_in_file_or_parent_is_skipped(tmp_path: Path) -> None:
    direct = tmp_path / "ATID-637-C.mp4"
    direct.touch()
    nested_dir = tmp_path / "ATID-638-C"
    nested_dir.mkdir()
    nested = nested_dir / "ATID-638.mp4"
    nested.touch()
    normal = tmp_path / "ATID-639-CAR.mp4"
    normal.touch()
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}})

    direct_decision = choose_processing_source(direct, probe, SubtitleConfig(), JA_ZH)
    nested_decision = choose_processing_source(nested, probe, SubtitleConfig(), JA_ZH)
    normal_decision = choose_processing_source(normal, probe, SubtitleConfig(), JA_ZH)

    assert direct_decision.action == "done"
    assert nested_decision.action == "done"
    assert "画面里已经有简体中文字幕" in render(direct_decision.reason)
    assert normal_decision.source_kind == SourceKind.AUDIO


def test_burned_in_chinese_path_rules_can_be_disabled(tmp_path: Path) -> None:
    video = tmp_path / "ATID-637-C.mp4"
    video.touch()
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}})

    decision = choose_processing_source(
        video,
        probe,
        SubtitleConfig(burned_in_target_path_patterns=[]), JA_ZH,
    )

    assert decision.source_kind == SourceKind.AUDIO




EN_ZH = LanguagePair("en", "zh-CN")


def test_english_library_translates_external_english_text_subtitle(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    english = tmp_path / "movie.en.srt"
    english.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "eng"}})

    decision = choose_processing_source(video, probe, SubtitleConfig(), EN_ZH)

    assert decision.action == "process"
    assert decision.source_kind == SourceKind.EXTERNAL_TEXT
    assert decision.source_path == english
    assert "英语文本字幕" in render(decision.reason)
    # 日语库不会把英文字幕当作源字幕。
    japanese = choose_processing_source(video, probe, SubtitleConfig(), JA_ZH)
    assert japanese.source_kind == SourceKind.AUDIO


def test_english_library_prefers_embedded_english_text_subtitle(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    probe = _probe(
        {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "jpn"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    )

    decision = choose_processing_source(video, probe, SubtitleConfig(), EN_ZH)

    assert decision.source_kind == SourceKind.EMBEDDED_TEXT
    assert decision.stream_index == 3


def test_english_library_selects_english_audio_track(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    probe = _probe(
        {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
        {"index": 2, "codec_type": "audio", "tags": {"language": "eng", "title": "Commentary"}},
        {"index": 3, "codec_type": "audio", "tags": {"language": "eng"}},
    )

    decision = choose_processing_source(video, probe, SubtitleConfig(), EN_ZH)

    assert decision.source_kind == SourceKind.AUDIO
    assert decision.stream_index == 3
    assert "英语主音轨" in render(decision.reason)
    skipped = choose_processing_source(
        video,
        _probe(
            {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
            {"index": 2, "codec_type": "audio", "tags": {"language": "kor"}},
        ),
        SubtitleConfig(),
        EN_ZH,
    )
    assert skipped.action == "unusable"
    assert "英语主音轨" in render(skipped.reason)


def test_english_library_skips_existing_simplified_chinese(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    (tmp_path / "movie.chs.srt").write_text("已有字幕", encoding="utf-8")
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "eng"}})

    decision = choose_processing_source(video, probe, SubtitleConfig(), EN_ZH)

    assert decision.action == "done"
    assert "已存在简体中文字幕" in render(decision.reason)


@pytest.mark.parametrize("name", ["movie.srt", "MOVIE.ass"])
def test_untagged_same_name_subtitle_counts_as_target(tmp_path: Path, name: str) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    (tmp_path / name).write_text("已有字幕", encoding="utf-8")
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}})

    decision = choose_processing_source(video, probe, SubtitleConfig(), JA_ZH)

    assert decision.action == "done"
    assert "无语言标记" in render(decision.reason)


def test_untagged_subtitle_with_unknown_segment_is_not_target(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    (tmp_path / "movie.forced.srt").write_text("字幕", encoding="utf-8")
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}})

    decision = choose_processing_source(video, probe, SubtitleConfig(), JA_ZH)

    assert decision.action == "process"


@pytest.mark.parametrize(
    ("accept", "external_skipped", "embedded_skipped"),
    [(True, True, True), (False, False, False)],
)
def test_other_chinese_variant_switch(
    tmp_path: Path, accept: bool, external_skipped: bool, embedded_skipped: bool
) -> None:
    config = SubtitleConfig(accept_other_chinese_variant=accept)
    audio = {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}}
    external = tmp_path / "external.mkv"
    external.touch()
    (tmp_path / "external.cht.srt").write_text("繁体字幕", encoding="utf-8")
    embedded = tmp_path / "embedded.mkv"
    embedded.touch()
    traditional_track = {
        "index": 2, "codec_type": "subtitle", "codec_name": "subrip",
        "tags": {"language": "chi", "title": "繁體中文"},
    }

    external_decision = choose_processing_source(external, _probe(audio), config, EN_ZH)
    embedded_decision = choose_processing_source(
        embedded, _probe(audio, traditional_track), config, EN_ZH
    )

    assert (external_decision.action == "done") is external_skipped
    assert (embedded_decision.action == "done") is embedded_skipped


def test_unspecified_chinese_always_counts_as_existing(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    probe = _probe(
        {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "chi"}},
    )
    config = SubtitleConfig(accept_other_chinese_variant=False)

    for target in ("zh-CN", "zh-TW"):
        decision = choose_processing_source(video, probe, config, LanguagePair("en", target))
        assert decision.action == "done"


def test_existing_target_rule_follows_target_language(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.touch()
    (tmp_path / "movie.zh.srt").write_text("中文字幕", encoding="utf-8")
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}})

    # 日→英：中文字幕和 -C 之外的中文硬字幕规则都与目标语言无关。
    decision = choose_processing_source(video, probe, SubtitleConfig(), LanguagePair("ja", "en"))
    assert decision.action == "process"
    (tmp_path / "movie.en.default.srt").write_text("English", encoding="utf-8")
    skipped = choose_processing_source(video, probe, SubtitleConfig(), LanguagePair("ja", "en"))
    assert skipped.action == "done"
    assert "movie.en.default.srt" in skipped.reason


def test_chinese_burned_in_name_rule_only_applies_to_chinese_targets(tmp_path: Path) -> None:
    folder = tmp_path / "ABC-123 中字"
    folder.mkdir()
    video = folder / "ABC-123.mkv"
    video.touch()
    probe = _probe({"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}})

    assert choose_processing_source(video, probe, SubtitleConfig(), JA_ZH).action == "done"
    english = choose_processing_source(video, probe, SubtitleConfig(), LanguagePair("ja", "en"))
    assert english.action == "process"
