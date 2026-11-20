"""多语言：后端显示给用户的文字（接口报错、通知、任务失败原因等）。

写法：需要显示给用户的文字一律用 ``msg("键", 参数=值)`` 生成，不直接写中文。
``msg`` 返回一个带标记的字符串，可以像普通字符串一样拼接、作为异常信息、存进数据库；
真正的翻译在离用户最近的地方完成：

- 接口响应：``I18nMiddleware`` 按请求的 Accept-Language 把 JSON 响应里的标记换成对应语言；
- 推送通知：按 ``notification_locale()``（环境变量 VIDEO_TURBO_LANGUAGE）翻译后发送；
- 日志：始终用默认语言（简体中文），见 ``LocalizedFormatter``。

所以保存在数据库里的通知、任务失败原因，会按查看者当时的界面语言显示。
插件、机器传回的原始报错没有标记，原样显示。

文案在同目录的 ``zh_hans.py``（源语言）和 ``en.py`` 中，两边的键和 ``{参数}`` 必须一致，
由 tests/test_i18n.py 检查。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..environment import getenv
from . import en, zh_hans

DEFAULT_LOCALE = "zh-Hans"
CATALOGS: dict[str, dict[str, str]] = {"zh-Hans": zh_hans.MESSAGES, "en": en.MESSAGES}
LOCALES = tuple(CATALOGS)
LANGUAGE_ENV = "VIDEO_TURBO_LANGUAGE"

# 标记的格式：⟦i18n:{"k": 键, "p": {参数}}⟧。SQL 里需要按行拼出标记时用这两个常量。
MARKER_OPEN = _OPEN = "⟦i18n:"
MARKER_CLOSE = _CLOSE = "⟧"
_decoder = json.JSONDecoder()
_current: ContextVar[str] = ContextVar("video_turbo_locale", default=DEFAULT_LOCALE)


def normalize_locale(value: str | None, fallback: str = DEFAULT_LOCALE) -> str:
    """把 zh-CN、zh_TW、en-US 之类的写法归到支持的语言；不认识时返回 ``fallback``。"""
    code = (value or "").strip().replace("_", "-").lower()
    if not code:
        return fallback
    if code.startswith("zh"):
        return "zh-Hans"
    if code.startswith("en"):
        return "en"
    return fallback


def negotiate(accept_language: str | None) -> str:
    """按 Accept-Language 选语言；没有请求头时用默认语言，其他语言的用户看英文。"""
    if not accept_language:
        return DEFAULT_LOCALE
    ranked = []
    for index, part in enumerate(accept_language.split(",")):
        code, _, quality = part.strip().partition(";q=")
        try:
            weight = float(quality) if quality else 1.0
        except ValueError:
            weight = 0.0
        ranked.append((-weight, index, code))
    for _, _, code in sorted(ranked):
        locale = normalize_locale(code, fallback="")
        if locale:
            return locale
    return "en"


def current_locale() -> str:
    return _current.get()


@contextmanager
def use_locale(locale: str) -> Iterator[None]:
    token = _current.set(normalize_locale(locale))
    try:
        yield
    finally:
        _current.reset(token)


def notification_locale() -> str:
    """推送通知（Webhook 等）使用的语言：环境变量 VIDEO_TURBO_LANGUAGE，默认简体中文。"""
    return normalize_locale(getenv(LANGUAGE_ENV))


def msg(key: str, /, **params: Any) -> str:
    """生成一段待翻译的文字。参数可以是数字、字符串、另一段 ``msg``，或者它们的列表（按语言习惯连接）。"""
    if key not in zh_hans.MESSAGES:
        raise KeyError(f"没有这条文案：{key}")
    payload = json.dumps({"k": key, "p": params}, ensure_ascii=False, default=str)
    return f"{_OPEN}{payload}{_CLOSE}"


def localized(value: Any) -> str:
    """插件提供的文字：字符串原样返回；``{"zh-Hans": "…", "en": "…"}`` 这样的字典转成按查看者语言显示的标记。

    语言键可以写 zh、zh-CN、en-US 等，按 ``normalize_locale`` 归类；没有当前语言时依次用简体中文、英文、第一项。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        texts: dict[str, str] = {}
        for code, text in value.items():
            if not isinstance(text, str) or not text.strip():
                continue
            texts.setdefault(normalize_locale(str(code), fallback=str(code)), text)
        if not texts:
            return ""
        payload = json.dumps({"t": texts}, ensure_ascii=False)
        return f"{_OPEN}{payload}{_CLOSE}"
    return str(value)


