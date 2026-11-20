import asyncio
import os
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from video_turbo.config_store import SETTINGS_KEY
from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.models import utc_now_iso
from video_turbo.orchestrator import auth
from video_turbo.protocol import PROTOCOL_HEADER, WORKER_PROTOCOL_VERSION
from video_turbo.settings_store import WORKER_TOKEN_KEY

# GPU 机器每个请求都带的协议版本请求头。
PROTOCOL = {PROTOCOL_HEADER: str(WORKER_PROTOCOL_VERSION)}


@pytest.fixture(autouse=True)
def _dump_stuck_tasks(request: pytest.FixtureRequest) -> Iterator[None]:
    """排查卡住的测试：设置 VIDEO_TURBO_TEST_DUMP_AFTER（秒）后，测试超过这个时间仍未结束时，
    打印所有未完成的 asyncio 任务停在哪里。pytest-timeout 只能打印线程栈，看不到协程在等什么。"""
    seconds = float(os.environ.get("VIDEO_TURBO_TEST_DUMP_AFTER") or 0)
    if seconds <= 0:
        yield
        return

    capture = request.config.pluginmanager.getplugin("capturemanager")

    def dump() -> None:
        # 暂停 pytest 的输出捕获，否则超时退出时这些内容随捕获一起丢掉。
        with capture.global_and_fixture_disabled():
            out = sys.stderr
            print(f"\n=== {request.node.nodeid} 超过 {seconds:g} 秒，未完成的 asyncio 任务：", file=out)
            for task in list(asyncio.tasks._all_tasks):  # 调试用：取所有事件循环里的任务
                if task.done():
                    continue
                print(f"--- {task.get_name()}", file=out)
                # 沿 await 链往里走，直到真正在等的那个对象（print_stack 只给出最外层）。
                awaited: Any = task.get_coro()
                while awaited is not None:
                    frame = getattr(awaited, "cr_frame", None) or getattr(awaited, "gi_frame", None)
                    if frame is None:
                        print(f"    等待 {awaited!r}", file=out)
                        break
                    print(f"    {frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}", file=out)
                    awaited = getattr(awaited, "cr_await", None) or getattr(awaited, "gi_yieldfrom", None)
            out.flush()

    timer = threading.Timer(seconds, dump)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


@pytest.fixture(autouse=True)
def _admin_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """现有接口测试不涉及登录；鉴权本身在 test_auth.py 中开启后测试。"""
    monkeypatch.setattr(auth, "ENFORCE", False)


def prepare_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any] | None = None,
    *,
    worker_token: str | None = None,
    backup_dir: Path | None = None,
    db_name: str = "app.db",
) -> Database:
    """主控没有配置文件：测试直接把控制台设置和 Worker 密钥写进数据库，部署项通过环境变量给出。"""
    monkeypatch.setenv("VIDEO_TURBO_DATABASE", str(tmp_path / db_name))
    monkeypatch.setenv("VIDEO_TURBO_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("VIDEO_TURBO_RESTORE_BACKUP", str(backup_dir) if backup_dir else "")
    monkeypatch.setenv("VIDEO_TURBO_LOG_DIR", str(tmp_path / "logs"))
    database = Database(tmp_path / db_name)
    database.initialize()
    database.set_runtime_json(SETTINGS_KEY, {"scan": {"enabled": False}, **(settings or {})})
    if worker_token:
        database.set_runtime_json(WORKER_TOKEN_KEY, worker_token)
    return database


def force_job_state(
    database: Database,
    job_id: str,
    state: str,
    *,
    error: str | None = None,
    fields: dict[str, Any] | None = None,
) -> None:
    """测试布置：把任务直接改成某个状态，不经过 job_states 的流转检查（Database.set_job_state 会检查）。"""
    updates: dict[str, Any] = {"state": state, "last_error": error, "updated_at": utc_now_iso(), **(fields or {})}
    assignment = ", ".join(f"{key} = ?" for key in updates)
    with database.connect() as conn:
        cursor = conn.execute(f"UPDATE jobs SET {assignment} WHERE id = ?", (*updates.values(), job_id))
        assert cursor.rowcount == 1, job_id


@contextmanager
def raises_rendered(exc_type: type[BaseException], pattern: str) -> Iterator[pytest.ExceptionInfo]:
    """像 pytest.raises(match=...) 一样检查错误信息，但先把 msg() 标记翻译成中文再匹配。"""
    with pytest.raises(exc_type) as info:
        yield info
    text = render(str(info.value))
    assert re.search(pattern, text), text


class same_path:
    """与数据库里的路径比较时用：Windows 上路径可能用 / 或 \\ 分隔，按指向的位置比较。

    ``assert job["audio_path"] == same_path(audio)``
    """

    def __init__(self, expected: Any) -> None:
        self.expected = Path(expected)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, (str, Path)) and Path(other) == self.expected

    def __repr__(self) -> str:
        return f"same_path({str(self.expected)!r})"
