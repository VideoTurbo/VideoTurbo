"""影片修复（Jasna）在主控侧的命名、资格判断与发布逻辑。

Worker 只负责下载原片、运行 Jasna 和回传结果；所有媒体库文件操作都在主控完成：

1. 回传文件先写入影片所在目录的隐藏临时文件，避免跨文件系统再复制一次大文件。
2. 校验大小、SHA-256 与时长后，把原片移到媒体库外的备份目录。
3. 临时文件改为新片名（-U / -UC），同名字幕、nfo、海报等附属文件一起改名。
4. 写入 ``<新片名>.jasna.json`` 标记文件；文件夹名包含原片名且只含这一部影片时，一并改名。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .i18n import msg
from .models import utc_now_iso

MARKER_SUFFIX = ".jasna.json"
UPLOAD_SUFFIX = ".jasna-upload"
# Jasna 能直接写入的容器；其他输入（如 avi、ts）统一输出为 mp4。
WRITABLE_EXTENSIONS = {"mp4", "mkv", "mov", "m4v"}
_TRAILING_C = re.compile(r"([-_.])([cC])$")


def restored_stem(stem: str) -> str:
    """ABC-123 → ABC-123-U；ABC-123-C → ABC-123-UC（保留原分隔符与大小写风格）。"""
    match = _TRAILING_C.search(stem)
    if match:
        separator, letter = match.groups()
        return stem[: match.start()] + separator + ("UC" if letter == "C" else "uc")
    return f"{stem}-U"


def output_extension(source_extension: str) -> str:
    extension = source_extension.lower().lstrip(".")
    return extension if extension in WRITABLE_EXTENSIONS else "mp4"


def marker_path(video: Path) -> Path:
    return video.with_name(video.stem + MARKER_SUFFIX)


def upload_temp_path(source: Path, output_ext: str) -> Path:
    """回传临时文件：隐藏文件、非视频扩展名，扫描和 Emby 都不会把它当影片。"""
    return source.with_name(f".{restored_stem(source.stem)}.{output_ext}{UPLOAD_SUFFIX}")


def matches_skip_name(video: Path, patterns: Iterable[str]) -> bool:
    candidates = (video.stem, video.parent.name)
    return any(re.search(pattern, candidate) for pattern in patterns for candidate in candidates)


def ineligible_reason(video: Path, patterns: Iterable[str]) -> str | None:
    """返回不能影片修复的原因；None 表示可以影片修复。"""
    if matches_skip_name(video, patterns):
        return msg("restore.files.alreadyMarked")
    if marker_path(video).is_file():
        return msg("restore.files.markerFile", name=marker_path(video).name)
    return None


def _video_stems(directory: Path, video_extensions: Iterable[str], exclude: Path) -> list[str]:
    extensions = {"." + value.lower().lstrip(".") for value in video_extensions}
    stems: list[str] = []
    for entry in directory.iterdir():
        if entry == exclude or entry.name.startswith(".") or not entry.is_file():
            continue
        if entry.suffix.lower() in extensions:
            stems.append(entry.stem)
    return stems


def sidecar_files(source: Path, video_extensions: Iterable[str]) -> list[Path]:
    """与原片同名开头的附属文件（字幕、nfo、-poster.jpg 等），不含其他影片的文件。

    例如目录里同时有 ABC-123.mp4 与 ABC-123-C.mp4 时，ABC-123-C.nfo 属于后者。
    """
    stem = source.stem
    lowered = stem.lower()
    extensions = {"." + value.lower().lstrip(".") for value in video_extensions}
    others = [
        other.lower()
        for other in _video_stems(source.parent, video_extensions, source)
        if other.lower() != lowered and other.lower().startswith(lowered)
    ]
    sidecars: list[Path] = []
    for entry in sorted(source.parent.iterdir()):
        name = entry.name
        if entry == source or not entry.is_file() or name.startswith("."):
            continue
        if entry.suffix.lower() in extensions:
            continue
        lowered_name = name.lower()
        if not lowered_name.startswith(lowered):
            continue
        rest = name[len(stem) :]
        if not rest or rest[0] not in ".-_ ":
            continue
        if any(lowered_name.startswith(other) and lowered_name[len(other) : len(other) + 1] in {".", "-", "_", " "} for other in others):
            continue
        if name.endswith((MARKER_SUFFIX, UPLOAD_SUFFIX)):
            continue
        sidecars.append(entry)
    return sidecars


def folder_rename_target(
    source: Path, library_root: Path, video_extensions: Iterable[str]
) -> Path | None:
    """文件夹名包含原片名且只有这一部影片时，返回改名后的文件夹路径。"""
    folder = source.parent
    if Path(os.path.abspath(folder)) == Path(os.path.abspath(library_root)):
        return None
    if _video_stems(folder, video_extensions, source):
        return None
    index = folder.name.lower().find(source.stem.lower())
    if index < 0:
        return None
    new_name = (
        folder.name[:index] + restored_stem(source.stem) + folder.name[index + len(source.stem) :]
    )
    target = folder.with_name(new_name)
    if target.exists():
        return None
    return target


@dataclass(slots=True)
class FinalizeResult:
    output_path: Path
    backup_path: Path
    marker: Path
    renamed: dict[str, str] = field(default_factory=dict)
    folder_renamed_from: str | None = None
    folder_renamed_to: str | None = None


def _move(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(msg("restore.files.targetExists", target=target))
    try:
        os.replace(source, target)
    except OSError:
        # 备份目录通常在另一个卷上，只能复制后删除；shutil.move 会保留修改时间。
        shutil.move(str(source), str(target))


def backup_path_for(source: Path, library_root: Path, library_name: str, backup_dir: Path) -> Path:
    relative = Path(os.path.relpath(source, library_root))
    if relative.parts and relative.parts[0] == "..":
        relative = Path(source.name)
    target = backup_dir / library_name / relative
    if not target.exists():
        return target
    # 同一路径曾经备份过：附加时间戳，绝不覆盖已有备份。
    stamp = utc_now_iso().replace(":", "").replace("-", "")[:15]
    return target.with_name(f"{target.stem}.{stamp}{target.suffix}")


def finalize_restore(
    *,
    source: Path,
    upload: Path,
    output_ext: str,
    library_root: Path,
    library_name: str,
    backup_dir: Path,
    video_extensions: Iterable[str],
    marker_payload: dict[str, Any],
    progress: Callable[[str], None] = lambda _stage: None,
) -> FinalizeResult:
    """把已校验的回传文件发布到媒体库。调用前必须已校验 upload 的完整性。"""
    if not source.is_file():
        raise FileNotFoundError(msg("restore.files.sourceMissing", source=source))
    if not upload.is_file():
        raise FileNotFoundError(msg("restore.files.uploadMissing", upload=upload))
    new_stem = restored_stem(source.stem)
    output = source.with_name(f"{new_stem}.{output_ext}")
    if output.exists():
        raise FileExistsError(msg("restore.files.outputExists", output=output))
    sidecars = sidecar_files(source, video_extensions)
    sidecar_targets = {
        path: path.with_name(new_stem + path.name[len(source.stem) :]) for path in sidecars
    }
    conflicts = [str(target) for target in sidecar_targets.values() if target.exists()]
    if conflicts:
        raise FileExistsError(msg("restore.files.sidecarExists", files=conflicts))
    folder_target = folder_rename_target(source, library_root, video_extensions)

    progress(msg("restore.files.stage.backup"))
    backup = backup_path_for(source, library_root, library_name, backup_dir)
    _move(source, backup)

    progress(msg("restore.files.stage.publish"))
    os.replace(upload, output)
    renamed: dict[str, str] = {}
    for path, target in sidecar_targets.items():
        os.replace(path, target)
        renamed[path.name] = target.name

    marker = marker_path(output)
    payload = {
        **marker_payload,
        "original_name": source.name,
        "output_name": output.name,
        "backup_path": str(backup),
        "finished_at": utc_now_iso(),
    }
    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, marker)

    result = FinalizeResult(output_path=output, backup_path=backup, marker=marker, renamed=renamed)
    if folder_target is not None:
        progress(msg("restore.files.stage.folder"))
        folder = output.parent
        try:
            os.replace(folder, folder_target)
        except OSError:
            # 文件夹改名失败不影响已发布的影片，保留在原文件夹。
            return result
        result.folder_renamed_from = str(folder)
        result.folder_renamed_to = str(folder_target)
        result.output_path = folder_target / output.name
        result.marker = folder_target / marker.name
    return result


def relocated_path(path: str | Path | None, result: FinalizeResult, source: Path) -> Path | None:
    """把原片目录下的文件路径映射到发布后的新位置（用于更新已发布字幕路径）。"""
    if path is None:
        return None
    candidate = Path(path)
    if candidate.parent != source.parent:
        return candidate
    name = result.renamed.get(candidate.name, candidate.name)
    folder = Path(result.folder_renamed_to) if result.folder_renamed_to else source.parent
    return folder / name