def _pick(texts: dict[str, str], locale: str) -> str:
    for code in (locale, DEFAULT_LOCALE, "en"):
        if texts.get(code):
            return texts[code]
    return next(iter(texts.values()), "")


def t(key: str, /, locale: str | None = None, **params: Any) -> str:
    """直接翻译成某种语言（默认当前请求的语言）。"""
    locale = normalize_locale(locale) if locale else current_locale()
    template = CATALOGS.get(locale, {}).get(key) or zh_hans.MESSAGES.get(key) or key
    separator = CATALOGS.get(locale, {}).get("common.listSeparator", ", ")
    rendered = {name: _render_param(value, locale, separator) for name, value in params.items()}
    try:
        return template.format(**rendered)
    except (KeyError, IndexError, ValueError):
        logging.getLogger(__name__).warning("文案 %s 的参数不匹配：%s", key, sorted(params))
        return template


def _render_param(value: Any, locale: str, separator: str) -> Any:
    """参数里的 msg 标记一起翻译；列表按语言的习惯连接（中文用“、”，英文用“, ”）。"""
    if isinstance(value, str):
        return render(value, locale)
    if isinstance(value, list | tuple):
        return separator.join(render(str(item), locale) for item in value)
    return value


def render(text: str, locale: str | None = None) -> str:
    """把字符串里的所有 ``msg`` 标记换成对应语言的文字；没有标记时原样返回。"""
    if _OPEN not in text:
        return text
    parts: list[str] = []
    position = 0
    while (start := text.find(_OPEN, position)) != -1:
        parts.append(text[position:start])
        try:
            data, end = _decoder.raw_decode(text, start + len(_OPEN))
        except ValueError:
            parts.append(text[start : start + len(_OPEN)])
            position = start + len(_OPEN)
            continue
        if not text.startswith(_CLOSE, end) or not isinstance(data, dict) or not ("k" in data or "t" in data):
            parts.append(text[start:end])
            position = end
            continue
        if "t" in data:
            parts.append(_pick(dict(data["t"]), normalize_locale(locale) if locale else current_locale()))
        else:
            parts.append(t(str(data["k"]), locale, **(data.get("p") or {})))
        position = end + len(_CLOSE)
    parts.append(text[position:])
    return "".join(parts)


def clip(text: str, limit: int, locale: str | None = None) -> str:
    """截短文字但不截断 ``msg`` 标记：本来就不超长时原样返回；带标记又超长时先翻译成文字再截短。"""
    if len(text) <= limit:
        return text
    if _OPEN in text:
        text = render(text, locale or DEFAULT_LOCALE)
    return text[:limit]


def render_data(value: Any, locale: str | None = None) -> Any:
    """递归翻译 JSON 数据中的所有字符串。"""
    if isinstance(value, str):
        return render(value, locale)
    if isinstance(value, dict):
        return {key: render_data(item, locale) for key, item in value.items()}
    if isinstance(value, list):
        return [render_data(item, locale) for item in value]
    return value


def system_locale() -> str:
    """显卡电脑上的程序和安装脚本使用的语言：VIDEO_TURBO_LANGUAGE，否则按系统语言（中文系统用简体中文，其他用英文）。"""
    explicit = getenv(LANGUAGE_ENV)
    if explicit:
        return normalize_locale(explicit)
    import locale as _locale

    for value in (getenv("LC_ALL"), getenv("LC_MESSAGES"), getenv("LANG"), _locale.getlocale()[0]):
        code = (value or "").lower()
        if code.startswith(("zh", "chinese")):
            return "zh-Hans"
        if code and code not in {"c", "posix", "c.utf-8"}:
            return "en"
    return "en"


class LocalizedFormatter(logging.Formatter):
    """日志里出现的 ``msg`` 标记按指定语言显示（主程序的日志用默认语言）。"""

    def __init__(self, *args: Any, locale: str = DEFAULT_LOCALE, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.locale = locale

    def format(self, record: logging.LogRecord) -> str:
        return render(super().format(record), self.locale)
