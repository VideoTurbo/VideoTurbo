from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .. import __version__, languages
from ..config import AudioConfig
from ..environment import getenv
from ..i18n import LocalizedFormatter, clip, msg, system_locale
from ..media import MediaCancelled, extract_audio
from ..media_sources import loader as source_loader
from ..models import WhisperJavProgress
from ..naming import job_dir as cache_job_dir
from ..protocol import PROTOCOL_HEADER, PROTOCOL_MISMATCH_STATUS, WORKER_PROTOCOL_VERSION
from ..security import sha256_file
from . import model_catalog
from .asr import TranscriptionCancelled
from .config import WorkerConfig, load_worker_config
from .errors import describe_asr_failure
from .gpu import query_gpu, select_device
from .gpu_lock import GpuLock
from .jasna import self_test as jasna_self_test
from .restore_pipeline import RestorePipeline
from .restore_task import cleanup_stale_dirs
from .whisperjav import WHISPERJAV_VERSION, WhisperJavTranscriber

logger = logging.getLogger(__name__)


class ProtocolMismatch(RuntimeError):
    """主程序拒绝了本机的通信协议版本；消息由主程序给出，直接展示给用户。"""


async def _check_protocol(response: httpx.Response) -> None:
    if response.status_code != PROTOCOL_MISMATCH_STATUS:
        return
    await response.aread()
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    raise ProtocolMismatch(detail or msg("worker.protocolMismatch"))

ROLES = ("asr", "restore")


