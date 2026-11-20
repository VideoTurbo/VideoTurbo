"""同一台机器上字幕识别与影片修复互斥使用 GPU。

字幕服务的多个实例各持共享锁，可以并行识别；影片修复服务修复影片时持独占锁，期间字幕实例不领新任务。
字幕优先：字幕实例只要还领得到任务就一直持有共享锁，影片修复只在本机字幕队列空闲时开始下一部；
正在修复的影片不会被打断。

锁是 ``flock`` 文件锁，进程退出（包括被强制结束）时由内核自动释放。Windows 上没有 fcntl，
也不运行影片修复，锁退化为总是成功。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Literal


class GpuLock:
    def __init__(self, path: Path | None) -> None:
        self.path = path if os.name != "nt" else None
        self._handle: IO[str] | None = None
        self.mode: Literal["shared", "exclusive"] | None = None

    @property
    def held(self) -> bool:
        return self.mode is not None

    def try_shared(self) -> bool:
        return self._try("shared")

    def try_exclusive(self) -> bool:
        return self._try("exclusive")

    def is_free(self) -> bool:
        """本机此刻没有其他进程持有锁（不保留锁）。"""
        if self.held:
            return True
        if not self.try_exclusive():
            return False
        self.release()
        return True

    def release(self) -> None:
        handle, self._handle, self.mode = self._handle, None, None
        if handle is None:
            return
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _try(self, mode: Literal["shared", "exclusive"]) -> bool:
        if self.mode == mode:
            return True
        if self.held:
            raise RuntimeError(f"GPU 锁已以 {self.mode} 方式持有，不能改为 {mode}。")
        if self.path is None:
            self.mode = mode
            return True
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        flag = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
        try:
            fcntl.flock(handle.fileno(), flag | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle, self.mode = handle, mode
        return True
