import asyncio
import importlib
import json
import os
import sys
import time
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from conftest import PROTOCOL, force_job_state, raises_rendered
from conftest import prepare_server as seed_server
from fastapi.testclient import TestClient

from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.models import AsrSettings, JobState, SourceKind, WhisperJavProgress
from video_turbo.orchestrator.app import app
from video_turbo.worker.asr import TranscriptionCancelled
from video_turbo.worker.config import WorkerConfig, load_worker_config
from video_turbo.worker.main import Worker
from video_turbo.worker.whisperjav import WhisperJavTranscriber


def prepare_server(tmp_path, monkeypatch):
    seed_server(tmp_path, monkeypatch, worker_token="test-token")

    async def idle(_app):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        importlib.import_module("video_turbo.orchestrator.app"), "_pipeline_loop", idle
    )


def queue_job(tmp_path, name, pair=None):
    movie = tmp_path / name
    movie.write_bytes(b"movie")
    db = app.state.database
    media_id, changed = db.upsert_media(
        path=movie,
        library_id="lib-test",
        size=5,
        mtime_ns=movie.stat().st_mtime_ns,
        duration=60,
        probe={},
        decision="process",
        reason="test",
    )
    job_id, _ = db.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=1,
        source_path=None,
        changed=changed,
        pair=pair or LanguagePair(),
    )
    audio = tmp_path / f"{name}.opus"
    audio.write_bytes(b"audio")
    db.start_job(job_id)
    force_job_state(db, 
        job_id, JobState.WAITING_ASR, fields={"audio_path": str(audio), "audio_sha256": "hash"}
    )
    return job_id


def registration(name, **extra):
    return {
        "name": name,
        "version": "0.0.0",
        "gpu_name": "test",
        "memory_total_mb": 12000,
        "model": "Qwen/Qwen3-ASR-1.7B",
        "compute_type": "float16",
        "supported_asr_modes": ["whisperjav"],
        **extra,
    }


def test_asr_failure_is_visible_and_survives_requeue_but_not_next_attempt(tmp_path, monkeypatch):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    with TestClient(app) as client:
        job_id = queue_job(tmp_path, "broken.mkv")
        for name in ("rtx4070ti", "cloud3090-01"):
            client.post("/api/workers/register", headers=headers, json=registration(
                name, supported_asr_modes=["whisperjav"], supports_whisperjav_ensemble=True,
            ))
        assert client.post("/api/workers/rtx4070ti/claim", headers=headers).json()
        url = f"/api/jobs/{job_id}/asr-error"
        failure = {"worker_name": "rtx4070ti", "error": "音频完整性检查失败：5 处缺口，10 秒。"}
        assert client.post(url, json=failure).status_code == 401
        assert client.post(url, headers=headers, json={**failure, "error": "x" * 8001}).status_code == 422
        assert client.post(url, headers=headers, json={**failure, "worker_name": "other"}).status_code == 409
        assert client.post(url, headers=headers, json=failure).json() == {"accepted": True}
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["state"] == JobState.TRANSCRIBING
        assert "机器 rtx4070ti" in render(job["last_error"])
        assert failure["error"] in job["last_error"]
        assert failure["error"] in client.get("/api/jobs").json()["items"][0]["last_error"]
        db = app.state.database
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                         ("2000-01-01T00:00:00+00:00", job_id))
        assert db.release_expired_leases() == 1
        job = db.get_job(job_id)
        assert job["state"] == JobState.WAITING_ASR
        assert failure["error"] in job["last_error"]
        assert "任务已重新排队" in render(job["last_error"])
        assert client.post("/api/workers/cloud3090-01/claim", headers=headers).json()
        assert db.get_job(job_id)["last_error"] is None
        assert client.post(url, headers=headers, json=failure).status_code == 409
        # An attempt that never reports an error must not inherit the previous reason.
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                         ("2000-01-01T00:00:00+00:00", job_id))
        db.release_expired_leases()
        job = db.get_job(job_id)
        assert "cloud3090-01" in job["last_error"]
        assert "主程序没有收到具体失败原因" in render(job["last_error"])
        assert "音频完整性" not in render(job["last_error"])


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_asr_failure_does_not_overwrite_user_action(tmp_path, monkeypatch, action):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    with TestClient(app) as client:
        job_id = queue_job(tmp_path, "stopped.mkv")
        client.post("/api/workers/register", headers=headers, json=registration(
            "rtx4070ti", supported_asr_modes=["whisperjav"], supports_whisperjav_ensemble=True,
        ))
        client.post("/api/workers/rtx4070ti/claim", headers=headers)
        client.post(f"/api/jobs/{job_id}/{action}")
        before = app.state.database.get_job(job_id)
        response = client.post(f"/api/jobs/{job_id}/asr-error", headers=headers,
                               json={"worker_name": "rtx4070ti", "error": "late error"})
        assert response.status_code == 409
        after = app.state.database.get_job(job_id)
        assert (after["state"], after["last_error"]) == (before["state"], before["last_error"])


