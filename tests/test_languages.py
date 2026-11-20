from pathlib import Path

import pytest
from conftest import raises_rendered

from video_turbo import languages
from video_turbo.i18n import render_data
from video_turbo.languages import CHINESE_UNSPECIFIED
from video_turbo.naming import published_subtitle_path


def test_table_codes_and_roles() -> None:
    assert [language.code for language in languages.LANGUAGES] == [
        "ja", "en", "ko", "zh-CN", "zh-TW",
    ]
    assert [language.code for language in languages.sources()] == ["ja", "en", "ko"]
    assert {language.code for language in languages.targets()} == {
        "ja", "en", "ko", "zh-CN", "zh-TW",
    }
    for language in languages.sources():
        assert language.asr_code


def test_get_rejects_unknown_code() -> None:
    with raises_rendered(ValueError, "不支持的语言"):
        languages.get("fr")


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("ja", "ja"),
        ("JPN", "ja"),
        ("eng", "en"),
        ("kor", "ko"),
        ("chs", "zh-CN"),
        ("zh-Hans", "zh-CN"),
        ("zh-TW", "zh-TW"),
        ("zh-hk", "zh-TW"),
        ("chi", CHINESE_UNSPECIFIED),
        ("zh", CHINESE_UNSPECIFIED),
        ("zh-xx", CHINESE_UNSPECIFIED),
        ("und", None),
        ("", None),
    ],
)
def test_match_tag(tag: str, expected: str | None) -> None:
    assert languages.match_tag(tag) == expected


def test_metadata_prefers_specific_chinese_variant() -> None:
    assert languages.match_metadata("chinese simplified") == {"zh-CN"}
    assert languages.match_metadata("chi 繁體中文") == {"zh-TW"}
    assert languages.match_metadata("中文字幕") == {CHINESE_UNSPECIFIED}
    assert languages.match("English SDH") == "en"
    assert languages.match("commentary") is None


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".chs.srt", "zh-CN"),
        (".zh-tw.ass", "zh-TW"),
        (".ja.srt", "ja"),
        (".en.forced.srt", "en"),
        (".kor(1).srt", "ko"),
        (".zh.default.srt", CHINESE_UNSPECIFIED),
        (".srt", None),
    ],
)
def test_match_filename(suffix: str, expected: str | None) -> None:
    assert languages.match_filename(suffix) == expected


@pytest.mark.parametrize(
    ("found", "target", "accept", "expected"),
    [
        ("zh-CN", "zh-CN", False, True),
        ("zh-TW", "zh-CN", True, True),
        ("zh-TW", "zh-CN", False, False),
        (CHINESE_UNSPECIFIED, "zh-TW", False, True),
        (CHINESE_UNSPECIFIED, "en", True, False),
        ("en", "en", False, True),
        ("ja", "en", True, False),
        (None, "en", True, False),
    ],
)
def test_satisfies_target(found, target, accept, expected) -> None:
    assert languages.satisfies_target(found, target, accept_other_variant=accept) is expected


def test_validate_pair() -> None:
    languages.validate_pair("ja", "zh-CN")
    languages.validate_pair("en", "ja")
    with raises_rendered(ValueError, "不能相同"):
        languages.validate_pair("en", "en")
    with raises_rendered(ValueError, "不能作为源语言"):
        languages.validate_pair("zh-CN", "en")
    with raises_rendered(ValueError, "不支持"):
        languages.validate_pair("ja", "fr")


@pytest.mark.parametrize(
    ("target", "name"),
    [
        ("zh-CN", "movie.zh.default.srt"),
        ("zh-TW", "movie.zh-TW.default.srt"),
        ("en", "movie.en.default.srt"),
        ("ja", "movie.ja.default.srt"),
        ("ko", "movie.ko.default.srt"),
    ],
)
def test_published_subtitle_path_uses_publish_tag(target: str, name: str) -> None:
    assert published_subtitle_path(Path("/media/movie.mkv"), target).name == name


def test_public_table_lists_roles() -> None:
    table = {item["code"]: render_data(item) for item in languages.public_table()}
    assert table["zh-CN"] == {
        "code": "zh-CN", "label": "简体中文", "can_be_source": False, "can_be_target": True,
    }
