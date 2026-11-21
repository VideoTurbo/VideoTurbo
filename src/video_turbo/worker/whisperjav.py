"""Run the pinned WhisperJAV CLI in its own Python environment."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .. import languages
from ..environment import getenv
from ..i18n import msg
from ..media import duration_from_probe, run_ffprobe
from ..models import Cue, WhisperJavProgress
from ..subtitles import parse_srt
from .asr import TranscriptionCancelled
from .config import WorkerConfig
from .gpu import query_gpu, select_device
from .whisperjav_launcher import FIRERED_VAD_GPU_ENV, PARALLEL_PASSES_ENV
from .whisperjav_progress import WhisperJavProgressParser

# tqdm redraws with bare carriage returns; treat them as line breaks for parsing.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

logger = logging.getLogger(__name__)
WHISPERJAV_VERSION = "1.9.5"
ANIME_MODEL = "litagin/anime-whisper"
FIRERED_VAD_REPO = "FireRedTeam/FireRedVAD"
# Qwen3-ASR (~5 GB) and anime-whisper (~2 GB) plus two CUDA contexts at once.
PARALLEL_PASSES_MIN_MEMORY_MB = 12000
THREAD_ENV_KEYS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)


def available_cpus(cgroup_root: Path = Path("/sys/fs/cgroup")) -> float:
    """CPUs this process may use, honouring the cgroup quota os.cpu_count ignores."""
    try:
        cpus = float(len(os.sched_getaffinity(0)))
    except AttributeError:  # macOS and Windows
        cpus = float(os.cpu_count() or 1)
    try:
        quota, period = (cgroup_root / "cpu.max").read_text().split()[:2]
    except (OSError, ValueError):
        try:  # cgroup v1
            quota = (cgroup_root / "cpu" / "cpu.cfs_quota_us").read_text().strip()
            period = (cgroup_root / "cpu" / "cpu.cfs_period_us").read_text().strip()
        except OSError:
            return cpus
    try:
        if quota not in {"max", "-1"}:
            cpus = min(cpus, int(quota) / int(period))
    except (ValueError, ZeroDivisionError):
        pass
    return cpus


def worker_count() -> int:
    """Workers sharing this host, as passed by the multi-Worker launch scripts."""
    try:
        return max(1, int(getenv("VIDEO_TURBO_WORKER_COUNT", "1")))
    except ValueError:
        return 1


def cpu_thread_budget() -> int:
    return max(1, int(available_cpus() / worker_count()))


def hf_repo_cached(cache: Path, repo_id: str) -> bool:
    """Whether the hub cache holds a finished download of repo_id's main revision."""
    repo = cache / ("models--" + repo_id.replace("/", "--"))
    try:
        commit = (repo / "refs" / "main").read_text().strip()
    except OSError:
        return False
    # exists() follows snapshot symlinks, so a dangling link to a missing blob fails.
    files = [path for path in (repo / "snapshots" / commit).rglob("*") if not path.is_dir()]
    return (
        bool(files)
        and all(path.exists() for path in files)
        and not any((repo / "blobs").glob("*.incomplete"))
    )


