"""单机影片修复流水线：GPU 修复当前影片时，网络同时预取下一部、回传上一部。

- 修复（Jasna）同一台机器同时只有一个，由 :class:`MachineLock` 保证；
  本实例持有锁直到既没有正在修复、也没有预取好的影片。
- 修复开始后，每分钟尝试预取一次下一部（``claim?prefetch=1``）；其他机器还有空闲的影片修复 Worker 时，
  主控会拒绝预取，让任务交给空闲机器。最多预取 1 部。
- 修复完成后结果在后台回传，同时可以开始下一部；未完成的回传最多 2 个，超出时先等回传。
- 与同机字幕识别互斥使用 GPU（:class:`GpuLock`，字幕优先）：本机有字幕任务在跑时不领新影片；
  下载好的影片等字幕实例全部空闲后才开始修复，修复期间持独占锁，字幕实例暂停领取。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..i18n import clip, msg
from ..models import RestoreProgress
from .config import WorkerConfig
from .gpu_lock import GpuLock
from .jasna import kill_orphaned_jasna, probe_nvenc, supported_models
from .restore_task import MachineLock, RestoreCancelled, RestoreTask, cleanup_stale_dirs

logger = logging.getLogger(__name__)

MAX_PENDING_UPLOADS = 2
PREFETCH_RETRY_SECONDS = 60
GPU_RETRY_SECONDS = 5


@dataclass(eq=False)
class PipelineJob:
    claim: dict[str, Any]
    task: RestoreTask = field(init=False)
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: RestoreProgress | None = None
    download: asyncio.Task | None = None
    upload: asyncio.Task | None = None

    @property
    def id(self) -> str:
        return self.claim["job_id"]


class RestorePipeline:
    def __init__(
        self,
        config: WorkerConfig,
        client: httpx.AsyncClient,
        *,
        report_error: Callable[[str, Exception], Awaitable[None]],
        active: bool = True,
        gpu: GpuLock | None = None,
    ) -> None:
        # 只有影片修复服务（--role restore）启用；字幕服务中保持空闲，不声明也不领取影片修复任务。
        self.active = active
        self.gpu = gpu or GpuLock(None)
        self.config = config
        self.client = client
        self.report_error = report_error
        self.lock = MachineLock(config.restore_dir)
        self.lock_held = False
        self.jobs: dict[str, PipelineJob] = {}
        self.processing: PipelineJob | None = None
        self.prefetched: PipelineJob | None = None
        self.claim_available = True
        # 启动时检查 GPU 视频编码器；检查前和检查失败时都不领取影片修复任务。
        self.encoder_checked = False
        self.encoder_error: str | None = None


    @property
    def models(self) -> list[str]:
        """向主控声明的影片修复模型；编码器不可用时为空，Worker 只做字幕识别。"""
        if not self.active or not self.encoder_checked or self.encoder_error:
            return []
        return supported_models(self.config)

    @property
    def unavailable_reason(self) -> str | None:
        """已启用影片修复但本次不可用的原因；未启用影片修复时为 None。"""
        if self.active and self.encoder_error:
            return msg("worker.restore.nvenc", encoder_error=clip(self.encoder_error, 500))
        return None

    @property
    def enabled(self) -> bool:
        return self.claim_available and bool(self.models)

    async def recover_orphans(self) -> list[int]:
        """Worker 启动时停止上次被强制结束后遗留的 Jasna，避免两个 Jasna 同时占用显卡。"""
        if not self.active or not supported_models(self.config) or not self._acquire_lock():
            return []  # 未启用影片修复，或同机另一个 Worker 实例正在影片修复（它的 Jasna 不是遗留进程）
        try:
            return await asyncio.to_thread(kill_orphaned_jasna, self.config.restore_dir)
        finally:
            self._release_lock_if_idle()

    async def check_encoder(self) -> bool:
        """检查本机能否运行 Jasna（需要 NVENC 保存视频）。未启用影片修复时直接返回 False。"""
        if not self.active or not supported_models(self.config):
            self.encoder_checked, self.encoder_error = True, None
            return False
        self.encoder_error = await asyncio.to_thread(probe_nvenc, self.config)
        self.encoder_checked = True
        if self.encoder_error:
            logger.warning(
                "显卡的视频编码功能（NVENC）用不了，本次不启用影片修复，只处理字幕识别任务：%s。"
                "常见原因：容器未开放视频能力（NVIDIA_DRIVER_CAPABILITIES=all）或宿主机限制了编码器，"
                "可联系平台或更换机器；重启这台机器上的程序时会重新检查。",
                self.encoder_error,
            )
            return False
        logger.info("GPU 视频编码器（NVENC）检查通过。")
        return True

    @property
    def pipelined(self) -> bool:
        return self.config.jasna.pipeline

    @property
    def current(self) -> PipelineJob | None:
        """Worker 心跳中的 current_job_id：GPU 正在处理的任务。"""
        return self.processing

    def reports(self) -> list[dict[str, Any]]:
        return [
            {"job_id": job.id, "progress": job.progress.model_dump() if job.progress else None}
            for job in self.jobs.values()
        ]

    def stop(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            job = self.jobs.get(job_id)
            if job is not None and not job.cancel.is_set():
                logger.info("影片修复任务 %s 已取消或已超时，停止处理（本地文件保留以便续传）。", job_id)
                job.cancel.set()

    def _acquire_lock(self) -> bool:
        if not self.lock_held:
            self.lock_held = self.lock.acquire()
        return self.lock_held

    def _release_lock_if_idle(self) -> None:
        if self.lock_held and self.processing is None and self.prefetched is None:
            self.lock.release()
            self.lock_held = False

    def _add(self, claim: dict[str, Any]) -> PipelineJob:
        job = PipelineJob(claim)

        def report(progress: RestoreProgress) -> None:
            job.progress = progress

        job.task = RestoreTask(
            self.config, self.client, claim, report=report, should_cancel=job.cancel.is_set
        )
        self.jobs[job.id] = job
        return job

    def _drop(self, job: PipelineJob) -> None:
        self.jobs.pop(job.id, None)
        if self.prefetched is job:
            self.prefetched = None
        self._release_lock_if_idle()


    async def _claim(self, *, prefetch: bool) -> dict[str, Any] | None:
        params = {"prefetch": "1"} if prefetch else None
        response = await self.client.post(
            f"/api/workers/{self.config.name}/restore/claim", params=params
        )
        if response.status_code in {404, 405}:
            logger.warning("主程序版本较旧，不支持影片修复任务，这台机器只处理字幕识别任务。")
            self.claim_available = False
            return None
        response.raise_for_status()
        return response.json() or None


    async def run_once(self) -> bool:
        """修复一部影片（优先用预取好的）。返回是否处理了任务。"""
        if not self.enabled:
            return False
        job = self.prefetched
        if job is None:
            if not self.gpu.is_free():
                return False  # 本机字幕任务正在使用 GPU：字幕优先，暂不领取新影片
            if not self._acquire_lock():
                return False  # 同机另一个 Worker 实例正在影片修复
            try:
                claim = await self._claim(prefetch=False)
            except BaseException:
                self._release_lock_if_idle()
                raise
            if claim is None:
                self._release_lock_if_idle()
                return False
            job = self._add(claim)
        self.prefetched = None
        self.processing = job
        logger.info(
            "开始影片修复任务 %s（%s / %s）。", job.id, job.claim["restoration_model"], job.claim["codec"]
        )
        prefetcher: asyncio.Task | None = None
        metadata: dict[str, Any] | None = None
        try:
            await self._wait_for_upload_slot(job)
            if job.download is not None:
                if not await job.download:
                    return True  # 预取下载失败，已上报并移除
            else:
                await job.task.download()
            await self._wait_for_gpu(job)
            if self.pipelined:
                prefetcher = asyncio.create_task(self._prefetch_loop(), name="restore-prefetch")
            metadata = await job.task.process()
        except RestoreCancelled:
            self._drop(job)
            return True
        except asyncio.CancelledError:
            self._drop(job)
            raise
        except Exception as exc:  # noqa: BLE001 - 单个任务失败上报后继续
            await self.report_error(job.id, exc)
            self._drop(job)
            return True
        finally:
            self.gpu.release()
            if prefetcher is not None:
                prefetcher.cancel()
                await asyncio.gather(prefetcher, return_exceptions=True)
            if metadata is None or self.pipelined:
                self.processing = None
                self._release_lock_if_idle()
        if self.pipelined:
            job.upload = asyncio.create_task(self._upload(job, metadata), name=f"restore-upload-{job.id}")
        else:
            try:
                await self._upload(job, metadata)
            finally:
                self.processing = None
                self._release_lock_if_idle()
        return True

    async def _wait_for_gpu(self, job: PipelineJob) -> None:
        """等本机字幕实例全部空闲后独占 GPU；等待期间任务仍在租约内，进度显示为排队。"""
        waiting = False
        while True:
            if job.cancel.is_set():
                raise RestoreCancelled(msg("worker.restore.cancelledBefore"))
            if self.gpu.try_exclusive():
                return
            if not waiting:
                waiting = True
                logger.info("本机字幕识别正在使用 GPU，影片修复任务 %s 等待字幕任务结束。", job.id)
            job.progress = RestoreProgress(stage="queued", detail=msg("worker.restore.waitSubtitle"))
            await asyncio.sleep(GPU_RETRY_SECONDS)

    async def _wait_for_upload_slot(self, job: PipelineJob) -> None:
        while True:
            pending = [other.upload for other in self.jobs.values() if other.upload and not other.upload.done()]
            if len(pending) < MAX_PENDING_UPLOADS:
                return
            job.progress = RestoreProgress(stage="queued", detail=msg("worker.restore.waitUpload"))
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    async def _prefetch_loop(self) -> None:
        while self.prefetched is None and self.enabled:
            try:
                claim = await self._claim(prefetch=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("提前下载下一部影片修复任务失败，稍后重试。", exc_info=True)
                claim = None
            if claim is not None:
                job = self._add(claim)
                self.prefetched = job
                logger.info("提前下载影片修复任务 %s：在当前影片修复期间下载原片。", job.id)
                # 下载任务独立于预取循环，当前修复结束后继续进行。
                job.download = asyncio.create_task(self._download(job), name=f"restore-download-{job.id}")
                return
            await asyncio.sleep(PREFETCH_RETRY_SECONDS)

    async def _download(self, job: PipelineJob) -> bool:
        try:
            await job.task.download()
        except RestoreCancelled:
            self._drop(job)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self.report_error(job.id, exc)
            self._drop(job)
            return False
        job.progress = RestoreProgress(stage="queued", detail=msg("worker.restore.waitGpu"))
        return True

    async def _upload(self, job: PipelineJob, metadata: dict[str, Any]) -> None:
        try:
            await job.task.upload(metadata)
            shutil.rmtree(job.task.job_dir, ignore_errors=True)
            logger.info("影片修复任务 %s 已传回，主程序将检查并保存。", job.id)
        except RestoreCancelled:
            logger.info("影片修复任务 %s 已停止传回，本地结果保留以便续传。", job.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self.report_error(job.id, exc)
        finally:
            self._drop(job)
            await asyncio.to_thread(
                cleanup_stale_dirs,
                self.config.restore_dir,
                self.config.jasna.keep_partial_hours,
                {job.task.job_dir.name for job in self.jobs.values()},
            )

    async def drain(self) -> None:
        """等待后台下载与回传结束（--once 退出前使用）。"""
        tasks = [task for job in self.jobs.values() for task in (job.download, job.upload) if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel.set()
        tasks = [task for job in self.jobs.values() for task in (job.download, job.upload) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.processing = None
        self.prefetched = None
        self.gpu.release()
        if self.lock_held:
            self.lock.release()
            self.lock_held = False
