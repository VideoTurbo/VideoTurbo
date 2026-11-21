import importlib.util
import json
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location("desktop_release", PROJECT / "tools" / "desktop_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifacts(root: Path, version: str, *, signed: bool = True) -> Path:
    files = {
        f"VideoTurbo-nsis/nsis/VideoTurbo_{version}_x64-setup.exe": "main-exe",
        f"VideoTurbo-nsis/nsis/VideoTurbo Worker_{version}_x64-setup.exe": "worker-exe",
        f"VideoTurbo-dmg/dmg/VideoTurbo_{version}_aarch64.dmg": "dmg",
        "VideoTurbo-dmg/macos/VideoTurbo.app.tar.gz": "tarball",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if signed and not name.endswith(".dmg"):
            Path(f"{path}.sig").write_text(f"sig-of-{content}\n", encoding="utf-8")
    return root


def test_release_files_and_update_manifests(tmp_path: Path) -> None:
    tool = load_tool()
    output = tmp_path / "out"
    tool.main(["--artifacts", str(artifacts(tmp_path / "in", "1.2.0")), "--tag", "v1.2.0", "--output", str(output)])
    assert sorted(path.name for path in output.iterdir()) == [
        "VideoTurbo-Worker_1.2.0_x64-setup.exe",
        "VideoTurbo_1.2.0_aarch64.app.tar.gz",
        "VideoTurbo_1.2.0_aarch64.dmg",
        "VideoTurbo_1.2.0_x64-setup.exe",
        "latest-worker.json",
        "latest.json",
    ]
    base = "https://github.com/VideoTurbo/VideoTurbo/releases/download/v1.2.0"
    main = json.loads((output / "latest.json").read_text(encoding="utf-8"))
    assert main["version"] == "1.2.0"
    assert main["platforms"] == {
        "windows-x86_64": {"signature": "sig-of-main-exe", "url": f"{base}/VideoTurbo_1.2.0_x64-setup.exe"},
        "darwin-aarch64": {"signature": "sig-of-tarball", "url": f"{base}/VideoTurbo_1.2.0_aarch64.app.tar.gz"},
    }
    worker = json.loads((output / "latest-worker.json").read_text(encoding="utf-8"))
    assert worker["platforms"] == {
        "windows-x86_64": {"signature": "sig-of-worker-exe", "url": f"{base}/VideoTurbo-Worker_1.2.0_x64-setup.exe"},
    }


def test_unsigned_builds_are_rejected(tmp_path: Path) -> None:
    tool = load_tool()
    with pytest.raises(SystemExit, match="签名"):
        tool.main(["--artifacts", str(artifacts(tmp_path / "in", "1.2.0", signed=False)), "--tag", "v1.2.0",
                   "--output", str(tmp_path / "out")])


def test_version_must_match_the_tag(tmp_path: Path) -> None:
    tool = load_tool()
    with pytest.raises(SystemExit, match="只有一个"):
        tool.main(["--artifacts", str(artifacts(tmp_path / "in", "1.1.0")), "--tag", "v1.2.0",
                   "--output", str(tmp_path / "out")])


def test_apps_read_the_manifests_this_tool_writes() -> None:
    for app, feed in (("videoturbo", "latest.json"), ("worker", "latest-worker.json")):
        config = json.loads((PROJECT / "desktop" / "apps" / app / "tauri.conf.json").read_text(encoding="utf-8"))
        updater = config["plugins"]["updater"]
        assert updater["endpoints"] == [f"https://github.com/VideoTurbo/VideoTurbo/releases/latest/download/{feed}"]
        assert updater["pubkey"]
        # 只有发布流程（带私钥）才生成更新包。
        assert config["bundle"]["createUpdaterArtifacts"] is False
