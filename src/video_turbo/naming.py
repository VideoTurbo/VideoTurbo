import re
from pathlib import Path

from . import languages

# 片名末尾的已修复 / 中字标记：-U、-UC、-C（分隔符可以是 - _ .，大小写不限）。
_EDITION_SUFFIX = re.compile(r"[-_.](?:uc|u|c)$", re.IGNORECASE)


def published_subtitle_path(
    video: Path, target_language: str, subtitle_format: str = "srt"
) -> Path:
    """返回媒体服务器可识别语言和默认标记的字幕发布路径：片名.{语言}.default.srt。"""
    extension = subtitle_format.lstrip(".")
    tag = languages.get(target_language).publish_tag
    return video.with_name(f"{video.stem}.{tag}.default.{extension}")


def title_key(stem: str) -> str:
    """去掉末尾 -U / -UC / -C 标记后的片名，用于识别影片修复或手动改名后的同一部影片。

    ABC-123、ABC-123-C、ABC-123-U、abc-123-uc 都得到 ``abc-123``。
    """
    key = stem.strip().lower()
    while True:
        stripped = _EDITION_SUFFIX.sub("", key)
        if stripped == key or not stripped:
            return key
        key = stripped


# 缓存目录名里的任务 ID 长度：足够区分同名影片，又不影响辨认片名。
SHORT_ID_LENGTH = 12
_MAX_TITLE_LENGTH = 80
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NON_ASCII_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def short_job_id(job_id: str) -> str:
    return job_id.replace("-", "")[:SHORT_ID_LENGTH]


def job_dir_name(title: str, job_id: str, *, ascii_only: bool = False) -> str:
    """任务缓存目录名 ``<片名>-<短ID>``，用户能按片名直接找到目录。

    ascii_only 用于 Jasna：它要求工作路径只含英文字符，其余字符替换为 ``_``。
    """
    pattern = _NON_ASCII_CHARS if ascii_only else _UNSAFE_CHARS
    cleaned = pattern.sub("_", title.strip())[:_MAX_TITLE_LENGTH].strip(" .")
    suffix = short_job_id(job_id)
    return f"{cleaned}-{suffix}" if cleaned.strip("_") else suffix


def job_dir(root: Path, title: str, job_id: str, *, ascii_only: bool = False) -> Path:
    """返回任务的缓存目录；影片改名后仍沿用按短 ID 找到的已有目录。"""
    suffix = short_job_id(job_id)
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry.name == suffix or entry.name.endswith(f"-{suffix}")):
                return entry
    return root / job_dir_name(title, job_id, ascii_only=ascii_only)
