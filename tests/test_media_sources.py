"""媒体库：逐个保存的接口、浏览文件夹，以及网盘来源插件从插件设置、扫描到 GPU 机器直接取片的流程。"""

import asyncio
import hashlib
import importlib
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import PROTOCOL, force_job_state, prepare_server
from fastapi.testclient import TestClient

from video_turbo.config import AppConfig, MediaLibraryConfig
from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.media_sources import loader
from video_turbo.models import Cue, JobState, SourceKind
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.processor import Orchestrator
from video_turbo.plugins import registry
from video_turbo.source_paths import text as path_text

TOKEN = "token"
HEADERS = {**PROTOCOL, "X-Worker-Token": TOKEN}

# 演示网盘：把一个本地目录当作网盘，地址按 http 给 ffprobe，GPU 机器下载时给本地路径。
# 每次下载在插件状态里记一次数，用来验证 GPU 机器回传插件状态。
DRIVE_SOURCE = '''
import os

from video_turbo.media_sources.base import MediaInput, MediaSourcePlugin, SourceEntry
from video_turbo.plugins.base import CredentialField

PLUGIN_API = 2


class FolderDrive(MediaSourcePlugin):
    id = "folder-drive"
    label = "演示网盘"
    settings_fields = (
        CredentialField("root", "目录", secret=False),
        CredentialField("token", "令牌"),
        CredentialField("mode", "线路", type="select", options=(("a", "A 线"), ("b", "B 线")), default="a"),
        CredentialField("fast", "加速", type="switch"),
        CredentialField("limit", "并发", type="number", min=1, max=8, required=False),
    )

    def _real(self, path):
        return os.path.join(self.settings["root"], path.lstrip("/"))

    def _entry(self, path, real):
        stat = os.stat(real)
        is_dir = os.path.isdir(real)
        return SourceEntry(path, is_dir, 0 if is_dir else stat.st_size, stat.st_mtime_ns,
                           None if is_dir else "id-" + str(stat.st_ino))

    def list_dir(self, path):
        real = self._real(path)
        if not os.path.isdir(real):
            raise FileNotFoundError(path)
        return [self._entry(os.path.join(path, name), os.path.join(real, name))
                for name in sorted(os.listdir(real)) if not name.startswith(".")]

    def stat(self, path):
        real = self._real(path)
        return self._entry(path, real) if os.path.exists(real) else None

    def read_bytes(self, path, limit=16 * 1024 * 1024):
        with open(self._real(path), "rb") as handle:
            return handle.read()

    def write_bytes(self, path, data, *, overwrite):
        real = self._real(path)
        if os.path.exists(real) and not overwrite:
            raise FileExistsError(path)
        with open(real, "wb") as handle:
            handle.write(data)

    def rename(self, source, target):
        os.rename(self._real(source), self._real(target))

    def media_input(self, path):
        return MediaInput("https://drive.example" + path, {"Authorization": "Bearer " + self.settings["token"]})

    def download(self, path):
        self.save_state({"downloads": self.state.get("downloads", 0) + 1})
        return MediaInput(self._real(path))

    def test(self):
        self.save_state({"tested": True})
        return "已连接演示网盘"
'''

AUDIO_PROBE = {
    "format": {"duration": "60"},
    "streams": [{"index": 0, "codec_type": "audio", "tags": {"language": "jpn"}}],
}


@pytest.fixture
def idle_pipeline(monkeypatch):
    async def idle(_app):
        await asyncio.Event().wait()

    monkeypatch.setattr(importlib.import_module("video_turbo.orchestrator.app"), "_pipeline_loop", idle)


def _install_drive(client: TestClient) -> str:
    upload = {"filename": "drive.py", "source": DRIVE_SOURCE}
    checked = client.post("/api/plugins/upload/preview", json=upload).json()
    assert client.post("/api/plugins/upload", json={**upload, "sha256": checked["sha256"]}).status_code == 200
    return checked["sha256"]


