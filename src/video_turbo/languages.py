"""内置语言表：媒体库可选的源语言和目标语言，以及识别、排版、发布和提示词所需的语言数据。

增加语言 = 在 LANGUAGES 里加一项。代码只按这里的数据工作，不再在各处写死某种语言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .i18n import msg

LayoutKind = Literal["cjk", "spaced"]

# 不区分简繁的中文标签（zh、chi 等）：对简中、繁中都算“已有目标字幕”。
CHINESE_UNSPECIFIED = "zh"
DEFAULT_SOURCE_LANGUAGE = "ja"
DEFAULT_TARGET_LANGUAGE = "zh-CN"


@dataclass(frozen=True, slots=True)
class LanguagePair:
    """媒体库或任务的语言对；代码必须在语言表内。"""

    source: str = DEFAULT_SOURCE_LANGUAGE
    target: str = DEFAULT_TARGET_LANGUAGE

    @property
    def source_language(self) -> Language:
        return get(self.source)

    @property
    def target_language(self) -> Language:
        return get(self.target)


@dataclass(frozen=True, slots=True)
class Layout:
    """目标语言的字幕排版：cjk 按字符数折行，spaced 按空格分词折行。"""

    kind: LayoutKind
    max_chars_per_line: int
    max_chars_per_second: float


@dataclass(frozen=True, slots=True)
class PromptTerms:
    """拼翻译提示词时使用的语言相关片段。"""

    # 作为源语言或目标语言时的称呼，例如“日语”“简体中文”。
    name: str
    # 字幕译者头衔里的简称，例如“中文字幕译者”的“中文”。
    subtitle_name: str
    # 作源语言：原文带敬语体系时，第 1 条要求保留敬语。
    honorifics: bool = False
    # 作源语言：附加要求（例如日语省略主语），插在通用的第 3 条之后。
    source_rules: tuple[str, ...] = ()
    # 作源语言：译文中不应保留的原文杂质，接在“不保留”之后。
    source_noise: str = ""
    # 作目标语言：每个 cue 可独立显示的要求，接在“应结合前后内容理解；”之后。
    fragment_rule: str = ""
    # 作目标语言：标点要求，例如“简体中文标点”。
    punctuation: str = ""
    # 作目标语言：源语言省略主语时的处理方式。
    omitted_subject: str = ""


@dataclass(frozen=True, slots=True)
class Language:
    code: str
    label: str
    # 字幕轨、音轨语言标签（小写）。
    aliases: frozenset[str]
    # 字幕轨 title、handler_name 等元数据里的关键词（小写）。
    metadata_markers: frozenset[str]
    # 元数据里作为独立单词出现才算的短代码，例如 chs。
    metadata_codes: tuple[str, ...]
    # 外部字幕文件名（片名之后的部分）里的关键词，命中即判定。
    filename_markers: tuple[str, ...]
    # 外部字幕文件名里以“.代码.”或“.代码(”形式出现的语言代码。
    filename_codes: tuple[str, ...]
    # 发布文件名里的语言段：片名.{publish_tag}.default.srt。
    publish_tag: str
    layout: Layout
    prompt: PromptTerms
    can_be_source: bool
    can_be_target: bool
    # 传给识别后端的语言：任务传递 asr_code，WhisperJAV 的 Qwen3-ASR 使用 asr_name。
    # 只有可作源语言的语言需要。
    asr_code: str | None = None
    asr_name: str | None = None
    # 能否使用 WhisperJAV 第二遍的 anime-whisper（日语专用模型）。
    supports_whisperjav_ensemble: bool = False
    # 同一语言的不同书写变体共用的家族（简中、繁中都是 zh）。
    family: str | None = None
    # 文件名或目录名符合即视为已有硬编码目标语言字幕（与语言无关的 -C 规则在配置里）。
    burned_in_path_patterns: tuple[str, ...] = ()
    # 外部文本字幕在 UTF-8/UTF-16 之外尝试的本地编码。
    legacy_encodings: tuple[str, ...] = ()
    _metadata_pattern: re.Pattern[str] | None = field(default=None, compare=False, repr=False)


_CJK_LAYOUT = Layout("cjk", 16, 9.0)
_CHINESE_BURNED_IN = (
    r"(?i)(?:^|[-_.])(?:chs|cht)(?:$|[-_.])",
    r"中字|中文|简中|簡中|繁中",
)
_CHINESE_FRAGMENT = "但每个 cue 的中文必须能独立显示，禁止把中文词拆成“舒／服”之类的残片。"


def _language(**values) -> Language:
    codes = values.get("metadata_codes", ())
    pattern = (
        re.compile(rf"(?<![a-z])(?:{'|'.join(map(re.escape, codes))})(?![a-z])") if codes else None
    )
    return Language(**values, _metadata_pattern=pattern)


LANGUAGES: tuple[Language, ...] = (
    _language(
        code="ja",
        label=msg("language.ja"),
        aliases=frozenset({"ja", "jpn", "japanese"}),
        metadata_markers=frozenset({"japanese", "日本語"}),
        metadata_codes=(),
        filename_markers=(),
        filename_codes=("ja", "jpn", "japanese"),
        publish_tag="ja",
        layout=_CJK_LAYOUT,
        prompt=PromptTerms(
            name="日语",
            subtitle_name="日文",
            honorifics=True,
            source_rules=(
                (
                    "日语省略主语，而上下文无法确定时，{omitted_subject}，"
                    "不得猜测人物身份、性别或亲属关系。"
                ),
            ),
            source_noise="日文标点、罗马字噪声或无意义字符",
            fragment_rule="但每个 cue 的日文必须能独立显示，禁止把一个日文词拆到两条 cue 中。",
            punctuation="日文标点",
            omitted_subject="优先使用省略主语的自然日语",
        ),
        can_be_source=True,
        can_be_target=True,
        asr_code="ja",
        asr_name="Japanese",
        supports_whisperjav_ensemble=True,
        legacy_encodings=("shift_jis", "cp932"),
    ),
    _language(
        code="en",
        label=msg("language.en"),
        aliases=frozenset({"en", "eng", "english"}),
        metadata_markers=frozenset({"english"}),
        metadata_codes=(),
        filename_markers=(),
        filename_codes=("en", "eng", "english"),
        publish_tag="en",
        # 每行 42 字符、每秒 20 字符：常见英文字幕规范的上限。
        layout=Layout("spaced", 42, 20.0),
        prompt=PromptTerms(
            name="英语",
            subtitle_name="英文",
            source_noise="英文标点或无意义字符",
            fragment_rule="但每个 cue 的英文必须能独立显示，禁止把一个英文单词拆到两条 cue 中。",
            punctuation="英文标点",
            omitted_subject="优先使用不点明具体人物的自然英文表达",
        ),
        can_be_source=True,
        can_be_target=True,
        asr_code="en",
        asr_name="English",
        legacy_encodings=("cp1252",),
    ),
    _language(
        code="ko",
        label=msg("language.ko"),
        aliases=frozenset({"ko", "kor", "korean"}),
        metadata_markers=frozenset({"korean", "한국어"}),
        metadata_codes=(),
        filename_markers=(),
        filename_codes=("ko", "kor", "korean"),
        publish_tag="ko",
        # 韩文词之间有空格，按词折行；行长和阅读速度沿用中日文字幕的上限。
        layout=Layout("spaced", 16, 9.0),
        prompt=PromptTerms(
            name="韩语",
            subtitle_name="韩文",
            honorifics=True,
            source_rules=(
                (
                    "韩语常省略主语，而上下文无法确定时，{omitted_subject}，"
                    "不得猜测人物身份、性别或亲属关系。"
                ),
            ),
            source_noise="韩文标点或无意义字符",
            fragment_rule="但每个 cue 的韩文必须能独立显示，禁止把一个韩文词拆到两条 cue 中。",
            punctuation="韩文标点",
            omitted_subject="优先使用省略主语的自然韩语",
        ),
        can_be_source=True,
        can_be_target=True,
        asr_code="ko",
        asr_name="Korean",
        legacy_encodings=("cp949",),
    ),
    _language(
        code="zh-CN",
        label=msg("language.zhHans"),
        aliases=frozenset({"chs", "zh-cn", "zh-sg", "zh-hans"}),
        metadata_markers=frozenset(
            {"简中", "簡中", "简体", "簡體", "简日", "簡日", "简英", "簡英",
             "gb2312", "gbk", "simplified", "zh-cn", "zh-hans"}
        ),
        metadata_codes=("chs",),
        filename_markers=("zh-cn", "chs", "简体", "simplified"),
        filename_codes=(),
        publish_tag="zh",
        layout=_CJK_LAYOUT,
        prompt=PromptTerms(
            name="简体中文",
            subtitle_name="中文",
            fragment_rule=_CHINESE_FRAGMENT,
            punctuation="简体中文标点",
            omitted_subject="优先使用省略主语的自然中文",
        ),
        can_be_source=False,
        can_be_target=True,
        family="zh",
        burned_in_path_patterns=_CHINESE_BURNED_IN,
    ),
    _language(
        code="zh-TW",
        label=msg("language.zhHant"),
        aliases=frozenset({"cht", "zh-tw", "zh-hk", "zh-mo", "zh-hant"}),
        metadata_markers=frozenset(
            {"繁中", "繁体", "繁體", "big5", "traditional", "zh-tw", "zh-hk", "zh-hant"}
        ),
        metadata_codes=("cht",),
        filename_markers=("zh-tw", "zh-hk", "cht", "繁体", "traditional"),
        filename_codes=(),
        # Emby 按 zh-TW 识别繁体中文外挂字幕。
        publish_tag="zh-TW",
        layout=_CJK_LAYOUT,
        prompt=PromptTerms(
            name="繁体中文",
            subtitle_name="中文",
            fragment_rule=_CHINESE_FRAGMENT,
            punctuation="繁体中文标点",
            omitted_subject="优先使用省略主语的自然中文",
        ),
        can_be_source=False,
        can_be_target=True,
        family="zh",
        burned_in_path_patterns=_CHINESE_BURNED_IN,
    ),
)

# 不区分简繁的中文：只用于“已有字幕”判断，不能作为媒体库的语言。
_CHINESE_UNSPECIFIED_ALIASES = frozenset({"zh", "zho", "chi", "cmn", "yue", "cn"})
_CHINESE_UNSPECIFIED_MARKERS = frozenset(
    {"中文", "中字", "汉语", "漢語", "中日", "日中", "中英", "英中",
     "chinese", "mandarin", "cantonese"}
)
_CHINESE_UNSPECIFIED_CODES = re.compile(r"(?<![a-z])(?:chi|zho|cmn|yue)(?![a-z])")
_CHINESE_UNSPECIFIED_FILENAME = re.compile(r"\.(?:zh|zho|chi)(?:\.|\()")

_BY_CODE = {language.code: language for language in LANGUAGES}


def get(code: str) -> Language:
    try:
        return _BY_CODE[code]
    except KeyError:
        raise ValueError(msg("language.unsupported", code=code)) from None


def is_known(code: str) -> bool:
    return code in _BY_CODE


def sources() -> list[Language]:
    return [language for language in LANGUAGES if language.can_be_source]


def targets() -> list[Language]:
    return [language for language in LANGUAGES if language.can_be_target]


def public_table() -> list[dict]:
    """控制台使用的语言表。"""
    return [
        {
            "code": language.code,
            "label": language.label,
            "can_be_source": language.can_be_source,
            "can_be_target": language.can_be_target,
        }
        for language in LANGUAGES
    ]


def _specific_only(found: set[str]) -> set[str]:
    # 同时命中“中文”和“简体”等具体变体时，以具体变体为准。
    if CHINESE_UNSPECIFIED in found and any(
        _BY_CODE[code].family == CHINESE_UNSPECIFIED for code in found if code in _BY_CODE
    ):
        found.discard(CHINESE_UNSPECIFIED)
    return found


def match_tag(tag: str) -> str | None:
    """按语言标签（ja、jpn、zh-tw…）返回语言代码；不区分简繁的中文返回 CHINESE_UNSPECIFIED。"""
    tag = tag.strip().lower()
    if not tag:
        return None
    for language in LANGUAGES:
        if tag in language.aliases:
            return language.code
    if tag in _CHINESE_UNSPECIFIED_ALIASES or tag.startswith("zh-"):
        return CHINESE_UNSPECIFIED
    return None


def match(tag_or_title: str) -> str | None:
    """按语言标签或字幕轨标题返回语言代码；命中多种时返回语言表中靠前的一种。"""
    found = match_metadata(tag_or_title)
    for language in LANGUAGES:
        if language.code in found:
            return language.code
    return CHINESE_UNSPECIFIED if found else None


def match_metadata(text: str) -> set[str]:
    """在语言标签和字幕轨元数据中找到的全部语言代码。"""
    text = text.strip().lower()
    found: set[str] = set()
    for token in text.split():
        code = match_tag(token)
        if code is not None:
            found.add(code)
    for language in LANGUAGES:
        if any(marker in text for marker in language.metadata_markers) or (
            language._metadata_pattern is not None
            and language._metadata_pattern.search(text) is not None
        ):
            found.add(language.code)
    if any(marker in text for marker in _CHINESE_UNSPECIFIED_MARKERS) or (
        _CHINESE_UNSPECIFIED_CODES.search(text) is not None
    ):
        found.add(CHINESE_UNSPECIFIED)
    return _specific_only(found)


def match_filename(suffix_part: str) -> str | None:
    """按外部字幕文件名中片名之后的部分（小写）推断语言。"""
    for language in LANGUAGES:
        if any(marker in suffix_part for marker in language.filename_markers):
            return language.code
    for language in LANGUAGES:
        if language.filename_codes and re.search(
            rf"\.(?:{'|'.join(map(re.escape, language.filename_codes))})(?:\.|\()", suffix_part
        ):
            return language.code
    if _CHINESE_UNSPECIFIED_FILENAME.search(suffix_part):
        return CHINESE_UNSPECIFIED
    return None


def satisfies_target(found: str | None, target: str, *, accept_other_variant: bool) -> bool:
    """找到的字幕语言是否算“已有目标语言字幕”。"""
    if found is None:
        return False
    if found == target:
        return True
    family = _BY_CODE[target].family
    if family is None:
        return False
    if found == CHINESE_UNSPECIFIED:
        return family == CHINESE_UNSPECIFIED
    return accept_other_variant and found in _BY_CODE and _BY_CODE[found].family == family


def from_asr_code(code: str) -> Language:
    """Worker 按 claim 里的识别语言代码找回语言表条目。"""
    for language in LANGUAGES:
        if language.can_be_source and language.asr_code == code:
            return language
    raise ValueError(msg("language.unsupportedAsr", code=code))


def ensemble_languages() -> list[str]:
    """可以运行 WhisperJAV 双遍识别的源语言。"""
    return [language.code for language in LANGUAGES if language.supports_whisperjav_ensemble]


def validate_pair(source: str, target: str) -> None:
    """媒体库语言对的校验；不合法时抛出 ValueError。"""
    source_language = get(source)
    target_language = get(target)
    if not source_language.can_be_source:
        raise ValueError(msg("language.badSource", label=source_language.label))
    if not target_language.can_be_target:
        raise ValueError(msg("language.badTarget", label=target_language.label))
    if source == target:
        raise ValueError(msg("language.same"))
