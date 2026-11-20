import importlib.util
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location("cleanup_worker", PROJECT / "tools/cleanup_worker.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # 不填 worker_token 也能清理。
    config = config_dir / "worker.yaml"
    config.write_text("cache_dir: ../cache\nasr:\n  download_root: ../models\n", encoding="utf-8")
    work = tmp_path / "cache"
    single = work / "asr" / f"ABC-123-{uuid.uuid4().hex[:12]}"
    instance = work / "asr" / f"影片-{uuid.uuid4().hex[:12]}"
    restore = work / "restore" / f"ABC-123-{uuid.uuid4().hex[:12]}"
    self_test = work / "restore" / "self-test"
    for directory in (single, instance, restore, self_test):
        directory.mkdir(parents=True)
    (single / "audio.opus").write_bytes(b"a" * 10)
    (instance / "audio.opus").write_bytes(b"b" * 20)
    (restore / "source.mp4").write_bytes(b"c" * 30)
    (work / "restore" / ".restore.lock").write_text("")
    (work / ".gpu.lock").write_text("")
    (work / "notes").mkdir()
    blobs = tmp_path / "models/huggingface/models--x--y/blobs"
    blobs.mkdir(parents=True)
    (blobs / "done").write_bytes(b"model")
    old_partial = blobs / "old.incomplete"
    old_partial.write_bytes(b"p" * 40)
    stale = time.time() - 2 * 3600
    os.utime(old_partial, (stale, stale))
    (blobs / "fresh.incomplete").write_bytes(b"f")
    return {
        "root": tmp_path, "config": config, "single": single, "instance": instance,
        "restore": restore, "self_test": self_test, "old_partial": old_partial, "blobs": blobs,
    }


def run(module, layout, *extra):
    return module.main(["--config", str(layout["config"]), "--skip", "pip", *extra])


def test_dry_run_lists_without_deleting(layout, capsys):
    module = load_tool()
    assert run(module, layout) == 0
    out = capsys.readouterr().out
    for key in ("single", "instance", "restore", "self_test", "old_partial"):
        assert str(layout[key]) in out
        assert layout[key].exists()
    assert "fresh.incomplete" not in out
    assert "--yes" in out


def test_yes_removes_only_leftovers(layout, monkeypatch):
    module = load_tool()
    monkeypatch.setattr(module, "running_processes", list)
    assert run(module, layout, "--yes") == 0
    work = layout["root"] / "cache"
    for key in ("single", "instance", "restore", "self_test", "old_partial"):
        assert not layout[key].exists()
    assert (work / "asr").is_dir()
    assert (work / "notes").is_dir()
    assert (work / ".gpu.lock").is_file()
    assert (work / "restore" / ".restore.lock").is_file()
    assert (layout["blobs"] / "done").is_file()
    assert (layout["blobs"] / "fresh.incomplete").is_file()


def test_skip_keeps_restore_jobs(layout, monkeypatch):
    module = load_tool()
    monkeypatch.setattr(module, "running_processes", list)
    assert run(module, layout, "--yes", "--skip", "restore") == 0
    assert layout["restore"].is_dir()
    assert not layout["single"].exists()


def test_refuses_while_worker_running(layout, monkeypatch, capsys):
    module = load_tool()
    monkeypatch.setattr(module, "running_processes", lambda: ["PID 42：python -m video_turbo.worker.main"])
    assert run(module, layout, "--yes") == 1
    assert "PID 42" in capsys.readouterr().err
    assert layout["single"].is_dir()
    assert layout["restore"].is_dir()


def test_pip_cache_uses_purge_command(tmp_path, monkeypatch):
    module = load_tool()
    cache = tmp_path / "pip-cache"
    cache.mkdir()
    (cache / "wheel").write_bytes(b"w" * 5)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert "PIP_NO_CACHE_DIR" not in kwargs["env"]
        stdout = f"{cache}\n" if command[-1] == "dir" else ""
        return module.subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setenv("PIP_NO_CACHE_DIR", "1")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    targets = module.pip_targets()
    assert [(t.path, t.size) for t in targets] == [(cache, 5)]
    assert module.remove(targets[0]) is None
    assert calls[-1][-2:] == ["cache", "purge"]
    assert cache.is_dir()
