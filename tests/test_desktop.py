import json
import re
import tomllib
from pathlib import Path

from video_turbo import __version__

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "desktop" / "apps" / "videoturbo"


def test_desktop_app_version_matches_the_python_package() -> None:
    # 安装包的版本号来自 Cargo.toml；发布时改 __version__ 要一起改。
    cargo = tomllib.loads((APP / "Cargo.toml").read_text(encoding="utf-8"))
    assert cargo["package"]["version"] == __version__


def test_bundled_repository_files_exist() -> None:
    config = json.loads((APP / "tauri.conf.json").read_text(encoding="utf-8"))
    for source in config["bundle"]["resources"]:
        # resources/ 下的内容由 deploy/desktop/build_sidecar.py 生成，不在仓库里。
        if source.startswith("resources/"):
            continue
        assert (APP / source).exists(), source
    for icon in config["bundle"]["icon"]:
        assert (APP / icon).is_file(), icon


def test_ffmpeg_downloads_are_pinned_for_both_platforms() -> None:
    source = (ROOT / "deploy" / "desktop" / "build_sidecar.py").read_text(encoding="utf-8")
    for platform in ("darwin-arm64", "win32-x64"):
        for tool in ("ffmpeg", "ffprobe"):
            assert re.search(rf'"{tool}-{platform}\.gz": "[0-9a-f]{{64}}"', source), (tool, platform)
