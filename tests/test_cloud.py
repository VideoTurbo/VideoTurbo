from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from conftest import PROTOCOL, force_job_state, prepare_server
from fastapi.testclient import TestClient

from video_turbo.cloud.base import (
    CAP_DESTROY,
    CAP_IDLE_CLOSE,
    CAP_LAUNCH,
    CAP_LIST,
    CAP_SAVE_IMAGE,
    CAP_SHUTDOWN,
    CloudError,
    CloudErrorKind,
    CloudInstance,
    CloudProvider,
    CredentialField,
    LaunchMethod,
)
from video_turbo.cloud_models import CloudBindingRequest, CloudSettings, LaunchRequest
from video_turbo.config import AppConfig
from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.languages import LanguagePair
from video_turbo.models import JobState, SourceKind, WorkerRegistration
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.cloud_service import CloudService, mask_secret
from video_turbo.plugins import registry

pytestmark = pytest.mark.anyio

def test_mask_secret() -> None:
    assert mask_secret("sk-abcdefgh1234") == "****1234"
    assert mask_secret("short") == "*****"
    assert mask_secret("") == ""


# ---------------------------------------------------------------------------
# 自动关机状态机（使用假平台插件）
# ---------------------------------------------------------------------------


class FakeProvider(CloudProvider):
    id = "fake"
    label = "测试云"
    capabilities = frozenset(
        {CAP_LIST, CAP_SHUTDOWN, CAP_DESTROY, CAP_SAVE_IMAGE, CAP_IDLE_CLOSE, CAP_LAUNCH}
    )
    launch_methods = (LaunchMethod("startup", "开机", "实例", creates_instance=False),)
    credential_fields = (
        CredentialField("api_key", "API Key"),
        CredentialField("region", "区域", secret=False, required=False),
    )
    idle_close_choices = (10, 30, 60, 120)
    instances: ClassVar[list[CloudInstance]] = []
    shutdowns: ClassVar[list[str]] = []
    destroys: ClassVar[list[str]] = []
    destroy_error: CloudError | None = None
    saved_images: ClassVar[list[tuple[str, str]]] = []
    save_image_error: CloudError | None = None
    startups: ClassVar[list[tuple[str, str, str, int]]] = []
    startup_error: CloudError | None = None
    idle_close: ClassVar[list[tuple[str, int]]] = []
    shutdown_error: CloudError | None = None

    async def list_instances(self) -> list[CloudInstance]:
        if self.credentials.get("api_key") == "bad":
            raise CloudError(CloudErrorKind.AUTH, "Key 无效")
        return list(type(self).instances)

    async def shutdown(self, instance_id: str) -> None:
        if type(self).shutdown_error:
            raise type(self).shutdown_error
        type(self).shutdowns.append(instance_id)

    async def set_idle_close(self, instance_id: str, minutes: int) -> None:
        type(self).idle_close.append((instance_id, minutes))

    async def destroy(self, instance_id: str) -> None:
        if type(self).destroy_error:
            raise type(self).destroy_error
        type(self).destroys.append(instance_id)

    async def save_image(self, instance_id: str, image_name: str = "") -> None:
        if type(self).save_image_error:
            raise type(self).save_image_error
        type(self).saved_images.append((instance_id, image_name))

    async def launch(self, method: str, target_id: str, gpu_id: str, gpu_count: int) -> str:
        if type(self).startup_error:
            raise type(self).startup_error
        type(self).startups.append((method, target_id, gpu_id, gpu_count))
        return target_id


@pytest.fixture
def fake_provider():
    FakeProvider.instances = [
        CloudInstance(id="i-1", title="租来的 5090", status="running", status_text="运行中")
    ]
    FakeProvider.shutdowns = []
    FakeProvider.destroys = []
    FakeProvider.destroy_error = None
    FakeProvider.saved_images = []
    FakeProvider.save_image_error = None
    FakeProvider.startups = []
    FakeProvider.startup_error = None
    FakeProvider.idle_close = []
    FakeProvider.shutdown_error = None
    registry.register("cloud", FakeProvider)
    yield FakeProvider
    registry.unregister("cloud", FakeProvider.id)