def test_settings_drive_claims_and_preserve_running_mode_and_metadata(tmp_path, monkeypatch):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    with TestClient(app) as client:
        assert client.get("/api/settings/asr").json() == {"mode": "whisperjav", "whisperjav_ensemble": True}
        assert client.put("/api/settings/asr", json={"mode": "faster_whisper"}).status_code == 422
        assert client.get("/api/status").json()["asr"] == {"mode": "whisperjav", "whisperjav_ensemble": True}
        first = queue_job(tmp_path, "one.mkv")
        client.post("/api/workers/register", headers=headers, json=registration("rtx4070ti"))
        # Old Workers must not silently run the wrong engine.
        assert client.post("/api/workers/rtx4070ti/claim", headers=headers).json() is None
        assert app.state.database.get_job(first)["state"] == JobState.WAITING_ASR
        client.post(
            "/api/workers/register",
            headers=headers,
            json=registration("rtx4070ti", supported_asr_modes=["whisperjav"], supports_whisperjav_ensemble=True),
        )
        claim = client.post("/api/workers/rtx4070ti/claim", headers=headers).json()
        assert claim["job_id"] == first
        assert claim["asr_mode"] == "whisperjav"
        assert claim["whisperjav_ensemble"] is True
        assert client.put("/api/settings/asr", json={"mode": "whisperjav", "whisperjav_ensemble": False}).status_code == 200
        assert app.state.database.get_job(first)["state"] == JobState.TRANSCRIBING
        response = client.post(
            "/api/workers/rtx4070ti/heartbeat",
            headers=headers,
            json={
                "current_job_id": first,
                "status": "busy",
                "asr_mode": "whisperjav",
                "asr_stage": "识别对白",
                "asr_elapsed_seconds": 12,
            },
        )
        assert response.status_code == 200
        progress = client.get(f"/api/jobs/{first}").json()["asr_progress"]
        assert progress == {"mode": "whisperjav", "stage": "识别对白", "elapsed_seconds": 12}
        metadata = {"asr_engine": "whisperjav", "model": "Qwen/Qwen3-ASR-1.7B"}
        response = client.post(
            f"/api/jobs/{first}/asr-result",
            headers=headers,
            json={
                "job_id": first,
                "worker_name": "rtx4070ti",
                "language": "ja",
                "duration_seconds": 60,
                "metadata": metadata,
                "cues": [{"cue_id": 1, "start": 1, "end": 3, "source": "こんにちは"}],
            },
        )
        assert response.status_code == 200
        detail = client.get(f"/api/jobs/{first}").json()
        assert detail["asr_metadata"] == metadata
        assert client.get(f"/api/jobs/{first}/files/asr_metadata").json() == metadata
        second = queue_job(tmp_path, "two.mkv")
        claim = client.post("/api/workers/rtx4070ti/claim", headers=headers).json()
        assert claim["job_id"] == second
        assert claim["asr_mode"] == "whisperjav"
    with TestClient(app) as client:
        assert client.get("/api/settings/asr").json() == {"mode": "whisperjav", "whisperjav_ensemble": False}
        client.put("/api/settings/asr", json={"mode": "whisperjav"})
    with TestClient(app) as client:
        assert client.get("/api/settings/asr").json() == {"mode": "whisperjav", "whisperjav_ensemble": True}


def test_selected_whisperjav_backend_knows_gpu_memory_for_parallel_passes(monkeypatch):
    async def check():
        config = WorkerConfig(
            worker_token="token", device="cuda", whisperjav={"python_executable": sys.executable}
        )
        worker = Worker(config)
        try:
            worker.supported_asr_modes = lambda: ["whisperjav"]
            worker._query_hardware = lambda: SimpleNamespace(device_type="cuda", memory_total_mb=24576)
            worker._select_transcriber("whisperjav")
            worker.transcriber.ensemble = True
            # The Worker presets device, so load() must not be the only source of memory.
            worker.transcriber.load()
            assert worker.transcriber.memory_total_mb == 24576
            assert worker.transcriber._parallel_passes()
            assert worker.transcriber.elapsed_clock == worker._asr_elapsed
        finally:
            await worker.close()

    asyncio.run(check())


def test_existing_worker_config_gets_isolated_python_path(tmp_path):
    config = tmp_path / "config" / "worker.yaml"
    config.parent.mkdir()
    config.write_text("server_url: http://nas:8765\nworker_token: token\n")
    parsed = load_worker_config(config)
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    assert parsed.whisperjav.python_executable == tmp_path / ".venv-whisperjav" / executable
    assert AsrSettings().mode == "whisperjav"
    assert AsrSettings().whisperjav_ensemble is True


