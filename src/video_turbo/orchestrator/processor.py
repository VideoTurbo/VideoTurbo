from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .. import languages
from ..config import AppConfig, MediaLibraryConfig
from ..database import Database
from ..i18n import msg
from ..job_states import PAUSABLE, REPROCESSABLE
from ..job_store import SourceSelection
from ..languages import LanguagePair
from ..list_status import JOB_STATUS, JOB_STEPS, SKIPPED_NOTE
from ..media import (
    MediaError,
    ProbeDecision,
    SubtitleFileCache,
    choose_processing_source,
    convert_text_subtitle,
    decide_without_probe,
    duration_from_probe,
    extract_audio,
    extract_text_subtitle,
    is_settled,
    output_subtitle_path,
    run_ffprobe,
    select_source_for_video,
    subtitle_files_in,
)
from ..media_sources.base import MediaInput, MediaSource
from ..models import AsrResult, Cue, JobState, SourceKind, WhisperJavProgress, utc_now_iso
from ..naming import job_dir as cache_job_dir
from ..security import sha256_file
from ..source_paths import text as path_text
from ..subtitles import (
    parse_srt,
    render_srt,
    repair_minor_overlaps,
    validate_cues,
)
from ..translation import (
    TranslationError,
    TranslationMetrics,
    TranslationNotConfigured,
    TranslationUpstreamPool,
    probe_translation_responses,
    translate_cues,
)
from .sources import MediaSources, SourceUnavailable


def job_actions(job: dict[str, Any], *, skipped: bool, downloadable: bool) -> list[str]:
    """界面上这条任务能做的操作；界面只按这份列表显示按钮。"""
    state = JobState(job["state"])
    scanned = job.get("source_kind") == SourceKind.NONE
    actions: list[str] = []
    if state == JobState.PENDING:
        actions += ["start", "skip"]
    elif skipped:
        actions.append("start")
    if state in PAUSABLE:
        actions += ["pause", "cancel"]
    elif state == JobState.PAUSED:
        actions += ["resume", "cancel"]
    retryable = state == JobState.FAILED and not scanned
    if retryable:
        actions.append("retry")
    if retryable or JOB_STATUS[state] in {"queued", "running", "paused"}:
        actions.append("priority")
    if job.get("can_mark_completed"):
        actions.append("complete")
    if state in REPROCESSABLE and not scanned and not skipped:
        actions.append("reprocess")
    if downloadable:
        actions.append("download")
    return actions

logger = logging.getLogger(__name__)


class ReprocessError(RuntimeError):
    """重新处理的前置检查失败，原因可直接显示给用户。"""


def library_pair(library: MediaLibraryConfig) -> LanguagePair:
    return LanguagePair(library.source_language, library.target_language)


def job_pair(job: dict[str, Any]) -> LanguagePair:
    return LanguagePair(str(job["source_language"]), str(job["target_language"]))


def media_target(source: MediaSource, path: str | Path) -> Path | MediaInput:
    """ffprobe / ffmpeg 的输入：本地来源直接用路径，网盘来源用地址和请求头。"""
    return source.media_input(path_text(path)) if source.remote else Path(str(path))


def source_subtitle_files(source: MediaSource, directory: Path) -> list[Path]:
    try:
        return subtitle_files_in(source.list_dir(path_text(directory)))
    except FileNotFoundError:
        return []