class Worker:
    """一个 Worker 进程只承担一种服务：字幕识别（asr，可同机多实例）或影片修复（restore，每台一个）。

    两种服务在同一台机器上通过 :class:`GpuLock` 串行使用 GPU，字幕优先。
    """

    def __init__(self, config: WorkerConfig, role: str = "asr"):
        if role not in ROLES:
            raise ValueError(f"未知的服务类型：{role}（可选 {'/'.join(ROLES)}）")
        self.role = role
        self.config = config
        self.gpu = GpuLock(config.gpu_lock_file)
        self.current_job_id: str | None = None
        self.cancel_requested = threading.Event()
        self.asr_progress_lock = threading.Lock()
        self.asr_completed_chunks: int | None = None
        self.asr_total_chunks: int | None = None
        self.asr_started_at: float | None = None
        self.asr_mode: str | None = None
        self.asr_stage: str | None = None
        self.asr_whisperjav_progress: WhisperJavProgress | None = None
        self.task_kind: str | None = None
        self.transcriber = WhisperJavTranscriber(
            config, self._update_asr_stage, self._update_whisperjav_progress, self._asr_elapsed
        )
        self.headers = {
            "X-Worker-Token": config.worker_token,
            PROTOCOL_HEADER: str(WORKER_PROTOCOL_VERSION),
        }
        self.client = httpx.AsyncClient(
            base_url=config.server_url.rstrip("/"),
            headers=self.headers,
            timeout=120,
            event_hooks={"response": [_check_protocol]},
        )
        self.restore = RestorePipeline(
            config,
            self.client,
            report_error=self.report_restore_failure,
            active=role == "restore",
            gpu=self.gpu,
        )

    async def close(self) -> None:
        self.cancel_requested.set()
        await self.restore.close()
        self.gpu.release()
        await self.client.aclose()

    @property
    def active_job_id(self) -> str | None:
        """心跳中的当前任务：字幕识别任务，或 GPU 正在处理的影片修复任务。"""
        if self.current_job_id:
            return self.current_job_id
        current = self.restore.current
        return current.id if current else None

    def supported_asr_modes(self) -> list[str]:
        if self.role != "asr":
            return []
        modes = []
        python = self.config.whisperjav.python_executable
        if python and python.is_file():
            marker = self._whisperjav_marker()
            try:
                if json.loads(marker.read_text())["version"] == WHISPERJAV_VERSION:
                    modes.append("whisperjav")
            except (OSError, ValueError, KeyError):
                pass
        if self.config.asr.local_models_only:
            downloaded, _ = model_catalog.supported_modes(self.config)
            modes = [mode for mode in modes if mode in downloaded]
        return modes

    def _query_hardware(self):
        if self.config.device == "auto":
            return query_gpu(self.config.device_index)
        return query_gpu(self.config.device_index, self.config.device)

    def supports_whisperjav_ensemble(self) -> bool:
        python = self.config.whisperjav.python_executable
        if python and "whisperjav" in self.supported_asr_modes():
            try:
                marker = json.loads(self._whisperjav_marker().read_text())
                if self.config.asr.local_models_only and not model_catalog.supported_modes(self.config)[1]:
                    return False
                return marker.get("ensemble") is True
            except (OSError, ValueError):
                pass
        return False

    def _whisperjav_marker(self) -> Path:
        root = self.config.whisperjav.python_executable.parent.parent
        return root / "video-turbo-whisperjav.json"

    def _select_transcriber(self, mode: str) -> None:
        if mode not in self.supported_asr_modes():
            raise RuntimeError(msg("worker.modeMissing", mode=mode))
        hardware = self._query_hardware()
        device = select_device(
            self.config.device, {getattr(hardware, "device_type", "cuda"), "cpu"}, {"cuda", "cpu"}
        )
        self.transcriber.device = device
        self.transcriber.memory_total_mb = getattr(hardware, "memory_total_mb", None)
        logger.info("识别后端 %s 使用设备 %s。", mode, device)
        with self.asr_progress_lock:
            self.asr_mode = mode
            self.asr_stage = msg("worker.stage.preparing")

    def _update_asr_stage(self, stage: str) -> None:
        with self.asr_progress_lock:
            self.asr_stage = stage

    def _asr_elapsed(self) -> float | None:
        started = self.asr_started_at
        return None if started is None else max(0.0, time.monotonic() - started)

    def _update_whisperjav_progress(self, progress: WhisperJavProgress) -> None:
        with self.asr_progress_lock:
            self.asr_stage = progress.stage
            self.asr_whisperjav_progress = progress

    async def register(self) -> None:
        gpu = await asyncio.to_thread(self._query_hardware)
        response = await self.client.post(
            "/api/workers/register",
            json={
                "name": self.config.name,
                "version": __version__,
                "gpu_name": gpu.name,
                "device_type": getattr(gpu, "device_type", "cuda"),
                "driver_version": gpu.driver_version,
                "memory_total_mb": gpu.memory_total_mb,
                "model": self.config.whisperjav.model,
                "compute_type": "float16" if getattr(gpu, "device_type", "cuda") == "cuda" else "float32",
                "supported_asr_modes": self.supported_asr_modes(),
                "supports_whisperjav_ensemble": self.supports_whisperjav_ensemble(),
                "supports_restore": bool(self.restore.models),
                "restore_models": self.restore.models,
                "restore_unavailable_reason": self.restore.unavailable_reason,
                # 有 ffmpeg 才能直接从网盘下载影片抽音频。
                "supports_media_fetch": self.role == "asr" and shutil.which("ffmpeg") is not None,
            },
        )
        response.raise_for_status()
        logger.info("机器 %s（v%s）已接入：%s", self.config.name, __version__, gpu.name)

    async def heartbeat(self, status: str = "online") -> None:
        try:
            gpu = await asyncio.to_thread(self._query_hardware)
            with self.asr_progress_lock:
                completed = self.asr_completed_chunks
                total = self.asr_total_chunks
                whisperjav_progress = (
                    self.asr_whisperjav_progress.model_dump()
                    if self.current_job_id and self.asr_whisperjav_progress is not None
                    else None
                )
                stage = self.asr_stage if self.current_job_id else None
                restore_job = None if self.current_job_id else self.restore.current
                restore = restore_job is not None
                restore_progress = (
                    restore_job.progress.model_dump() if restore_job and restore_job.progress else None
                )
                active_job_id = self.active_job_id
                task_kind = "restore" if restore else (self.task_kind if self.current_job_id else None)
                elapsed = (
                    max(0.0, time.monotonic() - self.asr_started_at)
                    if self.asr_started_at is not None
                    else None
                )
                eta = (
                    elapsed * (total - completed) / completed
                    if elapsed is not None and completed and total and completed < total
                    else 0.0
                    if completed is not None and total and completed >= total
                    else None
                )
            response = await self.client.post(
                f"/api/workers/{self.config.name}/heartbeat",
                json={
                    "gpu_utilization": gpu.utilization,
                    "device_type": getattr(gpu, "device_type", "cuda"),
                    "asr_device": self.transcriber.device if self.current_job_id and not restore else None,
                    "memory_free_mb": gpu.memory_free_mb,
                    "current_job_id": active_job_id,
                    "status": status,
                    "asr_completed_chunks": completed,
                    "asr_total_chunks": total,
                    "asr_elapsed_seconds": elapsed,
                    "asr_eta_seconds": eta,
                    "asr_mode": self.asr_mode if self.current_job_id and not restore else None,
                    "asr_stage": None if restore else stage,
                    "asr_whisperjav_progress": None if restore else whisperjav_progress,
                    "task_kind": task_kind,
                    "restore_progress": restore_progress,
                    "restore_jobs": self.restore.reports() if self.restore.pipelined else [],
                },
            )
            response.raise_for_status()
            heartbeat_result = response.json()
            if heartbeat_result.get("registered") is False:
                # 主控已因离线过久删除本 Worker，重新注册后才会出现在列表里。
                logger.info("主程序没有这台机器的记录，重新接入。")
                await self.register()
            lost = heartbeat_result.get("cancel_requested") or not heartbeat_result.get("lease_renewed")
            if restore and lost:
                self.restore.stop([restore_job.id])
            elif self.current_job_id and lost:
                self.cancel_requested.set()
            self.restore.stop(list(heartbeat_result.get("restore_stopped_job_ids") or []))
        except ProtocolMismatch as exc:
            logger.error("%s", exc)
        except Exception:
            logger.exception("向主程序报告状态失败。")

    async def _heartbeat_loop(self) -> None:
        while True:
            await self.heartbeat("busy" if self.active_job_id else "online")
            interval = self.config.heartbeat_interval_seconds
            # 影片修复的传输和修复进度变化快，缩短心跳间隔让控制台及时刷新。
            if self.restore.jobs:
                interval = min(interval, 10)
            await asyncio.sleep(interval)

    async def report_failure(self, job_id: str, error: Exception) -> None:
        with self.asr_progress_lock:
            stage = self.asr_stage
        description = describe_asr_failure(error, stage)
        logger.error("任务 %s：%s", job_id, description)
        try:
            response = await self.client.post(
                f"/api/jobs/{job_id}/asr-error",
                json={"worker_name": self.config.name, "error": description},
                timeout=10,
            )
            if response.status_code == 409:
                logger.info("任务 %s 已暂停、取消或已重新分配，忽略本次错误上报。", job_id)
                return
            response.raise_for_status()
        except Exception:
            logger.exception("任务 %s 的错误未能上报主程序，具体原因见上面的日志。", job_id)

    async def wait_until_idle(self) -> None:
        if not self.config.idle.enabled:
            return
        passed = 0
        while passed < self.config.idle.consecutive_checks:
            gpu = await asyncio.to_thread(self._query_hardware)
            idle = (
                gpu.utilization <= self.config.idle.max_gpu_utilization
                and gpu.memory_free_mb >= self.config.idle.min_free_memory_mb
            )
            passed = passed + 1 if idle else 0
            if passed < self.config.idle.consecutive_checks:
                await asyncio.sleep(self.config.idle.check_interval_seconds)

    async def report_restore_failure(self, job_id: str, error: Exception) -> None:
        description = clip(f"{error}", 7000, system_locale()) or type(error).__name__
        logger.error("影片修复任务 %s 失败：%s", job_id, description)
        try:
            response = await self.client.post(
                f"/api/restore/jobs/{job_id}/error",
                json={"worker_name": self.config.name, "error": description},
                timeout=10,
            )
            if response.status_code != 409:
                response.raise_for_status()
        except Exception:
            logger.exception("影片修复任务 %s 的错误未能上报主程序。", job_id)

    async def process_claim(self, claim: dict) -> None:
        job_id = claim["job_id"]
        self.task_kind = "asr"
        self.current_job_id = job_id
        self.cancel_requested.clear()
        with self.asr_progress_lock:
            self.asr_completed_chunks = None
            self.asr_total_chunks = None
            self.asr_whisperjav_progress = None
            self.asr_started_at = time.monotonic()
        job_dir = cache_job_dir(self.config.asr_dir, claim.get("title", ""), job_id)
        audio_path = job_dir / "audio.opus"
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            self._update_asr_stage(msg("worker.stage.preparing"))
            self._select_transcriber(claim.get("asr_mode", "whisperjav"))
            language = languages.from_asr_code(str(claim["language"]))
            self.transcriber.language = str(language.asr_code)
            self.transcriber.ensemble = bool(claim.get("whisperjav_ensemble", False))
            if self.transcriber.ensemble and not language.supports_whisperjav_ensemble:
                # anime-whisper 只识别日语，主控不会这样派发；防御性拒绝。
                raise RuntimeError(msg("worker.noDualPass", label=language.label))
            if self.transcriber.ensemble and not self.supports_whisperjav_ensemble():
                raise RuntimeError(msg("worker.dualMissing"))
            if claim.get("media"):
                self._update_asr_stage(msg("worker.stage.fetching"))
                await self._fetch_media_audio(claim["media"], audio_path, str(claim["job_id"]))
            else:
                self._update_asr_stage(msg("worker.stage.downloading"))
                async with self.client.stream("GET", claim["audio_url"]) as response:
                    response.raise_for_status()
                    with audio_path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if self.cancel_requested.is_set():
                                raise TranscriptionCancelled(msg("worker.cancelledDownload"))
                            handle.write(chunk)
                self._update_asr_stage(msg("worker.stage.checking"))
                if sha256_file(audio_path) != claim["audio_sha256"]:
                    raise RuntimeError(msg("worker.audioSha"))
            self._update_asr_stage(msg("worker.stage.recognizing"))
            cues, metadata = await asyncio.to_thread(
                self.transcriber.transcribe,
                audio_path,
                self.cancel_requested.is_set,
                self._update_asr_progress,
            )
            if not cues:
                raise RuntimeError(msg("worker.noDialogue"))
            if self.cancel_requested.is_set():
                raise TranscriptionCancelled(msg("worker.cancelledSubmit"))
            payload = {
                "job_id": job_id,
                "worker_name": self.config.name,
                "language": self.transcriber.language,
                "language_probability": metadata.get("language_probability"),
                "duration_seconds": metadata.get("duration_seconds"),
                "cues": [cue.model_dump() for cue in cues],
                "metadata": metadata,
                "asr_artifacts": getattr(self.transcriber, "artifacts", None),
            }
            self._update_asr_stage(msg("worker.stage.submitting"))
            response = await self.client.post(f"/api/jobs/{job_id}/asr-result", json=payload)
            if response.status_code == 409:
                logger.info("任务 %s 已被取消或已超时，不再提交识别结果。", job_id)
                return
            response.raise_for_status()
            logger.info("任务 %s 识别完成，共 %d 条对白。", job_id, len(cues))
        except TranscriptionCancelled:
            raise
        except Exception as exc:
            await self.report_failure(job_id, exc)
            raise
        finally:
            self.current_job_id = None
            self.task_kind = None
            with self.asr_progress_lock:
                self.asr_completed_chunks = None
                self.asr_total_chunks = None
                self.asr_started_at = None
                self.asr_stage = None
                self.asr_whisperjav_progress = None
            audio_path.unlink(missing_ok=True)
            try:
                job_dir.rmdir()
            except OSError:
                pass

    async def _media_plugin(self, plugin_id: str, sha256: str) -> type:
        """按文件指纹加载媒体库来源插件；本进程已加载过的直接复用。"""
        plugin = source_loader.cached(sha256, plugin_id)
        if plugin is not None:
            return plugin
        response = await self.client.get(f"/api/worker/plugins/{sha256}", timeout=30)
        response.raise_for_status()
        return await asyncio.to_thread(source_loader.load, response.text, sha256, plugin_id)

    async def _fetch_media_audio(self, media: dict, audio_path: Path, job_id: str) -> None:
        """网盘影片：用插件取得下载地址，边下载边提取音轨。下载地址失效时重新取一次。

        插件在这期间保存的状态（例如刷新后的令牌）在结束后回传主程序。
        """
        plugin = await self._media_plugin(media["plugin_id"], media["plugin_sha256"])
        audio = AudioConfig(
            channels=media["channels"],
            sample_rate=media["sample_rate"],
            opus_bitrate=media["opus_bitrate"],
        )
        state = dict(media.get("state") or {})
        changes: dict[str, Any] = {}
        changes_lock = threading.Lock()

        def remember(update: dict[str, Any]) -> None:
            with changes_lock:
                changes.update(update)
                for key, value in update.items():
                    if value is None:
                        state.pop(key, None)
                    else:
                        state[key] = value

        def run() -> None:
            for attempt in range(2):
                with changes_lock:
                    current = dict(state)
                with plugin(dict(media["settings"]), state=current, on_state=remember) as source:
                    target = source.download(media["path"])
                try:
                    extract_audio(
                        target,
                        int(media["stream_index"]),
                        audio_path,
                        audio,
                        timeout=6 * 3600,
                        cancelled=self.cancel_requested.is_set,
                    )
                    return
                except MediaCancelled as exc:
                    raise TranscriptionCancelled(msg("worker.cancelledVideo")) from exc
                except Exception:
                    if attempt == 1:
                        raise
                    logger.warning("从网盘提取音频失败，重新获取下载地址后再试一次。", exc_info=True)

        try:
            await asyncio.to_thread(run)
        finally:
            if changes:
                await self._report_plugin_state(job_id, changes)

    async def _report_plugin_state(self, job_id: str, changes: dict[str, Any]) -> None:
        try:
            response = await self.client.post(
                f"/api/jobs/{job_id}/plugin-state",
                json={"worker_name": self.config.name, "changes": changes},
                timeout=30,
            )
            response.raise_for_status()
        except Exception:
            logger.warning("回传网盘插件状态失败。", exc_info=True)

    def _update_asr_progress(self, completed: int, total: int) -> None:
        with self.asr_progress_lock:
            self.asr_completed_chunks = completed
            self.asr_total_chunks = total

    async def run(self, once: bool = False) -> None:
        if self.role == "restore":
            # 云 GPU 每次分到的机器不一定有可用的视频编码器：注册前检查，失败时不向主控声明影片修复能力。
            stopped = await self.restore.recover_orphans()
            if stopped:
                logger.warning("已停止 %d 个上次遗留的 Jasna 进程，对应任务会从保存的进度继续。", len(stopped))
            await self.restore.check_encoder()
        await self.register()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            if self.role == "restore":
                await self._run_restore(once)
            else:
                await self._run_asr(once)
        finally:
            self.cancel_requested.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _run_asr(self, once: bool) -> None:
        """字幕服务：持共享 GPU 锁领取字幕任务；只要还领得到任务就不放锁（字幕优先）。"""
        waiting_for_restore = False
        while True:
            await self.wait_until_idle()
            if not self.gpu.try_shared():
                if not waiting_for_restore:
                    waiting_for_restore = True
                    logger.info("本机影片修复正在使用 GPU，暂停领取字幕任务，影片修复这一部结束后继续。")
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue
            if waiting_for_restore:
                waiting_for_restore = False
                logger.info("GPU 已空闲，继续领取字幕任务。")
            response = await self.client.post(f"/api/workers/{self.config.name}/claim")
            response.raise_for_status()
            claim = response.json()
            if claim:
                try:
                    await self.process_claim(claim)
                except TranscriptionCancelled:
                    logger.info("语音识别任务 %s 已停止。", claim["job_id"])
                except Exception:
                    logger.exception("处理语音识别任务失败，已尝试向主程序上报具体原因。")
            else:
                self.gpu.release()
            if once:
                return
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _run_restore(self, once: bool) -> None:
        """影片修复服务：本机字幕空闲时逐部修复；每部之间让出 GPU，让排队的字幕任务先跑。"""
        if self.restore.models:
            logger.info(
                "影片修复已启用：%s；%s。",
                "、".join(self.restore.models),
                "提前下载下一部并在后台传回结果" if self.restore.pipelined else "逐部下载、修复、传回",
            )
            await asyncio.to_thread(
                cleanup_stale_dirs, self.config.restore_dir, self.config.jasna.keep_partial_hours
            )
        else:
            logger.warning(
                "本机影片修复不可用，影片修复服务只保持在线以便控制台显示原因：%s",
                self.restore.unavailable_reason or "worker.yaml 中 jasna.enabled 为 false，请先安装影片修复服务。",
            )
        while True:
            processed = False
            try:
                processed = await self.restore.run_once()
            except Exception:
                logger.exception("处理影片修复任务失败，已尝试向主程序上报具体原因。")
            if once:
                await self.restore.drain()
                return
            # 修复完一部后多等一个轮询周期，让同机字幕实例先拿到 GPU。
            delay = self.config.poll_interval_seconds * (2 if processed else 1)
            await asyncio.sleep(delay)