@pytest.mark.skipif(os.name == "nt", reason="Linux venv symlink behavior")
def test_isolated_interpreter_symlink_is_preserved(tmp_path):
    marker_name = "video-turbo-whisperjav.json"
    executable = tmp_path / ".venv-whisperjav/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    marker = executable.parent.parent / marker_name
    marker.write_text('{"version":"1.9.5"}')
    config = tmp_path / "config/worker.yaml"
    config.parent.mkdir()
    config.write_text("server_url: http://nas:8765\nworker_token: token\n")
    parsed = load_worker_config(config)
    assert parsed.whisperjav.python_executable == executable

    async def check():
        worker = Worker(parsed)
        try:
            assert worker.supported_asr_modes() == ["whisperjav"]
            assert worker.supports_whisperjav_ensemble() is False
            marker.write_text('{"version":"1.9.5","ensemble":true}')
            assert worker.supports_whisperjav_ensemble() is True
        finally:
            await worker.close()

    asyncio.run(check())


@pytest.fixture
def fake_backend(tmp_path, monkeypatch):
    script = tmp_path / "fake_backend.py"
    script.write_text("""import json, sys
from pathlib import Path
audio, output, temp, state, version, duration = sys.argv[1:]
output, temp = Path(output), Path(temp)
srt = output / 'result.ja.srt'
srt.write_text('1\\n00:00:01,000 --> 00:00:03,000\\nこんにちは\\n', encoding='utf-8')
(output / 'whisperjav_run.json').write_text(json.dumps({
    'whisperjav_version': version, 'exit_status': 0,
    'files': [{'state': state, 'output': str(srt)}]}))
(temp / (Path(audio).stem + '_master.json')).write_text(json.dumps({
    'duration_seconds': float(duration), 'summary': {'quality_metrics': {'nonverbal_filtered': 2}}}))
print('Phase 5: ASR', flush=True)
""")
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    stages = []
    backend = WhisperJavTranscriber(
        WorkerConfig(whisperjav={"python_executable": sys.executable}),
        lambda stage: stages.append(render(stage)),
    )

    def command(audio, output, temp):
        return [
            sys.executable,
            str(script),
            str(audio),
            str(output),
            str(temp),
            "done",
            "1.9.5",
            "60",
        ]

    monkeypatch.setattr(backend, "_command", command)
    return backend, audio, stages, command


def test_external_backend_returns_cues_real_duration_and_stage(fake_backend):
    backend, audio, stages, _ = fake_backend
    cues, metadata = backend.transcribe(audio)
    assert [(cue.start, cue.end, cue.source) for cue in cues] == [(1, 3, "こんにちは")]
    assert metadata["duration_seconds"] == 60  # Not the last cue's end (3).
    assert metadata["asr_engine"] == "whisperjav"
    assert metadata["quality_metrics"] == {"nonverbal_filtered": 2}
    assert stages == ["准备模型与音频", "识别对白"]
    assert not list(audio.parent.glob("whisperjav-*"))


def test_external_backend_reads_scene_events_including_final_output(fake_backend):
    backend, audio, _, _ = fake_backend
    script = audio.parent / "fake_backend.py"
    with script.open("a", encoding="utf-8") as handle:
        for line in (
            "[DecoupledPipeline] Generating scene 1/3 (10.0s audio)...",
            "[DecoupledPipeline]   Scene 1/3: 12 chars",
            "[DecoupledPipeline] Generating scene 2/3 (10.0s audio)...",
        ):
            handle.write(f"print({line!r}, flush=True)\n")
    observations = []
    backend.scene_callback = observations.append
    cues, metadata = backend.transcribe(audio)
    assert cues and metadata["duration_seconds"] == 60
    last = observations[-1]
    assert (last.completed_scenes, last.total_scenes, last.current_scene) == (1, 3, 2)
    assert render(last.stage) == "生成识别文本"


@pytest.mark.parametrize("broken", ["stage", "scene", "parser"])
def test_progress_observer_failure_does_not_break_recognition(fake_backend, monkeypatch, broken):
    backend, audio, _, _ = fake_backend

    def fail(*_args):
        raise ValueError("unavailable progress observer")

    if broken == "parser":
        monkeypatch.setattr(
            "video_turbo.worker.whisperjav.WhisperJavProgressParser.consume", fail
        )
    elif broken == "stage":
        backend.stage_callback = fail
    else:
        backend.scene_callback = fail
    cues, metadata = backend.transcribe(audio)
    assert cues[0].source == "こんにちは"
    assert metadata["duration_seconds"] == 60


