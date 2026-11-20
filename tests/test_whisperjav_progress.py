import pytest

from video_turbo.i18n import render
from video_turbo.worker.whisperjav import ANIME_MODEL
from video_turbo.worker.whisperjav_progress import WhisperJavProgressParser


@pytest.fixture
def parser():
    return WhisperJavProgressParser("Qwen/test", False, ANIME_MODEL)


def start_generation(parser):
    parser.consume("[QwenPipeline PID 123] Phase 5: ASR transcription (model=test)")
    parser.consume("[DecoupledPipeline] Steps 2-4: Generating + cleaning text for 3 scenes")


def test_generation_distinguishes_started_and_completed_scenes(parser):
    start_generation(parser)
    first = parser.consume("[DecoupledPipeline] Generating scene 1/3 (20.0s audio)...")
    assert (first.completed_scenes, first.current_scene, first.total_scenes) == (0, 1, 3)
    done = parser.consume("[DecoupledPipeline]   Scene 1/3: 12 chars")
    assert (done.completed_scenes, done.current_scene) == (1, None)
    # The next scene's start also proves the previous scene has been processed,
    # even if its completion message was omitted from the log.
    second = parser.consume("[DecoupledPipeline] Generating scene 2/3 (10.0s audio)...")
    assert (second.completed_scenes, second.current_scene) == (1, 2)
    empty = parser.consume("[DecoupledPipeline]   Scene 2/3: empty (no text generated)")
    assert empty.completed_scenes == 2
    cleaning = parser.consume("[DecoupledPipeline] Cleaning 3 scenes")
    assert render(cleaning.stage) == "清理识别文本"
    assert cleaning.total_scenes is None  # No invented per-scene cleaning progress.


def test_vad_and_reconstruction_counters_are_separate(parser):
    parser.consume("[QwenPipeline PID 123] Phase 4: Speech segmentation (backend=firered)")
    vad = parser.consume("Phase 4: Scene 2/3 — 5 speech segments (coverage=10.0%)")
    assert (vad.completed_scenes, vad.total_scenes, vad.current_scene) == (2, 3, None)
    asr = parser.consume("Phase 5: ASR transcription")
    assert asr.total_scenes is None
    parser.consume("[DecoupledPipeline] Step 9: Reconstructing 3 scenes")
    result = parser.consume(
        "[DecoupledPipeline] Scene 1/3: 12 words → 3 segments (sentinel: N/A)"
    )
    assert render(result.stage) == "重建字幕时间轴"
    assert result.completed_scenes == 1
    stitched = parser.consume("Phase 7: Stitching 3 scene SRTs")
    assert stitched.total_scenes is None


def test_ensemble_switch_clears_counts_and_changes_model():
    parser = WhisperJavProgressParser("Qwen/test", True, ANIME_MODEL)
    parser.consume("[1/1] audio — Pass 1 (qwen)...")
    start_generation(parser)
    parser.consume("[DecoupledPipeline] Generating scene 3/3 (10.0s audio)...")
    second = parser.consume("[1/1] audio — Pass 2 (qwen)...")
    assert (second.pass_number, second.total_passes, second.model) == (2, 2, ANIME_MODEL)
    assert second.total_scenes is None
    start_generation(parser)
    next_scene = parser.consume("[DecoupledPipeline] Generating scene 1/3 (10.0s audio)...")
    assert next_scene.completed_scenes == 0
    assert render(next_scene.stage) == "第 2/2 遍 · 生成识别文本"
    merge = parser.consume("[1/1] audio — Merge (pass1_primary)...")
    assert render(merge.stage) == "合并字幕 · Qwen 为主"
    assert merge.total_scenes is None
    assert merge.model is None


def test_parallel_passes_are_tracked_by_tag_and_pass_one_leads():
    now = [3.0]
    parser = WhisperJavProgressParser("Qwen/test", True, ANIME_MODEL, clock=lambda: now[0])
    waiting = parser.snapshot().passes
    assert [(p.pass_number, p.model, p.state) for p in waiting] == [
        (1, "Qwen/test", "waiting"), (2, ANIME_MODEL, "waiting")
    ]
    early = parser.consume("[pass2] [QwenPipeline PID 2] Phase 1: Extracting audio from movie")
    assert render(early.stage) == "第 1/2 遍 · Qwen3-ASR ｜ 第 2/2 遍 · 提取音频"
    assert early.pass_number == 1
    assert [(p.state, p.step, p.started_seconds) for p in early.passes] == [
        ("running", None, 3.0), ("running", 1, 3.0)
    ]
    parser.consume("[pass1] [QwenPipeline PID 1] Phase 5: ASR transcription (model=test)")
    parser.consume("[pass1] [DecoupledPipeline] Step 1: Framing 3 scenes")
    assert parser.snapshot().passes[0].step == 3  # framing reruns speech detection
    parser.consume("[pass1] [DecoupledPipeline] Steps 2-4: Generating + cleaning text for 3 scenes")
    parser.consume("[pass2] [QwenPipeline PID 2] Phase 4: Speech segmentation (backend=ten)")
    both = parser.consume("[pass1] [DecoupledPipeline] Generating scene 2/3 (10.0s audio)...")
    assert render(both.stage) == "第 1/2 遍 · 生成识别文本 ｜ 第 2/2 遍 · 语音检测"
    assert (both.pass_number, both.model, both.completed_scenes, both.total_scenes) == (
        1, "Qwen/test", 1, 3
    )
    first, second = both.passes
    assert (render(first.stage), first.step, first.completed_scenes, first.current_scene) == (
        "生成识别文本", 4, 1, 2
    )
    assert (render(second.stage), second.step, second.total_scenes) == ("语音检测", 3, None)
    # Pass 2's counters stay its own and do not overwrite pass 1's.
    vad = parser.consume("[pass2] Phase 4: Scene 1/9 — 2 speech segments (coverage=5.0%)")
    assert vad.total_scenes == 3 and vad.passes[1].total_scenes == 9
    # Untagged parent and library output is not attributed to either pass.
    assert parser.consume("Loading checkpoint shards: 50%") is None
    now[0] = 900.04
    first_done = parser.consume("Pass 1 completed: 1/1 file(s) successful, 9 subtitles total")
    assert render(first_done.stage) == "第 2/2 遍 · 语音检测"
    assert (first_done.pass_number, first_done.model) == (2, ANIME_MODEL)
    assert (first_done.completed_scenes, first_done.total_scenes) == (1, 9)
    done = first_done.passes[0]
    assert (done.state, done.stage, done.step, done.finished_seconds) == ("done", None, None, 900.0)
    now[0] = 1300.0
    merge = parser.consume("Pass 2 completed: 1/1 file(s) successful, 4 subtitles total")
    assert render(merge.stage) == "合并字幕 · Qwen 为主" and merge.pass_number is None
    assert [(p.state, p.finished_seconds) for p in merge.passes] == [("done", 900.0), ("done", 1300.0)]


