import asyncio
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
import yaml
from conftest import prepare_server, raises_rendered
from fastapi.testclient import TestClient

from video_turbo import cloud_store
from video_turbo.cloud.base import (
    CAP_LAUNCH,
    CAP_LIST,
    CAP_SHUTDOWN,
    CAP_SSH,
    CloudError,
    CloudErrorKind,
    CloudInstance,
    CloudProvider,
    CredentialField,
    GpuOffer,
    LaunchMethod,
    LaunchTarget,
    SshInfo,
)
from video_turbo.cloud_models import CloudBindingRequest, LaunchRequest
from video_turbo.config import AppConfig
from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.models import WorkerHeartbeat, WorkerRegistration
from video_turbo.orchestrator import cloud_launcher
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.cloud_deploy import (
    DeployError,
    DeployOutput,
    NvencPrecheckError,
    deploy_script,
    gpu_profile,
)
from video_turbo.orchestrator.cloud_launcher import CloudLauncher
from video_turbo.orchestrator.cloud_service import CloudService
from video_turbo.orchestrator.worker_bundle import WorkerBundle, build_bundle
from video_turbo.plugins import registry

pytestmark = pytest.mark.anyio
PROJECT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 部署包、配置写入与部署脚本
# ---------------------------------------------------------------------------


def test_worker_bundle_contains_linux_worker_files() -> None:
    bundle = build_bundle(PROJECT)
    assert bundle.version == build_bundle(PROJECT).version  # 内容不变，版本不变
    with tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
    for name in (
        "pyproject.toml",
        "src/video_turbo/worker/main.py",
        "deploy/worker/linux/worker.sh",
        "deploy/worker/requirements/asr.txt",
        "deploy/worker/requirements/restore.txt",
        "tools/configure_worker.py",
        "tools/download_jasna.py",
        "config/worker.example.yaml",
    ):
        assert name in members, name
    assert members["deploy/worker/linux/worker.sh"].mode == 0o755
    assert not any(name.startswith("deploy/worker/linux/") and name != "deploy/worker/linux/worker.sh" for name in members)
    assert not any(
        "__pycache__" in name or name.startswith(("tests/", "deploy/worker/windows")) for name in members
    )
    assert not any(name in {"config/worker.yaml", "deploy/server/.env"} for name in members)


