import logging
from pathlib import Path

import pytest
from conftest import prepare_server
from fastapi.testclient import TestClient

from video_turbo.i18n import render
from video_turbo.orchestrator import log_files
from video_turbo.orchestrator.app import app


@pytest.fixture(autouse=True)
def _close_log_file():
    yield
    log_files.close_log_file()


def test_server_writes_rotating_log_file(tmp_path: Path, monkeypatch, caplog) -> None:
    # pytest 已给根日志器挂了处理器，主程序的 basicConfig 不再生效：这里手动把级别放到 INFO。
    caplog.set_level(logging.INFO)
    prepare_server(tmp_path, monkeypatch)
    with TestClient(app):
        logging.getLogger("video_turbo.test").warning("写入日志文件")
        handler = log_files._handler
        assert handler is not None
        assert (handler.maxBytes, handler.backupCount) == (log_files.MAX_BYTES, log_files.BACKUP_COUNT)

    text = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    assert "VideoTurbo" in text and "启动" in render(text)
    assert "WARNING video_turbo.test: 写入日志文件" in render(text)
    # 主程序退出时关闭文件，之后的日志不再写入。
    assert log_files._handler is None


def test_install_is_idempotent_and_follows_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(log_files.LOG_DIR_ENV, str(tmp_path / "a"))
    first = log_files.install_log_file()
    assert log_files.install_log_file() == first
    assert sum(h is log_files._handler for h in logging.getLogger().handlers) == 1

    monkeypatch.setenv(log_files.LOG_DIR_ENV, str(tmp_path / "b"))
    assert log_files.install_log_file() == (tmp_path / "b" / "server.log").resolve()
    assert sum(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger().handlers) == 1


def test_empty_setting_disables_and_unwritable_directory_only_warns(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv(log_files.LOG_DIR_ENV, "")
    assert log_files.install_log_file() is None

    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv(log_files.LOG_DIR_ENV, str(blocker / "logs"))
    with caplog.at_level(logging.WARNING):
        assert log_files.install_log_file() is None
    assert "无法写入日志文件" in render(caplog.text)
