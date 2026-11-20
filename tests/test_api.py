import importlib
import sys
from pathlib import Path

import httpx
import pytest
from conftest import PROTOCOL, force_job_state, prepare_server
from fastapi.testclient import TestClient

from video_turbo import __version__
from video_turbo.config_store import LLM_KEY
from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.llm_config import LlmNode, LlmSettings
from video_turbo.models import JobState, SourceKind
from video_turbo.orchestrator.app import app


def test_manual_scan_selects_only_enabled_libraries(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch, {"media_libraries": [ {"id": "lib-first", "name": "first", "path": str(tmp_path / "first"), "enabled": True}, {"id": "lib-second", "name": "second", "path": str(tmp_path / "second"), "enabled": True}, {"id": "lib-disabled", "name": "disabled", "path": str(tmp_path / "disabled"), "enabled": False}, ]})

    with TestClient(app) as client:
        selected: list[tuple[str, ...] | None] = []

        async def fake_scan(library_ids: tuple[str, ...] | None = None) -> dict:
            selected.append(library_ids)
            return {"status": "completed"}

        monkeypatch.setattr(app.state.orchestrator, "scan", fake_scan)
        assert client.get("/api/status").json()["media_libraries"] == [
            {"id": "lib-first", "name": "first", "enabled": True},
            {"id": "lib-second", "name": "second", "enabled": True},
            {"id": "lib-disabled", "name": "disabled", "enabled": False},
        ]
        assert client.post("/api/scan", json={"library_ids": ["lib-second"]}).status_code == 200
        assert selected == [("lib-second",)]
        assert client.post("/api/scan", json={"library_ids": ["lib-disabled"]}).status_code == 400
        assert client.post("/api/scan", json={"library_ids": ["lib-x"]}).status_code == 400
        assert client.post("/api/scan", json={"library_ids": ["lib-second", "lib-second"]}).status_code == 400
        assert client.post("/api/scan", json={"library_ids": []}).status_code == 422
        assert selected == [("lib-second",)]
        assert client.post("/api/scan").status_code == 200
        assert selected == [("lib-second",), None]


def test_dashboard_lists_all_translation_nodes_and_can_enable_disabled_node(
    tmp_path: Path, monkeypatch
) -> None:
    database = prepare_server(tmp_path, monkeypatch, worker_token="integration-token")
    nodes = [
        LlmNode(name="节点-1", pool="translation", base_url="http://node-1/v1", api_key="token-1", model="gpt-test"),
        LlmNode(name="节点-2", pool="translation", base_url="http://node-2/v1", model="gpt-test"),
        LlmNode(name="DeepSeek-1", pool="fallback", base_url="http://deepseek-1/v1", api_key="fallback-token", model="deepseek-test"),
    ]
    database.set_runtime_json(LLM_KEY, LlmSettings(nodes=nodes).model_dump(mode="json"))
    database.set_translation_upstream_states(
        {"节点-1": {"enabled": False, "consecutive_failures": 8}}
    )

    with TestClient(app) as client:
        translation = client.get("/api/status").json()["translation"]
        upstreams = translation["upstreams"]
        assert [item["name"] for item in upstreams] == ["节点-1", "节点-2"]
        assert upstreams[0]["configured"] is True
        assert upstreams[0]["enabled"] is False
        assert upstreams[1]["configured"] is False
        assert [item["name"] for item in translation["fallback_upstreams"]] == [
            "DeepSeek-1"
        ]
        assert translation["fallback_upstreams"][0]["role"] == "fallback"
        assert "review_upstreams" not in translation

        enabled = client.post("/api/translation/upstreams/节点-1/enable")
        assert enabled.status_code == 200
        assert enabled.json() == {"enabled": True, "name": "节点-1"}
        assert client.get("/api/status").json()["translation"]["upstreams"][0][
            "consecutive_failures"
        ] == 0

        probed_urls: list[str] = []
        fail_probe = False

        def translation_probe_response(request: httpx.Request) -> httpx.Response:
            probed_urls.append(str(request.url))
            if fail_probe:
                return httpx.Response(401, json={"error": {"message": "bad key"}})
            return httpx.Response(
                200,
                json={
                    "model": "probe-model",
                    "output_text": '{"translations":[{"cue_id":1,"text":"谢谢"}]}',
                },
            )

        app.state.orchestrator.translation_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(translation_probe_response)
        )
        for name in ("节点-1", "DeepSeek-1"):
            probe = client.post(f"/api/translation/upstreams/{name}/probe")
            assert probe.status_code == 200
            assert probe.json()["compatible"] is True
        assert probed_urls == [
            "http://node-1/v1/responses",
            "http://deepseek-1/v1/responses",
        ]
        translation = client.get("/api/status").json()["translation"]
        assert translation["upstreams"][0]["last_probe_status"] == "compatible"
        assert translation["fallback_upstreams"][0]["last_probe_status"] == "compatible"
        assert client.post("/api/translation/upstreams/节点-2/probe").status_code == 409
        fail_probe = True
        failed_probe = client.post("/api/translation/upstreams/节点-1/probe")
        assert failed_probe.status_code == 200
        assert failed_probe.json()["compatible"] is False
        failed_node = client.get("/api/status").json()["translation"]["upstreams"][0]
        assert failed_node["last_probe_status"] == "incompatible"
        assert failed_node["active_requests"] == 0

        assert client.post("/api/review/upstreams/节点-1/enable").status_code == 404
        assert client.post("/api/review/upstreams/节点-1/probe").status_code == 404

        missing_key = client.post("/api/translation/upstreams/节点-2/enable")
        assert missing_key.status_code == 409


