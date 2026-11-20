import importlib.util
import io
import os
from pathlib import Path

import pytest
import yaml

from video_turbo.i18n import render
from video_turbo.worker.config import load_worker_config

PROJECT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT / "config/worker.example.yaml"
DEFAULT_ROOT = "/opt/models"


def load_tool():
    spec = importlib.util.spec_from_file_location("configure_worker", PROJECT / "tools/configure_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tool(monkeypatch, config, answers, *extra, tty=True):
    module = load_tool()
    replies = iter(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr("builtins.input", fake_input)
    stdin = io.StringIO()
    stdin.isatty = lambda: tty
    monkeypatch.setattr(module.sys, "stdin", stdin)
    code = module.main([
        "--config", str(config), "--template", str(TEMPLATE),
        "--default-download-root", DEFAULT_ROOT, *extra,
    ])
    assert code == 0
    return prompts


def test_first_install_prompts_and_accepts_defaults(tmp_path, monkeypatch):
    config = tmp_path / "worker.yaml"
    prompts = run_tool(monkeypatch, config, [
        "",  # The address is required: re-prompted.
        "ftp://nas", "http://100.70.1.2:8765/",
        "",  # Token is required.
        'tok"en #1',
        "",  # Recommended model directory.
    ])
    # Worker 名称由显卡自动生成，安装时不再询问。
    assert len(prompts) == 6
    assert not any("name" in prompt for prompt in prompts)
    assert f"[{DEFAULT_ROOT}]" in prompts[5]
    payload = yaml.safe_load(config.read_text())
    assert payload["server_url"] == "http://100.70.1.2:8765"
    assert payload["worker_token"] == 'tok"en #1'
    assert "name" not in payload
    assert payload["asr"]["download_root"] == DEFAULT_ROOT
    # Everything else, including comments, comes from the template unchanged.
    assert "# 接入密钥：从主程序控制台“设置 → 云主机 → 接入密钥”复制，必填。" in render(config.read_text())
    template = yaml.safe_load(TEMPLATE.read_text())
    for key in ("server_url", "worker_token"):
        payload.pop(key)
        template.pop(key)
    # 新建配置时，模板里的相对模型目录换成安装脚本给出的默认目录。
    assert template["asr"].pop("download_root") == "../models"
    payload["asr"].pop("download_root")
    assert payload == template
    monkeypatch.setenv("VIDEO_TURBO_MACHINE_ID", "gpu-3f2a9c1d")
    assert load_worker_config(config).name == "gpu-3f2a9c1d"


def test_custom_values_are_written(tmp_path, monkeypatch):
    config = tmp_path / "worker.yaml"
    run_tool(monkeypatch, config, [
        "https://nas.example:8765", "sec\\ret", "/data/models",
    ])
    payload = yaml.safe_load(config.read_text())
    assert payload["worker_token"] == "sec\\ret"
    assert payload["asr"]["download_root"] == "/data/models"


def test_inserted_download_root_keeps_backslashes(tmp_path, monkeypatch):
    config = tmp_path / "worker.yaml"
    config.write_text("asr:\n  model: large-v3\n")
    run_tool(monkeypatch, config, ["http://nas:8765", "t", "/data/a\\1"])
    assert yaml.safe_load(config.read_text())["asr"]["download_root"] == "/data/a\\1"


def test_complete_config_is_kept_unless_user_opts_in(tmp_path, monkeypatch):
    config = tmp_path / "worker.yaml"
    config.write_text(TEMPLATE.read_text().replace(
        'worker_token: ""', 'worker_token: "keep"'
    ).replace('server_url: ""', 'server_url: "http://100.70.1.2:8765"'))
    before = config.read_bytes()
    prompts = run_tool(monkeypatch, config, [""])
    assert len(prompts) == 1 and "(y/N)" in prompts[0]
    assert config.read_bytes() == before

    # Opting in shows current values as defaults; Enter keeps the token.
    prompts = run_tool(monkeypatch, config, ["y", "", "", ""])
    assert "[http://100.70.1.2:8765]" in prompts[1]
    assert "已设置" in render(prompts[2])
    payload = yaml.safe_load(config.read_text())
    assert payload["worker_token"] == "keep"


def test_existing_minimal_config_gets_download_root_under_asr(tmp_path, monkeypatch):
    config = tmp_path / "worker.yaml"
    config.write_text("name: cloud5090-old\nasr:\n  model: large-v3\n")
    run_tool(monkeypatch, config, ["http://nas:8765", "t", ""])
    payload = yaml.safe_load(config.read_text())
    assert payload["name"] == "cloud5090-old"  # 旧字段原样保留，只是运行时不再读取
    assert payload["asr"] == {"model": "large-v3", "download_root": DEFAULT_ROOT}


def test_non_interactive_copies_template_and_reports_missing(tmp_path, monkeypatch, capsys):
    config = tmp_path / "worker.yaml"
    prompts = run_tool(monkeypatch, config, [], tty=False)
    assert prompts == []
    assert config.read_text() == TEMPLATE.read_text().replace(
        "download_root: ../models", f'download_root: "{DEFAULT_ROOT}"'
    )
    assert "server_url、worker_token" in render(capsys.readouterr().out)


@pytest.mark.skipif(os.name == "nt", reason="用例里的绝对路径是 Linux 写法")
def test_print_download_root_resolves_relative_paths(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config/worker.yaml"
    config.parent.mkdir()
    config.write_text("asr:\n  download_root: ../models # cache\n")
    run_tool(monkeypatch, config, [], "--print-download-root")
    assert capsys.readouterr().out.strip() == str(tmp_path / "models")
    config.write_text("name: x\n")
    run_tool(monkeypatch, config, [], "--print-download-root")
    assert capsys.readouterr().out.strip() == DEFAULT_ROOT


def test_shared_machine_enables_idle_detection_only_for_new_config(tmp_path, monkeypatch):
    config = tmp_path / "worker.yaml"
    run_tool(monkeypatch, config, [], "--shared-machine", tty=False)
    payload = yaml.safe_load(config.read_text())
    assert payload["idle"]["enabled"] is True
    assert payload["jasna"]["enabled"] is False  # 只改 idle 段
    assert "enabled: false" in TEMPLATE.read_text().split("idle:", 1)[1].split("asr:", 1)[0]
    # Windows 路径（反斜杠）作为模型目录时仍是合法 YAML。
    other = tmp_path / "win.yaml"
    module = load_tool()
    stdin = io.StringIO()
    stdin.isatty = lambda: False
    monkeypatch.setattr(module.sys, "stdin", stdin)
    module.main(["--config", str(other), "--template", str(TEMPLATE), "--shared-machine",
                 "--default-download-root", "C:\\Dev\\video-turbo\\models", "--non-interactive"])
    assert yaml.safe_load(other.read_text())["asr"]["download_root"] == "C:\\Dev\\video-turbo\\models"
    # 已有配置不会被改动。
    config.write_text(config.read_text().replace("enabled: true", "enabled: false", 1))
    run_tool(monkeypatch, config, [], "--shared-machine", tty=False)
    assert yaml.safe_load(config.read_text())["idle"]["enabled"] is False