async def _async_main(args: argparse.Namespace) -> None:
    config = load_worker_config(args.config)
    role = args.role
    if args.self_test:
        role = "restore" if args.restore else "asr"
    worker = Worker(config, role)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    # SIGTERM from the Linux supervisor must also stop the external model process.
    if os.name != "nt":
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        if args.self_test and args.restore:
            model = "ltx" if args.ltx else "basicvsrpp" if args.basicvsrpp else None
            await asyncio.to_thread(jasna_self_test, config, model=model)
            return
        if args.self_test:
            gpu = await asyncio.to_thread(worker._query_hardware)
            logger.info(
                "设备自检通过：%s，设备 %s，内存 %d MiB",
                gpu.name,
                gpu.device_type,
                gpu.memory_total_mb,
            )
            modes = worker.supported_asr_modes() if args.asr_mode == "downloaded" else [args.asr_mode]
            if not modes:
                raise RuntimeError(msg("worker.noDownloadedModels"))
            for mode in modes:
                worker._select_transcriber(mode)
                # 桌面版没下载第二遍模型时只检查第一遍。
                worker.transcriber.ensemble = worker.supports_whisperjav_ensemble() or not config.asr.local_models_only
                await asyncio.to_thread(
                    worker.transcriber.self_test, worker.cancel_requested.is_set
                )
                logger.info("%s 识别后端自检通过。", mode)
            return
        await worker.run(once=args.once)
    finally:
        await worker.close()
        if os.name != "nt":
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    parser = argparse.ArgumentParser(description="VideoTurbo Worker")
    parser.add_argument("--version", action="version", version=f"VideoTurbo Worker {__version__}")
    parser.add_argument(
        "--config", default=getenv("VIDEO_TURBO_WORKER_CONFIG", "config/worker.yaml")
    )
    parser.add_argument(
        "--role",
        choices=ROLES,
        default=getenv("VIDEO_TURBO_WORKER_ROLE", "asr") or "asr",
        help="本进程承担的服务：asr 字幕识别（默认），restore 影片修复（仅 Linux NVIDIA）",
    )
    parser.add_argument("--once", action="store_true", help="最多处理一个任务后退出")
    parser.add_argument("--self-test", action="store_true", help="检查所选设备和模型加载")
    parser.add_argument(
        "--asr-mode",
        choices=["whisperjav", "downloaded"],
        default="whisperjav",
        help="自检的识别模式（默认 WhisperJAV）；downloaded：模型已下载的所有模式（桌面版，不下载模型）",
    )
    parser.add_argument("--restore", action="store_true", help="与 --self-test 一起使用：自检 Jasna 影片修复")
    restore_model = parser.add_mutually_exclusive_group()
    restore_model.add_argument(
        "--ltx", action="store_true", help="与 --restore 一起使用：自检 LTX（默认：已启用 LTX 时自检 LTX）"
    )
    restore_model.add_argument(
        "--basicvsrpp", action="store_true", help="与 --restore 一起使用：自检并编译 BasicVSR++"
    )
    parser.add_argument("--log-file", help="将日志以 UTF-8 追加到指定文件")
    args = parser.parse_args()
    handler = (
        logging.FileHandler(args.log_file, mode="a", encoding="utf-8")
        if args.log_file
        else logging.StreamHandler()
    )
    # 传给主程序的报错带多语言标记，由主程序按查看者的语言显示；本机日志按这台机器的语言显示。
    handler.setFormatter(
        LocalizedFormatter("%(asctime)s %(levelname)s: %(message)s", locale=system_locale())
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        asyncio.run(_async_main(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一记录错误后返回非零状态
        logger.error("服务退出：%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
