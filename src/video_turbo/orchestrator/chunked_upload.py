"""影片修复结果的分块乱序上传：Worker 多条连接同时上传不同分块，主控按位置写入。

- 第一次收到分块时按总大小预分配回传文件，并在旁边的 ``.chunks.json`` 记录已完整收到的分块。
- 分块必须完整写入（字节数与分块大小一致）才记为已收到；中途断开的分块之后重传覆盖即可。
- 主控重启后从 ``.chunks.json`` 恢复，Worker 只需补传缺少的分块。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any

from ..i18n import msg

MIN_CHUNK = 1024 * 1024
MAX_CHUNK = 256 * 1024 * 1024
WRITE_BUFFER = 4 * 1024 * 1024


class ChunkError(ValueError):
    pass


def state_path(upload: Path) -> Path:
    return upload.with_name(upload.name + ".chunks.json")


def chunk_count(total: int, chunk_size: int) -> int:
    return math.ceil(total / chunk_size)


def expected_length(index: int, total: int, chunk_size: int) -> int:
    return min(chunk_size, total - index * chunk_size)


def load_state(upload: Path) -> dict[str, Any] | None:
    path = state_path(upload)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not upload.is_file():
        return None
    state["received"] = set(state.get("received") or [])
    return state


def save_state(upload: Path, state: dict[str, Any]) -> None:
    path = state_path(upload)
    payload = {**state, "received": sorted(state["received"])}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def discard(upload: Path) -> None:
    upload.unlink(missing_ok=True)
    state_path(upload).unlink(missing_ok=True)


def status(upload: Path) -> dict[str, Any]:
    state = load_state(upload)
    if state is None:
        return {
            "offset": upload.stat().st_size if upload.is_file() else 0,
            "chunked": True,
            "chunk_size": None,
            "total": None,
            "received": [],
        }
    # 分块模式下文件已预分配，顺序上传的偏移量没有意义。
    return {
        "offset": 0,
        "chunked": True,
        "chunk_size": state["chunk_size"],
        "total": state["total"],
        "received": sorted(state["received"]),
    }


def prepare(upload: Path, *, total: int, chunk_size: int) -> dict[str, Any]:
    """返回与本次参数一致的上传状态；参数变化时丢弃旧数据重新开始。"""
    if not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
        raise ChunkError(msg("upload.chunkSize"))
    if total <= 0:
        raise ChunkError(msg("upload.badSize"))
    state = load_state(upload)
    if state is not None and state.get("total") == total and state.get("chunk_size") == chunk_size:
        return state
    # 参数变化，或之前是单连接顺序上传：丢弃已有数据重新开始。
    received: set[int] = set()
    upload.unlink(missing_ok=True)
    upload.parent.mkdir(parents=True, exist_ok=True)
    with upload.open("r+b" if upload.exists() else "wb") as handle:
        handle.truncate(total)
    state = {"total": total, "chunk_size": chunk_size, "received": received}
    save_state(upload, state)
    return state


class ChunkLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, job_id: str) -> asyncio.Lock:
        return self._locks.setdefault(job_id, asyncio.Lock())


async def write_chunk(upload: Path, offset: int, expected: int, body) -> int:
    """把请求体写到 offset 处，返回实际写入字节数；超出预期长度时抛出 ChunkError。"""
    written = 0
    buffer = bytearray()
    with upload.open("r+b") as handle:
        handle.seek(offset)
        async for data in body:
            if written + len(buffer) + len(data) > expected:
                raise ChunkError(msg("upload.chunkTooLarge"))
            buffer.extend(data)
            if len(buffer) >= WRITE_BUFFER:
                await asyncio.to_thread(handle.write, bytes(buffer))
                written += len(buffer)
                buffer.clear()
        if buffer:
            await asyncio.to_thread(handle.write, bytes(buffer))
            written += len(buffer)
    return written
