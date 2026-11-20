"""Worker 端的影片修复任务：断点续传下载原片 → Jasna 修复 → 分块续传回传。

任务目录 ``<缓存目录>/restore/<片名>-<短ID>/`` 在失败或中断后保留，同一任务再次分到本机时：

- ``source.<ext>`` 用 HTTP Range 从已下载位置继续；``source.verified`` 记录校验通过的大小。
- ``jasna.done.json`` 存在时跳过修复，直接回传 ``output.<ext>``。
- 回传前向主控查询已收到的字节数，从该位置继续上传。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from ..i18n import msg
from ..models import RestoreProgress
from ..naming import job_dir as cache_job_dir
from .config import WorkerConfig
from .jasna import JasnaCancelled, run_jasna
from .jasna_progress import JasnaProgress

logger = logging.getLogger(__name__)

RETRYABLE = (httpx.TransportError, httpx.TimeoutException)
MAX_TRANSFER_RETRIES = 30
# 多连接下载的分块大小；每条连接一次下载一块，完成的分块记录在 source.chunks.json。
DOWNLOAD_CHUNK = 32 * 1024 * 1024
PIECE = 1024 * 1024


class _RangeUnsupported(RuntimeError):
    """服务端忽略 Range 请求，只能单连接下载。"""


class _ChunkFailed(RuntimeError):
    """分块不完整或被主控拒绝，可以重试。"""


class RestoreCancelled(RuntimeError):
    pass


class MachineLock:
    """同一台机器同时只跑一个 Jasna：云端常在一张卡上启动多个 Worker 实例。"""

    def __init__(self, directory: Path | None) -> None:
        self.path = directory / ".restore.lock" if directory is not None else None
        self._handle = None

    def acquire(self) -> bool:
        if self.path is None:
            return False
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class TransferMeter:
    """按最近 15 秒的字节数计算速度和剩余时间。"""

    def __init__(self, total: int, done: int = 0, window: float = 15.0) -> None:
        self.total = total
        self.done = done
        self.window = window
        self.started = time.monotonic()
        self.samples: deque[tuple[float, int]] = deque([(self.started, done)])

    def advance(self, amount: int) -> None:
        self.done += amount
        now = time.monotonic()
        self.samples.append((now, self.done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    def reset(self, done: int) -> None:
        self.done = done
        self.samples = deque([(time.monotonic(), done)])

    @property
    def speed(self) -> float | None:
        if len(self.samples) < 2:
            return None
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else None

    def snapshot(self, stage: str, detail: str) -> RestoreProgress:
        speed = self.speed
        remaining = max(0, self.total - self.done)
        return RestoreProgress(
            stage=stage,  # type: ignore[arg-type]
            detail=detail,
            transferred_bytes=self.done,
            total_bytes=self.total,
            bytes_per_second=speed,
            percent=min(100.0, self.done * 100 / self.total) if self.total else None,
            eta_seconds=remaining / speed if speed else None,
            elapsed_seconds=time.monotonic() - self.started,
        )


async def _run_parallel(
    pending: deque[int],
    connections: int,
    work: Callable[[int], Awaitable[None]],
    finished: Callable[[int], None],
    what: str,
) -> None:
    """用 connections 条连接并行处理 pending 中的分块；失败的分块放回队尾重试。"""
    failures = 0

    async def runner() -> None:
        nonlocal failures
        while pending:
            index = pending.popleft()
            try:
                await work(index)
            except (*RETRYABLE, _ChunkFailed) as exc:
                failures += 1
                if failures > MAX_TRANSFER_RETRIES * connections:
                    raise RuntimeError(msg("worker.restoreTask.interrupted", what=what, exc=exc)) from exc
                pending.append(index)
                wait = min(30, 2 ** min(failures, 5))
                logger.warning("%s：分块 %d 中断，%d 秒后重试：%s", what, index, wait, exc)
                await asyncio.sleep(wait)
                continue
            finished(index)

    tasks = [asyncio.create_task(runner()) for _ in range(min(connections, len(pending)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        # 任一连接出错（或任务取消）时停止其余连接。
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _sha256(path: Path, should_cancel: Callable[[], bool]) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            if should_cancel():
                raise RestoreCancelled(msg("worker.restoreTask.cancelledCheck"))
            digest.update(chunk)
    return digest.hexdigest()


def cleanup_stale_dirs(root: Path, keep_hours: int, keep: set[str] = frozenset()) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - keep_hours * 3600
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name in keep or entry.name == "self-test":
            continue
        try:
            newest = max((item.stat().st_mtime for item in entry.rglob("*")), default=entry.stat().st_mtime)
        except OSError:
            continue
        if newest < cutoff:
            logger.info("清理过期的影片修复临时目录：%s", entry)
            shutil.rmtree(entry, ignore_errors=True)


class RestoreTask:
    def __init__(
        self,
        config: WorkerConfig,
        client: httpx.AsyncClient,
        claim: dict[str, Any],
        *,
        report: Callable[[RestoreProgress], None],
        should_cancel: Callable[[], bool],
    ) -> None:
        self.config = config
        self.client = client
        self.claim = claim
        self.report = report
        self.should_cancel = should_cancel
        # Jasna 要求路径只含英文字符，片名里的其他字符替换为下划线。
        self.job_dir = cache_job_dir(
            config.restore_dir, claim.get("title", ""), claim["job_id"], ascii_only=True
        )
        self.source = self.job_dir / f"source.{claim['source_extension'] or 'mp4'}"
        self.output = self.job_dir / f"output.{claim['output_extension']}"
        self.done_marker = self.job_dir / "jasna.done.json"
        self.headers = {"X-Worker-Name": config.name}
        self._last_report = 0.0

    def _check_cancel(self) -> None:
        if self.should_cancel():
            raise RestoreCancelled(msg("worker.jasna.cancelled"))

    async def run(self) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        await self.download()
        metadata = await self.process()
        await self.upload(metadata)
        shutil.rmtree(self.job_dir, ignore_errors=True)


    async def download(self) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        total = int(self.claim["source_size"])
        verified = self.job_dir / "source.verified"
        if verified.is_file() and self.source.is_file() and verified.read_text().strip() == self.claim["source_sha256"]:
            return
        connections = self.config.jasna.download_connections
        if connections > 1 and total > DOWNLOAD_CHUNK:
            try:
                await self._download_parallel(total, connections)
            except _RangeUnsupported:
                logger.warning("主程序不支持分段下载，改用单连接。")
                (self.job_dir / "source.chunks.json").unlink(missing_ok=True)
                self.source.unlink(missing_ok=True)
                await self._download_sequential(total)
        else:
            (self.job_dir / "source.chunks.json").unlink(missing_ok=True)
            await self._download_sequential(total)
        self.report(RestoreProgress(stage="downloading", detail=msg("worker.restoreTask.checkSource"), transferred_bytes=total, total_bytes=total, percent=100))
        digest = await asyncio.to_thread(_sha256, self.source, self.should_cancel)
        if digest != self.claim["source_sha256"]:
            self.source.unlink(missing_ok=True)
            raise RuntimeError(msg("worker.restoreTask.shaMismatch"))
        verified.write_text(digest)

    def _throttled_report(self, meter: TransferMeter, stage: str, detail: str) -> None:
        now = time.monotonic()
        if now - self._last_report >= 2:
            self._last_report = now
            self.report(meter.snapshot(stage, detail))

    async def _download_parallel(self, total: int, connections: int) -> None:
        count = math.ceil(total / DOWNLOAD_CHUNK)
        state_file = self.job_dir / "source.chunks.json"
        done: set[int] = set()
        if state_file.is_file() and self.source.is_file():
            try:
                done = {int(i) for i in json.loads(state_file.read_text())}
            except ValueError:
                done = set()
        with self.source.open("r+b" if self.source.exists() else "wb") as handle:
            handle.truncate(total)
        state_file.write_text(json.dumps(sorted(done)))

        def length(index: int) -> int:
            return min(DOWNLOAD_CHUNK, total - index * DOWNLOAD_CHUNK)

        meter = TransferMeter(total, sum(length(i) for i in done))
        detail = msg("worker.restoreTask.downloading", connections=connections)
        self.report(meter.snapshot("downloading", detail))
        pending = deque(i for i in range(count) if i not in done)
        fd = os.open(self.source, os.O_RDWR)

        async def fetch(index: int) -> None:
            start = index * DOWNLOAD_CHUNK
            end = start + length(index) - 1
            written = 0
            try:
                async with self.client.stream(
                    "GET", self.claim["source_url"],
                    headers={**self.headers, "Range": f"bytes={start}-{end}"},
                    timeout=httpx.Timeout(120, connect=30),
                ) as response:
                    if response.status_code == 200:
                        raise _RangeUnsupported()
                    response.raise_for_status()
                    async for data in response.aiter_bytes(PIECE):
                        self._check_cancel()
                        if written + len(data) > length(index):
                            raise _ChunkFailed(msg("upload.chunkTooLarge"))
                        os.pwrite(fd, data, start + written)
                        written += len(data)
                        meter.advance(len(data))
                        self._throttled_report(meter, "downloading", detail)
                if written != length(index):
                    raise _ChunkFailed(msg("worker.restoreTask.chunkIncomplete", written=written, expected=length(index)))
            except BaseException:
                meter.advance(-written)
                raise

        def finished(index: int) -> None:
            done.add(index)
            state_file.write_text(json.dumps(sorted(done)))

        try:
            await _run_parallel(pending, connections, fetch, finished, msg("worker.restoreTask.download"))
        finally:
            os.close(fd)
        state_file.unlink(missing_ok=True)

    async def _download_sequential(self, total: int) -> None:
        meter = TransferMeter(total, self.source.stat().st_size if self.source.is_file() else 0)
        detail = msg("worker.restoreTask.download")
        self.report(meter.snapshot("downloading", detail))
        failures = 0
        last_report = 0.0
        while True:
            offset = self.source.stat().st_size if self.source.is_file() else 0
            if offset > total:
                self.source.unlink()
                offset = 0
            if offset == total:
                break
            headers = {**self.headers, "Range": f"bytes={offset}-"} if offset else dict(self.headers)
            try:
                async with self.client.stream(
                    "GET", self.claim["source_url"], headers=headers, timeout=httpx.Timeout(120, connect=30)
                ) as response:
                    if response.status_code == 416:
                        self.source.unlink(missing_ok=True)
                        continue
                    response.raise_for_status()
                    mode = "ab" if response.status_code == 206 else "wb"
                    if mode == "wb":
                        meter.reset(0)
                    with self.source.open(mode) as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            self._check_cancel()
                            handle.write(chunk)
                            meter.advance(len(chunk))
                            now = time.monotonic()
                            if now - last_report >= 2:
                                self.report(meter.snapshot("downloading", detail))
                                last_report = now
                failures = 0
            except RETRYABLE as exc:
                failures += 1
                if failures > MAX_TRANSFER_RETRIES:
                    raise RuntimeError(msg("worker.restoreTask.downloadInterrupted", exc=exc)) from exc
                wait = min(60, 2 ** min(failures, 6))
                logger.warning("下载原片中断（第 %d 次），%d 秒后从断点续传：%s", failures, wait, exc)
                self.report(meter.snapshot("downloading", msg("worker.restoreTask.resume", wait=wait)))
                await asyncio.sleep(wait)


    async def process(self) -> dict[str, Any]:
        if self.done_marker.is_file() and self.output.is_file():
            try:
                return json.loads(self.done_marker.read_text(encoding="utf-8"))
            except ValueError:
                pass
        self.output.unlink(missing_ok=True)
        started = time.monotonic()
        state: dict[str, Any] = {"detail": msg("worker.restoreTask.startJasna")}

        def stage(detail: str) -> None:
            state["detail"] = detail
            self.report(RestoreProgress(stage="preparing", detail=detail, elapsed_seconds=0))

        def progress(value: JasnaProgress) -> None:
            self.report(
                RestoreProgress(
                    stage="processing",
                    detail=value.detail,
                    percent=value.percent,
                    fps=value.fps,
                    eta_seconds=value.eta_seconds,
                    elapsed_seconds=time.monotonic() - started,
                )
            )

        try:
            metadata = await asyncio.to_thread(
                run_jasna,
                self.config,
                source=self.source,
                output=self.output,
                working_directory=self.job_dir / "jasna-work",
                restoration_model=self.claim["restoration_model"],
                codec=self.claim["codec"],
                ltx_large_canvas=bool(self.claim.get("ltx_large_canvas")),
                should_cancel=self.should_cancel,
                on_progress=progress,
                on_stage=stage,
            )
        except JasnaCancelled as exc:
            raise RestoreCancelled(str(exc)) from exc
        self.done_marker.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        shutil.rmtree(self.job_dir / "jasna-work", ignore_errors=True)
        return metadata


    async def _upload_status(self) -> dict[str, Any]:
        response = await self.client.get(self.claim["upload_url"], headers=self.headers, timeout=60)
        response.raise_for_status()
        return response.json()

    async def _remote_offset(self) -> int:
        return int((await self._upload_status())["offset"])

    async def upload(self, metadata: dict[str, Any]) -> None:
        total = self.output.stat().st_size
        sha_file = self.job_dir / "output.sha256"
        if sha_file.is_file():
            digest = sha_file.read_text().strip()
        else:
            self.report(RestoreProgress(stage="uploading", detail=msg("worker.restoreTask.checkResult"), transferred_bytes=0, total_bytes=total))
            digest = await asyncio.to_thread(_sha256, self.output, self.should_cancel)
            sha_file.write_text(digest)
        connections = self.config.jasna.upload_connections
        chunk_size = self.config.jasna.upload_chunk_mb * 1024 * 1024
        status = await self._upload_status()
        if connections > 1 and status.get("chunked") and total > chunk_size:
            await self._upload_parallel(total, chunk_size, connections, status)
        else:
            await self._upload_sequential(total, chunk_size)
        self._check_cancel()
        response = await self.client.post(
            f"/api/restore/jobs/{self.claim['job_id']}/complete",
            json={"worker_name": self.config.name, "size": total, "sha256": digest, "metadata": metadata},
            timeout=120,
        )
        if response.status_code == 409:
            raise RestoreCancelled(msg("worker.restoreTask.rejected", response=response.text[:300]))
        response.raise_for_status()

    async def _upload_parallel(
        self, total: int, chunk_size: int, connections: int, status: dict[str, Any]
    ) -> None:
        count = math.ceil(total / chunk_size)

        def length(index: int) -> int:
            return min(chunk_size, total - index * chunk_size)

        same = status.get("chunk_size") == chunk_size and status.get("total") == total
        received = {int(i) for i in status.get("received") or []} if same else set()
        meter = TransferMeter(total, sum(length(i) for i in received))
        detail = msg("worker.restoreTask.uploading", connections=connections)
        self.report(meter.snapshot("uploading", detail))
        url = self.claim["upload_url"] + "/chunk"

        async def put(index: int) -> None:
            start = index * chunk_size
            sent = [0]

            async def body():
                with self.output.open("rb") as handle:
                    handle.seek(start)
                    remaining = length(index)
                    while remaining:
                        self._check_cancel()
                        piece = handle.read(min(PIECE, remaining))
                        if not piece:
                            break
                        remaining -= len(piece)
                        yield piece
                        sent[0] += len(piece)
                        meter.advance(len(piece))
                        self._throttled_report(meter, "uploading", detail)

            try:
                response = await self.client.put(
                    url,
                    params={"index": index, "chunk_size": chunk_size, "total": total},
                    content=body(),
                    headers={
                        **self.headers,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(length(index)),
                    },
                    timeout=httpx.Timeout(600, connect=30),
                )
                if response.status_code in {400, 409}:
                    raise _ChunkFailed(msg("worker.restoreTask.chunkRejected", response=response.text[:200]))
                response.raise_for_status()
            except BaseException:
                meter.advance(-sent[0])
                raise

        for _attempt in range(3):
            pending = deque(i for i in range(count) if i not in received)
            if not pending:
                break
            await _run_parallel(pending, connections, put, received.add, msg("worker.restoreTask.upload"))
            # 以主控记录为准，补传缺少的分块（例如主控重启时丢失的最后几块）。
            status = await self._upload_status()
            received = {int(i) for i in status.get("received") or []}
        if len(received) != count:
            raise RuntimeError(msg("worker.restoreTask.stillMissing"))

    async def _upload_sequential(self, total: int, chunk_size: int) -> None:
        failures = 0
        meter: TransferMeter | None = None
        while True:
            self._check_cancel()
            try:
                offset = await self._remote_offset()
                if offset > total:
                    raise RuntimeError(msg("worker.restoreTask.tooMuch"))
                if meter is None:
                    meter = TransferMeter(total, offset)
                elif meter.done != offset:
                    meter.reset(offset)
                self.report(meter.snapshot("uploading", msg("worker.restoreTask.upload")))
                while offset < total:
                    self._check_cancel()
                    data = await asyncio.to_thread(self._read_chunk, offset, chunk_size)
                    response = await self.client.put(
                        self.claim["upload_url"],
                        params={"offset": offset},
                        content=self._stream(data, meter),
                        headers={**self.headers, "Content-Type": "application/octet-stream", "Content-Length": str(len(data))},
                        timeout=httpx.Timeout(600, connect=30),
                    )
                    if response.status_code == 409:
                        break  # 位置不一致：重新查询主控已收到的字节数
                    response.raise_for_status()
                    offset = int(response.json()["offset"])
                    if meter.done != offset:
                        meter.reset(offset)
                    self.report(meter.snapshot("uploading", msg("worker.restoreTask.upload")))
                    failures = 0
                else:
                    break
            except RETRYABLE as exc:
                failures += 1
                if failures > MAX_TRANSFER_RETRIES:
                    raise RuntimeError(msg("worker.restoreTask.uploadInterrupted", exc=exc)) from exc
                wait = min(60, 2 ** min(failures, 6))
                logger.warning("传回中断（第 %d 次），%d 秒后从断点续传：%s", failures, wait, exc)
                if meter is not None:
                    self.report(meter.snapshot("uploading", msg("worker.restoreTask.resume", wait=wait)))
                await asyncio.sleep(wait)

    def _read_chunk(self, offset: int, size: int) -> bytes:
        with self.output.open("rb") as handle:
            handle.seek(offset)
            return handle.read(size)

    async def _stream(self, data: bytes, meter: TransferMeter):
        view = memoryview(data)
        last = 0.0
        for start in range(0, len(view), 1024 * 1024):
            piece = bytes(view[start : start + 1024 * 1024])
            yield piece
            meter.advance(len(piece))
            now = time.monotonic()
            if now - last >= 2:
                self.report(meter.snapshot("uploading", msg("worker.restoreTask.upload")))
                last = now