def _register(db: Database, name: str, **extra) -> None:
    db.register_worker(
        WorkerRegistration(
            name=name,
            version="0.0.0",
            gpu_name="RTX 5090",
            memory_total_mb=32000,
            model="large-v3",
            compute_type="float16",
            supported_asr_modes=["whisperjav", "faster_whisper"],
            supports_whisperjav_ensemble=True,
            **extra,
        )
    )


def _add_job(db: Database, tmp_path: Path, state: JobState, lease_owner: str | None = None) -> str:
    media_id, _ = db.upsert_media(
        path=tmp_path / f"{state}-{lease_owner}.mp4",
        library_id="lib-lib",
        size=1,
        mtime_ns=1,
        duration=60,
        probe={},
        decision="queue",
        reason="",
    )
    job_id, _ = db.create_or_reset_job(
        media_id=media_id,
        source_kind=SourceKind.AUDIO,
        source_stream_index=0,
        source_path=None,
        changed=True,
        pair=LanguagePair(),
    )
    if state != JobState.PENDING:
        db.start_job(job_id)
    force_job_state(db, job_id, state, fields={"lease_owner": lease_owner})
    return job_id


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


@pytest.fixture
def setup(tmp_path: Path, fake_provider):
    db = Database(tmp_path / "app.db")
    db.initialize()
    db.set_cloud_profile("fake", {"type": "fake"})
    db.set_cloud_credentials("fake", {"api_key": "k"})
    db.set_cloud_settings(CloudSettings(idle_minutes=10, backstop_minutes=45))
    orchestrator = SimpleNamespace(paused=False, scan_status="idle")
    service = CloudService(AppConfig(), db, orchestrator)
    _register(db, "cloud5090-01")
    _register(db, "cloud5090-02")
    db.bind_cloud_instance(
        "cloud5090", CloudBindingRequest(provider="fake", instance_id="i-1", shutdown_mode="always")
    )
    return db, service, orchestrator


async def test_idle_machine_drains_then_shuts_down(setup, fake_provider) -> None:
    db, service, _ = setup
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert binding["state"] == "watching" and binding["idle_since"]
    # 保底：45 分钟取平台支持的下一个档位 60。
    assert fake_provider.idle_close == [("i-1", 60)]
    assert binding["backstop_minutes"] == 60

    db.update_cloud_binding("cloud5090", idle_since=_ago(minutes=11))
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "draining"
    assert service.claims_blocked("cloud5090-02") and not service.claims_blocked("rtx4070ti")
    assert fake_provider.shutdowns == []

    db.update_cloud_binding("cloud5090", drain_started_at=_ago(seconds=61))
    await service.process_once()
    assert fake_provider.shutdowns == ["i-1"]
    assert db.get_cloud_binding("cloud5090")["state"] == "stopping"

    fake_provider.instances = [CloudInstance(id="i-1", status="stopped", status_text="已关机")]
    service.invalidate("fake")
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert binding["state"] == "stopped"
    assert binding["shutdown_mode"] == "always"  # 规则保留，下次开机跑完仍自动关机
    assert [render(n["title"]) for n in db.list_notices()] == ["云主机已关机", "云主机正在关机"]
    assert not service.claims_blocked("cloud5090-01")


def _deployment(db: Database, *, provider="fake", instance_id="i-1", state="deploying") -> str:
    launch_id = db.create_cloud_launch(LaunchRequest(
        provider=provider, method="startup", target_id=instance_id, gpu_id="5090",
        services=["asr", "restore"],
    ))
    db.update_cloud_launch(launch_id, instance_id=instance_id, state=state)
    return launch_id


