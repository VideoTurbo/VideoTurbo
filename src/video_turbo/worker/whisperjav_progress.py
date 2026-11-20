"""Read the pinned CLI's existing messages without changing its execution."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from ..i18n import msg
from ..models import WhisperJavPassProgress, WhisperJavProgress

PHASES = {
    1: msg("worker.whisperjav.stage.extract"), 2: msg("worker.whisperjav.stage.scenes"), 3: msg("worker.whisperjav.stage.prepare"), 4: msg("worker.whisperjav.stage.vad"),
    5: msg("worker.whisperjav.stage.recognize"), 6: msg("worker.whisperjav.stage.subtitles"), 7: msg("worker.whisperjav.stage.stitch"), 8: msg("worker.whisperjav.stage.cleanup"), 9: msg("worker.whisperjav.stage.analyze"),
}
# The dashboard's five steps: extract, split, detect speech, recognize, subtitles.
PHASE_STEPS = {1: 1, 2: 2, 3: 2, 4: 3, 5: 4, 6: 5, 7: 5, 8: 5, 9: 5}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Added by the launcher to every record of an ensemble pass worker.
_PASS_TAG = re.compile(r"\[pass([12])\] ")


class _PassProgress:
    """Stage and scene counters of one pipeline run."""

    def __init__(self, number: int, model: str, stage: str):
        self.number = number
        self.model = model
        self.state = "waiting"
        self.started: float | None = None
        self.finished: float | None = None
        self.phase: int | None = None
        self.stage = stage
        self.scope: str | None = None
        self.retrying = False
        self.resume_stage: str | None = None
        self.completed: int | None = None
        self.total: int | None = None
        self.current: int | None = None
        self.last_index: int | None = None

    def label(self) -> str:
        return (msg("worker.whisperjav.stage.retryPrefix") if self.retrying else "") + self.stage

    def start(self, now: float) -> None:
        if self.state == "waiting":
            self.state, self.started = "running", now

    def finish(self, now: float) -> None:
        if self.state != "done":
            self.state, self.finished = "done", now
            self.retrying = False
            self.completed = self.total = self.current = None

    def snapshot(self) -> WhisperJavPassProgress:
        step = PHASE_STEPS.get(self.phase) if self.phase else None
        if self.phase == 5 and self.scope == "framing":
            step = 3  # Framing reruns VAD before any text is generated.
        running = self.state == "running"
        return WhisperJavPassProgress(
            pass_number=self.number,
            model=self.model,
            state=self.state,
            stage=self.label() if running else None,
            step=step if running else None,
            completed_scenes=self.completed if running else None,
            total_scenes=self.total if running else None,
            current_scene=self.current if running else None,
            started_seconds=self.started,
            finished_seconds=self.finished,
        )

    def _stage(self, stage: str, scope: str | None = None) -> None:
        if stage != self.stage or scope != self.scope:
            self.completed = self.total = self.current = None
            self.last_index = None
        self.stage, self.scope = stage, scope

    def _count(self, index: int, total: int, *, starting: bool = False) -> None:
        if not 1 <= index <= total:
            return
        # A new total or a restarted sequence must not inherit an older count.
        if self.total != total or (self.last_index and index < self.last_index):
            self.completed = None
        self.total = total
        self.completed = index - 1 if starting else max(self.completed or 0, index)
        self.current = index if starting else None
        self.last_index = index

    def consume(self, line: str) -> None:
        phase = re.search(r"Phase ([1-9]):", line)
        if phase and int(phase[1]) != self.phase:
            self.phase = int(phase[1])
            self.retrying = False
            self.resume_stage = None
            self._stage(PHASES[self.phase])

        # Match only the known pipeline messages, not unrelated library counters.
        if self.phase == 4 and (match := re.search(r"Phase 4: Scene (\d+)/(\d+) —", line)):
            self._count(int(match[1]), int(match[2]))
        if "[DecoupledPipeline]" in line and self.phase == 5:
            if "Step-down:" in line and "retrying with tighter framing" in line:
                self.retrying = True
                self._stage(msg("worker.whisperjav.stage.retrying"), "retry")
            elif "Step 1: Framing " in line:
                self._stage(msg("worker.whisperjav.stage.segments"), "framing")
            elif "Steps 2-4: Generating + cleaning text" in line:
                self._stage(msg("worker.whisperjav.stage.text"), "generation")
            elif match := re.search(r"Generating scene (\d+)/(\d+) \(", line):
                self._stage(msg("worker.whisperjav.stage.text"), "generation")
                self._count(int(match[1]), int(match[2]), starting=True)
            elif self.scope == "generation" and (match := re.search(
                r"\[DecoupledPipeline\]\s+Scene (\d+)/(\d+): (?:\d+ chars|empty)", line
            )):
                self._count(int(match[1]), int(match[2]))
            elif re.search(r"\[DecoupledPipeline\] Cleaning \d+ scenes", line):
                self._stage(msg("worker.whisperjav.stage.cleanText"), "cleaning")
            elif "Steps 5-7: Aligning " in line:
                self._stage(msg("worker.whisperjav.stage.align"), "alignment")
            elif match := re.search(r"Aligning scene (\d+)/(\d+) \(", line):
                self._stage(msg("worker.whisperjav.stage.align"), "alignment")
                self._count(int(match[1]), int(match[2]), starting=True)
            elif "Step 9: Reconstructing " in line:
                self._stage(msg("worker.whisperjav.stage.rebuild"), "reconstruction")
            elif self.scope == "reconstruction" and (match := re.search(
                r"\[DecoupledPipeline\] Scene (\d+)/(\d+): \d+ words", line
            )):
                self._count(int(match[1]), int(match[2]))

        if self.phase == 5:
            if "Loading Qwen3-ASR model" in line or "[AnimeWhisperGenerator] Loading model" in line:
                self.resume_stage = self.stage
                self._stage(msg("worker.whisperjav.stage.loadModel"), "loading")
            elif self.scope == "loading" and (
                "Loaded (text-only) in " in line or "[AnimeWhisperGenerator] Model loaded" in line
            ):
                self._stage(self.resume_stage or msg("worker.whisperjav.stage.recognize"))
                self.resume_stage = None


class WhisperJavProgressParser:
    def __init__(
        self,
        model: str,
        ensemble: bool,
        anime_model: str,
        clock: Callable[[], float | None] | None = None,
    ):
        self.ensemble = ensemble
        self.passes = {1: _PassProgress(1, model, msg("worker.whisperjav.stage.prepareModel"))}
        if ensemble:
            self.passes[2] = _PassProgress(2, anime_model, msg("worker.whisperjav.stage.prepareModel"))
        else:
            self.passes[1].start(0.0)
        # Untagged pipeline lines belong to this pass: the only one when not an
        # ensemble, or the one announced by the multi-file serial ensemble path.
        self.default_pass: int | None = None if ensemble else 1
        self.merging = False
        origin = time.monotonic()
        self._clock = clock or (lambda: time.monotonic() - origin)

    def _now(self) -> float:
        now = self._clock()
        return round(max(0.0, now), 1) if now is not None else 0.0

    def snapshot(self) -> WhisperJavProgress:
        passes = [self.passes[n].snapshot() for n in sorted(self.passes)]
        total_passes = 2 if self.ensemble else 1
        if self.merging:
            return WhisperJavProgress(
                stage=msg("worker.whisperjav.stage.mergeQwen"), total_passes=total_passes, passes=passes
            )
        running = [p for p in self.passes.values() if p.state == "running"]
        running.sort(key=lambda p: p.number)
        if not running:
            return WhisperJavProgress(
                stage=msg("worker.whisperjav.stage.prepareModel"), total_passes=total_passes, passes=passes
            )
        # Pass 1 decides when a parallel ensemble finishes, so it leads the flat fields.
        lead = running[0]
        if self.ensemble:
            stage = " ｜ ".join(msg("worker.whisperjav.stage.passOf", number=p.number, label=p.label()) for p in running)
        else:
            stage = lead.label()
        return WhisperJavProgress(
            stage=stage,
            model=lead.model,
            pass_number=lead.number,
            total_passes=total_passes,
            completed_scenes=lead.completed,
            total_scenes=lead.total,
            current_scene=lead.current,
            passes=passes,
        )

    def _start(self, number: int) -> _PassProgress:
        state = self.passes[number]
        if state.state == "waiting":
            state.stage = "Qwen3-ASR" if number == 1 else "anime-whisper"
            state.start(self._now())
        return state

    def consume(self, line: str) -> WhisperJavProgress | None:
        before = self.snapshot()
        line = _ANSI.sub("", line)
        tag = _PASS_TAG.search(line) if self.ensemble else None
        if tag:
            number = int(tag[1])
            if number == 2 and self.passes[1].state == "waiting":
                self._start(1)  # Pass 2 logged first, so both passes run in parallel.
            self._start(number).consume(line[tag.end():])
        elif self.ensemble and (pass_start := re.search(r"— Pass ([12]) \(", line)):
            number = int(pass_start[1])
            for earlier in range(1, number):
                self.passes[earlier].finish(self._now())
            self._start(number)
            self.default_pass = number
        elif self.ensemble and (done := re.search(r"\bPass ([12]) (?:completed:|worker finished)", line)):
            self.passes[int(done[1])].finish(self._now())
            # The single-file path merges once both passes end; it logs no merge line.
            self.merging = all(state.state == "done" for state in self.passes.values())
        elif self.ensemble and "— Merge (" in line:
            for state in self.passes.values():
                state.finish(self._now())
            self.merging = True
        elif self.default_pass is not None:
            self.passes[self.default_pass].consume(line)
        after = self.snapshot()
        return after if after != before else None