def test_library_api_create_edit_delete_and_browse(tmp_path: Path, monkeypatch) -> None:
    media_root = tmp_path / "media"
    for name in ("movies", "shows", "shows/season1"):
        (media_root / name).mkdir(parents=True)
    monkeypatch.setenv("VIDEO_TURBO_MEDIA_ROOTS", str(media_root))
    prepare_server(tmp_path, monkeypatch)
    with TestClient(app) as client:
        browse = client.get("/api/libraries/browse").json()
        assert browse["path"] == path_text(media_root) and browse["parent"] is None
        assert [item["name"] for item in browse["folders"]] == ["movies", "shows"]
        assert client.get("/api/libraries/browse", params={"path": "/etc"}).status_code == 403
        assert client.get("/api/libraries/browse", params={"path": f"{media_root}/../.."}).status_code == 403

        created = client.post("/api/libraries", json={"name": "电影", "path": str(media_root / "movies")})
        assert created.status_code == 200, created.text
        library_id = created.json()["id"]
        assert library_id.startswith("lib-")
        missing = client.post("/api/libraries", json={"name": "缺", "path": str(media_root / "nope")})
        assert missing.status_code == 422 and "docker-compose.yml" in missing.json()["detail"]
        outside = client.post("/api/libraries", json={"name": "外", "path": str(tmp_path)})
        assert outside.status_code == 422
        duplicate = client.post("/api/libraries", json={"name": "电影", "path": str(media_root / "shows")})
        assert duplicate.status_code == 422 and "重复" in render(duplicate.json()["detail"])
        nested = client.post("/api/libraries", json={"name": "子目录", "path": str(media_root / "movies")})
        assert nested.status_code == 422 and "包含" in render(nested.json()["detail"])
        same_language = client.post(
            "/api/libraries",
            json={"name": "剧", "path": str(media_root / "shows"), "source_language": "en", "target_language": "en"},
        )
        assert same_language.status_code == 422

        # 已使用的文件夹在浏览时标出来。
        folders = {item["name"]: item for item in client.get("/api/libraries/browse").json()["folders"]}
        assert render(folders["movies"]["library"]) == "电影" and folders["shows"]["library"] is None
        # 媒体目录本身里面有媒体库；媒体库的子文件夹在它里面。两者都不能再添加。
        root_page = client.get("/api/libraries/browse").json()
        assert render(root_page["current"]["contains"]) == "电影" and root_page["current"]["inside"] is None
        (media_root / "movies" / "2024").mkdir(exist_ok=True)
        movies_page = client.get("/api/libraries/browse", params={"path": str(media_root / "movies")}).json()
        assert render(movies_page["current"]["library"]) == "电影"
        assert render(movies_page["folders"][0]["inside"]) == "电影" and movies_page["folders"][0]["contains"] is None

        listed = client.get("/api/libraries").json()
        assert render(listed[0]["source_label"]) == "本地文件夹" and listed[0]["media_count"] == 0
        assert render(listed[0]["target_language_label"]) == "简体中文"

        # 改名和语言不影响 id；来源和文件夹不能改。
        renamed = client.put(
            f"/api/libraries/{library_id}",
            json={"name": "日本电影", "enabled": False, "source_language": "ja", "target_language": "zh-TW"},
        )
        assert renamed.status_code == 200
        library = app.state.config.library(library_id)
        assert render(library.name) == "日本电影" and not library.enabled and library.target_language == "zh-TW"
        assert library.path == str(media_root / "movies")

        # 有进行中的任务时不能删除；删除后影片记录随之清除。
        movie = media_root / "movies" / "a.mkv"
        movie.write_bytes(b"movie")
        db = app.state.database
        media_id, changed = db.upsert_media(
            path=movie, library_id=library_id, size=5, mtime_ns=movie.stat().st_mtime_ns,
            duration=60, probe={}, decision="process", reason="",
        )
        job_id, _ = db.create_or_reset_job(
            media_id=media_id, source_kind=SourceKind.AUDIO, source_stream_index=0,
            source_path=None, changed=changed, pair=LanguagePair(),
        )
        db.start_job(job_id)
        force_job_state(db, job_id, JobState.TRANSLATING)
        busy = client.delete(f"/api/libraries/{library_id}")
        assert busy.status_code == 409 and "正在进行" in render(busy.json()["detail"])
        force_job_state(db, job_id, JobState.FAILED)
        assert client.delete(f"/api/libraries/{library_id}").json() == {"removed": True, "media_removed": 1}
        assert client.get("/api/libraries").json() == []
        assert db.get_job(job_id) is None and movie.exists()