def _load_configure_worker():
    spec = importlib.util.spec_from_file_location(
        "configure_worker", PROJECT / "tools/configure_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configure_worker_apply_json(tmp_path: Path) -> None:
    module = _load_configure_worker()
    config = tmp_path / "worker.yaml"
    settings = tmp_path / "deploy.json"
    settings.write_text(
        json.dumps(
            {"server_url": "http://1.2.3.4:8766", "worker_token": 't"ok', "name": "ignored"}
        )
    )
    args = [
        "--config",
        str(config),
        "--template",
        str(PROJECT / "config/worker.example.yaml"),
        "--default-download-root",
        "/opt/models",
        "--apply-json",
        str(settings),
    ]
    assert module.main(args) == 0
    loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert loaded["server_url"] == "http://1.2.3.4:8766"
    assert loaded["worker_token"] == 't"ok'
    assert "name" not in loaded  # Worker 名称由显卡自动生成，不写入配置


def test_deploy_script_and_profile() -> None:
    assert gpu_profile("NVIDIA GeForce RTX 5090") == "5090"
    assert gpu_profile("RTX 4090D") == "4090"
    assert gpu_profile("A100") is None
    script = deploy_script("5090", "abc123")
    assert "PROFILE=5090" in script and "VERSION=abc123" in script
    assert "SERVICES=asr\n" in script
    assert 'WORKER_SH="deploy/worker/linux/worker.sh"' in script
    assert 'bash "${WORKER_SH}" install "${SERVICE}" --profile "${PROFILE}" --yes' in script
    assert 'bash "${WORKER_SH}" start "${START}" --profile "${PROFILE}" --yes' in script
    assert "--apply-json" in script
    assert "UPDATE_JASNA=1\n" in script
    assert "UPDATE_JASNA=0\n" in deploy_script("5090", "v", update_jasna=False)
    # 顺序固定、去重；不选服务时不生成部署脚本。
    assert "SERVICES='asr restore'" in deploy_script("5090", "v", ["restore", "asr", "asr"])
    with pytest.raises(ValueError):
        deploy_script("5090", "v", [])


def test_launch_request_services() -> None:
    base = {"provider": "p", "method": "m", "target_id": "t", "gpu_id": "g"}
    default = LaunchRequest(**base)
    assert default.services == ["asr"] and default.deploy is True
    both = LaunchRequest(**base, services=["restore", "asr"])
    assert both.services == ["asr", "restore"] and both.deploy is True
    none = LaunchRequest(**base, services=[])
    assert none.services == [] and none.deploy is False
    with pytest.raises(ValueError):
        LaunchRequest(**base, shutdown_mode="once")
    with pytest.raises(ValueError):
        LaunchRequest(**base, services=["gpu"])


def test_launch_services_persist(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    db.initialize()
    base = {"provider": "p", "method": "m", "target_id": "t", "gpu_id": "g"}
    only_dc = db.create_cloud_launch(LaunchRequest(**base, services=["restore"]))
    boot_only = db.create_cloud_launch(LaunchRequest(**base, services=[]))
    assert db.get_cloud_launch(only_dc)["services"] == ["restore"]
    assert db.get_cloud_launch(boot_only)["services"] == []
    assert db.get_cloud_launch(boot_only)["deploy"] is False

    assert db.jasna_license() == ""
    db.set_jasna_license("  LIC-1  ")
    assert db.jasna_license() == "LIC-1"
    db.set_jasna_license(None)
    assert db.jasna_license() == ""


# ---------------------------------------------------------------------------
# 开云机流程（假平台 + 假部署）
# ---------------------------------------------------------------------------


class FakeLaunchProvider(CloudProvider):
    id = "fakelaunch"
    label = "测试云"
    capabilities = frozenset({CAP_LIST, CAP_SHUTDOWN, CAP_LAUNCH, CAP_SSH})
    credential_fields = (CredentialField("api_key", "API Key"),)
    launch_methods = (
        LaunchMethod("startup", "开启已关机实例", "实例", creates_instance=False),
        LaunchMethod("image", "镜像新建", "镜像"),
    )
    instances: ClassVar[list[CloudInstance]] = []
    script: ClassVar[list] = []
    launches: ClassVar[list[tuple]] = []

    async def list_instances(self) -> list[CloudInstance]:
        return [instance.model_copy() for instance in type(self).instances]

    async def gpu_offers(self) -> list[GpuOffer]:
        return [GpuOffer(id="g5090", label="RTX 5090")]

    async def launch_targets(self, method: str, query: str = "") -> list[LaunchTarget]:
        return [LaunchTarget(id="img", label="镜像")]

    async def launch(self, method: str, target_id: str, gpu_id: str, gpu_count: int) -> str | None:
        type(self).launches.append((method, target_id, gpu_id, gpu_count))
        step = type(self).script.pop(0) if type(self).script else "ok"
        if isinstance(step, CloudError):
            raise step
        if method == "startup":
            _set_status(target_id, "starting")
            return target_id
        type(self).instances.append(_instance("new-a1b2", "starting"))
        return "new-a1b2"


def _instance(instance_id: str, status: str, gpu: str = "RTX 5090") -> CloudInstance:
    return CloudInstance(
        id=instance_id,
        title=f"机器 {instance_id}",
        status=status,
        status_text=status,
        gpu_name=gpu,
        gpu_id="g5090",
        ssh=SshInfo(host="1.1.1.1", port=22, password="pw"),
    )


def _set_status(instance_id: str, status: str) -> None:
    for instance in FakeLaunchProvider.instances:
        if instance.id == instance_id:
            instance.status = status


@pytest.fixture
def launch_env(tmp_path: Path, monkeypatch):
    FakeLaunchProvider.instances = []
    FakeLaunchProvider.script = []
    FakeLaunchProvider.launches = []
    registry.register("cloud", FakeLaunchProvider)
    monkeypatch.setattr(cloud_launcher, "WORKER_ONLINE_TIMEOUT_SECONDS", 2)
    db = Database(tmp_path / "app.db")
    db.initialize()
    db.set_cloud_profile("fakelaunch", {"type": "fakelaunch"})
    db.set_cloud_credentials("fakelaunch", {"api_key": "k"})
    service = CloudService(AppConfig(), db, SimpleNamespace(paused=False, scan_status="idle"))
    deployed: list[dict] = []

    async def fake_deployer(
        ssh, *, bundle, profile, services, settings, log, update_jasna=True, model_dir=""
    ):
        log("[安装] 解压代码包")
        deployed.append(
            {
                "ssh": ssh,
                "profile": profile,
                "services": services,
                "settings": settings,
                "version": bundle.version,
            }
        )
        name = FAKE_MACHINE + "-01"

        async def come_online() -> None:
            await asyncio.sleep(0.05)
            _register(db, name)
            db.heartbeat_worker(name, WorkerHeartbeat())

        asyncio.get_running_loop().create_task(come_online())

    launcher = CloudLauncher(
        service,
        db,
        worker_token=lambda: "secret-token",
        deployer=fake_deployer,
        identifier=fake_identifier,
        bundle_factory=lambda: WorkerBundle(version="v1", data=b"x", file_count=1),
        poll_seconds=0.01,
    )
    yield db, service, launcher, deployed
    registry.unregister("cloud", FakeLaunchProvider.id)


def _register(db: Database, name: str) -> None:
    db.register_worker(
        WorkerRegistration(
            name=name, version="0.0.0", gpu_name="RTX 5090", memory_total_mb=32000, model="m", compute_type="f16", supported_asr_modes=["faster_whisper"]
        )
    )


def _create(db: Database, **overrides) -> str:
    request = LaunchRequest(
        provider=overrides.pop("provider", "fakelaunch"),
        method=overrides.pop("method", "image"),
        target_id=overrides.pop("target_id", "img"),
        target_label="镜像",
        gpu_id="g5090",
        gpu_label="RTX 5090",
        **overrides,
    )
    launch_id = db.create_cloud_launch(request)
    with db.connect() as conn:
        conn.execute("UPDATE cloud_launches SET interval_seconds = 0.01 WHERE id = ?", (launch_id,))
    return launch_id


FAKE_MACHINE = "gpu-a1b2c3d4"


async def fake_identifier(ssh, log) -> str:
    log("[安装] 读取 GPU UUID")
    return FAKE_MACHINE


async def _finish(launcher: CloudLauncher, launch_id: str, after_grab=None) -> None:
    launcher.start(launch_id)
    for _ in range(500):
        await asyncio.sleep(0.01)
        launch = launcher.database.get_cloud_launch(launch_id, with_log=False)
        if launch["state"] == "booting" and after_grab:
            after_grab()
            after_grab = None
        if launcher.tasks[launch_id].done():
            return
    raise AssertionError("开机任务没有结束")


async def test_instance_keeps_only_latest_launch(launch_env) -> None:
    db, service, launcher, _ = launch_env
    FakeLaunchProvider.instances = [_instance("old-1", "stopped")]
    # 已结束的旧任务：一条开机前就停止（只有目标实例），一条已经抢到；另一台实例的任务不受影响。
    stopped = _create(db, method="startup", target_id="old-1", services=[])
    done = _create(db, method="startup", target_id="old-1", services=[])
    other = _create(db, method="startup", target_id="old-2", services=[])
    image = _create(db, services=[])
    db.update_cloud_launch(stopped, state="cancelled")
    db.update_cloud_launch(done, state="running", instance_id="old-1")
    db.update_cloud_launch(other, state="cancelled")
    db.update_cloud_launch(image, state="failed")
    assert service.launch_instance_id(db.get_cloud_launch(stopped)) == "old-1"
    assert service.launch_instance_id(db.get_cloud_launch(image)) is None

    latest = _create(db, method="startup", target_id="old-1", services=[])
    await _finish(launcher, latest, after_grab=lambda: _set_status("old-1", "running"))
    assert db.get_cloud_launch(latest)["state"] == "running"
    assert {launch["id"] for launch in db.list_cloud_launches()} == {latest, other, image}
    assert service.status()["launches"][0]["target_instance_id"] == "old-1"

    # 移除实例时，它的任务记录一并删除。
    service.drop_instance_launches("fakelaunch", "old-1")
    assert {launch["id"] for launch in db.list_cloud_launches()} == {other, image}


@pytest.mark.parametrize("provider_id", ["fakelaunch", "cloud-custom"])
async def test_grab_boot_bind_and_deploy(launch_env, provider_id) -> None:
    db, _, launcher, deployed = launch_env
    if provider_id != "fakelaunch":
        db.set_cloud_profile(provider_id, {"type": "fakelaunch", "name": "自定义账号"})
        db.set_cloud_credentials(provider_id, {"api_key": "custom-key"})
    FakeLaunchProvider.script = [
        CloudError(CloudErrorKind.OUT_OF_STOCK, "无卡"),
        CloudError(CloudErrorKind.RATE_LIMIT, "慢点"),
        "ok",
    ]
    launch_id = _create(db, provider=provider_id, shutdown_mode="off")
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "running", launch["message"]
    assert launch["attempts"] == 3 and launch["instance_id"] == "new-a1b2"
    # 机器标识来自实例 GPU 的 UUID，不再按型号命名，部署也不再写入 name。
    assert launch["machine"] == FAKE_MACHINE
    assert f"{FAKE_MACHINE} 已上线" in render(launch["message"])
    assert deployed[0]["profile"] == "5090"
    assert deployed[0]["services"] == ["asr"]
    # 只部署字幕识别时不下发 Jasna 许可证码。
    assert deployed[0]["settings"] == {
        "server_url": "http://127.0.0.1:8766",
        "worker_token": "secret-token",
    }
    binding = db.get_cloud_binding(FAKE_MACHINE)
    assert binding["provider"] == provider_id
    assert binding["instance_id"] == "new-a1b2" and binding["shutdown_mode"] == "off"
    titles = [render(notice["title"]) for notice in db.list_notices()]
    assert titles == ["云主机已上线", "抢到 GPU 了"]
    assert "第 1 次：缺货" in render(launch["log"])


async def test_deploy_selected_services_with_license(launch_env) -> None:
    db, _, launcher, deployed = launch_env
    db.set_jasna_license("LIC-1")
    launch_id = _create(db, services=["asr", "restore"])
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    assert db.get_cloud_launch(launch_id)["state"] == "running"
    assert deployed[0]["services"] == ["asr", "restore"]
    assert deployed[0]["settings"]["jasna_license"] == "LIC-1"
    assert "LIC-1" not in db.get_cloud_launch(launch_id)["log"]

    launcher.redeploy(launch_id)  # 重新部署沿用创建时的选择
    await asyncio.gather(launcher.tasks[launch_id])
    assert deployed[1]["services"] == ["asr", "restore"]


async def test_boot_only_launch_skips_deploy(launch_env) -> None:
    db, _, launcher, deployed = launch_env
    launch_id = _create(db, services=[])
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "running" and "没有选择自动安装服务" in render(launch["message"])
    assert deployed == []


@pytest.mark.parametrize("services", [["restore"], ["asr", "restore"]])
async def test_nvenc_failure_stops_and_only_selected_asr_can_continue(launch_env, services) -> None:
    db, _, launcher, deployed = launch_env
    original_deployer = launcher.deployer

    async def failed_precheck(*args, **kwargs):
        raise NvencPrecheckError("NVENC 实际编码失败")

    launcher.deployer = failed_precheck
    launch_id = _create(db, services=services)
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "nvenc_failed" and launch["finished_at"]
    assert "仍在运行计费" in render(launch["message"])
    assert deployed == [] and launch_id not in db.active_cloud_launch_ids()
    notices = [n for n in db.list_notices() if "NVENC" in render(n["title"])]
    assert len(notices) == 1 and notices[0]["level"] == "error"
    assert "已停止安装" in render(notices[0]["message"])
    db.initialize()  # 重启初始化不会清除停止状态或恢复部署。
    launcher.resume_all()
    assert launcher.tasks[launch_id].done()

    if "asr" not in services:
        assert "继续" not in render(launch["message"])
        with raises_rendered(ValueError, "原先选了字幕服务"):
            launcher.continue_asr(launch_id)
        return

    assert "继续（仅字幕）" in render(launch["message"])
    launcher.deployer = original_deployer
    db.set_jasna_license("LIC-secret")
    launcher.continue_asr(launch_id)
    with pytest.raises(ValueError):
        launcher.continue_asr(launch_id)  # 连点不会重复启动。
    await launcher.tasks[launch_id]
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "running" and launch["asr_only"] is True
    assert launch["services"] == services  # 保留原服务选择。
    assert deployed[0]["services"] == ["asr"]
    assert "jasna_license" not in deployed[0]["settings"]
    db.initialize()
    assert db.get_cloud_launch(launch_id)["asr_only"] is True
    launcher.redeploy(launch_id)
    await launcher.tasks[launch_id]
    assert deployed[1]["services"] == services
    assert db.get_cloud_launch(launch_id)["asr_only"] is False


async def test_identity_failure_fails_deploy_without_binding(launch_env) -> None:
    db, _, launcher, deployed = launch_env

    async def broken(ssh, log):
        raise DeployError("nvidia-smi 不可用")

    launcher.identifier = broken
    launch_id = _create(db)
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "deploy_failed" and "识别云主机 GPU 失败" in render(launch["message"])
    assert deployed == []
    assert db.cloud_binding_for_instance("fakelaunch", "new-a1b2") is None


async def test_startup_falls_back_to_grabbing_when_stopped_again(launch_env, monkeypatch) -> None:
    db, _, launcher, _ = launch_env
    FakeLaunchProvider.instances = [_instance("old", "stopped")]
    launch_id = _create(db, method="startup", target_id="old", services=[])
    seen = []

    original = launcher._wait_boot

    async def wait_boot(launch):
        seen.append(launch["state"])
        if len(seen) == 1:
            _set_status("old", "stopped")  # 平台开机失败，实例回到已关机
            raise cloud_launcher._BackToGrabbing("开机后又回到关机，继续抢")
        _set_status("old", "running")
        return await original(launch)

    monkeypatch.setattr(launcher, "_wait_boot", wait_boot)
    await _finish(launcher, launch_id)
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "running"
    assert len(FakeLaunchProvider.launches) == 2  # 回到关机后又抢了一次
    assert "没有选择自动安装服务" in render(launch["message"])
    assert db.cloud_binding_for_instance("fakelaunch", "old") is None  # 不部署时不自动绑定


async def test_fatal_error_stops_grabbing(launch_env) -> None:
    db, _, launcher, _ = launch_env
    FakeLaunchProvider.script = [CloudError(CloudErrorKind.FATAL, "余额不足")]
    launch_id = _create(db)
    await _finish(launcher, launch_id)
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "failed" and "余额不足" in render(launch["message"])
    assert render(db.list_notices()[0]["title"]) == "开云主机失败"


async def test_timeout_checks_instance_list_before_retrying(launch_env) -> None:
    db, _, launcher, _ = launch_env

    def created_then_timeout():
        FakeLaunchProvider.instances.append(_instance("ghost", "running"))
        return CloudError(CloudErrorKind.TRANSIENT, "请求超时")

    original = FakeLaunchProvider.launch

    async def launch(self, method, target_id, gpu_id, gpu_count):
        raise created_then_timeout()

    FakeLaunchProvider.launch = launch
    try:
        launch_id = _create(db)
        await _finish(launcher, launch_id)
    finally:
        FakeLaunchProvider.launch = original
    launch = db.get_cloud_launch(launch_id)
    assert launch["instance_id"] == "ghost"  # 没有重复开机
    assert launch["state"] == "running"


async def test_deploy_failure_keeps_binding_and_can_redeploy(launch_env) -> None:
    db, _, launcher, _ = launch_env
    calls = []

    async def failing(ssh, **kwargs):
        calls.append(kwargs["profile"])
        raise DeployError("部署脚本退出码 1")

    launcher.deployer = failing
    launch_id = _create(db)
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "deploy_failed"
    assert (
        db.get_cloud_binding(FAKE_MACHINE)["instance_id"] == "new-a1b2"
    )  # 平台空闲关机保底仍然生效
    assert "云主机仍在运行计费" in render(db.list_notices()[0]["message"])

    launcher.redeploy(launch_id)
    await asyncio.gather(launcher.tasks[launch_id])
    assert calls == ["5090", "5090"]
    assert db.get_cloud_binding(FAKE_MACHINE)["instance_id"] == "new-a1b2"


async def test_host_key_is_remembered_per_instance_and_can_be_forgotten(launch_env) -> None:
    db, _, launcher, _ = launch_env
    seen: list[str | None] = []
    current = {"key": "ssh-ed25519 AAAA-first"}

    async def identifier(ssh, log):
        # 与 cloud_deploy._connect 相同的约定：没有记录时写入，有记录时必须一致。
        seen.append(ssh.host_key)
        if ssh.host_key is None:
            ssh.host_key = current["key"]
        elif ssh.host_key != current["key"]:
            raise DeployError("主机密钥与首次连接时记录的不一致，已拒绝连接。")
        return FAKE_MACHINE

    launcher.identifier = identifier
    launch_id = _create(db)
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    assert db.cloud_host_key("fakelaunch", "new-a1b2") == "ssh-ed25519 AAAA-first"

    current["key"] = "ssh-ed25519 AAAA-reinstalled"
    launcher.redeploy(launch_id)
    await asyncio.gather(launcher.tasks[launch_id])
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "deploy_failed" and "主机密钥" in render(launch["message"])
    assert db.cloud_host_key("fakelaunch", "new-a1b2") == "ssh-ed25519 AAAA-first"

    launcher.redeploy(launch_id, forget_host_key=True)
    await asyncio.gather(launcher.tasks[launch_id])
    assert seen == [None, "ssh-ed25519 AAAA-first", None]
    assert db.cloud_host_key("fakelaunch", "new-a1b2") == "ssh-ed25519 AAAA-reinstalled"
    assert db.cloud_host_key("fakelaunch", "other") is None


async def test_redeploy_moves_binding_to_gpu_identity(launch_env) -> None:
    db, _, launcher, _ = launch_env
    launch_id = _create(db, shutdown_mode="always")
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    # 实例之前绑定在其他机器名下。
    db.unbind_cloud_instance(FAKE_MACHINE)
    db.bind_cloud_instance(
        "cloud5090",
        CloudBindingRequest(provider="fakelaunch", instance_id="new-a1b2", shutdown_mode="always"),
    )
    launcher.redeploy(launch_id)
    await asyncio.gather(launcher.tasks[launch_id])
    assert db.get_cloud_binding("cloud5090") is None
    assert db.get_cloud_binding(FAKE_MACHINE)["instance_id"] == "new-a1b2"


async def test_cancel_and_resume(launch_env) -> None:
    db, _, launcher, _ = launch_env
    FakeLaunchProvider.script = [CloudError(CloudErrorKind.OUT_OF_STOCK, "无卡")] * 10_000
    launch_id = _create(db)
    launcher.start(launch_id)
    await asyncio.sleep(0.05)
    await launcher.cancel(launch_id)
    assert db.get_cloud_launch(launch_id)["state"] == "cancelled"

    other = _create(db)
    launcher.resume_all()  # 主控重启后继续进行中的任务
    assert other in launcher.tasks and launch_id not in db.active_cloud_launch_ids()
    await launcher.close()


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch, launch_env):
    prepare_server(tmp_path, monkeypatch, db_name="api.db")
    with TestClient(app) as test_client:
        yield test_client


def test_launch_api(client: TestClient, monkeypatch) -> None:
    db: Database = app.state.database
    db.set_cloud_profile("fakelaunch", {"type": "fakelaunch"})
    assert (
        client.get("/api/cloud/providers/fakelaunch/launch-options").status_code == 400
    )  # 未填 Key
    db.set_cloud_credentials("fakelaunch", {"api_key": "k"})
    options = client.get("/api/cloud/providers/fakelaunch/launch-options").json()
    assert [m["id"] for m in options["methods"]] == ["startup", "image"]
    assert options["gpus"][0]["id"] == "g5090"
    assert (
        client.get(
            "/api/cloud/providers/fakelaunch/launch-targets", params={"method": "image"}
        ).json()[0]["id"]
        == "img"
    )

    payload = {"provider": "fakelaunch", "method": "image", "target_id": "img", "gpu_id": "g5090"}
    assert (
        client.post(
            "/api/cloud/launches", json={**payload, "method": "nope", "services": []}
        ).status_code
        == 400
    )

    started: list[str] = []
    monkeypatch.setattr(app.state.cloud_launcher, "start", started.append)
    created = client.post("/api/cloud/launches", json={**payload, "services": []}).json()
    assert created["state"] == "grabbing" and started == [created["id"]]
    status = client.get("/api/status").json()["cloud"]
    assert status["launches"][0]["id"] == created["id"]
    assert status["launch_providers"] == [{"id": "fakelaunch", "label": "测试云"}]
    assert "log" not in status["launches"][0]
    assert client.delete(f"/api/cloud/launches/{created['id']}").status_code == 409  # 进行中不能删
    assert client.post(f"/api/cloud/launches/{created['id']}/redeploy").status_code == 409

    saved = client.put(
        "/api/settings/cloud", json={"idle_minutes": 10, "backstop_minutes": 60}
    ).json()
    assert saved["jasna_license_set"] is False and "worker_server_url" not in saved

    # Jasna 许可证码：保存后不回显，留空保持原值，可单独清除。
    cloud = {"idle_minutes": 10, "backstop_minutes": 60}
    saved = client.put("/api/settings/cloud", json={**cloud, "jasna_license": "LIC-9"}).json()
    assert saved["jasna_license_set"] is True and "LIC-9" not in json.dumps(saved)
    assert db.jasna_license() == "LIC-9"
    assert "LIC-9" not in client.get("/api/settings/cloud").text
    assert client.put("/api/settings/cloud", json={**cloud, "jasna_license": ""}).json()[
        "jasna_license_set"
    ]
    assert client.get("/api/status").json()["cloud"]["jasna_license_set"] is True
    cleared = client.delete("/api/settings/cloud/jasna-license").json()
    assert cleared["jasna_license_set"] is False and db.jasna_license() == ""

    # 选择服务创建任务；只开机的任务不能重新部署。
    both = client.post(
        "/api/cloud/launches", json={**payload, "services": ["restore", "asr"]}
    ).json()
    assert both["services"] == ["asr", "restore"] and both["deploy"] is True
    boot_only = db.get_cloud_launch(created["id"], with_log=False)
    assert boot_only["services"] == []
    db.update_cloud_launch(created["id"], state="running", instance_id="i-1")
    response = client.post(f"/api/cloud/launches/{created['id']}/redeploy")
    assert response.status_code == 409 and "没有选择要安装的服务" in render(response.json()["detail"])


def test_full_launch_log_is_kept_in_file_and_downloadable(client, monkeypatch) -> None:
    db = app.state.database
    monkeypatch.setattr(cloud_store, "LAUNCH_LOG_LIMIT", 50)
    launch_id = _create(db)
    lines = [f"第 {index} 行\n" for index in range(40)]
    for line in lines:
        db.append_cloud_launch_log(launch_id, line)
    # 数据库只留末尾给弹窗，文件里是完整日志。
    assert len(db.get_cloud_launch(launch_id)["log"]) == 50
    path = db.cloud_launch_log_path(launch_id)
    assert path.read_text(encoding="utf-8") == "".join(lines)

    response = client.get(f"/api/cloud/launches/{launch_id}/log")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.text.startswith(f"# 任务 {launch_id}")
    assert response.text.endswith("".join(lines))
    assert client.get("/api/cloud/launches/missing/log").status_code == 404

    # 没有日志文件（例如写入失败）时退回数据库里的末尾部分。
    path.unlink()
    assert client.get(f"/api/cloud/launches/{launch_id}/log").text.endswith(db.get_cloud_launch(launch_id)["log"])

    db.append_cloud_launch_log(launch_id, "再来一行\n")
    db.update_cloud_launch(launch_id, state="failed")
    assert client.delete(f"/api/cloud/launches/{launch_id}").status_code == 200
    assert not path.exists()
    db.append_cloud_launch_log(launch_id, "任务删掉后不再生成文件\n")
    assert not path.exists()


def test_launch_log_file_stops_at_size_limit(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "app.db")
    db.initialize()
    monkeypatch.setattr(cloud_store, "LAUNCH_LOG_FILE_LIMIT", 30)
    launch_id = _create(db)
    for _ in range(5):
        db.append_cloud_launch_log(launch_id, "0123456789\n")
    text = db.cloud_launch_log_path(launch_id).read_text(encoding="utf-8")
    assert text.startswith("0123456789\n0123456789\n") and "之后的内容不再写入此文件" in render(text)
    assert text.count("0123456789") == 2


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_deploy_output_samples_progress_and_keeps_last_state() -> None:
    clock, lines = FakeClock(), []
    output = DeployOutput(lines.append, clock=clock)
    output.feed("[安装] 下载模型\n")
    for percent in range(0, 100, 10):
        output.feed(f"\rmodel {percent}%")
        clock.now += 3
        output.tick()
    output.feed("\rmodel 100%\n[安装] 完成\n")
    output.close()
    progress = [line for line in lines if line.startswith("model")]
    # 每 10 秒左右记一次，而不是每次刷新都记；最后的 100% 一定在。
    assert len(progress) < 6 and progress[0] == "model 0%" and progress[-1] == "model 100%"
    assert len(progress) == len(set(progress))
    assert render(lines[0]) == "[安装] 下载模型" and render(lines[-1]) == "[安装] 完成"
    assert render(output.step) == "完成"


def test_deploy_output_handles_crlf_split_across_chunks() -> None:
    clock, lines = FakeClock(), []
    output = DeployOutput(lines.append, clock=clock)
    output.feed("\rbar 1%\r")
    output.feed("bar 2%\r")  # 10 秒内的刷新先不记
    output.feed("\n[安装] 解压\r")
    output.feed("\n下一行\n")
    assert lines == ["bar 1%", "bar 2%", "[安装] 解压", "下一行"]
    assert render(output.step) == "解压"


def test_deploy_output_reports_unfinished_line_and_silence() -> None:
    clock, lines = FakeClock(), []
    output = DeployOutput(lines.append, clock=clock)
    output.feed("[安装] 安装依赖\n正在编译")
    clock.now += 29
    output.tick()
    assert lines == ["[安装] 安装依赖"]
    clock.now += 1
    output.tick()
    assert render(lines[-1]) == "正在编译 …（这一行还没结束，先显示已收到的部分）"
    output.feed("....")  # 还在输出但没换行：30 秒后再显示一次最新内容
    clock.now += 30
    output.tick()
    assert render(lines[-1]).startswith("正在编译....")
    clock.now += 30
    output.tick()
    # 从最后一次收到输出（"...."）算起。
    assert render(lines[-1]) == "[等待] 已 60 秒没有新输出，仍在执行：安装依赖"
    clock.now += 600
    output.tick()
    assert render(lines[-1]) == "[等待] 已 11 分钟没有新输出，仍在执行：安装依赖"
    output.feed(" 完成\n")
    output.close()
    assert render(lines[-1]) == "正在编译.... 完成"


def test_deploy_output_shows_stalled_progress_refresh() -> None:
    clock, lines = FakeClock(), []
    output = DeployOutput(lines.append, clock=clock)
    output.feed("\rdownload 10%\rdownload 20%")
    assert lines == ["download 10%"]
    clock.now += 10
    output.tick()  # 进度条停在 20%：按抽样间隔写出，不等 30 秒
    assert lines == ["download 10%", "download 20%"]
    output.feed("\r")
    output.close()
    assert lines == ["download 10%", "download 20%"]


def test_continue_asr_api_enforces_stopped_state_and_original_selection(client, monkeypatch):
    db = app.state.database
    started = []
    monkeypatch.setattr(app.state.cloud_launcher, "start", started.append)
    assert client.post("/api/cloud/launches/missing/continue-asr").status_code == 404
    launch_id = _create(db, services=["asr", "restore"])
    endpoint = f"/api/cloud/launches/{launch_id}/continue-asr"
    assert client.post(endpoint).status_code == 409
    db.update_cloud_launch(launch_id, state="nvenc_failed", instance_id="i-1")
    response = client.post(endpoint)
    assert response.status_code == 200
    assert response.json()["state"] == "deploying" and response.json()["asr_only"] is True
    assert started == [launch_id]
    assert client.post(endpoint).status_code == 409
    only_restore = _create(db, services=["restore"])
    db.update_cloud_launch(only_restore, state="nvenc_failed", instance_id="i-2")
    assert client.post(f"/api/cloud/launches/{only_restore}/continue-asr").status_code == 409
    db.update_cloud_launch(launch_id, state="nvenc_failed")
    retried = client.post(f"/api/cloud/launches/{launch_id}/redeploy")
    assert retried.status_code == 200 and retried.json()["asr_only"] is False


# ---------------------------------------------------------------------------
# SSH 部署（本地 asyncssh 服务端）
# ---------------------------------------------------------------------------


async def test_deploy_worker_over_ssh(tmp_path: Path) -> None:
    import asyncssh

    from video_turbo.orchestrator.cloud_deploy import deploy_worker

    scripts: list[str] = []
    exit_codes = [0, 3, 6]
    progress_seen = asyncio.Event()
    progress_lines = [
        "LTX download:  50%|█████     | 7.50G/15.0G [00:10<00:10, 750MB/s]",
        "字幕模型:  25%|██▌       | 250M/1.00G [00:05<00:15, 50.0MB/s]",
    ]

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return username == "root" and password == "pw"

    async def handle(process: asyncssh.SSHServerProcess) -> None:
        scripts.append(await process.stdin.read())
        exit_code = exit_codes.pop(0)
        process.stdout.write("[安装] 解压代码包\r\n")
        if exit_code == 0:
            # tqdm 用回车刷新；必须在下载/进程结束之前转发，不能等到换行。
            process.stdout.write("\r" + progress_lines[0][:20])
            await asyncio.sleep(0)
            process.stdout.write(progress_lines[0][20:] + "\r\n\r" + progress_lines[1] + "\r")
            try:
                await asyncio.wait_for(progress_seen.wait(), timeout=3)
            except TimeoutError:
                process.stderr.write("下载进度未实时转发\n")
                process.exit(9)
                return
        process.stdout.write("[安装] 完成\n末尾没有换行")
        process.exit(exit_code)

    server = await asyncssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=handle,
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(tmp_path)),
    )
    port = server.sockets[0].getsockname()[1]
    ssh = SshInfo(host="127.0.0.1", port=port, username="root", password="pw")
    bundle = WorkerBundle(version="v9", data=b"bundle-bytes", file_count=1)
    lines: list[str] = []

    def log(line: str) -> None:
        lines.append(line)
        if all(progress in lines for progress in progress_lines):
            progress_seen.set()

    try:
        await deploy_worker(
            ssh,
            bundle=bundle,
            profile="5090",
            settings={"server_url": "http://nas:8766", "worker_token": "tok"},
            log=log,
        )
        # 上传到登录用户的家目录（SFTP 相对路径），不假定是 /root。
        assert (tmp_path / ".video-turbo-bundle.tar.gz").read_bytes() == b"bundle-bytes"
        settings_file = tmp_path / ".video-turbo-deploy.json"
        assert json.loads(settings_file.read_text())["worker_token"] == "tok"
        assert settings_file.stat().st_mode & 0o777 == 0o600
        assert "PROFILE=5090" in scripts[0] and "VERSION=v9" in scripts[0]
        assert "tok" not in scripts[0]  # 密钥只经 SFTP 传递
        assert "[安装] 完成" in render(lines)
        assert progress_seen.is_set()
        assert "末尾没有换行" in render(lines)
        assert all("\r" not in line for line in lines)
        with raises_rendered(DeployError, "退出码 3"):
            await deploy_worker(ssh, bundle=bundle, profile="5090", settings={}, log=lines.append)
        with raises_rendered(NvencPrecheckError, "NVENC"):
            await deploy_worker(
                ssh, bundle=bundle, profile="5090", services=["restore"],
                settings={}, log=lines.append,
            )
        # 首次连接记录主机密钥；之后同一把密钥通过校验（上面第二次部署已连上）。
        assert ssh.host_key and ssh.host_key.startswith("ssh-ed25519 ")
        assert any("已记录身份标识（SSH 主机密钥）" in render(line) for line in lines)
        # 密钥与记录的不一致时立即拒绝，不重试、不发送任何内容。
        other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
        spoofed = ssh.model_copy(update={"host_key": other})
        sent = len(scripts)
        with raises_rendered(DeployError, "主机密钥）和第一次连接时不一致"):
            await deploy_worker(spoofed, bundle=bundle, profile="5090", settings={}, log=lines.append)
        assert len(scripts) == sent
    finally:
        server.close()
        await server.wait_closed()