@pytest.mark.parametrize("state", ["watching", "draining", "failed"])
@pytest.mark.parametrize("launch_state", ["booting", "deploying"])
async def test_partial_deployment_prevents_idle_shutdown(setup, fake_provider, state, launch_state) -> None:
    db, service, orchestrator = setup
    # 字幕已上线且没有任务，影片修复仍在安装；重启主控也必须保留部署保护。
    launch_id = _deployment(db, state=launch_state)
    service = CloudService(service.config, db, orchestrator)
    db.update_cloud_binding(
        "cloud5090", state=state, idle_since=_ago(minutes=30),
        drain_started_at=_ago(minutes=5), attempts=1, next_attempt_at=_ago(minutes=1),
    )
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert fake_provider.shutdowns == []
    assert binding["state"] == "watching"
    assert binding["idle_since"] is None and binding["drain_started_at"] is None
    assert binding["attempts"] == 0 and binding["next_attempt_at"] is None
    assert not service.claims_blocked("cloud5090-01")
    assert fake_provider.idle_close == [("i-1", 0)]

    # 所有部署结束后，从当前时间重新计算空闲，不能沿用部署前的倒计时。
    db.update_cloud_launch(launch_id, state="running")
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert binding["state"] == "watching" and binding["idle_since"] > _ago(seconds=5)
    assert fake_provider.idle_close == [("i-1", 0), ("i-1", 60)]
    db.update_cloud_binding("cloud5090", idle_since=_ago(minutes=11))
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "draining"
    db.update_cloud_binding("cloud5090", drain_started_at=_ago(seconds=61))
    await service.process_once()
    assert fake_provider.shutdowns == ["i-1"]


@pytest.mark.parametrize("finished_state", ["running", "deploy_failed", "failed", "cancelled", "nvenc_failed"])
async def test_deployment_suspends_existing_platform_backstop(setup, fake_provider, finished_state) -> None:
    db, service, _ = setup
    await service.process_once()
    assert fake_provider.idle_close == [("i-1", 60)]
    launch_id = _deployment(db)
    await service.process_once()
    assert fake_provider.idle_close == [("i-1", 60), ("i-1", 0)]
    assert db.get_cloud_binding("cloud5090")["idle_since"] is None

    db.update_cloud_launch(launch_id, state=finished_state)
    await service.process_once()
    assert fake_provider.idle_close == [("i-1", 60), ("i-1", 0), ("i-1", 60)]


async def test_deployment_stays_protected_when_platform_backstop_fails(setup, fake_provider, monkeypatch) -> None:
    db, service, _ = setup
    await service.process_once()
    _deployment(db)

    async def fail_backstop(self, instance_id, minutes):
        fake_provider.idle_close.append((instance_id, minutes))
        raise CloudError(CloudErrorKind.TRANSIENT, "平台繁忙")

    monkeypatch.setattr(fake_provider, "set_idle_close", fail_backstop)
    db.update_cloud_binding(
        "cloud5090", state="draining", idle_since=_ago(minutes=30), drain_started_at=_ago(minutes=5),
    )
    await service.process_once()
    await service.process_once()
    assert fake_provider.shutdowns == []
    assert db.get_cloud_binding("cloud5090")["idle_since"] is None
    assert "平台繁忙" in render(db.get_cloud_binding("cloud5090")["last_error"])
    assert fake_provider.idle_close == [("i-1", 60), ("i-1", 0)]  # 同一目标失败后不频繁请求。


@pytest.mark.parametrize("provider,instance_id", [("fake", "other-instance"), ("other-account", "i-1")])
async def test_deployment_does_not_protect_other_instances(setup, fake_provider, provider, instance_id) -> None:
    db, service, _ = setup
    _deployment(db, provider=provider, instance_id=instance_id)
    db.update_cloud_binding(
        "cloud5090", state="draining", idle_since=_ago(minutes=30), drain_started_at=_ago(minutes=5),
    )
    await service.process_once()
    assert fake_provider.shutdowns == ["i-1"]