def test_cloud_library_scans_through_plugin_and_gpu_fetches_media(
    tmp_path: Path, monkeypatch, idle_pipeline
) -> None:
    drive = tmp_path / "drive"
    (drive / "影视").mkdir(parents=True)
    (drive / "影视" / "movie.mkv").write_bytes(b"movie")
    (drive / "影视" / "done.mkv").write_bytes(b"movie")
    (drive / "影视" / "done.zh.default.srt").write_text("字幕", encoding="utf-8")
    prepare_server(tmp_path, monkeypatch, worker_token=TOKEN)
    probed: list = []

    def fake_probe(target):
        probed.append(target)
        return AUDIO_PROBE

    monkeypatch.setattr("video_turbo.orchestrator.processor.run_ffprobe", fake_probe)
    with TestClient(app) as client:
        sha256 = _install_drive(client)
        plugins = {item["id"]: item for item in client.get("/api/plugins").json()["plugins"]}
        assert plugins["folder-drive"]["has_settings"] and plugins["folder-drive"]["settings_missing"]
        assert not plugins["emby"]["has_settings"]
        sources = client.get("/api/libraries/sources").json()
        assert [item["id"] for item in sources] == ["folder-drive"]
        assert "目录、令牌" in render(sources[0]["problem"])
        # 必填设置没填时不能用它添加媒体库。
        blocked = client.post("/api/libraries", json={"name": "网盘电影", "source": "folder-drive", "path": "/影视"})
        assert blocked.status_code == 422 and "设置" in render(blocked.json()["detail"])

        settings = {"root": str(drive), "token": "secret-token", "mode": "b", "fast": True, "limit": "3"}
        assert client.post("/api/plugins/folder-drive/settings/test", json={"values": settings}).json() == {
            "ok": True, "message": "已连接演示网盘",
        }
        bad = client.put("/api/plugins/folder-drive/settings", json={"values": {**settings, "limit": 9}})
        assert bad.status_code == 400 and "不能大于 8" in render(bad.json()["detail"])
        assert client.put("/api/plugins/folder-drive/settings", json={"values": settings}).json() == {
            "saved": True, "changed": True,
        }
        db: Database = app.state.database
        # 测试时插件保存的状态不写入数据库。
        assert db.plugin_data("folder-drive") == {
            "settings": {"root": str(drive), "token": "secret-token", "mode": "b", "fast": True, "limit": 3},
            "state": {},
        }
        described = client.get("/api/plugins/folder-drive/settings").json()
        assert described["testable"] and render(described["label"]) == "演示网盘"
        assert described["values"] == {"root": str(drive), "token": "••••••", "mode": "b", "fast": True, "limit": 3}
        assert [field["type"] for field in described["fields"]] == ["text", "text", "select", "switch", "number"]
        # 密钥留空沿用，非密钥字段提交空值表示清空。
        assert client.put(
            "/api/plugins/folder-drive/settings", json={"values": {"token": "", "limit": None, "fast": False}}
        ).json()["changed"]
        assert db.plugin_data("folder-drive")["settings"] == {
            "root": str(drive), "token": "secret-token", "mode": "b", "fast": False,
        }
        assert client.get("/api/plugins/emby/settings").status_code == 404
        assert client.get("/api/libraries/sources").json()[0]["problem"] is None
        # 浏览网盘目录。
        browse = client.get("/api/libraries/browse", params={"source": "folder-drive"}).json()
        assert browse["path"] == "/" and [item["path"] for item in browse["folders"]] == ["/影视"]
        created = client.post("/api/libraries", json={"name": "网盘电影", "source": "folder-drive", "path": "/影视"})
        assert created.status_code == 200, created.text
        library_id = created.json()["id"]
        listed = client.get("/api/libraries").json()[0]
        assert render(listed["source_label"]) == "演示网盘" and render(listed["source_type"]) == "演示网盘"
        assert listed["problem"] is None
        assert client.get("/api/plugins").json()["plugins"][-1]["usage"] == 1

        assert client.post("/api/scan", json={"library_ids": [library_id]}).status_code == 200
        # ffprobe 读网盘地址，带上插件给的请求头，不需要下载整部影片。
        assert [(item.url, dict(item.headers)) for item in probed] == [
            ("https://drive.example/影视/movie.mkv", {"Authorization": "Bearer secret-token"})
        ]
        jobs = {Path(job["media_path"]).name: job for job in db.list_jobs()}
        assert jobs["done.mkv"]["state"] == JobState.COMPLETED
        assert jobs["movie.mkv"]["state"] == JobState.PENDING
        assert client.get("/api/libraries").json()[0]["media_count"] == 2
        job_id = jobs["movie.mkv"]["id"]
        page = client.get("/api/jobs", params={"library": library_id}).json()
        assert {item["library_name"] for item in page["items"]} == {"网盘电影"}

        # 网盘影片不由主程序抽音频：准备阶段直接等待 GPU 机器。
        db.start_job(job_id)
        app.state.orchestrator._prepare_job(db.get_job(job_id))
        job = db.get_job(job_id)
        assert job["state"] == JobState.WAITING_ASR and job["audio_path"] is None and job["fetch_media"] == 1

        registration = {
            "name": "gpu", "version": "0", "gpu_name": "test", "memory_total_mb": 1, "model": "m",
            "compute_type": "f", "supported_asr_modes": ["whisperjav", "faster_whisper"],
            "supports_whisperjav_ensemble": True,
        }
        # 没有 ffmpeg 的 GPU 机器不领取网盘任务。
        client.post("/api/workers/register", headers=HEADERS, json=registration)
        assert client.post("/api/workers/gpu/claim", headers=HEADERS).json() is None
        client.post("/api/workers/register", headers=HEADERS, json={**registration, "supports_media_fetch": True})
        claim = client.post("/api/workers/gpu/claim", headers=HEADERS).json()
        assert claim["job_id"] == job_id and claim["audio_url"] is None
        saved_settings = db.plugin_data("folder-drive")["settings"]
        assert claim["media"] == {
            "plugin_id": "folder-drive", "plugin_sha256": sha256, "settings": saved_settings, "state": {},
            "path": "/影视/movie.mkv", "stream_index": 0, "channels": 1, "sample_rate": 16000,
            "opus_bitrate": app.state.config.audio.opus_bitrate,
        }

        # GPU 机器按文件指纹下载插件源码并加载。
        assert client.get(f"/api/worker/plugins/{'0' * 64}", headers=HEADERS).status_code == 404
        assert client.get(f"/api/worker/plugins/{sha256}").status_code == 401
        downloaded = client.get(f"/api/worker/plugins/{sha256}", headers=HEADERS)
        assert downloaded.text == DRIVE_SOURCE
        plugin = loader.load(downloaded.text, sha256, "folder-drive")
        reported: list = []
        with plugin(saved_settings, on_state=reported.append) as source:
            assert source.download("/影视/movie.mkv").url == str(drive / "影视" / "movie.mkv")
        assert reported == [{"downloads": 1}]
        with pytest.raises(loader.PluginLoadError):
            loader.load(DRIVE_SOURCE + "\n", sha256, "folder-drive")

        # GPU 机器回传插件状态：只接受领取了这个任务的机器。
        state_url = f"/api/jobs/{job_id}/plugin-state"
        assert client.post(state_url, headers=HEADERS, json={"worker_name": "other", "changes": {"a": 1}}).status_code == 409
        assert client.post(state_url, json={"worker_name": "gpu", "changes": {"a": 1}}).status_code == 401
        assert client.post(state_url, headers=HEADERS, json={"worker_name": "gpu", "changes": {"downloads": 1}}).json() == {
            "saved": True,
        }
        assert db.plugin_data("folder-drive")["state"] == {"downloads": 1}
        # 主程序上插件保存的状态立即写入；修改设置后状态清空。
        with app.state.orchestrator.sources.open("folder-drive") as source:
            assert source.state == {"downloads": 1}
            source.download("/影视/movie.mkv")
        assert db.plugin_data("folder-drive")["state"] == {"downloads": 2}
        assert client.put("/api/plugins/folder-drive/settings", json={"values": {"mode": "b"}}).json()["changed"] is False
        assert db.plugin_data("folder-drive")["state"] == {"downloads": 2}
        client.put("/api/plugins/folder-drive/settings", json={"values": {"mode": "a"}})
        assert db.plugin_data("folder-drive")["state"] == {}
        assert client.delete("/api/plugins/folder-drive").status_code == 200
        # 卸载后设置保留，媒体库标记为不可用。
        assert db.plugin_data("folder-drive")["settings"]["root"] == str(drive)
        listed = client.get("/api/libraries").json()[0]
        assert render(listed["source_label"]) == "folder-drive（插件未安装）" and "未安装" in render(listed["problem"])