class Orchestrator:
    def __init__(self, config: AppConfig, database: Database, cache_dir: Path):
        self.config = config
        self.database = database
        # 唯一的缓存根目录：subtitles/<片名>-<短ID>/ 放字幕中间文件，thumbnails/ 放海报缩略图。
        self.cache_dir = cache_dir
        self.subtitle_dir = cache_dir / "subtitles"
        self.thumbnail_dir = cache_dir / "thumbnails"
        self.subtitle_dir.mkdir(parents=True, exist_ok=True)
        self._job_dirs: dict[str, Path] = {}
        self.scan_lock = asyncio.Lock()
        self.scan_status = "idle"
        self.last_scan: dict = {}
        self.pause_event = threading.Event()
        self.scan_cancel = threading.Event()
        self.translation_tasks: dict[str, asyncio.Task[None]] = {}
        # 按媒体库打开来源；数据库判断媒体库里的文件是否存在也经过它。
        self.sources = MediaSources(database, config)
        database.files = self.sources
        # 字幕发布后调用，通知媒体服务器刷新（integrations.MediaRefresher.request），由应用启动时设置。
        self.on_published: Callable[[], None] = lambda: None
        self.translation_metrics: dict[str, TranslationMetrics] = {}
        self.translation_request_semaphore = asyncio.Semaphore(config.translation.concurrency)
        self._build_pools(config)
        self.translation_http_client: httpx.AsyncClient | None = None
        # 热更新后被替换的 HTTP 客户端；进行中的请求仍在使用，关闭主控时统一释放。
        self._retired_http_clients: list[httpx.AsyncClient] = []
        if self.database.is_pipeline_paused():
            self.pause_event.set()

    def _build_pools(self, config: AppConfig) -> None:
        database = self.database
        self.translation_upstream_pool = (
            TranslationUpstreamPool(
                config.translation,
                saved_states=database.translation_upstream_states(),
                state_changed=database.set_translation_upstream_states,
            )
            if config.translation.upstreams
            else None
        )
        fallback_pool_config = config.translation.fallback_pool_config()
        self.translation_fallback_upstream_pool = (
            TranslationUpstreamPool(
                fallback_pool_config,
                saved_states=database.translation_upstream_states(),
                state_changed=database.set_translation_upstream_states,
            )
            if fallback_pool_config is not None
            else None
        )

    def apply_config(self, config: AppConfig) -> None:
        """控制台保存配置后热更新：新任务和新批次使用新节点池，进行中的批次继续用旧对象完成。"""
        previous = self.config
        old_pools = [
            self.translation_upstream_pool,
            self.translation_fallback_upstream_pool,
        ]
        self.config = config
        self.sources.config = config
        # 旧节点池停止写入状态，避免进行中的请求覆盖新节点池读取的记录。
        for pool in old_pools:
            if pool is not None:
                pool.detach_state_persistence()
        self._build_pools(config)
        if config.translation.concurrency != previous.translation.concurrency:
            self.translation_request_semaphore = asyncio.Semaphore(config.translation.concurrency)
        if (
            self.translation_http_client is not None
            and config.translation.request_timeout_seconds
            != previous.translation.request_timeout_seconds
        ):
            self._retired_http_clients.append(self.translation_http_client)
            self.translation_http_client = None

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    def set_paused(self, paused: bool) -> None:
        self.database.set_pipeline_paused(paused)
        if paused:
            self.pause_event.set()
        else:
            self.pause_event.clear()

    def _job_dir(self, job_id: str, job: dict[str, Any] | None = None) -> Path:
        known = self._job_dirs.get(job_id)
        if known is not None and known.is_dir():
            return known
        if job is None:
            job = self.database.get_job(job_id)
        media_path = (job or {}).get("media_path")
        path = cache_job_dir(self.subtitle_dir, Path(str(media_path)).stem if media_path else "", job_id)
        if path.is_dir():
            self._job_dirs[job_id] = path
        return path

    def save_asr_result(self, job: dict[str, Any], result: AsrResult) -> Path:
        """保存机器传回的识别结果，任务进入“识别完成”，由自动处理继续翻译。返回结果文件路径。"""
        job_id = str(job["id"])
        expected_language = languages.get(job["source_language"]).asr_code
        if result.language.lower() != str(expected_language).lower():
            logger.warning(
                "任务 %s 的识别语言为 %s，任务源语言为 %s。",
                job_id,
                result.language,
                expected_language,
            )
        path = self._job_dir(job_id, job) / "result.source.json"
        metadata_path = path.with_name("result.source.metadata.json")
        payloads = [
            (metadata_path, json.dumps(result.metadata, ensure_ascii=False, indent=2, allow_nan=False)),
            (path, json.dumps([cue.model_dump() for cue in result.cues], ensure_ascii=False, indent=2)),
        ]
        artifacts = result.asr_artifacts.model_dump() if result.asr_artifacts else {}
        payloads.extend(
            (path.with_name(f"result.source.{kind}.srt"), content)
            for kind, content in artifacts.items()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        for target, content in payloads:
            temporary = target.with_suffix(target.suffix + ".tmp")
            # 按 GPU 机器回传的原样保存：Windows 上不把换行改成 \r\n。
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary.replace(target)
        if not artifacts:
            for kind in ("pass1", "pass2", "merged"):
                path.with_name(f"result.source.{kind}.srt").unlink(missing_ok=True)
        self.database.set_job_state(
            job_id,
            JobState.ASR_COMPLETED,
            fields={
                "source_result_path": path_text(path),
                "asr_duration_seconds": result.duration_seconds,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )
        return path

    def enrich_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for job in jobs:
            item = dict(job)
            raw_translation_stats = item.pop("translation_stats_json", None)
            if raw_translation_stats:
                try:
                    item["translation_stats"] = json.loads(str(raw_translation_stats))
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.warning("任务 %s 的翻译模型统计无效。", item["id"])
            metrics = self.translation_metrics.get(str(item["id"]))
            if metrics is not None:
                item["translation_progress"] = metrics.snapshot()
                item["translation_stats"] = metrics.model_usage()
            completed = item.get("asr_completed_chunks")
            total = item.get("asr_total_chunks")
            raw_whisperjav_progress = item.pop("asr_whisperjav_progress_json", None)
            worker_job = item.pop("worker_current_job_id", None)
            whisperjav_progress = None
            if (
                raw_whisperjav_progress
                and worker_job == item["id"]
                and item.get("worker_asr_mode") == "whisperjav"
                and item["state"] == JobState.TRANSCRIBING
            ):
                try:
                    whisperjav_progress = WhisperJavProgress.model_validate_json(
                        raw_whisperjav_progress
                    )
                except ValueError:
                    logger.debug("忽略任务 %s 的无效 WhisperJAV 进度。", item["id"])
            if whisperjav_progress is not None:
                progress = whisperjav_progress.model_dump()
                completed_scenes = whisperjav_progress.completed_scenes
                total_scenes = whisperjav_progress.total_scenes
                progress.update(
                    mode="whisperjav",
                    elapsed_seconds=item.get("asr_elapsed_seconds"),
                    percent=round(100 * min(completed_scenes, total_scenes) / total_scenes, 1)
                    if completed_scenes is not None and total_scenes else None,
                )
                item["asr_progress"] = progress
            elif total:
                item["asr_progress"] = {
                    "completed_chunks": completed or 0,
                    "total_chunks": total,
                    "percent": round(100 * (completed or 0) / total, 1),
                    "elapsed_seconds": item.get("asr_elapsed_seconds"),
                    "eta_seconds": item.get("asr_eta_seconds"),
                }
            elif (
                item.get("asr_stage") and item["state"] == JobState.TRANSCRIBING
                and (not raw_whisperjav_progress or worker_job == item["id"])
            ):
                item["asr_progress"] = {
                    "mode": item.get("worker_asr_mode"),
                    "stage": item["asr_stage"],
                    "elapsed_seconds": item.get("asr_elapsed_seconds"),
                }
            pair = job_pair(item)
            item["source_language_label"] = pair.source_language.label
            item["target_language_label"] = pair.target_language.label
            library = self.config.library(item.get("library_id"))
            item["library_name"] = library.name if library else msg("jobs.deletedLibrary")
            published_subtitle = output_subtitle_path(
                Path(str(item["media_path"])), str(item["target_language"]), self.config.subtitles
            )
            item["published_subtitle_path"] = path_text(published_subtitle)
            # 网盘媒体库不在列表里逐个请求网盘，按本任务发布的记录判断。
            item["published_subtitle_exists"] = (
                item.get("subtitle_path") == path_text(published_subtitle)
                if library is not None and not library.is_local
                else published_subtitle.is_file()
            )
            item["can_mark_completed"] = bool(
                item["state"] != JobState.COMPLETED
                and item["published_subtitle_exists"]
            )
            state = JobState(item["state"])
            skipped = bool(
                state == JobState.COMPLETED
                and item.get("last_error") == SKIPPED_NOTE
                and not item.get("subtitle_path")
            )
            subtitle_path = item.get("subtitle_path")
            # 网盘影片的字幕在网盘里，主程序没有文件可下载。
            downloadable = bool(
                state == JobState.COMPLETED
                and subtitle_path
                and library is not None
                and library.is_local
                and Path(str(subtitle_path)).is_file()
            )
            item["status"] = JOB_STATUS[state]
            item["step"] = JOB_STEPS.get(state)
            item["actions"] = job_actions(item, skipped=skipped, downloadable=downloadable)
            enriched.append(item)
        return enriched

    def job_files(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        job_dir = self._job_dir(str(job["id"]), job)
        candidates = {
            "audio": (msg("jobs.file.audio"), job.get("audio_path") or job_dir / "audio.opus"),
            "source": (msg("jobs.file.source"), job_dir / "source.srt"),
            "source_result": (
                msg("jobs.file.sourceJson"),
                job.get("source_result_path") or job_dir / "result.source.json",
            ),
            "asr_metadata": (msg("jobs.file.asrMetadata"), job_dir / "result.source.metadata.json"),
            "asr_pass1": (msg("jobs.file.pass1"), job_dir / "result.source.pass1.srt"),
            "asr_pass2": (msg("jobs.file.pass2"), job_dir / "result.source.pass2.srt"),
            "asr_merged": (msg("jobs.file.merged"), job_dir / "result.source.merged.srt"),
            "target_partial": (msg("jobs.file.partial"), job_dir / "result.target.partial.json"),
            "target_result": (
                msg("jobs.file.target"),
                job.get("target_result_path") or job_dir / "result.target.json",
            ),
            "subtitle": (msg("jobs.file.subtitle"), job.get("subtitle_path") or ""),
        }
        if self.sources.is_remote(job.get("library_id")):
            # 已发布的字幕在网盘里，不能从主程序下载。
            candidates["subtitle"] = (msg("jobs.file.subtitle"), "")
        files: list[dict[str, Any]] = []
        for kind, (label, value) in candidates.items():
            path = Path(str(value)) if value else None
            exists = bool(path and path.is_file())
            files.append(
                {
                    "kind": kind,
                    "label": label,
                    "path": path_text(path) if path else None,
                    "exists": exists,
                    "size": path.stat().st_size if exists and path else None,
                }
            )
        return files

    def job_detail(self, job_id: str) -> dict[str, Any] | None:
        job = self.database.get_job(job_id)
        if job is None:
            return None
        detail = self.enrich_jobs([job])[0]
        detail["files"] = self.job_files(job)
        metadata_path = self._job_dir(job_id) / "result.source.metadata.json"
        if job.get("source_result_path") and metadata_path.is_file():
            try:
                detail["asr_metadata"] = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("任务 %s 的识别后端参数无效。", job_id)
        return detail

    def resolve_job_file(self, job_id: str, kind: str) -> Path | None:
        detail = self.job_detail(job_id)
        if detail is None:
            return None
        match = next((item for item in detail["files"] if item["kind"] == kind), None)
        if not match or not match["exists"] or not match["path"]:
            return None
        return Path(str(match["path"]))

    async def _stop_translation_task(self, job_id: str) -> None:
        task = self.translation_tasks.pop(job_id, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def pause_job(self, job_id: str) -> bool:
        if not self.database.pause_job(job_id):
            return False
        await self._stop_translation_task(job_id)
        return True

    async def cancel_job(self, job_id: str) -> bool:
        if not self.database.cancel_job(job_id):
            return False
        await self._stop_translation_task(job_id)
        return True

    async def complete_jobs_with_existing_subtitles(
        self, job_ids: list[str]
    ) -> tuple[list[str], dict[str, str]]:
        succeeded, invalid = self.database.complete_jobs_with_existing_subtitles(job_ids)
        if invalid:
            return [], invalid
        for job_id in succeeded:
            await self._stop_translation_task(job_id)
            self.translation_metrics.pop(job_id, None)
        return succeeded, {}

    def start_job(self, job_id: str) -> JobState | None:
        return self.database.start_job(
            job_id,
            reusable_source_result_path=self._job_dir(job_id) / "result.source.json",
        )

    def resume_job(self, job_id: str) -> JobState | None:
        return self.database.resume_job(
            job_id,
            reusable_source_result_path=self._job_dir(job_id) / "result.source.json",
        )

    async def apply_job_action(self, job_id: str, action: str) -> bool:
        """批量操作中的单个任务；返回是否成功。“标记为已完成”需要整批校验，见 complete_jobs_with_existing_subtitles。"""
        match action:
            case "start":
                return self.start_job(job_id) is not None
            case "skip":
                return self.database.skip_job(job_id)
            case "pause":
                return await self.pause_job(job_id)
            case "resume":
                return self.resume_job(job_id) is not None
            case "cancel":
                return await self.cancel_job(job_id)
            case "priority_high":
                return self.database.set_job_priority(job_id, 100)
            case "priority_normal":
                return self.database.set_job_priority(job_id, 0)
            case "continue" | "retranslate":
                return self.reprocess_job(job_id, action) is not None
        raise ValueError(f"未知的批量操作：{action}")

    def _reselect_source(self, job: dict[str, Any]) -> SourceSelection | None:
        """“从头处理”按媒体库当前的语言对重新选择来源；语言对未变时沿用原来源。"""
        library = self.config.library(job.get("library_id"))
        if library is None:
            return None
        pair = library_pair(library)
        if pair == job_pair(job):
            return None
        video = Path(str(job["media_path"]))
        try:
            source = self.sources.open(library.source)
        except SourceUnavailable as exc:
            raise ReprocessError(exc.message) from exc
        with source:
            entry = source.stat(path_text(video))
            if entry is None:
                raise ReprocessError(msg("jobs.reprocess.videoMissing"))
            probe = self.database.cached_probe(
                library.id, video, entry.size, entry.mtime_ns
            ) or run_ffprobe(media_target(source, video))
            subtitle_files = source_subtitle_files(source, video.parent)
        decision = select_source_for_video(video, probe, pair, subtitle_files)
        if decision.action != "process" or decision.source_kind is None:
            raise ReprocessError(msg("jobs.reprocess.cannotHandle", reason=decision.reason))
        return SourceSelection(
            pair=pair,
            source_kind=decision.source_kind,
            stream_index=decision.stream_index,
            source_path=path_text(decision.source_path) if decision.source_path else None,
        )

    def reprocess_job(self, job_id: str, mode: str) -> JobState | None:
        job_dir = self._job_dir(job_id)
        reselected = None
        if mode == "full":
            job = self.database.get_job(job_id)
            if job is not None:
                try:
                    reselected = self._reselect_source(job)
                except (OSError, MediaError) as exc:
                    raise ReprocessError(msg("jobs.reprocess.probeFailed", exc=exc)) from exc
        next_state = self.database.reprocess_job(
            job_id,
            mode,
            reusable_source_result_path=job_dir / "result.source.json",
            reselected=reselected,
        )
        if next_state is None or mode == "continue":
            return next_state
        self.translation_metrics.pop(job_id, None)
        cleanup_names = {
            "reasr": [
                "result.source.json",
                "result.source.metadata.json",
                "result.target.partial.json",
                "result.target.json",
            ],
            "full": [
                "audio.opus",
                "source.srt",
                "result.source.json",
                "result.source.metadata.json",
                "result.target.partial.json",
                "result.target.json",
            ],
        }
        if mode in {"reasr", "full"}:
            cleanup_names[mode].extend(f"result.source.{kind}.srt" for kind in ("pass1", "pass2", "merged"))
        for filename in cleanup_names.get(mode, []):
            path = job_dir / filename
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
        return next_state

    def translation_status(self) -> dict[str, Any]:
        translation_metrics = [
            self.translation_metrics[job_id]
            for job_id in self.translation_tasks
            if job_id in self.translation_metrics
        ]
        return {
            "active_jobs": len(translation_metrics),
            "job_concurrency": self.config.translation.job_concurrency,
            "inflight_requests": sum(item.inflight_requests for item in translation_metrics),
            "request_concurrency": self.config.translation.concurrency,
            "request_count": sum(item.request_count for item in translation_metrics),
            "retry_count": sum(item.retry_count for item in translation_metrics),
            "fallback_count": sum(item.fallback_count for item in translation_metrics),
            "primary_available": self.translation_upstream_pool is None
            or self.translation_upstream_pool.has_available_upstream(),
            "upstreams": (
                [
                    {**item, "role": "primary"}
                    for item in self.translation_upstream_pool.snapshot()
                ]
                if self.translation_upstream_pool is not None
                else []
            ),
            "fallback_upstreams": (
                [
                    {**item, "role": "fallback"}
                    for item in self.translation_fallback_upstream_pool.snapshot()
                ]
                if self.translation_fallback_upstream_pool is not None
                else []
            ),
        }

    async def enable_translation_upstream(self, name: str) -> bool:
        for pool in (
            self.translation_upstream_pool,
            self.translation_fallback_upstream_pool,
        ):
            if pool is not None and await pool.enable(name):
                return True
        return False

    async def probe_translation_upstream(self, name: str) -> dict[str, Any] | None:
        for pool in (
            self.translation_upstream_pool,
            self.translation_fallback_upstream_pool,
        ):
            if pool is None:
                continue
            runtime = await pool.acquire_for_probe(name)
            if runtime is None:
                continue
            if self.translation_http_client is None:
                self.translation_http_client = httpx.AsyncClient(
                    timeout=self.config.translation.request_timeout_seconds
                )
            started_at = asyncio.get_running_loop().time()
            try:
                result = await probe_translation_responses(
                    self.translation_http_client, runtime.request_config
                )
            except TranslationError as exc:
                duration = round(asyncio.get_running_loop().time() - started_at, 3)
                error = str(exc)
                await pool.release(runtime, failed=True, error=error)
                await pool.record_probe(
                    runtime,
                    compatible=False,
                    error=error,
                    model=str(runtime.request_config.model),
                    duration_seconds=duration,
                )
                return {
                    "compatible": False,
                    "name": name,
                    "error": error,
                    "duration_seconds": duration,
                }
            await pool.release(runtime)
            await pool.record_probe(
                runtime,
                compatible=True,
                model=str(result.get("model") or runtime.request_config.model),
                structured_output=result.get("structured_output"),
                duration_seconds=result.get("duration_seconds"),
            )
            return {"name": name, **result}
        return None

    def _reap_translation_tasks(self) -> None:
        for job_id, task in list(self.translation_tasks.items()):
            if not task.done():
                continue
            self.translation_tasks.pop(job_id, None)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("后台翻译任务异常退出：%s", job_id)

    def _start_translation_job(self, job: dict[str, Any]) -> bool:
        if len(self.translation_tasks) >= self.config.translation.job_concurrency:
            return False
        job_id = str(job["id"])
        if job_id in self.translation_tasks:
            return False
        if not self.config.translation.ready:
            return self.database.set_job_state(job_id, JobState.WAITING_TRANSLATION)
        primary_pool = self.translation_upstream_pool
        if primary_pool is not None and not primary_pool.has_available_upstream():
            if job.get("state") != JobState.WAITING_TRANSLATION:
                self.database.set_job_state(
                    job_id,
                    JobState.WAITING_TRANSLATION,
                    error=msg("translation.primaryDown"),
                )
            return False
        if not self.database.set_job_state(job_id, JobState.TRANSLATING):
            return False
        self.translation_metrics.pop(job_id, None)
        self.translation_tasks[job_id] = asyncio.create_task(
            self._translate_job(job, state_claimed=True),
            name=f"translate-{job_id}",
        )
        return True

    async def close(self) -> None:
        tasks = list(self.translation_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.translation_tasks.clear()
        if self.translation_http_client is not None:
            await self.translation_http_client.aclose()
            self.translation_http_client = None
        for client in self._retired_http_clients:
            await client.aclose()
        self._retired_http_clients.clear()

    async def scan(self, library_ids: tuple[str, ...] | None = None) -> dict:
        if self.paused:
            return {**self.last_scan, "status": "paused"}
        if self.scan_lock.locked():
            return {"status": "already_running", **self.last_scan}
        async with self.scan_lock:
            self.scan_cancel.clear()
            self.scan_status = "running"
            result = {
                "status": "running",
                "started_at": utc_now_iso(),
                "finished_at": None,
                "current_library": None,
                "current_path": None,
                "libraries": [
                    library.name
                    for library in self.config.media_libraries
                    if library.enabled and (library_ids is None or library.id in library_ids)
                ],
                "scanned": 0,
                # 本次新增或转入该状态的任务数；已在列表里且状态不变的影片不计入。
                "pending": 0,
                "completed": 0,
                "unusable": 0,
                # 文件仍在写入（修改时间未超过稳定时长），本次不处理。
                "unsettled": 0,
                "relocated": 0,
                "errors": [],
            }
            # 扫描线程会持续更新同一个对象，状态接口因此可以展示实时进度。
            self.last_scan = result
            try:
                interrupted = await asyncio.to_thread(self._scan_sync, result, library_ids)
                if interrupted and self.scan_cancel.is_set():
                    result["status"] = "cancelled"
                else:
                    result["status"] = "paused" if interrupted else "completed"
                return result
            except Exception as exc:  # 媒体库根目录等全局错误也应显示在控制台
                logger.exception("媒体库扫描失败。")
                result["status"] = "failed"
                result["errors"].append(
                    {"path": result.get("current_path") or "", "error": str(exc)}
                )
                return result
            finally:
                result["current_library"] = None
                result["current_path"] = None
                result["finished_at"] = utc_now_iso()
                self.scan_status = "idle"

    def cancel_scan(self) -> bool:
        """请求停止正在进行的扫描；已入库的影片保留，未扫到的留待下次。"""
        if not self.scan_lock.locked():
            return False
        self.scan_cancel.set()
        return True

    def _scan_sync(self, result: dict, library_ids: tuple[str, ...] | None = None) -> bool:
        for library in self.config.media_libraries:
            if not library.enabled or (library_ids is not None and library.id not in library_ids):
                continue
            result["current_library"] = library.name
            try:
                source = self.sources.open(library.source)
            except SourceUnavailable as exc:
                result["errors"].append({"path": library.name, "error": exc.message})
                continue
            with source:
                outcome = self._scan_library(library, source, result)
            if outcome == "interrupted":
                return True
            if outcome == "completed":
                self.database.record_library_scan(library.id)
        return False

    def _scan_library(
        self, library: MediaLibraryConfig, source: MediaSource, result: dict
    ) -> str:
        """扫描一个媒体库：completed、interrupted（暂停或停止），或 failed（列目录出错）。"""
        pair = library_pair(library)
        subtitle_cache = SubtitleFileCache(lambda directory: source_subtitle_files(source, directory))
        present_ids: set[int] = set()
        entries = source.walk(library.path, self.config.scan.video_extensions)
        while True:
            # 中途停止时跳过改名合并：没扫到的影片不能当作已消失。
            if self.paused or self.scan_cancel.is_set():
                return "interrupted"
            try:
                entry = next(entries, None)
            except Exception as exc:  # 网盘列目录失败：保留已扫到的，不做改名合并
                logger.exception("列出媒体库 %s 失败。", library.name)
                result["errors"].append({"path": library.name, "error": msg("jobs.scan.listFailed", exc=exc)})
                return "failed"
            if entry is None:
                break
            if self.paused or self.scan_cancel.is_set():
                return "interrupted"
            video = Path(entry.path)
            result["current_path"] = entry.path
            result["scanned"] += 1
            try:
                if not is_settled(entry.mtime_ns, self.config.scan.settle_minutes):
                    result["unsettled"] += 1
                    continue
                subtitle_files = subtitle_cache.files(video.parent)
                early_skip = decide_without_probe(
                    video, self.config.subtitles, pair, subtitle_files
                )
                if early_skip is not None:
                    media_id, _ = self.database.upsert_media(
                        path=entry.path,
                        library_id=library.id,
                        size=entry.size,
                        mtime_ns=entry.mtime_ns,
                        duration=None,
                        probe=None,
                        decision=early_skip.action,
                        reason=early_skip.reason,
                        file_key=entry.file_key,
                    )
                    present_ids.add(media_id)
                    self._record_scan_outcome(
                        media_id, library, video, early_skip, pair, result, subtitle_files
                    )
                    continue
                # 文件大小和修改时间未变时复用上次探测结果，判断规则仍按当前配置重新执行。
                probe = self.database.cached_probe(
                    library.id, entry.path, entry.size, entry.mtime_ns
                ) or run_ffprobe(media_target(source, entry.path))
                decision = choose_processing_source(
                    video, probe, self.config.subtitles, pair, subtitle_files
                )
                media_id, changed = self.database.upsert_media(
                    path=entry.path,
                    library_id=library.id,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    duration=duration_from_probe(
                        probe,
                        stream_index=(
                            decision.stream_index
                            if decision.source_kind == SourceKind.AUDIO
                            else None
                        ),
                    ),
                    probe=probe,
                    decision=decision.action,
                    reason=decision.reason,
                    file_key=entry.file_key,
                )
                present_ids.add(media_id)
                if decision.action == "process" and decision.source_kind:
                    _, created = self.database.create_or_reset_job(
                        media_id=media_id,
                        source_kind=decision.source_kind,
                        source_stream_index=decision.stream_index,
                        source_path=path_text(decision.source_path) if decision.source_path else None,
                        changed=changed,
                        pair=pair,
                    )
                    if created:
                        result["pending"] += 1
                else:
                    self._record_scan_outcome(
                        media_id, library, video, decision, pair, result, subtitle_files
                    )
            except Exception as exc:  # 单个媒体失败不应中断整库扫描
                logger.exception("扫描媒体失败：%s", entry.path)
                result["errors"].append({"path": entry.path, "error": str(exc)})
        self._reconcile_renamed(library, source, present_ids, result)
        return "completed"

    def _record_scan_outcome(
        self,
        media_id: int,
        library: MediaLibraryConfig,
        video: Path,
        decision: ProbeDecision,
        pair: LanguagePair,
        result: dict,
        subtitle_files: list[Path],
    ) -> None:
        """已有目标字幕的影片记为已完成，找不到识别来源的记为失败，都显示在任务列表里。"""
        outcome = "done" if decision.action == "done" else "unusable"
        output = output_subtitle_path(video, pair.target, self.config.subtitles)
        if self.database.record_scan_outcome(
            media_id=media_id,
            outcome=outcome,
            reason=decision.reason,
            pair=pair,
            subtitle_path=path_text(output) if outcome == "done" and output in subtitle_files else None,
        ):
            result["completed" if outcome == "done" else "unusable"] += 1

    def _reconcile_renamed(
        self,
        library: MediaLibraryConfig,
        source: MediaSource,
        present_ids: set[int],
        result: dict,
    ) -> None:
        """整库扫描完成后，把影片修复或手动改名后失联的旧记录合并到新文件上。"""
        try:
            merges = self.database.reconcile_renamed_media(library.id, present_ids)
        except Exception as exc:  # 合并失败不影响本次扫描结果
            logger.exception("合并改名影片记录失败：%s", library.name)
            result["errors"].append({"path": library.name, "error": msg("jobs.scan.mergeFailed", exc=exc)})
            return
        for merge in merges:
            logger.info(
                "影片改名后沿用原记录：%s → %s%s",
                merge["old_path"],
                merge["new_path"],
                f"（字幕已改名：{merge['subtitle_renamed']}）" if merge.get("subtitle_renamed") else "",
            )
        result["relocated"] = result.get("relocated", 0) + len(merges)

    async def process_once(self) -> bool:
        """处理一个不需要 GPU Worker 的流水线阶段。"""
        self._reap_translation_tasks()
        if self.paused:
            return False
        # 优先把 GPU 已完成识别的影片翻译并发布；GPU 可同时领取下一部影片，
        # 避免大量新任务的音频提取让已识别任务长期等待翻译。
        job = self.database.next_job_in_state(JobState.ASR_COMPLETED)
        if job and self._start_translation_job(job):
            return True

        job = self.database.next_job_in_state(JobState.DISCOVERED)
        if job:
            await asyncio.to_thread(self._prepare_job, job)
            return True

        if self.config.translation.ready:
            job = self.database.next_job_in_state(JobState.WAITING_TRANSLATION)
            if job and self._start_translation_job(job):
                return True
        return False

    def _prepare_job(self, job: dict) -> None:
        job_id = str(job["id"])
        work_dir = self._job_dir(job_id, job)
        work_dir.mkdir(parents=True, exist_ok=True)
        if not self.database.set_job_state(job_id, JobState.EXTRACTING):
            return
        try:
            source_kind = SourceKind(job["source_kind"])
            library_id = str(job.get("library_id") or "")
            if source_kind == SourceKind.AUDIO and self.sources.is_remote(library_id):
                # 网盘影片不经过主程序：GPU 机器领取后自己下载并抽音频。
                self.database.set_job_state(
                    job_id, JobState.WAITING_ASR, fields={"fetch_media": 1}
                )
                return
            with self.sources.for_library(library_id) as source:
                self._prepare_from_source(job, source, source_kind, work_dir)
        except SourceUnavailable as exc:
            self.database.set_job_state(job_id, JobState.FAILED, error=exc.message)
        except Exception as exc:
            self.database.set_job_state(job_id, JobState.FAILED, error=str(exc))
            logger.exception("准备任务失败：%s", job_id)

    def _prepare_from_source(
        self, job: dict, source: MediaSource, source_kind: SourceKind, work_dir: Path
    ) -> None:
        job_id = str(job["id"])
        if source_kind == SourceKind.AUDIO:
            audio_path = work_dir / "audio.opus"
            extract_audio(
                media_target(source, job["media_path"]),
                int(job["source_stream_index"]),
                audio_path,
                self.config.audio,
            )
            audio_sha256 = sha256_file(audio_path)
            transitioned = self.database.set_job_state(
                job_id,
                JobState.WAITING_ASR,
                fields={
                    "audio_path": path_text(audio_path),
                    "audio_sha256": audio_sha256,
                    "fetch_media": 0,
                },
            )
            if not transitioned:
                self.database.save_paused_audio(job_id, audio_path, audio_sha256)
            return

        srt_path = work_dir / "source.srt"
        if source_kind == SourceKind.EMBEDDED_TEXT:
            # 网盘影片的内嵌文本字幕由主程序读取（ffmpeg 需要读完整个文件）。
            extract_text_subtitle(
                media_target(source, job["media_path"]),
                int(job["source_stream_index"]),
                srt_path,
            )
        else:
            subtitle = Path(str(job["source_path"]))
            data = source.read_bytes(path_text(subtitle))
            if subtitle.suffix.lower() == ".srt":
                srt_path.write_bytes(data)
            else:
                original = work_dir / f"source.original{subtitle.suffix.lower()}"
                original.write_bytes(data)
                try:
                    convert_text_subtitle(original, srt_path)
                finally:
                    original.unlink(missing_ok=True)
        cues = parse_srt(srt_path, job_pair(job).source_language.legacy_encodings)
        if not cues:
            raise MediaError(msg("jobs.noDialogue"))
        source_result = work_dir / "result.source.json"
        source_result.write_text(
            json.dumps([cue.model_dump() for cue in cues], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.database.set_job_state(
            job_id, JobState.ASR_COMPLETED, fields={"source_result_path": path_text(source_result)}
        )

    async def _validation_duration(self, job: dict) -> float | None:
        """校验字幕时长用的影片时长：容器时长、识别时长与解码音频时长中最长的一个。"""
        job_id = str(job["id"])
        duration_candidates = [
            float(value)
            for value in (job.get("duration"), job.get("asr_duration_seconds"))
            if value is not None
        ]
        if job.get("source_kind") != SourceKind.AUDIO or not job.get("audio_path"):
            return max(duration_candidates, default=None)
        audio_path = Path(str(job["audio_path"]))
        if not audio_path.is_file():
            return max(duration_candidates, default=None)
        try:
            audio_probe = await asyncio.to_thread(run_ffprobe, audio_path)
        except MediaError as exc:
            logger.warning("任务 %s 无法探测音频时长：%s", job_id, exc)
            return max(duration_candidates, default=None)
        audio_duration = duration_from_probe(audio_probe)
        if audio_duration is not None:
            previous_duration = max(duration_candidates, default=None)
            duration_candidates.append(audio_duration)
            if previous_duration is not None and audio_duration > float(previous_duration) + 0.5:
                logger.warning(
                    "任务 %s 的解码音频比容器时长多 %.1f 秒，字幕校验改用音频时长 %.1f 秒。",
                    job_id,
                    audio_duration - float(previous_duration),
                    max(duration_candidates),
                )
        return max(duration_candidates, default=None)

    def _repair_source_overlaps(self, job_id: str, source_result_path: Path, cues: list[Cue]) -> None:
        overlap_repairs = repair_minor_overlaps(cues)
        if not overlap_repairs:
            return
        temporary = source_result_path.with_suffix(source_result_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps([cue.model_dump() for cue in cues], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(source_result_path)
        logger.warning(
            "任务 %s 自动修复 %d 处不超过 200 毫秒的字幕时间重叠。", job_id, len(overlap_repairs)
        )

    async def _publish(
        self,
        job: dict,
        translated: list[Cue],
        metrics: TranslationMetrics,
        target_path: Path,
        temporary_target_path: Path,
        checkpoint_path: Path,
    ) -> None:
        """译文已通过校验：保存结果，把字幕写到影片旁边，任务记为已完成。"""
        job_id = str(job["id"])
        pair = job_pair(job)
        target_layout = pair.target_language.layout
        if not self.database.set_job_state(
            job_id,
            JobState.VALIDATING,
            fields={
                "target_result_path": path_text(target_path),
                "translation_stats_json": json.dumps(
                    metrics.model_usage(), ensure_ascii=False
                ),
            },
        ):
            temporary_target_path.unlink(missing_ok=True)
            return
        temporary_target_path.replace(target_path)
        output = output_subtitle_path(Path(job["media_path"]), pair.target, self.config.subtitles)
        # PUBLISHING 阶段很短且会原子替换字幕文件；成功进入后不再接受取消，
        # 避免“界面显示已取消但文件仍在发布”的竞态。
        if not self.database.set_job_state(job_id, JobState.PUBLISHING):
            return
        managed_subtitle = job.get("subtitle_path")
        may_replace_managed_subtitle = bool(
            managed_subtitle and Path(str(managed_subtitle)) == output
        )
        content = render_srt(translated, self.config.subtitles, target_layout)
        with self.sources.for_library(str(job.get("library_id") or "")) as source:
            await asyncio.to_thread(
                source.write_bytes,
                path_text(output),
                content.encode("utf-8"),
                overwrite=self.config.subtitles.overwrite_existing
                or may_replace_managed_subtitle,
            )
        completed = self.database.set_job_state(
            job_id,
            JobState.COMPLETED,
            fields={"subtitle_path": path_text(output), "reprocess_mode": None},
        )
        if completed:
            self.translation_metrics.pop(job_id, None)
        checkpoint_path.unlink(missing_ok=True)
        self.on_published()

    async def _translate_job(self, job: dict, *, state_claimed: bool = False) -> None:
        job_id = str(job["id"])
        if not self.config.translation.ready:
            self.database.set_job_state(job_id, JobState.WAITING_TRANSLATION)
            return
        try:
            if not state_claimed and not self.database.set_job_state(job_id, JobState.TRANSLATING):
                return
            pair = job_pair(job)
            source_layout = pair.source_language.layout
            target_layout = pair.target_language.layout
            source_result_path = Path(str(job["source_result_path"]))
            payload = json.loads(source_result_path.read_text(encoding="utf-8"))
            cues = [Cue.model_validate(item) for item in payload]
            validation_duration = await self._validation_duration(job)
            self._repair_source_overlaps(job_id, source_result_path, cues)
            source_issues = validate_cues(
                cues, self.config.subtitles, source_layout, validation_duration
            )
            source_errors = [issue for issue in source_issues if issue.severity == "error"]
            if source_errors:
                detail = msg("common.listJoin").join(issue.message for issue in source_errors[:5])
                self.database.set_job_state(
                    job_id,
                    JobState.FAILED,
                    error=msg("jobs.timingMismatch", detail=detail),
                )
                return
            job_dir = self._job_dir(job_id, job)
            checkpoint_path = job_dir / "result.target.partial.json"
            if job.get("reprocess_mode") == "retranslate":
                # 批量提交时保留所有旧翻译；轮到本任务真正开始时才丢弃旧检查点。
                # 完整译文 JSON 和已发布字幕继续保留到新结果成功写入。
                checkpoint_path.unlink(missing_ok=True)
                checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp").unlink(
                    missing_ok=True
                )
                if not self.database.mark_reprocess_started(job_id, "retranslate"):
                    return
            request_config = self.config.translation
            metrics = TranslationMetrics(len(cues), request_config.concurrency)
            self.translation_metrics[job_id] = metrics
            if self.translation_http_client is None:
                self.translation_http_client = httpx.AsyncClient(
                    timeout=self.config.translation.request_timeout_seconds
                )
            translated = await translate_cues(
                cues,
                request_config,
                checkpoint_path=checkpoint_path,
                request_semaphore=self.translation_request_semaphore,
                upstream_pool=self.translation_upstream_pool,
                fallback_upstream_pool=self.translation_fallback_upstream_pool,
                http_client=self.translation_http_client,
                metrics=metrics,
                pair=pair,
            )
            target_path = job_dir / "result.target.json"
            temporary_target_path = target_path.with_suffix(target_path.suffix + ".tmp")
            temporary_target_path.write_text(
                json.dumps([cue.model_dump() for cue in translated], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            issues = validate_cues(
                translated, self.config.subtitles, target_layout, validation_duration
            )
            hard_errors = [issue for issue in issues if issue.severity == "error"]
            if hard_errors:
                temporary_target_path.unlink(missing_ok=True)
                detail = msg("common.listJoin").join(issue.message for issue in hard_errors[:5])
                self.database.set_job_state(job_id, JobState.FAILED, error=detail)
                return
            await self._publish(
                job, translated, metrics, target_path, temporary_target_path, checkpoint_path
            )
        except (TranslationNotConfigured, TranslationError) as exc:
            self.database.set_job_state(job_id, JobState.WAITING_TRANSLATION, error=str(exc))
        except SourceUnavailable as exc:
            self.database.set_job_state(job_id, JobState.FAILED, error=exc.message)
        except FileExistsError as exc:
            # 目标位置已有不归本任务管理的字幕：影片已有字幕，直接记为已完成。
            existing_output = output_subtitle_path(
                Path(job["media_path"]), job_pair(job).target, self.config.subtitles
            )
            self.database.set_job_state(
                job_id,
                JobState.COMPLETED,
                error=str(exc),
                fields={"subtitle_path": path_text(existing_output), "reprocess_mode": None},
            )
        except Exception as exc:
            self.database.set_job_state(job_id, JobState.FAILED, error=str(exc))
            logger.exception("翻译或发布任务失败：%s", job_id)