async def test_busy_or_pending_work_prevents_shutdown(setup, tmp_path: Path) -> None:
    db, service, orchestrator = setup
    job_id = _add_job(db, tmp_path, JobState.TRANSCRIBING, lease_owner="cloud5090-02")
    db.update_cloud_binding(
        "cloud5090", idle_since=_ago(minutes=30), state="draining", drain_started_at=_ago(minutes=5)
    )
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert binding["state"] == "watching" and binding["idle_since"] is None

    db.start_job(job_id)
    force_job_state(db, job_id, JobState.WAITING_ASR, fields={"lease_owner": None})
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["idle_since"] is None  # 有等待识别的任务

    orchestrator.paused = True  # 暂停时不会派单，视为没有待办
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["idle_since"] is not None

    orchestrator.paused = False
    orchestrator.scan_status = "running"
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["idle_since"] is None


async def test_pending_job_for_unsupported_mode_does_not_block(setup, tmp_path: Path) -> None:
    db, service, _ = setup
    with db.connect() as conn:
        conn.execute("UPDATE workers SET supported_asr_modes_json = '[\"faster_whisper\"]'")
    _add_job(db, tmp_path, JobState.WAITING_ASR)
    await service.process_once()  # 默认 whisperjav 模式，这台机器领不到
    assert db.get_cloud_binding("cloud5090")["idle_since"] is not None


async def test_offline_workers_or_mode_off_never_shut_down(setup, fake_provider) -> None:
    db, service, _ = setup
    with db.connect() as conn:
        conn.execute("UPDATE workers SET last_seen_at = ?", (_ago(minutes=10),))
    db.update_cloud_binding(
        "cloud5090", idle_since=_ago(minutes=30), state="draining", drain_started_at=_ago(minutes=5)
    )
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "watching"
    assert fake_provider.shutdowns == []

    with db.connect() as conn:
        conn.execute("UPDATE workers SET last_seen_at = ?", (_ago(seconds=1),))
    db.update_cloud_binding(
        "cloud5090",
        shutdown_mode="off",
        idle_since=_ago(minutes=30),
        state="draining",
        drain_started_at=_ago(minutes=5),
    )
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "watching"
    assert fake_provider.shutdowns == []


async def test_shutdown_failure_retries_with_backoff(setup, fake_provider) -> None:
    db, service, _ = setup
    fake_provider.shutdown_error = CloudError(CloudErrorKind.TRANSIENT, "平台繁忙")
    db.update_cloud_binding(
        "cloud5090", idle_since=_ago(minutes=30), state="draining", drain_started_at=_ago(minutes=5)
    )
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert (
        binding["state"] == "failed"
        and binding["attempts"] == 1
        and binding["last_error"] == "平台繁忙"
    )
    assert datetime.fromisoformat(binding["next_attempt_at"]) > datetime.now(UTC)
    assert render(db.list_notices()[0]["title"]) == "云主机关机失败"

    fake_provider.shutdown_error = None
    db.update_cloud_binding("cloud5090", next_attempt_at=_ago(seconds=1))
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "draining"  # 重试前先停止派单
    db.update_cloud_binding("cloud5090", drain_started_at=_ago(seconds=61))
    await service.process_once()
    assert fake_provider.shutdowns == ["i-1"]


async def test_missing_or_stopped_instance_is_reported(setup, fake_provider) -> None:
    db, service, _ = setup
    fake_provider.instances = [CloudInstance(id="i-1", status="stopped", status_text="已关机")]
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "stopped"
    assert db.list_notices() == []  # 刚关机，旧心跳尚未超时
    db.update_cloud_binding("cloud5090", stopped_at=_ago(minutes=5))
    await service.process_once()
    assert render(db.list_notices()[0]["title"]) == "云主机绑定可能有误"  # 关机后仍有新心跳

    fake_provider.instances = []
    service.invalidate("fake")
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert "不存在" in render(binding["last_error"])
    assert fake_provider.shutdowns == []