def stop_process_tree(process: subprocess.Popen) -> None:
    """Reap the isolated process group, including model worker children."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # SIGKILL the whole group even if its leader exited on SIGTERM.
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class WhisperJavTranscriber:
    def __init__(
        self,
        config: WorkerConfig,
        stage_callback: Callable[[str], None] | None = None,
        scene_callback: Callable[[WhisperJavProgress], None] | None = None,
        elapsed_clock: Callable[[], float | None] | None = None,
    ):
        self.config = config
        # The Worker's recognition elapsed time, so pass start/finish share its origin.
        self.elapsed_clock = elapsed_clock
        self.stage_callback = stage_callback
        self.scene_callback = scene_callback
        self._reported_stage: str | None = None
        self.device: str | None = None
        self.memory_total_mb: int | None = None
        self.ensemble = False
        # 由 claim 的 language 设置（语言表的 asr_code）；双遍只用于日语。
        self.language = "ja"
        self.artifacts: dict[str, str] | None = None

    def _report_progress(self, progress: WhisperJavProgress) -> None:
        """Optional observers must never interrupt recognition or result validation."""
        if progress.stage != self._reported_stage:
            self._reported_stage = progress.stage
            logger.info("WhisperJAV：%s", progress.stage)
            if self.stage_callback:
                try:
                    self.stage_callback(progress.stage)
                except Exception:
                    logger.debug("WhisperJAV 阶段回调失败。", exc_info=True)
        if self.scene_callback:
            try:
                self.scene_callback(progress)
            except Exception:
                logger.debug("WhisperJAV 场景进度回调失败。", exc_info=True)

    def load(self) -> None:
        python = self.config.whisperjav.python_executable
        if python is None or not python.is_file():
            raise RuntimeError(msg("worker.whisperjav.notInstalled"))
        if self.device is None:
            hardware = query_gpu(self.config.device_index, self.config.device)
            self.device = select_device(
                self.config.device, {hardware.device_type, "cpu"}, {"cuda", "cpu"}
            )
            self.memory_total_mb = hardware.memory_total_mb
            logger.info(
                "语音识别每个进程使用 %d 个 CPU 线程（可用 %.1f 核，同一台机器 %d 个服务）。",
                cpu_thread_budget(), available_cpus(), worker_count(),
            )

    def _uncached_models(self) -> list[str]:
        """Models this job would fetch from the hub; empty means it can run offline."""
        options = self.config.whisperjav
        cache = self.config.asr.download_root / "huggingface"
        needed = [options.model] + ([ANIME_MODEL] if self.ensemble else [])
        for segmenter in dict.fromkeys([options.speech_segmenter] + (["ten"] if self.ensemble else [])):
            if segmenter == "firered-vad":
                needed.append(FIRERED_VAD_REPO)
            elif segmenter != "ten":  # TEN VAD ships inside the package
                # Other segmenters pin their own revisions; stay online for them.
                needed.append(segmenter)
        return [name for name in needed if not Path(name).is_dir() and not hf_repo_cached(cache, name)]

    def _environment(self, offline: bool = False) -> dict[str, str]:
        env = dict(os.environ)
        # Do not leak the faster-whisper environment's native libraries/import paths.
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LD_LIBRARY_PATH"):
            env.pop(key, None)
        env["CUDA_VISIBLE_DEVICES"] = str(self.config.device_index) if self.device == "cuda" else ""
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # Keep the launcher's directory off sys.path; whisperjav.py there would
        # shadow the upstream package in the CLI and its spawned passes.
        env["PYTHONSAFEPATH"] = "1"
        env["HF_HUB_CACHE"] = str(self.config.asr.download_root / "huggingface")
        env[FIRERED_VAD_GPU_ENV] = "1" if self._firered_vad_gpu() else "0"
        env[PARALLEL_PASSES_ENV] = "1" if self._parallel_passes() else "0"
        # Native pools size themselves from the host's cores, not a container's
        # CPU quota; oversubscribing the quota throttles the thread feeding the GPU.
        threads = str(cpu_thread_budget())
        for key in THREAD_ENV_KEYS:
            env.setdefault(key, threads)
        if offline:
            # Each online load revalidates ~25 files against the hub one request at a
            # time; behind a slow mirror that alone takes from seconds to minutes.
            env.setdefault("HF_HUB_OFFLINE", "1")
        return env

    def _firered_vad_gpu(self) -> bool:
        return self.device == "cuda" and self.config.whisperjav.firered_vad_gpu

    def _parallel_passes(self) -> bool:
        return (
            self.ensemble
            and self.device == "cuda"
            and self.config.whisperjav.parallel_passes
            and (self.memory_total_mb or 0) >= PARALLEL_PASSES_MIN_MEMORY_MB
        )

    def _vad_check(self, segmenter: str) -> list[str]:
        options = ["--check", "vad", "--segmenter", segmenter]
        if segmenter == "firered-vad" and self._firered_vad_gpu():
            options.append("--use-gpu")
        return options

    def self_test(self, cancel_check: Callable[[], bool] | None = None) -> None:
        self.load()
        steps = [
            (msg("worker.whisperjav.check.pass1", model=self.config.whisperjav.model), ["--check", "qwen", "--model", self.config.whisperjav.model]),
            (msg("worker.whisperjav.check.vad1", speech_segmenter=self.config.whisperjav.speech_segmenter), self._vad_check(self.config.whisperjav.speech_segmenter)),
        ]
        # 桌面版只检查已下载的模型：没下载第二遍模型（ensemble 为 False）时跳过第二遍和合并。
        if self.ensemble or not self.config.asr.local_models_only:
            steps.append((msg("worker.whisperjav.check.pass2", ANIME_MODEL=ANIME_MODEL), ["--check", "anime", "--model", ANIME_MODEL]))
            if self.config.whisperjav.speech_segmenter != "ten":
                steps.append((msg("worker.whisperjav.check.vad2"), self._vad_check("ten")))
            steps.append((msg("worker.whisperjav.check.merge"), ["--check", "merge"]))
        logger.info("WhisperJAV 双遍自检：将提前下载缺失模型，串行加载检查后释放。")
        logger.info("Hugging Face 模型缓存：%s", self.config.asr.download_root / "huggingface")
        with tempfile.TemporaryDirectory(prefix="whisperjav-test-") as directory:
            for index, (label, options) in enumerate(steps, 1):
                if cancel_check and cancel_check():
                    raise TranscriptionCancelled(msg("worker.whisperjav.checkCancelled"))
                logger.info("WhisperJAV 自检 [%d/%d]：%s", index, len(steps), label)
                try:
                    self._run(
                        [
                            str(self.config.whisperjav.python_executable),
                            # Exclude this directory's whisperjav.py from import
                            # search paths; it would shadow the upstream package.
                            "-I", "-X", "utf8",
                            str(Path(__file__).with_name("whisperjav_self_test.py")),
                            "--expected-version", WHISPERJAV_VERSION,
                            "--device", self.device or "cpu",
                            *options,
                        ],
                        Path(directory),
                        cancel_check,
                        echo=True,
                    )
                except TranscriptionCancelled:
                    raise
                except RuntimeError as exc:
                    raise RuntimeError(msg("worker.whisperjav.checkFailed", label=label, exc=exc)) from exc
                logger.info("WhisperJAV 自检 [%d/%d] 通过：%s", index, len(steps), label)
        logger.info("WhisperJAV 双遍模型、VAD 和字幕合并自检全部通过。")

    def _command(self, audio: Path, output: Path, temp: Path) -> list[str]:
        options = self.config.whisperjav
        command = [
            str(options.python_executable),
            str(Path(__file__).with_name("whisperjav_launcher.py")),
            str(audio),
            "--mode",
            "qwen",
            "--device",
            self.device or "cpu",
            "--qwen-device",
            self.device or "cpu",
            "--qwen-generator",
            "qwen3",
            "--qwen-model-id",
            options.model,
            "--qwen-dtype",
            "float16" if self.device == "cuda" else "float32",
            "--qwen-attn",
            "sdpa",
            "--qwen-scene",
            "semantic",
            "--qwen-segmenter",
            options.speech_segmenter,
            "--qwen-sensitivity",
            options.sensitivity,
            "--qwen-enhancer",
            "none",
            "--qwen-timestamp-mode",
            "vad_only",
            "--qwen-regroup",
            "off",
            "--qwen-max-group-duration",
            str(min(3.0, self.config.asr.max_cue_duration_seconds)),
            "--qwen-max-speech-duration",
            str(min(4.0, self.config.asr.max_cue_duration_seconds)),
            "--output-dir",
            str(output),
            "--temp-dir",
            str(temp),
            "--output-format",
            "srt",
            "--fail-on",
            "empty",
            "--fail-on",
            "suspect",
            "--debug",
            "--log-level",
            "INFO",
            "--accept-cpu-mode",
            "--qwen-language",
            str(languages.from_asr_code(self.language).asr_name),
        ]
        if self.ensemble:
            common = {
                "device": self.device or "cpu",
                "dtype": "float16" if self.device == "cuda" else "float32",
                "attn_implementation": "sdpa",
                "timestamp_mode": "vad_only",
                "aligner_backend": "none",
                "regroup_mode": "off",
                "max_group_duration": min(3.0, self.config.asr.max_cue_duration_seconds),
                "max_speech_duration": min(4.0, self.config.asr.max_cue_duration_seconds),
            }
            command += ["--ensemble", "--ensemble-serial", "--keep-temp", "--merge-strategy", "pass1_primary"]
            for number, model, generator, segmenter, sensitivity in (
                (1, options.model, "qwen3", options.speech_segmenter, options.sensitivity),
                (2, ANIME_MODEL, "anime-whisper", "ten", "aggressive"),
            ):
                params = {**common, "generator_backend": generator}
                if number == 2:
                    params.update(assembly_cleaner=False, stepdown=False)
                command += [
                    f"--pass{number}-pipeline", "qwen",
                    f"--pass{number}-model", model,
                    f"--pass{number}-scene-detector", "semantic",
                    f"--pass{number}-speech-segmenter", segmenter,
                    f"--pass{number}-speech-enhancer", "none",
                    f"--pass{number}-sensitivity", sensitivity,
                    f"--pass{number}-qwen-params", json.dumps(params),
                ]
        return command

    def _run(
        self,
        command: list[str],
        work: Path,
        cancel_check: Callable[[], bool] | None,
        offline: bool = False,
        echo: bool = False,
    ) -> None:
        """Run WhisperJAV, parsing progress from its combined output.

        With ``echo`` the raw output (including tqdm's carriage-return updates)
        is mirrored to stderr, so self-tests show model download progress live.
        """
        tail: deque[str] = deque(maxlen=12)
        parser = WhisperJavProgressParser(
            self.config.whisperjav.model, self.ensemble, ANIME_MODEL, self.elapsed_clock
        )
        started = time.monotonic()
        with (work / "whisperjav.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                cwd=work,
                env=self._environment(offline),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            def consume(line: str) -> None:
                line = line.strip()
                if line:
                    tail.append(line[-1000:])
                try:
                    progress = parser.consume(line)
                except Exception:
                    logger.debug("无法解析 WhisperJAV 进度。", exc_info=True)
                    return
                if progress:
                    self._report_progress(progress)

            try:
                # newline="" keeps raw "\r" for echoing; lines still split on \r and \n.
                with (work / "whisperjav.log").open(
                    encoding="utf-8", errors="replace", newline=""
                ) as reader:
                    pending = ""
                    while True:
                        if cancel_check and cancel_check():
                            raise TranscriptionCancelled(msg("worker.whisperjav.cancelled"))
                        if time.monotonic() - started > self.config.whisperjav.timeout_seconds:
                            raise RuntimeError(msg("worker.whisperjav.unresponsive"))
                        # Observe exit before draining so final messages cannot land
                        # between reaching EOF and deciding to stop reading.
                        exited = process.poll() is not None
                        chunk = reader.read()
                        if chunk:
                            if echo:
                                sys.stderr.write(chunk)
                                sys.stderr.flush()
                            *lines, pending = _LINE_BREAK.split(pending + chunk)
                            for line in lines:
                                consume(line)
                        if exited:
                            if pending:
                                consume(pending)
                            break
                        time.sleep(0.2)
                if process.returncode:
                    raise RuntimeError(
                        msg("worker.whisperjav.exited", returncode=process.returncode) + "\n".join(tail)
                    )
            finally:
                # Also remove orphan children after a native-library crash.
                if os.name != "nt" or process.poll() is None:
                    stop_process_tree(process)

    def transcribe(
        self,
        audio_path: Path,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[list[Cue], dict]:
        self.load()
        self.artifacts = None
        if cancel_check and cancel_check():
            raise TranscriptionCancelled(msg("worker.cancelledByServer"))
        self._reported_stage = None
        self._report_progress(
            WhisperJavProgressParser(
                self.config.whisperjav.model, self.ensemble, ANIME_MODEL, self.elapsed_clock
            ).snapshot()
        )
        with tempfile.TemporaryDirectory(prefix="whisperjav-", dir=audio_path.parent) as directory:
            work = Path(directory).resolve()
            output, temp = work / "output", work / "temp"
            output.mkdir()
            temp.mkdir()
            uncached = self._uncached_models()
            if uncached:
                logger.info("WhisperJAV 模型缓存不完整（%s），本次在线加载。", "、".join(uncached))
            else:
                logger.info("WhisperJAV 模型均已缓存，本次离线加载。")
            if self._parallel_passes():
                logger.info("WhisperJAV 双遍并行运行。")
            elif self.ensemble and self.config.whisperjav.parallel_passes and self.device == "cuda":
                logger.info(
                    "显存 %s MB 低于 %d MB，两遍识别依次运行。",
                    self.memory_total_mb, PARALLEL_PASSES_MIN_MEMORY_MB,
                )
            try:
                self._run(
                    self._command(audio_path.resolve(), output, temp),
                    work,
                    cancel_check,
                    offline=not uncached,
                )
            except RuntimeError as exc:
                # The CLI's exit tail only says a pass failed; its reason is in the summary.
                detail = self._ensemble_failure_detail(temp) if self.ensemble else ""
                if detail:
                    raise RuntimeError(f"{detail}\n{exc}") from exc
                raise
            if cancel_check and cancel_check():
                raise TranscriptionCancelled(msg("worker.cancelledSubmit"))
            manifest = json.loads((output / "whisperjav_run.json").read_text(encoding="utf-8"))
            if manifest.get("whisperjav_version") != WHISPERJAV_VERSION:
                raise RuntimeError(
                    msg("worker.whisperjav.versionNeeded", WHISPERJAV_VERSION=WHISPERJAV_VERSION)
                )
            files = manifest.get("files", [])
            if (
                manifest.get("exit_status") != 0
                or len(files) != 1
                or files[0].get("state") != "done"
            ):
                raise RuntimeError(
                    msg("worker.whisperjav.untrusted") + json.dumps(files, ensure_ascii=False)
                )
            srt_path = self._output_path(files[0]["output"], output, work)
            cues = parse_srt(srt_path)
            if not cues:
                raise RuntimeError(msg("worker.whisperjav.noDialogue"))
            if self.ensemble:
                return self._ensemble_result(audio_path, output, temp, work, srt_path, cues)
            # --debug retains the decoded duration, rather than guessing from the last cue.
            master = json.loads(
                (temp / f"{audio_path.stem}_master.json").read_text(encoding="utf-8")
            )
            duration = float(master["duration_seconds"])
            if not math.isfinite(duration) or duration <= 0:
                raise RuntimeError(msg("worker.whisperjav.noDuration"))
            metadata = {
                "asr_engine": "whisperjav",
                "asr_mode": "whisperjav",
                "device": self.device,
                "whisperjav_version": WHISPERJAV_VERSION,
                "model": self.config.whisperjav.model,
                "pipeline": "qwen",
                "speech_segmenter": self.config.whisperjav.speech_segmenter,
                "sensitivity": self.config.whisperjav.sensitivity,
                "timestamp_mode": "vad_only",
                "speech_enhancer": "none",
                "ensemble": False,
                "duration_seconds": duration,
                "quality_metrics": master.get("summary", {}).get("quality_metrics", {}),
                "degradations": master.get("summary", {}).get("degradations", []),
            }
            return cues, metadata

    @staticmethod
    def _ensemble_failure_detail(temp: Path) -> str:
        """Each failed pass's own error, as recorded in the ensemble run summary."""
        details = []
        for summary in sorted(temp.glob("ensemble_summary_*.json")):
            try:
                rows = json.loads(summary.read_text(encoding="utf-8")).get("files") or []
            except (OSError, ValueError, AttributeError):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for key, label in (("pass1", msg("worker.whisperjav.pass1")), ("pass2", msg("worker.whisperjav.pass2")), ("merge", msg("worker.whisperjav.merge"))):
                    info = row.get(key)
                    # A merge skipped because a pass failed adds nothing to that pass's error.
                    if not isinstance(info, dict) or info.get("status") in {"completed", "skipped"}:
                        continue
                    # Upstream stores whole tracebacks; the exception is on the last lines.
                    lines = str(info.get("error") or info.get("status")).strip().splitlines()
                    details.append(
                        msg("worker.whisperjav.stepFailed", label=label) + "\n".join(line[-500:] for line in lines[-4:])
                    )
        return "\n".join(details)

    @staticmethod
    def _output_path(value: str, output: Path, work: Path) -> Path:
        path = Path(value)
        path = (path if path.is_absolute() else work / path).resolve()
        if not path.is_relative_to(output) or not path.is_file():
            raise RuntimeError(msg("worker.whisperjav.badPath"))
        return path

    def _ensemble_result(
        self, audio: Path, output: Path, temp: Path, work: Path, merged: Path, cues: list[Cue]
    ) -> tuple[list[Cue], dict]:
        summaries = list(temp.glob("ensemble_summary_*.json"))
        if len(summaries) != 1:
            raise RuntimeError(msg("worker.whisperjav.noDualRecord"))
        rows = json.loads(summaries[0].read_text(encoding="utf-8")).get("files", [])
        if len(rows) != 1:
            raise RuntimeError(msg("worker.whisperjav.badDualFiles"))
        row = rows[0]
        merge = row.get("merge") or {}
        if (
            row.get("status") != "completed"
            or merge.get("status") != "completed"
            or merge.get("strategy") != "pass1_primary"
            or any((row.get(key) or {}).get("status") != "completed" for key in ("pass1", "pass2"))
        ):
            detail = self._ensemble_failure_detail(temp)
            raise RuntimeError(
                msg("worker.whisperjav.onePassOnly")
                + (f"\n{detail}" if detail else "")
            )
        if self._output_path(merge["output_path"], output, work) != merged:
            raise RuntimeError(msg("worker.whisperjav.mergeMismatch"))
        artifacts = {}
        passes = []
        for number, model, segmenter in (
            (1, self.config.whisperjav.model, self.config.whisperjav.speech_segmenter),
            (2, ANIME_MODEL, "ten"),
        ):
            info = row[f"pass{number}"]
            path = self._output_path(info["srt_path"], output, work)
            original = parse_srt(path)
            if not original:
                raise RuntimeError(msg("worker.whisperjav.passNoDialogue", number=number))
            artifacts[f"pass{number}"] = path.read_text(encoding="utf-8-sig")
            passes.append({
                "pass": number, "model": model, "speech_segmenter": segmenter,
                "sensitivity": self.config.whisperjav.sensitivity if number == 1 else "aggressive",
                "cue_count": len(original), "processing_time": info.get("processing_time"),
            })
        artifacts["merged"] = merged.read_text(encoding="utf-8-sig")
        # Upstream 1.9.5's ensemble Qwen worker does not save the per-pass master
        # metadata even with --debug. Probe the input instead of guessing its end.
        duration = duration_from_probe(run_ffprobe(audio, timeout=30))
        if duration is None:
            raise RuntimeError(msg("worker.whisperjav.dualNoDuration"))
        self.artifacts = artifacts
        return cues, {
            "asr_engine": "whisperjav", "asr_mode": "whisperjav",
            "device": self.device, "whisperjav_version": WHISPERJAV_VERSION,
            "model": self.config.whisperjav.model, "pipeline": "qwen",
            "ensemble": True, "merge_strategy": "pass1_primary",
            "timestamp_mode": "vad_only", "speech_enhancer": "none",
            "passes": passes, "merge_statistics": merge.get("statistics", {}),
            "duration_seconds": duration,
        }