def test_deploy_script_runs_in_bash(tmp_path: Path) -> None:
    import subprocess

    from video_turbo.orchestrator import cloud_deploy

    calls = tmp_path / "calls.txt"
    fail_flag = tmp_path / "fail-restore"
    deps_file = tmp_path / "deps"
    outdated_flag = tmp_path / "jasna-outdated"
    precheck_fail = tmp_path / "precheck-fail"
    # 桩脚本记录调用及收到的许可证码；安装模拟创建虚拟环境和 Jasna。
    # __deps_key 输出 deps_file 中的依赖指纹，__jasna_outdated 在 outdated_flag 存在时返回 0。
    stub = (
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = __restore_precheck ]; then [ ! -f {precheck_fail} ]; exit $?; fi\n'
        f'if [ "$1" = __deps_key ]; then cat {deps_file}; exit 0; fi\n'
        f'if [ "$1" = __jasna_outdated ]; then [ -f {outdated_flag} ]; exit $?; fi\n'
        f'[ "$1" != install ] || [ "$2" != restore ] || echo "update=$VIDEO_TURBO_JASNA_UPDATE" >> {calls}.env\n'
        f'echo "$(basename "$0") $*${{VIDEO_TURBO_JASNA_LICENSE:+ license=$VIDEO_TURBO_JASNA_LICENSE}}" >> {calls}\n'
        'if [ "$1" = install ] && [ "$2" = restore ] && '
        f"[ -f {fail_flag} ]; then exit 7; fi\n"
        'if [ "$1" = install ]; then mkdir -p .venv/bin .venv-whisperjav/bin jasna/current\n'
        "touch .venv/bin/python .venv-whisperjav/bin/python jasna/current/jasna\n"
        "chmod +x .venv/bin/python .venv-whisperjav/bin/python jasna/current/jasna\nfi\n"
    )
    files = {
        "tools/configure_worker.py": (PROJECT / "tools/configure_worker.py").read_text(),
        "config/worker.example.yaml": (
            PROJECT / "config/worker.example.yaml"
        ).read_text(),
        "deploy/worker/linux/worker.sh": stub,
    }
    home = tmp_path / "home"
    home.mkdir()
    bundle = home / cloud_deploy.REMOTE_BUNDLE
    settings = home / cloud_deploy.REMOTE_SETTINGS
    root = home / cloud_deploy.REMOTE_ROOT

    def run(
        deps: str,
        services=("asr",),
        license_code: str = "",
        ok: bool = True,
        version: str = "code-1",
        update_jasna: bool = True,
        model_dir: str = "",
    ) -> str:
        calls.write_text("")
        deps_file.write_text(deps + "\n")
        with tarfile.open(bundle, "w:gz") as archive:
            for name, content in files.items():
                data = content.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))
        payload = {"server_url": "http://nas:8766", "worker_token": "tok"}
        if license_code:
            payload["jasna_license"] = license_code
        settings.write_text(json.dumps(payload))
        script = cloud_deploy.deploy_script(
            "5090",
            version,
            services,
            update_jasna=update_jasna,
            model_dir=model_dir,
            home=str(home),
        )
        result = subprocess.run(
            ["bash", "-s"], input=script, text=True, capture_output=True, check=False
        )
        assert (result.returncode == 0) is ok, result.stdout + result.stderr
        assert not settings.exists() and not bundle.exists()
        return result.stdout

    def called() -> list[str]:
        return calls.read_text().splitlines()

    # 全新实例同时选择两个服务：预检失败时，连字幕安装和配置写入也不能开始。
    precheck_fail.touch()
    out = run("v1", ("asr", "restore"), ok=False)
    assert "显卡视频编码检查未通过" in render(out)
    assert called() == []
    assert not (root / "config/worker.yaml").exists()
    assert not (root / ".video-turbo-deployed-asr").exists()
    precheck_fail.unlink()

    # 全新实例：没有可停止的 Worker，直接安装并启动。
    run("v1")
    assert called() == [
        "worker.sh install asr --profile 5090 --yes",
        "worker.sh start asr --profile 5090 --yes",
    ]
    assert (root / ".video-turbo-deployed-asr").read_text().strip() == "v1"
    assert yaml.safe_load((root / "config/worker.yaml").read_text())["worker_token"] == "tok"
    # 模型目录默认在登录用户的家目录；配置里填写的目录每次部署都写入 worker.yaml。
    def download_root() -> str:
        return yaml.safe_load((root / "config/worker.yaml").read_text())["asr"]["download_root"]

    assert download_root() == str(home / "models") and (home / "models").is_dir()
    assert f"模型目录：{tmp_path}/data/models" in render(run("v1", model_dir=str(tmp_path / "data/models")))
    assert download_root() == str(tmp_path / "data/models")
    run("v1", model_dir="~/cache/models")
    assert download_root() == str(home / "cache/models")
    run("v1", model_dir="$HOME/models")
    assert download_root() == str(home / "models")
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    assert "模型目录不可写" in render(run("v1", model_dir=str(blocked / "models"), ok=False))
    run("v1")

    # 依赖没变：只停止并重新启动（启动时自检已通过则不再运行）；纯代码更新（版本变化）同样不重装。
    for version in ("code-1", "code-2"):
        out = run("v1", version=version)
        assert called() == [
            "worker.sh stop --force --yes",
            "worker.sh start asr --profile 5090 --yes",
        ]
        assert "[安装] 字幕识别服务已是最新，跳过安装" in render(out)

    # 加选影片修复：字幕已是当前版本，只安装影片修复；许可证码只传给影片修复安装，记录中只有指纹。
    run("v1", ("asr", "restore"), license_code="LIC-1")
    assert called() == [
        "worker.sh stop --force --yes",
        "worker.sh install restore --profile 5090 --yes license=LIC-1",
        "worker.sh start all --profile 5090 --yes",
    ]
    marker = (root / ".video-turbo-deployed-restore").read_text()
    assert marker.startswith("v1 license=") and "LIC-1" not in marker

    # 只选影片修复：已安装的字幕识别保持停止。
    run("v1", ("restore",), license_code="LIC-1")
    assert called() == [
        "worker.sh stop --force --yes",
        "worker.sh start restore --profile 5090 --yes",
    ]

    run("v1", ("restore",), license_code="LIC-2")  # 换了许可证码：重新安装以激活
    assert called()[1] == "worker.sh install restore --profile 5090 --yes license=LIC-2"

    # Jasna 有新版本：开启自动更新时重新安装影片修复，关闭时保持不动。
    outdated_flag.touch()
    run("v1", ("restore",), license_code="LIC-2", update_jasna=False)
    assert called() == [
        "worker.sh stop --force --yes",
        "worker.sh start restore --profile 5090 --yes",
    ]
    out = run("v1", ("restore",), license_code="LIC-2")
    assert called()[1] == "worker.sh install restore --profile 5090 --yes license=LIC-2"
    assert "[安装] 安装影片修复服务（Jasna 有新版本）" in render(out)
    outdated_flag.unlink()
    # 关闭自动更新时，依赖变化触发的重装也要求保留已安装的 Jasna 版本。
    env_log = Path(f"{calls}.env")
    env_log.write_text("")
    run("v1b", ("restore",), license_code="LIC-2", update_jasna=False)
    assert called()[1] == "worker.sh install restore --profile 5090 --yes license=LIC-2"
    assert env_log.read_text().splitlines() == ["update=0"]
    run("v1", ("asr", "restore"), license_code="LIC-2")

    # 依赖更新且影片修复安装失败：字幕照常启动，脚本以非零退出码结束。
    fail_flag.touch()
    out = run("v2", ("asr", "restore"), ok=False)
    assert called() == [
        "worker.sh stop --force --yes",
        "worker.sh install asr --profile 5090 --yes",
        "worker.sh install restore --profile 5090 --yes",
        "worker.sh start asr --profile 5090 --yes",
    ]
    assert "[安装] 影片修复服务安装失败，其余服务已启动" in render(out)
    assert not (root / ".video-turbo-deployed-restore").exists()

    fail_flag.unlink()
    run("v2", ("asr", "restore"))  # 失败后重新部署会重试安装
    assert called()[1:] == [
        "worker.sh install restore --profile 5090 --yes",
        "worker.sh start all --profile 5090 --yes",
    ]


def test_launch_reports_host_key_change(tmp_path: Path) -> None:
    from video_turbo.i18n import msg

    db = Database(tmp_path / "app.db")
    db.initialize()
    launch_id = db.create_cloud_launch(LaunchRequest(provider="p", method="m", target_id="t", gpu_id="g"))
    assert db.get_cloud_launch(launch_id)["host_key_changed"] is False
    changed = msg("cloud.deploy.hostKeyChanged", name=msg("cloud.deploy.hostKeyName"), host="h", port=22)
    db.update_cloud_launch(launch_id, state="deploy_failed", message=changed)
    assert db.get_cloud_launch(launch_id)["host_key_changed"] is True
    db.update_cloud_launch(launch_id, message="其他错误")
    assert db.get_cloud_launch(launch_id)["host_key_changed"] is False
