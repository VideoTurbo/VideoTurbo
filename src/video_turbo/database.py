from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .cloud_store import CloudStoreMixin
from .job_store import JobStoreMixin
from .media_store import LocalMediaFiles, MediaFiles, MediaStoreMixin
from .migrations import migrate
from .plugin_store import PluginStoreMixin
from .restore_store import RestoreStoreMixin
from .settings_store import SettingsStoreMixin
from .worker_store import WorkerStoreMixin


class Database(
    SettingsStoreMixin,
    MediaStoreMixin,
    JobStoreMixin,
    WorkerStoreMixin,
    RestoreStoreMixin,
    CloudStoreMixin,
    PluginStoreMixin,
):
    """主程序的 SQLite 数据库。各部分的读写分在 *_store.py 的 mixin 中，见 store_base。"""

    def __init__(self, path: str | Path):
        self.files: MediaFiles = LocalMediaFiles()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每个线程复用一条连接，省去每次查询重新打开文件和设置 PRAGMA 的开销。
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        # 正在执行查询的复用连接。close() 不能从别的线程关闭它们（SQLite 会崩溃），由使用者用完后自行关闭。
        self._busy: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        # 连接只在创建它的线程中使用；关闭检查是为了让 close() 能从其他线程统一回收。
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        local = self._local
        connection: sqlite3.Connection | None = getattr(local, "connection", None)
        # 同一线程嵌套使用时另开临时连接，保持各自独立的事务。
        reuse = not getattr(local, "busy", False)
        if not reuse:
            connection = self._open()
        elif connection is None:
            connection = self._open()
            local.connection = connection
            with self._connections_lock:
                self._connections.add(connection)
        if reuse:
            local.busy = True
            with self._connections_lock:
                self._busy.add(connection)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if reuse:
                local.busy = False
                with self._connections_lock:
                    self._busy.discard(connection)
                    # 使用期间 close() 已把它移出连接池：轮到本线程自己关闭。
                    retired = connection not in self._connections
                if retired:
                    local.connection = None
                    connection.close()
            else:
                connection.close()

    def close(self) -> None:
        """关闭所有线程持有的连接；之后再次使用会自动重新打开。

        正在其他线程中执行查询的连接（例如关机时仍在运行的后台线程）留给该线程用完后自行关闭。
        """
        with self._connections_lock:
            connections, self._connections = self._connections, set()
            connections -= self._busy
        self._local = threading.local()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def initialize(self) -> None:
        """创建或升级数据库结构，见 migrations.py。"""
        with self.connect() as conn:
            migrate(conn, self.path)
