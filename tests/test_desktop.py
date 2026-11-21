import json
import re
import tomllib
from pathlib import Path

import pytest

from video_turbo import __version__

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "desktop" / "apps" / "videoturbo"
APPS = [APP, ROOT / "desktop" / "apps" / "worker"]


@pytest.mark.parametrize("app", APPS, ids=lambda path: path.name)
def test_desktop_app_version_matches_the_python_package(app: Path) -> None:
    # 安装包的版本号来自 Cargo.toml；发布时改 __version__ 要一起改。
    cargo = tomllib.loads((app / "Cargo.toml").read_text(encoding="utf-8"))
    assert cargo["package"]["version"] == __version__


@pytest.mark.parametrize("app", APPS, ids=lambda path: path.name)
def test_bundled_repository_files_exist(app: Path) -> None:
    config = json.loads((app / "tauri.conf.json").read_text(encoding="utf-8"))
    for source in config["bundle"]["resources"]:
        # resources/ 下的内容由 deploy/desktop/build_sidecar.py 生成，不在仓库里。
        if source.startswith(("resources/", "../videoturbo/resources/")):
            continue
        assert (app / source).exists(), source
    for icon in config["bundle"]["icon"]:
        assert (app / icon).is_file(), icon


def test_uv_downloads_are_pinned_for_both_platforms() -> None:
    source = (ROOT / "deploy" / "desktop" / "build_sidecar.py").read_text(encoding="utf-8")
    for platform in ("darwin-arm64", "win32-x64"):
        assert re.search(rf'"{platform}": \("uv-[^"]+", "[0-9a-f]{{64}}"\)', source), platform


def test_ffmpeg_downloads_are_pinned_for_both_platforms() -> None:
    source = (ROOT / "deploy" / "desktop" / "build_sidecar.py").read_text(encoding="utf-8")
    for platform in ("darwin-arm64", "win32-x64"):
        for tool in ("ffmpeg", "ffprobe"):
            assert re.search(rf'"{tool}-{platform}\.gz": "[0-9a-f]{{64}}"', source), (tool, platform)


def test_both_apps_share_the_same_model_list_script() -> None:
    # 主程序的“本机识别”窗口和 Worker 应用各带一份，内容必须一致。
    apps = Path(__file__).resolve().parents[1] / "desktop" / "apps"
    main = (apps / "videoturbo" / "ui" / "models.js").read_bytes()
    assert main == (apps / "worker" / "ui" / "models.js").read_bytes()


def test_console_favicon_matches_the_logo() -> None:
    # 控制台图标、桌面版图标（make-icons.sh 按同样的坐标画）和头像是同一个图形。
    import re

    from video_turbo.orchestrator.dashboard import FAVICON_SVG

    project = Path(__file__).resolve().parents[1]
    shapes = re.compile(r'<rect x="[^"]+" y="[^"]+" width="[^"]+" height="[^"]+" rx="[^"]+"(?: opacity="[^"]+")?/>')
    logo = shapes.findall((project / "desktop" / "icons" / "logo.svg").read_text(encoding="utf-8"))
    assert len(logo) == 6
    assert shapes.findall(FAVICON_SVG) == logo
    assert shapes.findall((project / "docs" / "images" / "brand" / "avatar.svg").read_text(encoding="utf-8")) == logo