async def test_local_preview_does_not_act(setup, fake_provider) -> None:
    db, _, orchestrator = setup
    service = CloudService(AppConfig(), db, orchestrator, auto_enabled=False)
    db.update_cloud_binding(
        "cloud5090", idle_since=_ago(minutes=30), state="draining", drain_started_at=_ago(minutes=5)
    )
    await service.process_once()
    assert fake_provider.shutdowns == [] and fake_provider.idle_close == []
    assert service.status()["auto_enabled"] is False


def test_binding_same_instance_twice_is_rejected(setup) -> None:
    db, _, _ = setup
    with pytest.raises(ValueError):
        db.bind_cloud_instance("other", CloudBindingRequest(provider="fake", instance_id="i-1"))


# ---------------------------------------------------------------------------
# 控制台接口
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch, fake_provider):
    prepare_server(tmp_path, monkeypatch)
    with TestClient(app) as test_client:
        app.state.database.set_cloud_profile("fake", {"type": "fake"})
        yield test_client


def test_cloud_api_flow(client: TestClient, fake_provider) -> None:
    providers = {item["id"]: item for item in client.get("/api/cloud/providers").json()}
    assert set(providers) == {"fake"} and providers["fake"]["configured"] is False
    assert (
        client.put("/api/cloud/providers/fake/credentials", json={"values": {}}).status_code == 400
    )
    assert (
        client.put(
            "/api/cloud/providers/nope/credentials", json={"values": {"api_key": "x"}}
        ).status_code
        == 404
    )

    assert (
        client.post(
            "/api/cloud/providers/fake/test", json={"values": {"api_key": "bad"}}
        ).status_code
        == 400
    )
    tested = client.post(
        "/api/cloud/providers/fake/test", json={"values": {"api_key": "secret-key-9876"}}
    ).json()
    assert tested["ok"] is True and "1 台运行中" in render(tested["message"])
    assert client.get("/api/cloud/providers").json()  # 测试不保存
    assert {i["id"]: i for i in client.get("/api/cloud/providers").json()}["fake"][
        "configured"
    ] is False

    assert client.put(
        "/api/cloud/providers/fake/credentials",
        json={"values": {"api_key": "secret-key-9876", "region": "cn"}},
    ).json() == {"saved": True}
    fake = {i["id"]: i for i in client.get("/api/cloud/providers").json()}["fake"]
    assert fake["configured"] is True
    assert fake["values"] == {"api_key": "****9876", "region": "cn"}
    # 留空保持原 Key
    client.put("/api/cloud/providers/fake/credentials", json={"values": {"region": "hk"}})
    assert app.state.database.cloud_credentials("fake") == {
        "api_key": "secret-key-9876",
        "region": "hk",
    }

    instances = client.get("/api/cloud/instances").json()
    assert [g["provider"] for g in instances] == ["fake"]
    assert instances[0]["instances"][0]["id"] == "i-1"

    assert (
        client.put(
            "/api/cloud/bindings/cloud5090", json={"provider": "fake", "instance_id": "nope"}
        ).status_code
        == 404
    )
    bound = client.put(
        "/api/cloud/bindings/cloud5090", json={"provider": "fake", "instance_id": "i-1"}
    ).json()
    assert render(bound["instance_title"]) == "租来的 5090" and bound["shutdown_mode"] == "always"
    assert (
        client.put(
            "/api/cloud/bindings/other", json={"provider": "fake", "instance_id": "i-1"}
        ).status_code
        == 409
    )
    assert (
        client.put(
            "/api/cloud/bindings/cloud5090/shutdown-mode", json={"shutdown_mode": "off"}
        ).status_code
        == 200
    )
    status = client.get("/api/status").json()["cloud"]
    assert status["bindings"]["cloud5090"]["shutdown_mode"] == "off"
    assert status["providers_configured"] is True

    settings = client.put(
        "/api/settings/cloud", json={"idle_minutes": 15, "backstop_minutes": 30}
    ).json()
    assert settings == {
        "idle_minutes": 15,
        "backstop_minutes": 30,
        "update_jasna": True,
        "jasna_license_set": False,
    }
    off = client.put(
        "/api/settings/cloud",
        json={"idle_minutes": 15, "backstop_minutes": 30, "update_jasna": False},
    ).json()
    assert off["update_jasna"] is False
    assert (
        client.put(
            "/api/settings/cloud", json={"idle_minutes": 1, "backstop_minutes": 30}
        ).status_code
        == 422
    )

    assert client.post("/api/cloud/instances/fake/i-1/shutdown").json() == {"accepted": True}
    assert fake_provider.shutdowns == ["i-1"]
    notices = client.get("/api/notices").json()
    assert notices["unread"] == 1 and render(notices["items"][0]["title"]) == "已手动关机"
    assert client.post("/api/notices/read").json()["unread"] == 0

    assert client.delete("/api/cloud/bindings/cloud5090").json() == {"unbound": True}
    assert client.delete("/api/cloud/providers/fake/credentials").json() == {"cleared": True}
    assert app.state.database.cloud_credentials("fake") == {}


