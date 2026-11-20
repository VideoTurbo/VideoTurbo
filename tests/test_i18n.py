import json
import re
import string
from typing import ClassVar

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from video_turbo import i18n
from video_turbo.i18n import en, render, zh_hans
from video_turbo.orchestrator.i18n_middleware import I18nMiddleware


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_catalogs_have_the_same_keys_and_placeholders() -> None:
    assert set(en.MESSAGES) == set(zh_hans.MESSAGES)
    for key, template in zh_hans.MESSAGES.items():
        assert _placeholders(en.MESSAGES[key]) == _placeholders(template), key
        assert en.MESSAGES[key].strip(), key


def test_every_msg_key_in_the_source_exists() -> None:
    from pathlib import Path

    root = Path(i18n.__file__).resolve().parents[1]
    used = set()
    for path in root.rglob("*.py"):
        used |= set(re.findall(r"\bmsg\(\s*\"([a-z0-9_.]+)\"", path.read_text(encoding="utf-8")))
    assert used <= set(zh_hans.MESSAGES), sorted(used - set(zh_hans.MESSAGES))


@pytest.mark.parametrize(
    ("header", "locale"),
    [
        (None, "zh-Hans"),
        ("en-US,en;q=0.9", "en"),
        ("zh-CN,zh;q=0.9,en;q=0.8", "zh-Hans"),
        ("zh-TW", "zh-Hans"),
        ("fr-FR,fr;q=0.9", "en"),
        ("fr;q=0.9,zh;q=0.5", "zh-Hans"),
    ],
)
def test_negotiate(header, locale) -> None:
    assert i18n.negotiate(header) == locale


def test_markers_survive_concatenation_nesting_and_storage() -> None:
    inner = i18n.msg("common.worker_token_invalid")
    text = f"原因：{i18n.msg('common.job_not_owned')} / {inner}"
    stored = json.loads(json.dumps({"error": text}, ensure_ascii=False))["error"]
    assert i18n.render(stored, "en") == (
        "原因：This job no longer belongs to this machine (it may have timed out and been reassigned)."
        " / The access key is incorrect."
    )
    assert i18n.render(stored, "zh-Hans").endswith("接入密钥不正确。")
    assert render(i18n.render("没有标记的原文", "en")) == "没有标记的原文"
    with pytest.raises(KeyError):
        i18n.msg("no.such.key")


def test_middleware_renders_json_responses_in_the_request_language() -> None:
    app = FastAPI()

    @app.get("/ok")
    def ok() -> dict:
        return {"message": i18n.msg("common.job_not_owned"), "items": [i18n.msg("common.worker_token_invalid")]}

    @app.get("/error")
    def error() -> dict:
        raise HTTPException(status_code=409, detail=i18n.msg("common.job_not_owned"))

    @app.get("/text")
    def text():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("plain")

    app.add_middleware(I18nMiddleware)
    client = TestClient(app)
    english = client.get("/ok", headers={"Accept-Language": "en"})
    assert english.json()["items"] == ["The access key is incorrect."]
    assert english.headers["vary"] == "Accept-Language"
    assert client.get("/ok").json()["items"] == ["接入密钥不正确。"]
    assert client.get("/error", headers={"Accept-Language": "en"}).json()["detail"].startswith("This job")
    assert client.get("/text").text == "plain"


def test_plugin_texts_can_be_given_per_language() -> None:
    from video_turbo.plugins.base import CredentialField, PluginInfo, localize_plugin

    field = CredentialField(
        "token",
        {"zh-CN": "令牌", "en-US": "Token"},
        help={"en": "Paste the token"},
        type="select",
        options=(("a", {"zh": "选项", "en": "Option"}),),
    )
    assert i18n.render(field.label, "en") == "Token"
    assert i18n.render(field.label, "zh-Hans") == "令牌"
    # 没有当前语言时依次用简体中文、英文、第一项。
    assert i18n.render(field.help, "zh-Hans") == "Paste the token"
    assert i18n.render(field.describe()["options"][0]["label"], "en") == "Option"
    assert i18n.render(i18n.localized({"ja": "トークン"}), "en") == "トークン"
    assert i18n.localized("plain") == "plain" and i18n.localized({}) == ""

    class Demo(PluginInfo):
        id = "demo"
        label: ClassVar[dict[str, str]] = {"zh-Hans": "演示", "en": "Demo"}

    localize_plugin(Demo)
    assert i18n.render(Demo.describe()["label"], "en") == "Demo"