def test_worker_reports_scene_progress_without_chunk_eta(tmp_path):
    async def check():
        worker = Worker(WorkerConfig(cache_dir=tmp_path, worker_token="token"))
        worker.supported_asr_modes = lambda: ["whisperjav"]
        worker._query_hardware = lambda: SimpleNamespace(
            device_type="cpu", utilization=10, memory_free_mb=8000
        )
        payloads = []

        def server(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"lease_renewed": True})

        await worker.client.aclose()
        worker.client = httpx.AsyncClient(
            base_url="http://nas", transport=httpx.MockTransport(server)
        )
        try:
            worker.current_job_id = "job"
            worker.asr_started_at = time.monotonic() - 12
            worker._select_transcriber("whisperjav")
            progress = WhisperJavProgress(
                stage="第 2/2 遍 · 生成识别文本", model="litagin/anime-whisper",
                pass_number=2, total_passes=2, completed_scenes=1,
                total_scenes=4, current_scene=2,
            )
            worker.transcriber._report_progress(progress)
            await worker.heartbeat("busy")
            assert payloads[-1]["asr_whisperjav_progress"] == progress.model_dump()
            assert payloads[-1]["asr_stage"] == progress.stage
            assert payloads[-1]["asr_total_chunks"] is None
            assert payloads[-1]["asr_eta_seconds"] is None
            worker.current_job_id = None
            await worker.heartbeat()
            assert payloads[-1]["asr_whisperjav_progress"] is None
            assert payloads[-1]["asr_stage"] is None
        finally:
            await worker.close()

    asyncio.run(check())


def test_whisperjav_scene_progress_roundtrip_and_legacy_fallback(tmp_path, monkeypatch):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    with TestClient(app) as client:
        job_id = queue_job(tmp_path, "progress.mkv")
        worker = registration(
            "rtx4070ti", supported_asr_modes=["whisperjav"],
            supports_whisperjav_ensemble=True,
        )
        client.post("/api/workers/register", headers=headers, json=worker)
        claim = client.post("/api/workers/rtx4070ti/claim", headers=headers).json()
        assert claim["job_id"] == job_id
        progress = WhisperJavProgress(
            stage="第 2/2 遍 · 生成识别文本", model="litagin/anime-whisper",
            pass_number=2, total_passes=2, completed_scenes=1, total_scenes=4,
            current_scene=2,
        ).model_dump()
        heartbeat = {
            "current_job_id": job_id, "status": "busy", "asr_mode": "whisperjav",
            "asr_stage": progress["stage"], "asr_elapsed_seconds": 12,
            "asr_whisperjav_progress": progress,
        }
        url = "/api/workers/rtx4070ti/heartbeat"
        assert client.post(url, headers=headers, json=heartbeat).status_code == 200
        expected = {**progress, "mode": "whisperjav", "percent": 25.0, "elapsed_seconds": 12}
        assert client.get(f"/api/jobs/{job_id}").json()["asr_progress"] == expected
        assert client.get("/api/jobs").json()["items"][0]["asr_progress"] == expected
        status_worker = client.get("/api/status").json()["workers"][0]
        assert status_worker["asr_whisperjav_progress"] == progress
        assert status_worker["asr_total_chunks"] is None

        # Malformed persisted progress falls back to the existing stage display.
        with app.state.database.connect() as conn:
            conn.execute(
                "UPDATE workers SET asr_whisperjav_progress_json = 'invalid' WHERE name = ?",
                ("rtx4070ti",),
            )
        fallback = {"mode": "whisperjav", "stage": progress["stage"], "elapsed_seconds": 12}
        assert client.get(f"/api/jobs/{job_id}").json()["asr_progress"] == fallback
        assert client.get("/api/status").json()["workers"][0]["asr_whisperjav_progress"] is None

        # A late heartbeat belonging to a different job must not attach its scenes.
        assert client.post(url, headers=headers, json={
            **heartbeat, "current_job_id": "another-job",
        }).status_code == 200
        assert "asr_progress" not in client.get(f"/api/jobs/{job_id}").json()

        # Old Workers omit the new field and keep their existing stage contract.
        heartbeat.pop("asr_whisperjav_progress")
        assert client.post(url, headers=headers, json=heartbeat).status_code == 200
        assert client.get(f"/api/jobs/{job_id}").json()["asr_progress"] == fallback
        assert client.get("/api/status").json()["workers"][0]["asr_whisperjav_progress"] is None

        # Re-registering clears a previous run's detailed progress.
        client.post(url, headers=headers, json={**heartbeat, "asr_whisperjav_progress": progress})
        client.post("/api/workers/register", headers=headers, json=worker)
        assert app.state.database.get_worker("rtx4070ti")["asr_whisperjav_progress"] is None