def test_unconfigured_plugin_fails_scan(tmp_path: Path, monkeypatch, idle_pipeline) -> None:
    drive = tmp_path / "drive"
    (drive / "影视").mkdir(parents=True)
    prepare_server(tmp_path, monkeypatch, worker_token=TOKEN)
    with TestClient(app) as client:
        _install_drive(client)
        client.put("/api/plugins/folder-drive/settings", json={"values": {"root": str(drive), "token": "t"}})
        library_id = client.post(
            "/api/libraries", json={"name": "网盘电影", "source": "folder-drive", "path": "/影视"}
        ).json()["id"]
        # 必填设置被清空（例如手动改了数据库）后，媒体库显示原因，扫描报错。
        app.state.database.update_plugin_data("folder-drive", lambda record: {**record, "settings": {"root": str(drive)}})
        problem = "请先在“设置 → 插件”里填写“演示网盘”的设置：令牌"
        assert client.get("/api/libraries").json()[0]["problem"] == problem
        client.post("/api/scan", json={"library_ids": [library_id]})
        errors = client.get("/api/status").json()["last_scan"]["errors"]
        assert errors == [{"path": "网盘电影", "error": problem}]
        assert client.delete("/api/plugins/folder-drive").status_code == 200


def test_cloud_library_publishes_subtitle_through_plugin(tmp_path: Path, monkeypatch) -> None:
    drive = tmp_path / "drive"
    (drive / "影视").mkdir(parents=True)
    (drive / "影视" / "movie.mkv").write_bytes(b"movie")
    namespace: dict = {}
    exec(compile(DRIVE_SOURCE, "drive.py", "exec"), namespace)  # noqa: S102
    registry.register("media_source", namespace["FolderDrive"])
    database = Database(tmp_path / "app.db")
    database.initialize()
    database.update_plugin_data(
        "folder-drive", lambda record: {**record, "settings": {"root": str(drive), "token": "t"}}
    )
    config = AppConfig(
        translation={"api_key": "token"},
        media_libraries=[MediaLibraryConfig(id="lib-cloud", name="网盘", source="folder-drive", path="/影视")],
    )
    orchestrator = Orchestrator(config, database, tmp_path / "cache")
    media_id, changed = database.upsert_media(
        path="/影视/movie.mkv", library_id="lib-cloud", size=5, mtime_ns=1,
        duration=60, probe={}, decision="process", reason="",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id, source_kind=SourceKind.AUDIO, source_stream_index=0,
        source_path=None, changed=changed, pair=LanguagePair(),
    )
    result = orchestrator._job_dir(job_id) / "result.source.json"
    result.parent.mkdir(parents=True)
    result.write_text(Cue(cue_id=1, start=1, end=3, source="こんにちは").model_dump_json().join("[]"), encoding="utf-8")
    database.start_job(job_id)
    force_job_state(database, job_id, JobState.ASR_COMPLETED, fields={"source_result_path": str(result)})

    class FakeClient:
        async def aclose(self) -> None:
            return None

    async def fake_translate(cues, _config, **kwargs):
        translated = [cue.model_copy(update={"target": "你好"}) for cue in cues]
        kwargs["metrics"].finish()
        return translated

    monkeypatch.setattr("video_turbo.orchestrator.processor.translate_cues", fake_translate)
    orchestrator.translation_http_client = FakeClient()

    async def run() -> None:
        await orchestrator._translate_job(database.get_job(job_id))
        await orchestrator.close()

    asyncio.run(run())
    job = database.get_job(job_id)
    assert job["state"] == JobState.COMPLETED, job["last_error"]
    assert render(job["subtitle_path"]) == "/影视/movie.zh.default.srt"
    assert "你好" in (drive / "影视" / "movie.zh.default.srt").read_text(encoding="utf-8")
    # 列表不为网盘逐个请求，按本任务发布的记录判断字幕存在。
    assert orchestrator.enrich_jobs([database.get_job(job_id)])[0]["published_subtitle_exists"] is True
    # 网盘媒体库不能提交影片修复。
    from video_turbo.orchestrator.restore_service import RestoreService

    service = RestoreService(config.model_copy(update={"restore": config.restore.model_copy(update={"backup_dir": tmp_path / "b"})}), database)
    assert render(service.create_jobs([media_id], "basicvsrpp", "hevc")["skipped"][0]["reason"]) == "网盘媒体库暂不支持影片修复。"
    registry.unregister("media_source", "folder-drive")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 FFmpeg")