def test_named_cloud_profiles_are_independent(client: TestClient, fake_provider, monkeypatch):
    async def account_instances(self):
        return [CloudInstance(id="shared-id", title=self.credentials["api_key"], status="running")]

    monkeypatch.setattr(fake_provider, "list_instances", account_instances)
    first = client.post(
        "/api/cloud/providers",
        json={"type": "fake", "name": "主账号", "values": {"api_key": "first-key"}},
    )
    second = client.post(
        "/api/cloud/providers",
        json={"type": "fake", "name": "备用账号", "values": {"api_key": "second-key"}},
    )
    assert first.status_code == second.status_code == 200
    a, b = first.json()["id"], second.json()["id"]
    assert a != b
    service = app.state.cloud
    # 高级设置里的模型安装目录按配置保存；留空时用平台默认目录，再没有就是登录用户的 $HOME/models（空）。
    assert service.model_dir(a) == ""
    update = {"name": "主账号", "values": {}}
    assert client.put(f"/api/cloud/providers/{a}", json={**update, "model_dir": "data/models"}).status_code == 400
    assert client.put(f"/api/cloud/providers/{a}", json={**update, "model_dir": " ~/data/models "}).status_code == 200
    assert service.model_dir(a) == "~/data/models" and service.model_dir(b) == ""
    listed = {item["id"]: item for item in client.get("/api/cloud/providers").json()}
    assert listed[a]["model_dir"] == "~/data/models" and listed[b]["model_dir"] == ""
    monkeypatch.setattr(fake_provider, "default_model_dir", "/data/models", raising=False)
    assert service.model_dir(b) == "/data/models" and service.model_dir(a) == "~/data/models"
    assert client.put(f"/api/cloud/providers/{a}", json=update).status_code == 200
    assert service.model_dir(a) == "/data/models"
    monkeypatch.setattr(fake_provider, "default_model_dir", "")
    assert service.provider(a) is service.provider(b) is fake_provider
    assert service.status()["launch_providers"] == [
        {"id": a, "label": "主账号"},
        {"id": b, "label": "备用账号"},
    ]
    groups = {g["provider"]: g for g in client.get("/api/cloud/instances").json()}
    assert groups[a]["instances"][0]["title"] == "first-key"
    assert groups[b]["instances"][0]["title"] == "second-key"
    assert (
        client.put(
            "/api/cloud/bindings/machine-a", json={"provider": a, "instance_id": "shared-id"}
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/cloud/bindings/machine-b", json={"provider": b, "instance_id": "shared-id"}
        ).status_code
        == 200
    )
    assert client.put(f"/api/cloud/providers/{a}", json={"name": "重命名"}).status_code == 200
    assert render(service.binding_views()["machine-a"]["provider_label"]) == "重命名"
    assert service.database.cloud_credentials(a)["api_key"] == "first-key"
    assert client.get(f"/api/cloud/providers/{a}/launch-options").status_code == 200
    monkeypatch.setattr(app.state.cloud_launcher, "start", lambda _: None)
    launch = client.post(
        "/api/cloud/launches",
        json={"provider": a, "method": "startup", "target_id": "shared-id", "gpu_id": "existing"},
    )
    assert launch.status_code == 200 and launch.json()["provider"] == a
    assert client.delete(f"/api/cloud/providers/{a}").status_code == 409
    service.database.update_cloud_launch(launch.json()["id"], state="cancelled")
    assert client.delete(f"/api/cloud/providers/{a}").status_code == 200
    assert service.database.cloud_credentials(a) == {}
    assert service.database.cloud_credentials(b)["api_key"] == "second-key"
    assert service.binding_views()["machine-a"]["provider_missing"] is True


