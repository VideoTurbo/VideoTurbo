from types import SimpleNamespace

from video_turbo.worker.asr import (
    Transcriber,
    _has_catastrophic_timeline_error,
    segments_to_cues,
)
from video_turbo.worker.config import WorkerConfig


def _word(start: float, end: float, text: str) -> SimpleNamespace:
    return SimpleNamespace(start=start, end=end, word=text)


def _segment(start: float, end: float, text: str, words: list) -> SimpleNamespace:
    return SimpleNamespace(
        start=start,
        end=end,
        text=text,
        words=words,
        avg_logprob=-0.2,
    )


def test_giant_segment_is_split_by_word_timestamps() -> None:
    config = WorkerConfig()
    words = []
    for index in range(30):
        start = 310.0 + index * 1.2
        words.append(_word(start, start + 0.45, "言葉"))
    segment = _segment(310.0, 7589.45, "".join(word.word for word in words), words)

    cues = segments_to_cues([segment], config)

    assert len(cues) > 1
    assert [cue.cue_id for cue in cues] == list(range(1, len(cues) + 1))
    assert all(cue.end - cue.start <= config.asr.max_cue_duration_seconds for cue in cues)
    assert "".join(cue.source for cue in cues) == segment.text


def test_long_silence_starts_a_new_cue() -> None:
    config = WorkerConfig()
    segment = _segment(
        1.0,
        12.0,
        "こんにちは。またね。",
        [
            _word(1.0, 1.6, "こんにちは。"),
            _word(10.0, 10.6, "またね。"),
        ],
    )

    cues = segments_to_cues([segment], config)

    assert [(cue.start, cue.end, cue.source) for cue in cues] == [
        (1.0, 1.6, "こんにちは。"),
        (10.0, 10.6, "またね。"),
    ]


def test_short_japanese_words_are_not_fragmented_by_small_gaps() -> None:
    config = WorkerConfig()
    segment = _segment(
        0.0,
        4.7,
        "これは本当。",
        [
            _word(0.0, 0.2, "こ"),
            _word(1.05, 1.25, "れ"),
            _word(2.1, 2.3, "は"),
            _word(3.15, 3.35, "本"),
            _word(4.2, 4.4, "当"),
            _word(4.45, 4.65, "。"),
        ],
    )

    cues = segments_to_cues([segment], config)

    assert [(cue.start, cue.end, cue.source) for cue in cues] == [(0.0, 4.65, "これは本当。")]


def test_short_tail_is_rebalanced_at_duration_boundary() -> None:
    config = WorkerConfig()
    words = [
        _word(0.0, 1.0, "これは"),
        _word(1.1, 2.1, "長い文"),
        _word(2.2, 3.2, "なので"),
        _word(3.3, 4.3, "途中で"),
        _word(4.4, 5.4, "自然に"),
        _word(5.5, 6.5, "切る。"),
    ]
    segment = _segment(0.0, 6.5, "".join(word.word for word in words), words)

    cues = segments_to_cues([segment], config)

    assert [cue.source for cue in cues] == ["これは長い文なので途中で", "自然に切る。"]
    assert all(len(cue.source) >= config.asr.min_cue_chars for cue in cues)


def test_missing_word_timestamps_is_left_for_quality_gate() -> None:
    config = WorkerConfig()
    segment = _segment(5.0, 100.0, "時間位置を推測しない", [])

    cues = segments_to_cues([segment], config)

    assert len(cues) == 1
    assert cues[0].start == 5.0
    assert cues[0].end == 100.0


def test_normal_long_cue_does_not_retry_whole_audio_block() -> None:
    normal_long_cue = [
        SimpleNamespace(start=1.0, end=9.0),
    ]
    collapsed_cue = [
        SimpleNamespace(start=0.0, end=30.0),
    ]

    assert not _has_catastrophic_timeline_error(normal_long_cue, 300.0, 30.0)
    assert _has_catastrophic_timeline_error(collapsed_cue, 300.0, 30.0)


def test_transcriber_retries_bad_blocks_with_fixed_small_chunks(tmp_path) -> None:
    sampling_rate = 10

    class FakeAudio:
        def __init__(self, samples: int) -> None:
            self.samples = samples

        def __len__(self) -> int:
            return self.samples

        def __getitem__(self, value: slice):
            start, stop, step = value.indices(self.samples)
            assert step == 1
            return FakeAudio(max(0, stop - start))

    info = SimpleNamespace(language_probability=1.0)

    class FakeModel:
        def __init__(self) -> None:
            self.durations = []

        def transcribe(self, audio, **_kwargs):
            duration = len(audio) / sampling_rate
            self.durations.append(duration)
            if duration > 10:
                return [_segment(0.0, duration, "异常长字幕", [])], info
            return [
                _segment(0.0, min(2.0, duration), "正常字幕。", [_word(0.0, 2.0, "正常字幕。")])
            ], info

    config = WorkerConfig(
        worker_token="token",
        asr={"vad_filter": False, "chunk_seconds": 30, "retry_chunk_seconds": 10},
    )
    transcriber = Transcriber(config)
    transcriber._model = FakeModel()
    transcriber._sampling_rate = sampling_rate
    transcriber._decode_audio = lambda _path, sampling_rate: FakeAudio(65 * sampling_rate)

    progress = []
    cues, metadata = transcriber.transcribe(
        tmp_path / "audio.opus",
        progress_callback=lambda completed, total: progress.append((completed, total)),
    )

    assert metadata["asr_engine"] == "fixed_chunks"
    assert metadata["fallback_chunk_count"] == 2
    assert max(transcriber._model.durations) == 30
    assert all(cue.end - cue.start <= config.asr.max_cue_duration_seconds for cue in cues)
    assert [cue.start for cue in cues] == [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    assert progress == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_transcriber_uses_claim_language(tmp_path) -> None:
    calls = []

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            calls.append(kwargs["language"])
            return [], SimpleNamespace(language_probability=1.0)

    transcriber = Transcriber(WorkerConfig(worker_token="token", asr={"vad_filter": False}))
    transcriber._model = FakeModel()
    transcriber.language = "en"
    transcriber._transcribe_once([0.0] * 16000, vad_filter=False)
    assert calls == ["en"]
