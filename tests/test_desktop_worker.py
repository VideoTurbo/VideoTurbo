import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

from video_turbo import __version__, connection_string

PROJECT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location("desktop_worker", PROJECT / "tools" / "desktop_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events(output: str) -> list[dict]:
    return [json.loads(line[len("@@VT "):]) for line in output.splitlines() if line.startswith("@@VT ")]


def test_configure_from_connection_code(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    code = connection_string.encode("http://192.168.1.10:8766", "tok-1")
    assert tool.main(["configure", "--root", str(tmp_path), "--bundle", str(PROJECT), "--connection", code,
                      "--shared-machine"]) == 0
    config = yaml.safe_load((tmp_path / "config" / "worker.yaml").read_text(encoding="utf-8"))
    assert config["server_url"] == "http://192.168.1.10:8766"
    assert config["worker_token"] == "tok-1"
    assert config["asr"]["download_root"] == "../models"
    # 与桌面、游戏共用显卡的电脑默认只在空闲时接单。
    assert config["idle"]["enabled"] is True
    assert events(capsys.readouterr().out)[-1]["event"] == "configured"


def test_reconfigure_keeps_other_settings(tmp_path: Path) -> None:
    tool = load_tool()
    tool.main(["configure", "--root", str(tmp_path), "--bundle", str(PROJECT),
               "--server-url", "http://127.0.0.1:8766/", "--token", "a"])
    path = tmp_path / "config" / "worker.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("poll_interval_seconds: 5", "poll_interval_seconds: 9"),
                    encoding="utf-8")
    tool.main(["configure", "--root", str(tmp_path), "--bundle", str(PROJECT),
               "--server-url", "http://127.0.0.1:9766", "--token", "b"])
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert (config["server_url"], config["worker_token"], config["poll_interval_seconds"]) == (
        "http://127.0.0.1:9766", "b", 9)
    assert config["idle"]["enabled"] is False


def test_invalid_connection_code_reports_an_error(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    assert tool.main(["configure", "--root", str(tmp_path), "--bundle", str(PROJECT), "--connection", "VT1:x"]) == 2
    assert events(capsys.readouterr().out)[-1] == {"event": "error", "code": "connection", "message": "接入串无效"}


def test_install_runs_the_steps_in_order(tmp_path: Path, monkeypatch, capsys) -> None:
    tool = load_tool()
    commands: list[list[str]] = []
    monkeypatch.setattr(tool.select_sources, "select", lambda names: {"VIDEO_TURBO_PYPI_INDEX_URL": "https://pypi.example/simple"})
    monkeypatch.setattr(tool, "ensure_git", lambda layout, env: None)

    def run(command, env):
        commands.append(command)
        assert env["UV_DEFAULT_INDEX"] == "https://pypi.example/simple"
        assert env["PATH"].startswith(str(tmp_path / "ffmpeg") + os.pathsep)

    monkeypatch.setattr(tool, "_run", run)
    root = tmp_path / "worker"
    assert tool.main(["install", "--root", str(root), "--bundle", str(PROJECT), "--uv", "uv",
                      "--python", "py311", "--ffmpeg-dir", str(tmp_path / "ffmpeg"), "--device", "cpu"]) == 0
    assert [command[:2] for command in commands] == [
        ["uv", "venv"], ["uv", "pip"], ["uv", "pip"], [str(tool.Layout.python(root / ".venv")), commands[3][1]],
    ]
    assert "--require-hashes" in commands[1] and "--no-deps" in commands[2]
    assert commands[3][-2:] == ["--device", "cpu"]
    output = events(capsys.readouterr().out)
    assert [event["key"] for event in output if event["event"] == "step"] == [
        "sources", "git", "venv", "deps", "program", "backend"]
    assert output[-1] == {"event": "done", "version": __version__}
    assert json.loads((root / "state.json").read_text(encoding="utf-8"))["version"] == __version__


def test_failed_step_reports_error_and_leaves_no_state(tmp_path: Path, monkeypatch, capsys) -> None:
    tool = load_tool()
    monkeypatch.setattr(tool.select_sources, "select", lambda names: {})
    monkeypatch.setattr(tool, "ensure_git", lambda layout, env: None)

    def run(command, env):
        raise tool.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(tool, "_run", run)
    root = tmp_path / "worker"
    assert tool.main(["install", "--root", str(root), "--bundle", str(PROJECT), "--uv", "uv",
                      "--python", "py311", "--ffmpeg-dir", str(tmp_path)]) == 1
    assert events(capsys.readouterr().out)[-1]["code"] == "failed"
    assert not (root / "state.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="macOS / Linux 上的 Git 检查")
def test_missing_git_is_reported_with_a_code(tmp_path: Path, monkeypatch) -> None:
    tool = load_tool()
    layout = tool.Layout(tmp_path)
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: tool.subprocess.CompletedProcess(a, 2))
    with pytest.raises(tool.SetupError) as error:
        tool.ensure_git(layout, {"PATH": ""})
    assert error.value.code == "need_clt"


def test_status_reflects_install_and_config(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    tool.main(["status", "--root", str(tmp_path), "--bundle", str(PROJECT)])
    assert json.loads(capsys.readouterr().out) == {
        "installed": False, "version": "", "current_version": __version__, "configured": False, "server_url": ""}
