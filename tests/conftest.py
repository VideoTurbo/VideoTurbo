import re
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
