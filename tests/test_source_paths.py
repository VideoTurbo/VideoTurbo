from pathlib import PurePosixPath, PureWindowsPath

import pytest

from video_turbo import source_paths
from video_turbo.orchestrator import libraries_api


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("D:\\影片\\", "D:/影片"),
        ("d:/影片//日剧", "D:/影片/日剧"),
        ("D:", "D:/"),
        ("D:\\", "D:/"),
        ("\\\\nas\\share\\影片", "//nas/share/影片"),
        ("//nas/share/", "//nas/share"),
    ],
)
def test_windows_local_paths_use_drive_or_share_roots(value: str, expected: str) -> None:
    assert source_paths.normalize(value, local=True, windows=True) == expected


@pytest.mark.parametrize("value", ["/media/影片", "影片", "//nas", "D:/影片/../x", "//nas/../x"])
def test_windows_local_paths_must_be_full_and_without_dots(value: str) -> None:
    with pytest.raises(ValueError):
        source_paths.normalize(value, local=True, windows=True)


def test_cloud_sources_always_start_with_slash_even_on_windows() -> None:
    assert source_paths.normalize("\\影片\\日剧\\", local=False, windows=True) == "/影片/日剧"
    with pytest.raises(ValueError):
        source_paths.normalize("D:/影片", local=False, windows=True)


def test_posix_paths_are_unchanged() -> None:
    assert source_paths.normalize("/media//library_1/", local=True, windows=False) == "/media/library_1"
    assert source_paths.normalize("/", local=True, windows=False) == "/"
    with pytest.raises(ValueError):
        source_paths.normalize("D:/影片", local=True, windows=False)


def test_parent_and_root_on_windows() -> None:
    assert source_paths.parent("D:/影片", local=True, windows=True) == "D:/"
    assert source_paths.parent("D:/影片/日剧", local=True, windows=True) == "D:/影片"
    assert source_paths.parent("D:/", local=True, windows=True) == "D:/"
    assert source_paths.parent("//nas/share/影片", local=True, windows=True) == "//nas/share"
    assert source_paths.is_root("//nas/share", local=True, windows=True)
    assert source_paths.parent("/影片", local=False, windows=True) == "/"


def test_contains_handles_roots_that_end_with_slash() -> None:
    assert source_paths.contains("D:/", "D:/影片")
    assert source_paths.contains("/", "/media")
    assert source_paths.contains("D:/影片", "D:/影片/日剧")
    assert not source_paths.contains("D:/影片", "D:/影片2")
    assert not source_paths.contains("D:/影片", "D:/影片")
    assert source_paths.overlap("D:/影片", "D:/影片")
    assert source_paths.overlap("D:/", "D:/影片/日剧")
    assert not source_paths.overlap("D:/影片", "E:/影片")


def test_text_always_uses_forward_slashes() -> None:
    assert source_paths.text(PureWindowsPath("D:\\影片\\a.mkv")) == "D:/影片/a.mkv"
    assert source_paths.text(PureWindowsPath("/网盘/a.mkv")) == "/网盘/a.mkv"
    assert source_paths.text(PurePosixPath("/media/a.mkv")) == "/media/a.mkv"


def test_media_roots_use_the_platform_path_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(libraries_api.os, "pathsep", ";")
    monkeypatch.setattr(source_paths, "WINDOWS", False)
    monkeypatch.setenv("VIDEO_TURBO_MEDIA_ROOTS", "/Volumes;/Users/me/;;relative")
    assert libraries_api.media_roots() == ["/Volumes", "/Users/me"]


def test_windows_media_roots_default_to_drives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_TURBO_MEDIA_ROOTS", raising=False)
    monkeypatch.setattr(source_paths, "WINDOWS", True)
    monkeypatch.setattr(source_paths, "windows_drives", lambda: ["C:/", "D:/"])
    assert libraries_api.media_roots() == ["C:/", "D:/"]