def test_cloud_profile_validation_and_rename(client: TestClient, fake_provider):
    for body, status in [
        ({"type": "fake", "name": "   ", "values": {"api_key": "k"}}, 400),
        ({"type": "missing", "name": "账号"}, 404),
        ({"type": "fake", "name": "账号"}, 400),
    ]:
        assert client.post("/api/cloud/providers", json=body).status_code == status
    assert app.state.database.cloud_profiles() == {"fake": {"type": "fake"}}
    app.state.database.set_cloud_credentials("fake", {"api_key": "saved-key"})
    assert client.put("/api/cloud/providers/fake", json={"name": "旧账号"}).status_code == 200
    assert app.state.cloud.provider("fake") is fake_provider
    assert app.state.database.cloud_credentials("fake") == {"api_key": "saved-key"}
    assert (
        client.post(
            "/api/cloud/providers",
            json={"type": "fake", "name": "旧账号", "values": {"api_key": "new-key"}},
        ).status_code
        == 409
    )


@pytest.mark.parametrize("action", ["destroy", "save-image"])
def test_stopped_actions_require_fresh_stopped_instance(
    client: TestClient, fake_provider, action: str
) -> None:
    db = app.state.database
    db.set_cloud_profile("fake", {"type": "fake"})
    db.set_cloud_credentials("fake", {"api_key": "k"})
    fake_provider.instances = [CloudInstance(id="i-1", status="stopped")]
    # 界面缓存已关机，平台状态变化后仍应拒绝操作。
    assert client.get("/api/cloud/instances").status_code == 200
    for state in ("running", "starting", "stopping", "unknown"):
        fake_provider.instances = [CloudInstance(id="i-1", status=state)]
        response = client.post(f"/api/cloud/instances/fake/i-1/{action}")
        assert response.status_code == 400
        assert "尚未关机" in render(response.json()["detail"])
    fake_provider.instances = []
    assert client.post(f"/api/cloud/instances/fake/i-1/{action}").status_code == 400
    assert fake_provider.destroys == [] and fake_provider.saved_images == []
    assert fake_provider.startups == []
    assert client.post(f"/api/cloud/instances/nope/i-1/{action}").status_code == 404


def test_destroy_success_unbinds_but_failure_preserves_binding(
    client: TestClient, fake_provider
) -> None:
    db = app.state.database
    db.set_cloud_profile("fake", {"type": "fake"})
    db.set_cloud_credentials("fake", {"api_key": "k"})
    db.bind_cloud_instance("cloud5090", CloudBindingRequest(provider="fake", instance_id="i-1"))
    fake_provider.instances = [CloudInstance(id="i-1", title="租来的 5090", status="stopped")]
    fake_provider.destroy_error = CloudError(CloudErrorKind.TRANSIENT, "平台繁忙")
    assert client.post("/api/cloud/instances/fake/i-1/destroy").status_code == 502
    assert db.get_cloud_binding("cloud5090") is not None
    assert fake_provider.destroys == []
    fake_provider.destroy_error = None
    assert client.post("/api/cloud/instances/fake/i-1/destroy").json() == {"accepted": True}
    assert fake_provider.destroys == ["i-1"]
    assert db.get_cloud_binding("cloud5090") is None
    assert "fake" not in app.state.cloud._cache
    assert db.notice_summary()["unread"] > 0