def test_worker_extracts_audio_from_plugin_download(tmp_path: Path) -> None:
    from video_turbo.worker.config import WorkerConfig
    from video_turbo.worker.main import Worker

    drive = tmp_path / "drive"
    drive.mkdir()
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "aac", str(drive / "movie.mp4")],
        check=True,
    )
    sha256 = hashlib.sha256(DRIVE_SOURCE.encode()).hexdigest()
    loader.load(DRIVE_SOURCE, sha256, "folder-drive")
    worker = Worker(WorkerConfig(server_url="http://main", worker_token="t", name="w", work_dir=tmp_path / "w"))
    output = tmp_path / "audio.opus"
    media = {
        "plugin_id": "folder-drive", "plugin_sha256": sha256, "settings": {"root": str(drive), "token": "t"},
        "state": {"downloads": 5}, "path": "/movie.mp4", "stream_index": 0, "channels": 1, "sample_rate": 16000, "opus_bitrate": "32k",
    }

    reported: list = []

    async def report(job_id, changes) -> None:
        reported.append((job_id, changes))

    worker._report_plugin_state = report

    async def run() -> None:
        await worker._fetch_media_audio(media, output, "job-1")
        await worker.close()

    asyncio.run(run())
    assert output.stat().st_size > 0
    # 插件在下载时保存的状态回传主程序。
    assert reported == [("job-1", {"downloads": 6})]
