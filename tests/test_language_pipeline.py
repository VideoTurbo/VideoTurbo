"""按媒体库语言对跑通扫描 → 文本字幕准备 → 翻译 → 发布（翻译节点用模拟接口）。"""

import asyncio
import json
import re
from pathlib import Path

import httpx
from conftest import force_job_state, raises_rendered

from video_turbo.config import AppConfig, MediaLibraryConfig
from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.models import JobState, SourceKind
from video_turbo.orchestrator.processor import Orchestrator, ReprocessError
from video_turbo.source_paths import text as path_text

SOURCE_SRT = """1
00:00:01,000 --> 00:00:03,000
{first}

2
00:00:04,000 --> 00:00:06,000
{second}
"""


def _orchestrator(tmp_path: Path, library: MediaLibraryConfig) -> tuple[Orchestrator, Database]:
    config = AppConfig(
        media_libraries=[library],
        scan={"settle_minutes": 0},
        translation={"api_key": "token", "base_url": "http://node/v1", "model": "mock"},
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    return Orchestrator(config, database, tmp_path / "cache"), database


def _mock_node(requests: list[dict], translate) -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        ids = json.loads(re.search(r"target_cue_ids=(\[[^\n]+\])", payload["input"])[1])
        dialogue = json.loads(payload["input"].split("dialogue=", 1)[1])
        source = {item["cue_id"]: item["source"] for item in dialogue}
        items = [{"cue_id": cue_id, "text": translate(source[cue_id])} for cue_id in ids]
        return httpx.Response(
            200, json={"output_text": json.dumps({"translations": items}, ensure_ascii=False)}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


def _run_pipeline(orchestrator: Orchestrator, database: Database, monkeypatch, probe: dict) -> dict:
    monkeypatch.setattr("video_turbo.orchestrator.processor.run_ffprobe", lambda _path: probe)

    published: list[bool] = []
    orchestrator.on_published = lambda: published.append(True)

    async def run() -> dict:
        scan = await orchestrator.scan()
        assert scan["pending"] == 1, scan
        job = database.list_jobs()[0]
        assert database.start_job(job["id"])
        for _ in range(4):
            await orchestrator.process_once()
            await asyncio.gather(*orchestrator.translation_tasks.values())
        final = database.get_job(job["id"])
        await orchestrator.close()
        # 发布字幕后通知媒体服务器刷新。
        assert published == ([True] if final["state"] == JobState.COMPLETED else [])
        return final

    return asyncio.run(run())


def test_english_text_subtitle_job_publishes_simplified_chinese(tmp_path, monkeypatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    video = root / "movie.mkv"
    video.write_bytes(b"movie")
    (root / "movie.en.srt").write_text(
        SOURCE_SRT.format(first="Hello there.", second="See you tomorrow."), encoding="utf-8"
    )
    library = MediaLibraryConfig(id="lib-2", name="美剧", path=root, source_language="en", target_language="zh-CN"
    )
    orchestrator, database = _orchestrator(tmp_path, library)
    requests: list[dict] = []
    orchestrator.translation_http_client = _mock_node(
        requests, {"Hello there.": "你好。", "See you tomorrow.": "明天见。"}.__getitem__
    )
    probe = {
        "format": {"duration": "60"},
        "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "eng"}}],
    }

    job = _run_pipeline(orchestrator, database, monkeypatch, probe)

    assert job["state"] == JobState.COMPLETED, job["last_error"]
    assert (job["source_language"], job["target_language"]) == ("en", "zh-CN")
    assert job["source_kind"] == SourceKind.EXTERNAL_TEXT
    published = root / "movie.zh.default.srt"
    assert job["subtitle_path"] == path_text(published)
    assert "你好。" in render(published.read_text(encoding="utf-8"))
    assert requests[0]["instructions"].startswith("你是专业的英语影视中文字幕译者")


def test_japanese_to_english_job_publishes_english_subtitle(tmp_path, monkeypatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    video = root / "movie.mkv"
    video.write_bytes(b"movie")
    (root / "movie.ja.srt").write_text(
        SOURCE_SRT.format(first="こんにちは", second="また明日"), encoding="utf-8"
    )
    library = MediaLibraryConfig(id="lib-3", name="日剧", path=root, source_language="ja", target_language="en")
    orchestrator, database = _orchestrator(tmp_path, library)
    requests: list[dict] = []
    long_line = "I never thought I would see you again, not after everything."
    orchestrator.translation_http_client = _mock_node(
        requests, {"こんにちは": long_line, "また明日": "See you tomorrow."}.__getitem__
    )
    probe = {
        "format": {"duration": "60"},
        "streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}}],
    }

    job = _run_pipeline(orchestrator, database, monkeypatch, probe)

    assert job["state"] == JobState.COMPLETED, job["last_error"]
    published = root / "movie.en.default.srt"
    assert job["subtitle_path"] == path_text(published)
    content = published.read_text(encoding="utf-8")
    # 英文按词折成两行。
    assert "I never thought I would see you again,\nnot after everything." in content
    assert requests[0]["instructions"].startswith("你是专业的日语影视英文字幕译者")
    detail = orchestrator.job_detail(job["id"])
    assert detail["published_subtitle_path"] == path_text(published)


def test_pending_job_follows_library_language_change(tmp_path, monkeypatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"movie")
    library = MediaLibraryConfig(id="lib-4", name="库", path=root)
    orchestrator, database = _orchestrator(tmp_path, library)
    monkeypatch.setattr(
        "video_turbo.orchestrator.processor.run_ffprobe",
        lambda _path: {
            "format": {"duration": "60"},
            "streams": [
                {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
                {"index": 2, "codec_type": "audio", "tags": {"language": "eng"}},
            ],
        },
    )
    asyncio.run(orchestrator.scan())
    job = database.list_jobs()[0]
    assert (job["source_language"], job["source_stream_index"]) == ("ja", 1)

    library.source_language = "en"
    asyncio.run(orchestrator.scan())
    refreshed = database.get_job(job["id"])
    assert refreshed["state"] == JobState.PENDING
    assert (refreshed["source_language"], refreshed["source_stream_index"]) == ("en", 2)


def test_full_reprocess_rereads_library_language_pair(tmp_path, monkeypatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"movie")
    library = MediaLibraryConfig(id="lib-4", name="库", path=root)
    orchestrator, database = _orchestrator(tmp_path, library)
    streams = [
        {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"}},
        {"index": 2, "codec_type": "audio", "tags": {"language": "eng"}},
    ]
    monkeypatch.setattr(
        "video_turbo.orchestrator.processor.run_ffprobe",
        lambda _path: {"format": {"duration": "60"}, "streams": streams},
    )
    asyncio.run(orchestrator.scan())
    job_id = database.list_jobs()[0]["id"]
    database.start_job(job_id)
    force_job_state(database, job_id, JobState.FAILED, error="测试")

    library.source_language = "en"
    library.target_language = "zh-TW"
    # 继续处理沿用原语言对；从头处理按媒体库当前语言对重新选择音轨。
    assert orchestrator.reprocess_job(job_id, "full") == JobState.DISCOVERED
    job = database.get_job(job_id)
    assert (job["source_language"], job["target_language"]) == ("en", "zh-TW")
    assert job["source_stream_index"] == 2

    force_job_state(database, job_id, JobState.FAILED, error="测试")
    library.source_language = "ko"
    with raises_rendered(ReprocessError, "韩语主音轨"):
        orchestrator.reprocess_job(job_id, "full")
    assert database.get_job(job_id)["source_language"] == "en"