def test_single_pass_reports_one_running_pass(parser):
    start_generation(parser)
    progress = parser.consume("[DecoupledPipeline] Generating scene 1/3 (10.0s audio)...")
    assert render(progress.stage) == "生成识别文本"
    assert [(p.pass_number, p.state, p.step, p.current_scene) for p in progress.passes] == [
        (1, "running", 4, 1)
    ]


def test_internal_retry_resets_counts_without_changing_ensemble_pass(parser):
    start_generation(parser)
    parser.consume("[DecoupledPipeline] Generating scene 3/3 (10.0s audio)...")
    retry = parser.consume(
        "[DecoupledPipeline] Step-down: 1/3 scenes collapsed, "
        "retrying with tighter framing (2.0s max group)"
    )
    assert render(retry.stage) == "内部重试 · 准备重试"
    assert retry.total_scenes is None
    assert retry.pass_number == 1
    parser.consume("[DecoupledPipeline] Step 1: Framing 1 scenes (reframe override=2.0s)")
    next_scene = parser.consume("[DecoupledPipeline] Generating scene 1/1 (10.0s audio)...")
    assert (next_scene.completed_scenes, next_scene.total_scenes) == (0, 1)
    assert "内部重试" in render(next_scene.stage)
    finished = parser.consume("Phase 6: Generating scene SRT files")
    assert "内部重试" not in render(finished.stage)
    assert finished.total_scenes is None


def test_loading_model_restores_generation_and_ignores_unrelated_logs(parser):
    start_generation(parser)
    loading = parser.consume("Loading Qwen3-ASR model (text-only, no aligner)...")
    assert render(loading.stage) == "加载识别模型"
    assert parser.consume("download: 5/10 files") is None
    assert parser.consume("[Qwen3TextGenerator] Model loaded (text-only)") is None
    loaded = parser.consume("  Loaded (text-only) in 3.0s")
    assert render(loaded.stage) == "生成识别文本"
    colored = parser.consume(
        "\x1b[32m[DecoupledPipeline] Generating scene 1/3 (10.0s audio)...\x1b[0m"
    )
    assert colored.current_scene == 1


@pytest.mark.parametrize("line", [
    "[DecoupledPipeline] Generating scene 0/3 (10.0s audio)...",
    "[DecoupledPipeline] Generating scene 4/3 (10.0s audio)...",
    "[DecoupledPipeline] Generating scene 1/0 (10.0s audio)...",
    "[DecoupledPipeline] Generating scene ?/? (10.0s audio)...",
    "[OtherLibrary] Scene 1/3: 20 chars",
    "[DecoupledPipeline] Step 1: Complete — 3 scenes, 100 total frames",
])
def test_unknown_or_invalid_counters_do_not_invent_progress(parser, line):
    start_generation(parser)
    parser.consume(line)
    assert parser.snapshot().total_scenes is None


def test_restarted_scene_sequence_does_not_keep_previous_completion(parser):
    start_generation(parser)
    parser.consume("[DecoupledPipeline]   Scene 3/3: 12 chars")
    restarted = parser.consume("[DecoupledPipeline] Generating scene 1/3 (10.0s audio)...")
    assert restarted.completed_scenes == 0


def test_parallel_pass_two_finishing_first_does_not_start_merge():
    parser = WhisperJavProgressParser("Qwen/test", True, ANIME_MODEL, clock=lambda: 60.0)
    parser.consume("[pass1] [DecoupledPipeline] Steps 2-4: Generating + cleaning text for 3 scenes")
    parser.consume("[pass2] Phase 8: Sanitizing")
    early = parser.consume("Pass 2 worker finished")
    assert render(early.stage) == "第 1/2 遍 · Qwen3-ASR"
    assert [p.state for p in early.passes] == ["running", "done"]
    merge = parser.consume("Pass 1 completed: 1/1 file(s) successful, 9 subtitles total")
    assert render(merge.stage) == "合并字幕 · Qwen 为主"
    # WhisperJAV's own late pass 2 line changes nothing.
    assert parser.consume("Pass 2 completed: 1/1 file(s) successful, 4 subtitles total") is None