def test_worker_executes_selected_backend_and_uploads_normalized_result(fake_backend, monkeypatch):
    _, audio, _, command = fake_backend
    monkeypatch.setattr(WhisperJavTranscriber, "_command", lambda self, *args: command(*args))
    monkeypatch.setattr(
        "video_turbo.worker.main.query_gpu", lambda _: SimpleNamespace(memory_free_mb=10000)
    )
    uploaded = []

    def server(request):
        if request.method == "GET":
            return httpx.Response(200, content=audio.read_bytes())
        uploaded.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    async def check():
        worker = Worker(
            WorkerConfig(
                worker_token="token",
                cache_dir=audio.parent / "worker",
                whisperjav={"python_executable": sys.executable},
            )
        )
        worker.supported_asr_modes = lambda: ["whisperjav"]
        await worker.client.aclose()
        worker.client = httpx.AsyncClient(
            base_url="http://nas", transport=httpx.MockTransport(server)
        )
        try:
            await worker.process_claim(
                {
                    "job_id": "job",
                    "audio_url": "/audio", "language": "ja",
                    "asr_mode": "whisperjav",
                    "audio_sha256": sha256(audio.read_bytes()).hexdigest(),
                }
            )
            assert worker.current_job_id is None
            assert worker.asr_whisperjav_progress is None
        finally:
            await worker.close()

    asyncio.run(check())
    assert len(uploaded) == 1
    assert uploaded[0]["duration_seconds"] == 60
    assert uploaded[0]["metadata"]["asr_engine"] == "whisperjav"
    assert uploaded[0]["cues"][0]["source"] == "こんにちは"
    assert not (audio.parent / "worker/job/audio.opus").exists()


@pytest.mark.parametrize("index,value", [(5, "suspect"), (5, "empty"), (6, "1.9.4"), (7, "nan")])
def test_external_backend_rejects_untrusted_output(fake_backend, monkeypatch, index, value):
    backend, audio, _, command = fake_backend

    def bad_command(*args):
        result = command(*args)
        result[index] = value
        return result

    monkeypatch.setattr(backend, "_command", bad_command)
    with pytest.raises(RuntimeError):
        backend.transcribe(audio)