@pytest.mark.parametrize("action", ["destroy", "save-image"])
def test_stopped_action_unsupported_provider(
    client: TestClient, fake_provider, monkeypatch, action: str
) -> None:
    monkeypatch.setattr(fake_provider, "capabilities", frozenset({CAP_LIST}))
    response = client.post(f"/api/cloud/instances/fake/i-1/{action}")
    assert response.status_code == 400
    assert "不支持" in render(response.json()["detail"])
    assert fake_provider.destroys == [] and fake_provider.saved_images == []
    assert fake_provider.startups == []


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"image_name": ""}, {"image_name": "   "}, {"image_name": "  干净环境  "}],
)
def test_save_image_api_names_and_preserves_binding(
    client: TestClient, fake_provider, payload
) -> None:
    db = app.state.database
    db.set_cloud_profile("fake", {"type": "fake"})
    db.set_cloud_credentials("fake", {"api_key": "k"})
    db.bind_cloud_instance("cloud5090", CloudBindingRequest(provider="fake", instance_id="i-1"))
    fake_provider.instances = [CloudInstance(id="i-1", status="stopped")]
    response = client.post("/api/cloud/instances/fake/i-1/save-image", json=payload)
    assert response.json() == {"accepted": True}
    assert fake_provider.saved_images == [("i-1", (payload or {}).get("image_name", "").strip())]
    assert db.get_cloud_binding("cloud5090") is not None
    assert fake_provider.shutdowns == [] and fake_provider.destroys == []
    assert "fake" not in app.state.cloud._cache
    assert (
        client.post(
            "/api/cloud/instances/fake/i-1/save-image", json={"image_name": "x" * 301}
        ).status_code
        == 422
    )
    assert len(fake_provider.saved_images) == 1


def test_save_image_api_surfaces_platform_rejection(client: TestClient, fake_provider) -> None:
    app.state.database.set_cloud_profile("fake", {"type": "fake"})
    app.state.database.set_cloud_credentials("fake", {"api_key": "k"})
    fake_provider.instances = [CloudInstance(id="i-1", status="stopped")]
    fake_provider.save_image_error = CloudError(CloudErrorKind.FATAL, "当前状态不允许保存镜像")
    response = client.post("/api/cloud/instances/fake/i-1/save-image")
    assert response.status_code == 400
    assert render(response.json()["detail"]) == "当前状态不允许保存镜像"
    assert fake_provider.saved_images == []
    assert fake_provider.shutdowns == [] and fake_provider.destroys == []


def test_draining_machine_gets_no_jobs(client: TestClient, tmp_path: Path) -> None:
    db: Database = app.state.database
    token = app.state.worker_token
    _register(db, "cloud5090-01")
    job_id = _add_job(db, tmp_path, JobState.WAITING_ASR)
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET created_at = ?, audio_sha256 = 'x' WHERE id = ?",
            (_ago(days=1), job_id),
        )
    db.set_cloud_profile("fake", {"type": "fake"})
    db.set_cloud_credentials("fake", {"api_key": "k"})
    db.bind_cloud_instance("cloud5090", CloudBindingRequest(provider="fake", instance_id="i-1"))
    db.update_cloud_binding("cloud5090", state="draining")
    headers = {**PROTOCOL, "X-Worker-Token": token}
    assert client.post("/api/workers/cloud5090-01/claim", headers=headers).json() is None
    assert client.post("/api/workers/cloud5090-01/restore/claim", headers=headers).json() is None
    db.update_cloud_binding("cloud5090", state="watching")
    assert client.post("/api/workers/cloud5090-01/claim", headers=headers).json()["job_id"]


def test_ui_settings_defaults(client: TestClient) -> None:
    assert client.get("/api/settings/ui").json() == {"show_posters": True, "notice_sound": True}