def test_health_status_and_worker_auth(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch, worker_token="integration-token")

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "version": __version__}
        for path in ("/favicon.svg", "/favicon.ico"):
            icon = client.get(path)
            assert icon.status_code == 200
            assert icon.headers["content-type"].startswith("image/svg+xml")
            assert icon.text.startswith("<svg")
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["version"] == __version__
        assert status.json()["manual_scan_enabled"] is True
        assert status.json()["heartbeat_timeout_seconds"] == 90
        assert status.json()["pipeline_paused"] is False
        assert status.json()["translation"]["active_jobs"] == 0
        assert status.json()["translation"]["request_concurrency"] == 1

        pause = client.post("/api/pipeline/pause")
        assert pause.status_code == 200
        assert pause.json() == {"paused": True}
        assert client.get("/api/status").json()["pipeline_paused"] is True
        paused_scan = client.post("/api/scan")
        assert paused_scan.status_code == 409
        assert "已暂停" in render(paused_scan.json()["detail"])

        resume = client.post("/api/pipeline/resume")
        assert resume.status_code == 200
        assert resume.json() == {"paused": False}
        assert client.get("/api/status").json()["pipeline_paused"] is False
        payload = {
            "name": "rtx4070ti",
            "version": "0.1.0",
            "gpu_name": "RTX 4070 Ti",
            "driver_version": "595.97",
            "memory_total_mb": 12282,
            "model": "large-v3",
            "compute_type": "float16",
            "supported_asr_modes": ["faster_whisper"],
        }
        assert client.post("/api/workers/register", json=payload).status_code == 401
        response = client.post(
            "/api/workers/register",
            json=payload,
            headers={**PROTOCOL, "X-Worker-Token": "integration-token"},
        )
        assert response.status_code == 200
        heartbeat = client.post(
            "/api/workers/rtx4070ti/heartbeat",
            json={
                "status": "busy",
                "current_job_id": "job-1",
                "asr_completed_chunks": 2,
                "asr_total_chunks": 5,
                "asr_elapsed_seconds": 30,
                "asr_eta_seconds": 45,
            },
            headers={**PROTOCOL, "X-Worker-Token": "integration-token"},
        )
        assert heartbeat.status_code == 200
        worker = client.get("/api/status").json()["workers"][0]
        assert worker["name"] == "rtx4070ti"
        assert worker["version"] == "0.1.0"
        assert worker["status"] == "busy"
        assert worker["current_job_id"] == "job-1"
        assert worker["asr_completed_chunks"] == 2
        assert worker["asr_total_chunks"] == 5

        movie = tmp_path / "movie.mkv"
        movie.write_bytes(b"movie")
        media_id, changed = app.state.database.upsert_media(
            path=movie,
            library_id="lib-1",
            size=5,
            mtime_ns=movie.stat().st_mtime_ns,
            duration=100,
            probe={},
            decision="process",
            reason="test",
        )
        job_id, _ = app.state.database.create_or_reset_job(
            media_id=media_id,
            source_kind=SourceKind.AUDIO,
            source_stream_index=1,
            source_path=None,
            changed=changed,
            pair=LanguagePair(),
        )
        audio = tmp_path / "audio.opus"
        audio.write_bytes(b"audio")
        app.state.database.start_job(job_id)
        force_job_state(app.state.database, 
            job_id,
            JobState.FAILED,
            error="字幕显示时间过长。",
            fields={"audio_path": str(audio), "audio_sha256": "hash"},
        )
        retry = client.post(f"/api/jobs/{job_id}/retry")
        assert retry.status_code == 200
        assert retry.json()["state"] == JobState.WAITING_ASR

        priority = client.post(f"/api/jobs/{job_id}/priority", json={"priority": 100})
        assert priority.status_code == 200
        paused = client.post(f"/api/jobs/{job_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["state"] == JobState.PAUSED
        detail = client.get(f"/api/jobs/{job_id}")
        assert detail.status_code == 200
        assert detail.json()["priority"] == 100
        assert any(item["kind"] == "audio" and item["exists"] for item in detail.json()["files"])
        file_download = client.get(f"/api/jobs/{job_id}/files/audio")
        assert file_download.content == b"audio"
        assert file_download.headers["cache-control"] == "no-store, max-age=0"
        assert file_download.headers["pragma"] == "no-cache"
        assert file_download.headers["expires"] == "0"
        filtered = client.get(
            "/api/jobs",
            params={
                "view": "active",
                "q": "movie.mkv",
                "library": "lib-1",
                "sort": "name_asc",
            },
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["id"] == job_id
        assert client.get("/api/jobs", params={"view": "pending"}).json()["total"] == 0
        assert client.get("/api/jobs", params={"sort": "smart"}).status_code == 422
        resumed = client.post(f"/api/jobs/{job_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["state"] == JobState.WAITING_ASR

        batch = client.post(
            "/api/jobs/batch/action",
            json={"job_ids": [job_id], "action": "priority_normal"},
        )
        assert batch.status_code == 200
        assert batch.json()["succeeded"] == [job_id]
        assert client.get(f"/api/jobs/{job_id}").json()["priority"] == 0

        ja_result = tmp_path / "result.source.json"
        ja_result.write_text('{"cues":[]}', encoding="utf-8")
        force_job_state(app.state.database, 
            job_id,
            JobState.FAILED,
            fields={"source_result_path": str(ja_result)},
        )
        retranslate_batch = client.post(
            "/api/jobs/batch/action",
            json={"job_ids": [job_id], "action": "retranslate"},
        )
        assert retranslate_batch.status_code == 200
        assert retranslate_batch.json()["succeeded"] == [job_id]
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == JobState.ASR_COMPLETED

        cancel = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["state"] == JobState.PENDING
        jobs = client.get("/api/jobs", params={"view": "pending", "page": 1, "page_size": 50})
        assert jobs.status_code == 200
        assert jobs.json()["total"] == 1
        assert jobs.json()["counts"]["pending"] == 1
        item = jobs.json()["items"][0]
        assert item["id"] == job_id
        assert item["status"] == "pending"
        assert item["actions"] == ["start", "skip"]
        assert client.get("/api/jobs", params={"view": "unknown"}).status_code == 422
        heartbeat = client.post(
            "/api/workers/rtx4070ti/heartbeat",
            json={"status": "busy", "current_job_id": job_id},
            headers={**PROTOCOL, "X-Worker-Token": "integration-token"},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["cancel_requested"] is True
        assert heartbeat.json()["registered"] is True

        online_delete = client.delete("/api/workers/rtx4070ti")
        assert online_delete.status_code == 409
        assert "仍在线" in render(online_delete.json()["detail"])
        with app.state.database.connect() as conn:
            conn.execute(
                "UPDATE workers SET status = 'offline', last_seen_at = ? WHERE name = ?",
                ("2000-01-01T00:00:00+00:00", "rtx4070ti"),
            )
        deleted = client.delete("/api/workers/rtx4070ti")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "name": "rtx4070ti"}
        assert client.get("/api/status").json()["workers"] == []
        assert client.delete("/api/workers/rtx4070ti").status_code == 404
        orphan = client.post(
            "/api/workers/rtx4070ti/heartbeat",
            json={"status": "online"},
            headers={**PROTOCOL, "X-Worker-Token": "integration-token"},
        )
        assert orphan.status_code == 200
        assert orphan.json()["registered"] is False


def test_review_actions_are_rejected(tmp_path: Path, monkeypatch) -> None:
    prepare_server(tmp_path, monkeypatch, worker_token="integration-token")

    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert not any(key.startswith("review") for key in status["translation"])
        assert not any("审校" in warning["message"] for warning in status["warnings"])
        assert "review" not in client.get("/api/config").json()["settings"]
        review = client.post("/api/jobs/nonexistent/reprocess", json={"mode": "review"})
        assert review.status_code == 422
        batch = client.post(
            "/api/jobs/batch/action", json={"job_ids": ["nonexistent"], "action": "review"}
        )
        assert batch.status_code == 422


def test_existing_srt_can_mark_job_completed_and_batch_is_all_or_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    prepare_server(tmp_path, monkeypatch, worker_token="integration-token")

    with TestClient(app) as client:
        def create_job(name: str) -> tuple[str, Path]:
            movie = tmp_path / f"{name}.mkv"
            movie.write_bytes(b"movie")
            media_id, changed = app.state.database.upsert_media(
                path=movie,
                library_id="lib-1",
                size=movie.stat().st_size,
                mtime_ns=movie.stat().st_mtime_ns,
                duration=100,
                probe={},
                decision="process",
                reason="test",
            )
            job_id, _ = app.state.database.create_or_reset_job(
                media_id=media_id,
                source_kind=SourceKind.AUDIO,
                source_stream_index=1,
                source_path=None,
                changed=changed,
                pair=LanguagePair(),
            )
            app.state.database.start_job(job_id)
            force_job_state(app.state.database, job_id, JobState.FAILED, error="字幕校验失败")
            return job_id, tmp_path / f"{name}.zh.default.srt"

        first_id, first_subtitle = create_job("first")
        second_id, second_subtitle = create_job("second")
        first_subtitle.write_text("已有字幕", encoding="utf-8")

        jobs = client.get("/api/jobs", params={"view": "all"}).json()["items"]
        jobs_by_id = {job["id"]: job for job in jobs}
        assert jobs_by_id[first_id]["can_mark_completed"] is True
        assert jobs_by_id[second_id]["can_mark_completed"] is False

        rejected = client.post(
            "/api/jobs/batch/action",
            json={"job_ids": [first_id, second_id], "action": "complete"},
        )
        assert rejected.status_code == 409
        assert "批量操作未执行" in render(rejected.json()["detail"])
        assert app.state.database.get_job(first_id)["state"] == JobState.FAILED
        assert app.state.database.get_job(second_id)["state"] == JobState.FAILED

        second_subtitle.write_text("已有字幕", encoding="utf-8")
        completed = client.post(
            "/api/jobs/batch/action",
            json={"job_ids": [first_id, second_id], "action": "complete"},
        )
        assert completed.status_code == 200
        assert completed.json()["succeeded"] == [first_id, second_id]
        assert completed.json()["failed"] == []
        assert app.state.database.get_job(first_id)["subtitle_path"] == str(first_subtitle)
        assert app.state.database.get_job(second_id)["subtitle_path"] == str(second_subtitle)

        repeated = client.post(f"/api/jobs/{first_id}/complete")
        assert repeated.status_code == 409
        assert "已经是已完成状态" in render(repeated.json()["detail"])

        missing_id, _ = create_job("missing")
        missing = client.post(f"/api/jobs/{missing_id}/complete")
        assert missing.status_code == 409
        assert "目标 SRT 不存在" in render(missing.json()["detail"])


def test_pending_jobs_are_started_manually_and_worker_token_can_be_reset(
    tmp_path: Path, monkeypatch
) -> None:
    database = prepare_server(tmp_path, monkeypatch)
    job_ids = []
    for index in range(3):
        movie = tmp_path / f"movie-{index}.mkv"
        movie.write_bytes(b"x" * (index + 1))
        media_id, changed = database.upsert_media(
            path=movie,
            library_id="lib-1",
            size=index + 1,
            mtime_ns=movie.stat().st_mtime_ns,
            duration=100,
            probe={},
            decision="process",
            reason="test",
        )
        job_ids.append(
            database.create_or_reset_job(
                media_id=media_id,
                source_kind=SourceKind.AUDIO,
                source_stream_index=1,
                source_path=None,
                changed=changed,
                pair=LanguagePair(),
            )[0]
        )

    with TestClient(app) as client:
        client.post("/api/pipeline/pause")  # 不让流水线在测试途中领走已提交的任务
        pending = client.get("/api/jobs", params={"view": "pending", "sort": "size_desc"}).json()
        assert pending["page_size"] == 20
        assert [item["id"] for item in pending["items"]] == job_ids[::-1]
        assert client.get("/api/jobs", params={"view": "active"}).json()["total"] == 0
        paged = client.get("/api/jobs", params={"view": "pending", "page_size": 2, "page": 9}).json()
        assert (paged["page"], paged["pages"], len(paged["items"])) == (2, 2, 1)

        assert client.post(f"/api/jobs/{job_ids[0]}/start").json() == {"started": True}
        assert client.post(f"/api/jobs/{job_ids[0]}/start").status_code == 409
        batch = client.post(
            "/api/jobs/batch/action", json={"job_ids": job_ids, "action": "start"}
        ).json()
        assert batch["succeeded"] == job_ids[1:] and batch["failed"] == job_ids[:1]
        assert client.get("/api/jobs", params={"view": "pending"}).json()["total"] == 0

        token = client.get("/api/settings/worker-token").json()["token"]
        headers = {**PROTOCOL, "X-Worker-Token": token}
        assert client.post("/api/workers/ghost/heartbeat", json={}, headers=headers).status_code != 401
        new_token = client.post("/api/settings/worker-token/reset").json()["token"]
        assert new_token != token
        assert client.post("/api/workers/ghost/heartbeat", json={}, headers=headers).status_code == 401
        assert client.get("/api/settings/worker-token").json()["token"] == new_token


@pytest.mark.parametrize(
    ("entry", "expected"),
    [("video_turbo.orchestrator.app", "VideoTurbo"), ("video_turbo.worker.main", "VideoTurbo Worker")],
)
def test_cli_prints_version(entry: str, expected: str, monkeypatch, capsys) -> None:
    module = importlib.import_module(entry)
    monkeypatch.setattr(sys, "argv", ["prog", "--version"])
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f"{expected} {__version__}"
