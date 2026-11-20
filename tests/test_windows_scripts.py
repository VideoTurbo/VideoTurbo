from pathlib import Path

WINDOWS_DIR = Path(__file__).resolve().parents[1] / "deploy" / "worker" / "windows"


def test_windows_powershell_scripts_have_utf8_bom() -> None:
    scripts = sorted(WINDOWS_DIR.glob("*.ps1"))
    assert [script.name for script in scripts] == ["worker.ps1"]
    for script in scripts:
        assert script.read_bytes().startswith(b"\xef\xbb\xbf"), (
            f"{script.name} 必须使用 UTF-8 BOM，以兼容 Windows PowerShell 5.1。"
        )


def test_windows_directory_only_holds_the_manager_and_hidden_launcher() -> None:
    assert sorted(path.name for path in WINDOWS_DIR.iterdir()) == ["run-hidden.vbs", "worker.ps1"]


def test_hidden_launcher_starts_the_supervisor_of_worker_script() -> None:
    script = (WINDOWS_DIR / "worker.ps1").read_text(encoding="utf-8-sig")
    launcher = (WINDOWS_DIR / "run-hidden.vbs").read_text(encoding="ascii")
    assert "wscript.exe" in script and "run-hidden.vbs" in script
    assert '"worker.ps1"' in launcher and "__supervise" in launcher
    assert '"__supervise"' in script


def test_worker_script_resolves_project_root_and_commands() -> None:
    script = (WINDOWS_DIR / "worker.ps1").read_text(encoding="utf-8-sig")
    # deploy/worker/windows 在项目根目录下三层。
    assert 'Join-Path $ScriptDir "..\\..\\.."' in script
    for command in ("install", "start", "stop", "restart", "status", "logs", "self-test", "clean", "uninstall", "help"):
        assert f'"{command}" {{' in script
    assert "--shared-machine" in script
    # Windows PowerShell 5.1 不支持的 PowerShell 7 语法。
    assert "??" not in script and "&&" not in script and "?." not in script


def test_worker_script_installs_locked_requirements() -> None:
    script = (WINDOWS_DIR / "worker.ps1").read_text(encoding="utf-8-sig")
    # 依赖按 uv.lock 导出的清单安装，与 CI 测试的版本一致；项目代码单独安装，不再解析依赖。
    assert '"deploy\\worker\\requirements\\asr.txt"' in script
    assert "pip install --require-hashes -r $Requirements" in script
    assert "[worker]" not in script
    assert "--force-reinstall --no-deps $ProjectRoot" in script
