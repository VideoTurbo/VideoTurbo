import hashlib
import importlib.util
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from conftest import raises_rendered


@pytest.fixture
def packager():
    path = Path(__file__).resolve().parents[1] / "tools/package_release.py"
    spec = importlib.util.spec_from_file_location("package_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path, packager):
    root = tmp_path / "project"
    for name in packager.REQUIRED_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("public deployment file\n", encoding="utf-8")
    (root / "src/video_turbo/__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    return root


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.skipif(os.name == "nt", reason="发布包在 Linux 上制作，要检查脚本的可执行权限")
def test_package_includes_deployment_sources_without_local_secrets(project, tmp_path, packager):
    safe_files = {
        "src/video_turbo/worker/main.py": b"# source including uncommitted changes\n",
        "deploy/worker/linux/worker.sh": b"#!/bin/sh\necho test\n",
        "README.en.md": b"# English readme\n",
        "tools/package_release.py": b"# packaging tool\n",
    }
    sensitive_files = (
        ".env", "deploy/server/.env", "deploy/server/docker-compose.yml", "config/worker.yaml",
        "deploy/worker/linux/worker.log", "config/models/weights.bin",
        "src/video_turbo/__pycache__/stale.py", "src/video_turbo/__pycache__/main.pyc",
        "src/video_turbo/.local.py", "src/video_turbo/cache/private.py",
        "work/job/audio.opus", "runtime/local.db", "logs/worker.log",
        ".venv/lib/site.py", ".venv-whisperjav/lib/site.py", ".git/config",
        "build/lib/video_turbo/main.py", "dist/old/src/video_turbo/main.py",
    )
    for name, content in safe_files.items():
        write(project, name, content)
    for name in sensitive_files:
        write(project, name, b"DO_NOT_SHIP_SECRET")
    launcher = project / "deploy/worker/linux/worker.sh"
    launcher.chmod(0o755)
    output = tmp_path / "release"
    manifest = packager.build_release(project, output)
    assert manifest["version"] == "1.2.3"
    assert (output / "deploy/server/docker-compose.example.yml").is_file()
    assert (output / "config/worker.example.yaml").is_file()
    assert (output / "config/worker.example.yaml").is_file()
    assert (output / "config/worker.example.yaml").is_file()
    assert sorted(path.name for path in (output / "deploy/worker/linux").iterdir()) == ["worker.sh"]
    assert (output / "deploy/server/Dockerfile").is_file()
    for name, content in safe_files.items():
        assert (output / name).read_bytes() == content
    for name in sensitive_files:
        assert not (output / name).exists(), name
    assert (output / "deploy/worker/linux/worker.sh").stat().st_mode & 0o111
    assert (output / "UPDATE.txt").read_text(encoding="utf-8-sig").startswith("video-turbo")
    assert json.loads((output / packager.MANIFEST).read_text()) == manifest
    for item in manifest["files"]:
        payload = (output / item["path"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(payload).hexdigest()
        assert item["size"] == len(payload)
        assert b"DO_NOT_SHIP_SECRET" not in payload


def test_repack_removes_deleted_sources_and_keeps_latest_contents(project, tmp_path, packager):
    output = tmp_path / "release"
    old = write(project, "src/video_turbo/removed.py", b"old code")
    packager.build_release(project, output)
    old.unlink()
    write(project, "src/video_turbo/__init__.py", b'__version__ = "1.2.4"\n')
    manifest = packager.build_release(project, output)
    assert not (output / "src/video_turbo/removed.py").exists()
    assert (output / "src/video_turbo/__init__.py").read_bytes() == b'__version__ = "1.2.4"\n'
    assert manifest["version"] == "1.2.4"
    assert not list(tmp_path.glob(".video-turbo-package-*"))


def test_failed_copy_preserves_previous_package(project, tmp_path, packager, monkeypatch):
    output = tmp_path / "release"
    packager.build_release(project, output)
    before = (output / packager.MANIFEST).read_bytes()

    def failed_copy(*_args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(packager.shutil, "copy2", failed_copy)
    with raises_rendered(OSError, "simulated disk failure"):
        packager.build_release(project, output)
    assert (output / packager.MANIFEST).read_bytes() == before


def test_failed_publish_restores_previous_package(project, tmp_path, packager, monkeypatch):
    output = tmp_path / "release"
    packager.build_release(project, output)
    before = (output / packager.MANIFEST).read_bytes()
    rename = Path.rename

    def failed_publish(path, target):
        if path.name == "new":
            raise OSError("simulated publish failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", failed_publish)
    with raises_rendered(OSError, "simulated publish failure"):
        packager.build_release(project, output)
    assert (output / packager.MANIFEST).read_bytes() == before


@pytest.mark.parametrize("output_name", ["", "src", "config", "deploy", "tools"])
def test_refuses_to_overwrite_project_sources(project, packager, output_name):
    with pytest.raises(ValueError):
        packager.build_release(project, project / output_name)
    assert (project / "pyproject.toml").is_file()


def test_refuses_to_replace_unowned_or_modified_deployment_directory(project, tmp_path, packager):
    unowned = tmp_path / "actual-deployment"
    config = write(unowned, "config/worker.yaml", b"worker secret")
    with raises_rendered(ValueError, "拒绝覆盖"):
        packager.build_release(project, unowned)
    assert config.read_bytes() == b"worker secret"
    output = tmp_path / "release"
    packager.build_release(project, output)
    config = write(output, "config/worker.yaml", b"worker secret")
    with raises_rendered(ValueError, "非打包文件"):
        packager.build_release(project, output)
    assert config.read_bytes() == b"worker secret"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires privileges")
def test_refuses_symlinked_source_and_output(project, tmp_path, packager):
    link = project / "src/video_turbo/linked.py"
    link.symlink_to(project / "pyproject.toml")
    with raises_rendered(ValueError, "符号链接"):
        packager.build_release(project, tmp_path / "release")
    link.unlink()
    output = tmp_path / "linked-release"
    output.symlink_to(project, target_is_directory=True)
    with raises_rendered(ValueError, "符号链接"):
        packager.build_release(project, output)


@pytest.mark.skipif(os.name == "nt", reason="发布包在 Linux 上制作，要检查脚本的可执行权限")
def test_archives_wrap_release_in_single_top_level_folder(project, tmp_path, packager):
    launcher = write(project, "deploy/worker/linux/worker.sh", b"#!/bin/sh\necho test\n")
    launcher.chmod(0o755)
    output = tmp_path / "dist/video-turbo"
    manifest = packager.build_release(project, output)
    zip_path, tar_path = packager.build_archives(output)
    assert zip_path == tmp_path / "dist/video-turbo.zip"
    assert tar_path == tmp_path / "dist/video-turbo.tar.gz"
    expected = sorted(
        "video-turbo/" + name for name in [item["path"] for item in manifest["files"]] + [packager.MANIFEST]
    )
    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == expected
        assert archive.read("video-turbo/deploy/worker/linux/worker.sh") == launcher.read_bytes()
        mode = archive.getinfo("video-turbo/deploy/worker/linux/worker.sh").external_attr >> 16
        assert mode & 0o111
    with tarfile.open(tar_path) as archive:
        members = archive.getmembers()
        assert sorted(member.name for member in members) == expected
        assert all(member.isfile() and member.uid == 0 and not member.uname for member in members)
        assert archive.getmember("video-turbo/deploy/worker/linux/worker.sh").mode & 0o111
    # Extracting in the parent of an existing deployment merges in place and keeps local files.
    parent = tmp_path / "root"
    secret = write(parent, "video-turbo/config/worker.yaml", b"keep me")
    with tarfile.open(tar_path) as archive:
        archive.extractall(parent, filter="data")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(parent)
    assert secret.read_bytes() == b"keep me"
    assert sorted(path.name for path in parent.iterdir()) == ["video-turbo"]
    assert (parent / "video-turbo/UPDATE.txt").is_file()
    assert not list(output.parent.glob(".video-turbo-archive-*"))


def test_archives_are_rebuilt_and_refuse_non_file_targets(project, tmp_path, packager):
    output = tmp_path / "dist/video-turbo"
    packager.build_release(project, output)
    packager.build_archives(output)
    write(project, "src/video_turbo/added.py", b"new")
    packager.build_release(project, output)
    zip_path, _ = packager.build_archives(output)
    with zipfile.ZipFile(zip_path) as archive:
        assert "video-turbo/src/video_turbo/added.py" in archive.namelist()
    (tmp_path / "dist/video-turbo.zip").unlink()
    (tmp_path / "dist/video-turbo.zip").mkdir()
    with raises_rendered(ValueError, "不是普通文件"):
        packager.build_archives(output)


def test_main_reports_archives(project, tmp_path, packager, monkeypatch, capsys):
    monkeypatch.setattr(packager, "PROJECT_ROOT", project)
    output = tmp_path / "dist/video-turbo"
    monkeypatch.setattr(sys, "argv", ["package_release.py", "--output", str(output)])
    packager.main()
    out = capsys.readouterr().out
    assert "video-turbo.zip" in out and "video-turbo.tar.gz" in out
    assert "unzip -o video-turbo.zip" in out