def test_external_backend_cancellation_stops_subprocess(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "slow.py"
    pulse = tmp_path / "child.pulse"
    script.write_text(
        "import os,sys,time,subprocess,json; from pathlib import Path\n"
        'program="import sys,time; from pathlib import Path\\nwhile True:\\n Path(sys.argv[1]).write_text(str(time.monotonic())); time.sleep(.02)"\n'
        "child=subprocess.Popen([sys.executable,'-c',program,sys.argv[2]])\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
        "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))\n"
        "time.sleep(120)\n"
    )
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    backend = WhisperJavTranscriber(WorkerConfig(whisperjav={"python_executable": sys.executable}))
    monkeypatch.setattr(
        backend, "_command", lambda *_: [sys.executable, str(script), str(pidfile), str(pulse)]
    )
    with pytest.raises(TranscriptionCancelled):
        backend.transcribe(audio, cancel_check=pidfile.is_file)
    pid, child_pid = json.loads(pidfile.read_text())
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        # Verify execution stopped without relying on privileged process inventories.
        last_pulse = pulse.read_text()
        time.sleep(0.2)
        try:
            assert pulse.read_text() == last_pulse
        finally:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
    assert not list(tmp_path.glob("whisperjav-*"))


def test_dual_pass_settings_capabilities_and_downloads(tmp_path, monkeypatch):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    settings = {"mode": "whisperjav", "whisperjav_ensemble": True}
    artifacts = {
        "pass1": "1\n00:00:01,000 --> 00:00:03,000\nこんにちは\n",
        "pass2": "1\n00:00:05,000 --> 00:00:06,000\nありがとう\n",
        "merged": "1\n00:00:01,000 --> 00:00:03,000\nこんにちは\n\n2\n00:00:05,000 --> 00:00:06,000\nありがとう\n",
    }
    with TestClient(app) as client:
        assert client.put("/api/settings/asr", json=settings).json() == settings
        first = queue_job(tmp_path, "dual.mkv")
        worker = registration("rtx4070ti", supported_asr_modes=["whisperjav"])
        client.post("/api/workers/register", headers=headers, json=worker).raise_for_status()
        assert client.post("/api/workers/rtx4070ti/claim", headers=headers).json() is None
        worker["supports_whisperjav_ensemble"] = True
        client.post("/api/workers/register", headers=headers, json=worker).raise_for_status()
        claim = client.post("/api/workers/rtx4070ti/claim", headers=headers).json()
        assert claim["whisperjav_ensemble"] is True
        assert client.get("/api/status").json()["workers"][0]["supports_whisperjav_ensemble"] is True
        # Changing settings cannot alter the already issued claim.
        client.put("/api/settings/asr", json={"mode": "whisperjav", "whisperjav_ensemble": False})
        payload = {
            "job_id": first, "worker_name": "rtx4070ti", "language": "ja",
            "duration_seconds": 60, "metadata": {"ensemble": True},
            "cues": [{"cue_id": 1, "start": 1, "end": 3, "source": "こんにちは"}],
            "asr_artifacts": artifacts,
        }
        invalid = {**payload, "asr_artifacts": {**artifacts, "../../evil": "bad"}}
        assert client.post(f"/api/jobs/{first}/asr-result", headers=headers, json=invalid).status_code == 422
        client.post(f"/api/jobs/{first}/asr-result", headers=headers, json=payload).raise_for_status()
        for kind, content in artifacts.items():
            assert client.get(f"/api/jobs/{first}/files/asr_{kind}").text == content
        # Re-translation keeps originals; a new recognition removes them.
        app.state.database.start_job(first)
        force_job_state(app.state.database, first, JobState.COMPLETED)
        assert client.post(f"/api/jobs/{first}/reprocess", json={"mode": "retranslate"}).status_code == 200
        assert client.get(f"/api/jobs/{first}/files/asr_pass1").status_code == 200
        force_job_state(app.state.database, first, JobState.COMPLETED)
        assert client.post(f"/api/jobs/{first}/reprocess", json={"mode": "reasr"}).status_code == 200
        for kind in artifacts:
            assert client.get(f"/api/jobs/{first}/files/asr_{kind}").status_code == 404
        claim = client.post("/api/workers/rtx4070ti/claim", headers=headers).json()
        assert claim["asr_mode"] == "whisperjav"
        assert claim["whisperjav_ensemble"] is False
    with TestClient(app) as client:
        assert client.get("/api/settings/asr").json() == {"mode": "whisperjav", "whisperjav_ensemble": False}


@pytest.mark.parametrize("device,dtype", [("cpu", "float32"), ("cuda", "float16")])
def test_dual_pass_command_selects_real_anime_backend(tmp_path, device, dtype):
    backend = WhisperJavTranscriber(WorkerConfig())
    backend.ensemble = True
    backend.device = device
    command = backend._command(tmp_path / "audio", tmp_path / "out", tmp_path / "temp")

    def option(name):
        return command[command.index(name) + 1]

    assert "--ensemble" in command and "--ensemble-serial" in command and "--keep-temp" in command
    assert option("--merge-strategy") == "pass1_primary"
    assert option("--pass1-pipeline") == option("--pass2-pipeline") == "qwen"
    assert option("--pass1-model") == "Qwen/Qwen3-ASR-1.7B"
    assert option("--pass2-model") == "litagin/anime-whisper"
    assert option("--pass1-speech-segmenter") == "firered-vad"
    assert option("--pass2-speech-segmenter") == "ten"
    assert json.loads(option("--pass1-qwen-params"))["generator_backend"] == "qwen3"
    params = json.loads(option("--pass2-qwen-params"))
    assert params["generator_backend"] == "anime-whisper"
    assert params["device"] == device and params["dtype"] == dtype
    assert params["timestamp_mode"] == "vad_only" and params["aligner_backend"] == "none"
    assert params["assembly_cleaner"] is False and params["stepdown"] is False


@pytest.fixture
def fake_ensemble_backend(tmp_path, monkeypatch):
    script = tmp_path / "fake_ensemble.py"
    # Mirror the pinned upstream's output/summary contract in a real child process.
    script.write_text('''import json, sys
from pathlib import Path
audio, output, temp, failure = sys.argv[1:]
output, temp = Path(output), Path(temp)
p1 = output / 'audio.ja.pass1.srt'
p2 = output / 'audio.ja.pass2.srt'
merged = output / 'audio.ja.merged.whisperjav.srt'
p1.write_text('1\\n00:00:01,000 --> 00:00:03,000\\nこんにちは\\n', encoding='utf-8')
p2.write_text('1\\n00:00:05,000 --> 00:00:06,000\\nありがとう\\n', encoding='utf-8')
merged.write_text(p1.read_text() + '\\n2\\n00:00:05,000 --> 00:00:06,000\\nありがとう\\n', encoding='utf-8')
row = {'status': 'completed', 'input_file': audio,
       'pass1': {'status': 'completed', 'srt_path': str(p1), 'processing_time': 10},
       'pass2': {'status': 'completed', 'srt_path': str(p2), 'processing_time': 12},
       'merge': {'status': 'completed', 'strategy': 'pass1_primary',
                 'output_path': str(merged), 'statistics': {'added': 1}}}
if failure == 'degraded': row['status'] = 'degraded'
if failure == 'missing': p2.unlink()
if failure == 'empty': p2.write_text('')
if failure == 'outside': row['pass2']['srt_path'] = str(Path(audio))
if failure == 'wrong_merge': row['merge']['output_path'] = str(p1)
if failure == 'pass2_failed':
    row['status'] = 'degraded'
    row['pass2'] = {'status': 'failed', 'error': 'Traceback (most recent call last):\\n  File "w.py"\\ntorch.OutOfMemoryError: CUDA out of memory.'}
    row['merge'] = {'status': 'skipped', 'reason': 'pass2_failed'}
(temp / 'ensemble_summary_20261021_000000.json').write_text(json.dumps({'files': [row]}))
(output / 'whisperjav_run.json').write_text(json.dumps({
    'whisperjav_version': '1.9.5', 'exit_status': 0,
    'files': [{'state': 'done', 'output': str(merged)}]}))
print('[1/1] audio — Pass 1 (qwen)...', flush=True)
print('Phase 5: ASR', flush=True)
print('[1/1] audio — Pass 2 (qwen)...', flush=True)
print('Phase 5: ASR', flush=True)
print('[1/1] audio — Merge (pass1_primary)...', flush=True)
if failure == 'pass2_failed': sys.exit(1)
''', encoding="utf-8")
    audio = tmp_path / "audio.opus"
    audio.write_bytes(b"audio")
    stages = []
    backend = WhisperJavTranscriber(WorkerConfig(whisperjav={"python_executable": sys.executable}), lambda stage: stages.append(render(stage)))
    backend.ensemble = True
    backend.device = "cpu"

    def command(audio, output, temp):
        return [sys.executable, str(script), str(audio), str(output), str(temp), "ok"]

    monkeypatch.setattr(backend, "_command", command)
    monkeypatch.setattr("video_turbo.worker.whisperjav.run_ffprobe", lambda *a, **kw: {"format": {"duration": "60"}})
    return backend, audio, stages, command


def test_dual_pass_retains_originals_and_reports_each_stage(fake_ensemble_backend):
    backend, audio, stages, _ = fake_ensemble_backend
    cues, metadata = backend.transcribe(audio)
    assert [cue.source for cue in cues] == ["こんにちは", "ありがとう"]
    assert metadata["ensemble"] is True and metadata["merge_strategy"] == "pass1_primary"
    assert metadata["duration_seconds"] == 60
    assert metadata["passes"][1]["model"] == "litagin/anime-whisper"
    assert metadata["merge_statistics"] == {"added": 1}
    assert "ありがとう" not in backend.artifacts["pass1"]
    assert "ありがとう" in backend.artifacts["pass2"]
    assert "こんにちは" in backend.artifacts["merged"]
    assert stages == ["准备模型与音频", "第 1/2 遍 · Qwen3-ASR", "第 1/2 遍 · 识别对白",
                      "第 2/2 遍 · anime-whisper", "第 2/2 遍 · 识别对白", "合并字幕 · Qwen 为主"]
    assert not list(audio.parent.glob("whisperjav-*"))


@pytest.mark.parametrize("failure", ["degraded", "missing", "empty", "outside", "wrong_merge"])
def test_dual_pass_rejects_incomplete_or_mismatched_outputs(fake_ensemble_backend, monkeypatch, failure):
    backend, audio, _, command = fake_ensemble_backend

    def bad_command(*args):
        result = command(*args)
        result[-1] = failure
        return result

    monkeypatch.setattr(backend, "_command", bad_command)
    with pytest.raises(RuntimeError):
        backend.transcribe(audio)
    assert backend.artifacts is None


def test_dual_pass_failure_reports_the_failed_pass_reason(fake_ensemble_backend, monkeypatch):
    backend, audio, _, command = fake_ensemble_backend
    monkeypatch.setattr(backend, "_command", lambda *args: [*command(*args)[:-1], "pass2_failed"])
    with pytest.raises(RuntimeError) as caught:
        backend.transcribe(audio)
    message = render(str(caught.value))
    assert message.startswith("识别程序第 2 遍失败：")
    assert "torch.OutOfMemoryError: CUDA out of memory." in message
    assert "识别程序意外退出（代码 1）" in render(message)
    assert "合并失败" not in render(message) and "第 1 遍失败" not in render(message)


def test_worker_uploads_dual_artifacts_then_resets_to_single(fake_ensemble_backend, fake_backend, monkeypatch):
    _, audio, _, ensemble_command = fake_ensemble_backend
    _, _, _, single_command = fake_backend
    monkeypatch.setattr(WhisperJavTranscriber, "_command", lambda self, *args: (ensemble_command if self.ensemble else single_command)(*args))
    monkeypatch.setattr("video_turbo.worker.main.query_gpu", lambda _: SimpleNamespace(memory_free_mb=10000))
    uploaded = []

    def server(request):
        if request.method == "GET":
            return httpx.Response(200, content=audio.read_bytes())
        uploaded.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    async def check():
        worker = Worker(WorkerConfig(worker_token="token", cache_dir=audio.parent / "worker", whisperjav={"python_executable": sys.executable}))
        worker.supported_asr_modes = lambda: ["whisperjav"]
        worker.supports_whisperjav_ensemble = lambda: True
        await worker.client.aclose()
        worker.client = httpx.AsyncClient(base_url="http://nas", transport=httpx.MockTransport(server))
        try:
            for ensemble in (True, False):
                await worker.process_claim({"job_id": "job", "audio_url": "/audio", "language": "ja", "asr_mode": "whisperjav",
                                            "whisperjav_ensemble": ensemble, "audio_sha256": sha256(audio.read_bytes()).hexdigest()})
        finally:
            await worker.close()

    asyncio.run(check())
    assert len(uploaded) == 2
    assert uploaded[0]["metadata"]["ensemble"] is True
    assert set(uploaded[0]["asr_artifacts"]) == {"pass1", "pass2", "merged"}
    assert uploaded[1]["metadata"]["ensemble"] is False
    assert uploaded[1]["asr_artifacts"] is None


def test_explicit_single_pass_setting_survives_restart_and_claims(tmp_path, monkeypatch):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    single = {"mode": "whisperjav", "whisperjav_ensemble": False}
    with TestClient(app) as client:
        assert client.put('/api/settings/asr', json=single).json() == single
    with TestClient(app) as client:
        assert client.get('/api/settings/asr').json() == single
        job_id = queue_job(tmp_path, 'single.mkv')
        client.post(
            '/api/workers/register', headers=headers,
            json=registration('rtx4070ti', supported_asr_modes=['whisperjav']),
        ).raise_for_status()
        claim = client.post('/api/workers/rtx4070ti/claim', headers=headers).json()
        assert claim['job_id'] == job_id
        assert claim['whisperjav_ensemble'] is False


def test_claim_uses_job_language_and_runs_anime_whisper_only_for_japanese(tmp_path, monkeypatch):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    with TestClient(app) as client:
        assert client.get("/api/settings/asr").json()["whisperjav_ensemble"] is True
        japanese = queue_job(tmp_path, "ja.mkv")
        english = queue_job(tmp_path, "en.mkv", LanguagePair("en", "zh-CN"))
        # 没装双遍依赖的 Worker 跳过日语双遍任务，仍可单遍识别英语任务。
        client.post(
            "/api/workers/register",
            headers=headers,
            json=registration("single", supported_asr_modes=["whisperjav"]),
        )
        claim = client.post("/api/workers/single/claim", headers=headers).json()
        assert claim["job_id"] == english
        assert claim["language"] == "en"
        assert claim["asr_mode"] == "whisperjav"
        assert claim["whisperjav_ensemble"] is False
        assert client.post("/api/workers/single/claim", headers=headers).json() is None
        client.post(
            "/api/workers/register",
            headers=headers,
            json=registration(
                "dual", supported_asr_modes=["whisperjav"], supports_whisperjav_ensemble=True
            ),
        )
        claim = client.post("/api/workers/dual/claim", headers=headers).json()
        assert claim["job_id"] == japanese
        assert claim["language"] == "ja"
        assert claim["whisperjav_ensemble"] is True


def test_single_pass_claims_carry_source_language(tmp_path, monkeypatch):
    prepare_server(tmp_path, monkeypatch)
    headers = {**PROTOCOL, "X-Worker-Token": "test-token"}
    with TestClient(app) as client:
        client.put("/api/settings/asr", json={"mode": "whisperjav", "whisperjav_ensemble": False})
        korean = queue_job(tmp_path, "ko.mkv", LanguagePair("ko", "zh-CN"))
        client.post("/api/workers/register", headers=headers, json=registration("single"))
        claim = client.post("/api/workers/single/claim", headers=headers).json()
        assert (claim["job_id"], claim["language"], claim["asr_mode"]) == (
            korean, "ko", "whisperjav"
        )
        assert claim["whisperjav_ensemble"] is False


@pytest.mark.parametrize(("language", "name"), [("ja", "Japanese"), ("en", "English"), ("ko", "Korean")])
def test_single_pass_command_passes_language(tmp_path, language, name):
    backend = WhisperJavTranscriber(WorkerConfig())
    backend.language = language
    command = backend._command(tmp_path / "audio", tmp_path / "out", tmp_path / "temp")
    assert command[command.index("--qwen-language") + 1] == name
    assert "--ensemble" not in command
    assert "litagin/anime-whisper" not in command


def test_worker_rejects_dual_pass_for_non_japanese_claim(tmp_path):
    worker = Worker(WorkerConfig(worker_token="token", cache_dir=tmp_path / "worker"))
    worker.supported_asr_modes = lambda: ["whisperjav"]
    failures = []

    async def record(job_id, error):
        failures.append(str(error))

    worker.report_failure = record

    async def run():
        try:
            with raises_rendered(RuntimeError, "不能使用双遍识别"):
                await worker.process_claim(
                    {
                        "job_id": "job", "audio_url": "/audio", "audio_sha256": "x",
                        "language": "en", "asr_mode": "whisperjav", "whisperjav_ensemble": True,
                    }
                )
        finally:
            await worker.close()

    asyncio.run(run())
    assert failures and "英语任务不能使用双遍识别" in render(failures[0])
