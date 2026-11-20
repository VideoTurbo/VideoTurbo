"""SSH 反向隧道与手动 SSH 平台。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import prepare_server, raises_rendered
from fastapi.testclient import TestClient
from test_cloud_launch import (
    FAKE_MACHINE,
    FakeLaunchProvider,
    _create,
    _finish,
    _instance,
    _set_status,
    launch_env,  # noqa: F401
)

from video_turbo.cloud.base import CloudError, SshInfo
from video_turbo.cloud.providers import manual_ssh
from video_turbo.cloud.providers.manual_ssh import ManualSshProvider, parse_ssh_command
from video_turbo.cloud_models import CloudBindingRequest
from video_turbo.database import Database
from video_turbo.i18n import render
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.cloud_service import CloudService
from video_turbo.orchestrator.cloud_tunnel import WORKER_SERVER_URL, TunnelError, TunnelManager
from video_turbo.plugins import registry

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# 手动 SSH 平台
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ssh -p 40022 root@gpu.example.com", ("gpu.example.com", 40022, "root")),
        ("ssh root@gpu.example.com -p40022", ("gpu.example.com", 40022, "root")),
        ("ssh -o StrictHostKeyChecking=no -p 2201 root@1.2.3.4", ("1.2.3.4", 2201, "root")),
        ("root@gpu.example.com:2222", ("gpu.example.com", 2222, "root")),
        ("ssh://root@gpu.example.com:2222", ("gpu.example.com", 2222, "root")),
        ("gpu.example.com", ("gpu.example.com", 22, "root")),
        ("ssh -l root -p 23 gpu.example.com", ("gpu.example.com", 23, "root")),
        # 非 root 账号同样可以部署（装到它的家目录）。
        ("ssh -p 2201 ubuntu@gpu.example.com", ("gpu.example.com", 2201, "ubuntu")),
    ],
)
def test_parse_ssh_command(text: str, expected: tuple[str, int, str]) -> None:
    assert parse_ssh_command(text) == expected


@pytest.mark.parametrize("text", ["", "ssh -p abc root@h", "ssh -p 99999 root@h"])
def test_parse_ssh_command_rejects(text: str) -> None:
    with pytest.raises(CloudError):
        parse_ssh_command(text)


@pytest.fixture
def ssh_probe(monkeypatch):
    state = {"online": True, "probed": []}

    async def fake_probe(ssh: SshInfo) -> str:
        state["probed"].append(ssh)
        if ssh.password != "pw":
            raise CloudError("auth", "SSH 登录被拒绝，请检查密码。")
        return "NVIDIA GeForce RTX 4090"

    async def fake_reachable(host: str, port: int) -> bool:
        return state["online"]

    monkeypatch.setattr(manual_ssh, "probe_gpu", fake_probe)
    monkeypatch.setattr(manual_ssh, "reachable", fake_reachable)
    return state


async def test_manual_ssh_machines(ssh_probe) -> None:
    form = {"ssh_command": "ssh -p 40022 root@gpu.example.com", "password": "pw"}
    with pytest.raises(CloudError):
        ManualSshProvider.merge_credentials({}, {})
    with pytest.raises(CloudError):
        ManualSshProvider.merge_credentials({}, {"ssh_command": form["ssh_command"]})
    async with ManualSshProvider(ManualSshProvider.merge_credentials({}, form)) as provider:
        assert "RTX 4090" in await provider.test()
        saved = await provider.prepare_credentials()
    assert set(saved) == {"machines"} and ManualSshProvider.credentials_configured(saved)

    # 第二台追加；同一台重新添加时留空密码沿用原值。
    second = {"ssh_command": "root@other.example.com:2201", "password": "pw"}
    async with ManualSshProvider(ManualSshProvider.merge_credentials(saved, second)) as provider:
        saved = await provider.prepare_credentials()
    again = ManualSshProvider.merge_credentials(saved, {"ssh_command": form["ssh_command"]})
    assert again["password"] == "pw"
    assert ManualSshProvider.merge_credentials(saved, {}) == saved

    async with ManualSshProvider(saved) as provider:
        instances = await provider.list_instances()
        assert [i.id for i in instances] == ["gpu.example.com-40022", "other.example.com-2201"]
        first = instances[0]
        assert first.status == "running" and first.gpu_name == "NVIDIA GeForce RTX 4090"
        assert first.ssh == SshInfo(host="gpu.example.com", port=40022, password="pw")
        assert "pw" not in json.dumps(first.model_dump())
        assert [t.id for t in await provider.launch_targets("connect")] == [i.id for i in instances]
        assert await provider.launch("connect", first.id, "existing", 1) == first.id
        ssh_probe["online"] = False
        assert (await provider.list_instances())[0].status == "stopped"
        with pytest.raises(CloudError):
            await provider.launch("connect", first.id, "existing", 1)

    saved = ManualSshProvider.forget_instance(saved, "gpu.example.com-40022")
    assert [m["id"] for m in json.loads(saved["machines"])] == ["other.example.com-2201"]
    assert ManualSshProvider.forget_instance(saved, "other.example.com-2201") is None

    wrong = ManualSshProvider.merge_credentials({}, {**form, "password": "bad"})
    async with ManualSshProvider(wrong) as provider:
        with pytest.raises(CloudError):
            await provider.prepare_credentials()


async def test_manual_ssh_structured_fields_and_edit(ssh_probe):
    merged = ManualSshProvider.merge_credentials({}, {"host": "gpu.example.com", "password": "pw"})
    async with ManualSshProvider(merged) as provider:
        saved = await provider.prepare_credentials()
    ssh = ssh_probe["probed"][-1]
    assert (ssh.host, ssh.port, ssh.username) == ("gpu.example.com", 22, "root")
    entry = json.loads(saved["machines"])[0]
    # 改地址必须重新填写密码：已保存的密码不会发往新的主机。
    moved = {"host": "other.example.com", "port": "2201", "machine_id": entry["id"]}
    with raises_rendered(CloudError, "重新填写密码"):
        ManualSshProvider.merge_credentials(saved, moved)
    kept = ManualSshProvider.merge_credentials(
        saved, {"host": "gpu.example.com", "username": "ubuntu", "machine_id": entry["id"]}
    )
    assert kept["password"] == "pw"
    changed = ManualSshProvider.merge_credentials(saved, {**moved, "password": "pw"})
    async with ManualSshProvider(changed) as provider:
        edited = await provider.prepare_credentials()
    machines = json.loads(edited["machines"])
    assert len(machines) == 1 and machines[0]["id"] == entry["id"]
    assert machines[0]["host"] == "other.example.com" and machines[0]["password"] == "pw"
    for port in ("0", "65536", "abc", "22.5"):
        with pytest.raises(CloudError):
            ManualSshProvider.merge_credentials({}, {"host": "h", "port": port, "password": "pw"})


async def test_named_profile_maintains_tunnel(launch_env):  # noqa: F811
    db, service, launcher, _ = launch_env
    db.set_cloud_profile("named", {"type": "fakelaunch", "name": "命名账号"})
    db.set_cloud_credentials("named", {"api_key": "k"})
    FakeLaunchProvider.instances = [_instance("i-1", "running")]
    db.bind_cloud_instance(FAKE_MACHINE, CloudBindingRequest(provider="named", instance_id="i-1"))
    db.set_cloud_host_key("named", "i-1", "ssh-ed25519 AAAA")
    connector = FakeConnector()
    manager = _manager(db, service, connector)
    assert manager.desired() == {("named", "i-1")}
    assert await manager.ensure("named", "i-1", timeout=2) is None
    assert connector.calls[0][0].password == "pw"
    assert launcher._method({"provider": "named", "method": "image"}).id == "image"
    await manager.close()


# ---------------------------------------------------------------------------
# 隧道管理
# ---------------------------------------------------------------------------


class FakeTunnel:
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    def close(self) -> None:
        self.closed.set()

    async def wait_closed(self) -> None:
        await self.closed.wait()


class FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[SshInfo, str, int]] = []
        self.tunnels: list[FakeTunnel] = []
        self.error: TunnelError | None = None

    async def __call__(self, ssh: SshInfo, local_host: str, local_port: int) -> FakeTunnel:
        self.calls.append((ssh, local_host, local_port))
        if self.error:
            raise self.error
        self.tunnels.append(FakeTunnel())
        return self.tunnels[-1]


def _manager(db: Database, service: CloudService, connector: FakeConnector) -> TunnelManager:
    return TunnelManager(
        service,
        db,
        local_host="127.0.0.1",
        local_port=8766,
        connector=connector,
        sync_seconds=0.01,
        retry_seconds=(0.01,),
    )


def _bind(db: Database, instance_id: str = "i-1") -> None:
    db.bind_cloud_instance(
        FAKE_MACHINE, CloudBindingRequest(provider="fakelaunch", instance_id=instance_id)
    )


async def _until(predicate) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


async def test_tunnel_follows_bindings_and_reconnects(launch_env) -> None:  # noqa: F811
    db, service, _, _ = launch_env
    FakeLaunchProvider.instances = [_instance("i-1", "running")]
    connector = FakeConnector()
    manager = _manager(db, service, connector)
    key = ("fakelaunch", "i-1")

    _bind(db)
    assert manager.desired() == set()  # 没有记录过主机密钥：不是主控部署的实例，不连接
    assert await manager.ensure(*key, timeout=0.05)
    db.set_cloud_host_key("fakelaunch", "i-1", "ssh-ed25519 AAAA")
    assert manager.desired() == {key}

    assert await manager.ensure(*key, timeout=2) is None
    ssh, host, port = connector.calls[0]
    assert (host, port) == ("127.0.0.1", 8766)
    assert ssh.host_key == "ssh-ed25519 AAAA" and ssh.password == "pw"
    assert manager.error(*key) is None

    connector.tunnels[0].close()  # 连接断开后自动重连
    await _until(lambda: len(connector.tunnels) == 2 and manager.error(*key) is None)

    db.unbind_cloud_instance(FAKE_MACHINE)  # 解除绑定后关闭隧道
    manager.sync()
    await _until(lambda: connector.tunnels[1].closed.is_set())
    assert key not in manager.tasks and manager.error(*key) is None
    await manager.close()


async def test_tunnel_failure_is_reported_once(launch_env) -> None:  # noqa: F811
    db, service, _, _ = launch_env
    FakeLaunchProvider.instances = [_instance("i-1", "running")]
    connector = FakeConnector()
    connector.error = TunnelError("实例拒绝了端口转发")
    manager = _manager(db, service, connector)
    _bind(db)
    db.set_cloud_host_key("fakelaunch", "i-1", "ssh-ed25519 AAAA")

    assert "端口转发" in render(await manager.ensure("fakelaunch", "i-1", timeout=0.2))
    await _until(lambda: len(connector.calls) >= 6)
    notices = [n for n in db.list_notices() if render(n["title"]) == "云主机连接中断"]
    assert len(notices) == 1 and FAKE_MACHINE in notices[0]["message"]

    connector.error = None  # 恢复后提醒一次
    await _until(lambda: manager.error("fakelaunch", "i-1") is None)
    assert [render(n["title"]) for n in db.list_notices()].count("云主机连接已恢复") == 1

    # 实例关机属于正常情况：不连接，也不提醒。
    _set_status("i-1", "stopped")
    service.invalidate("fakelaunch")
    calls = len(connector.calls)
    connector.tunnels[-1].close()
    await _until(lambda: "未在运行" in render(manager.error("fakelaunch", "i-1") or ""))
    await asyncio.sleep(0.05)
    assert len(connector.calls) == calls
    assert [render(n["title"]) for n in db.list_notices()].count("云主机连接中断") == 1
    await manager.close()


async def test_deploy_waits_for_tunnel(launch_env) -> None:  # noqa: F811
    db, service, launcher, deployed = launch_env
    connector = FakeConnector()
    launcher.tunnels = _manager(db, service, connector)
    launcher.identifier = _identifier_recording_key
    launch_id = _create(db)
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "running", launch["message"]
    assert deployed[0]["settings"]["server_url"] == WORKER_SERVER_URL == "http://127.0.0.1:8766"
    assert "建立和主程序的连接" in render(launch["log"])
    assert len(connector.tunnels) == 1  # 隧道在部署前已连通
    await launcher.tunnels.close()


async def test_deploy_fails_without_tunnel(launch_env, monkeypatch) -> None:  # noqa: F811
    from video_turbo.orchestrator import cloud_tunnel

    monkeypatch.setattr(cloud_tunnel, "ENSURE_TIMEOUT_SECONDS", 0.2)
    db, service, launcher, deployed = launch_env
    connector = FakeConnector()
    connector.error = TunnelError("实例拒绝了端口转发：SSH 服务可能禁用了 AllowTcpForwarding")
    manager = _manager(db, service, connector)
    launcher.tunnels = SimpleNamespace(
        ensure=lambda provider, instance: manager.ensure(provider, instance, timeout=0.2),
        error=manager.error,
    )
    launcher.identifier = _identifier_recording_key
    launch_id = _create(db)
    await _finish(launcher, launch_id, after_grab=lambda: _set_status("new-a1b2", "running"))
    launch = db.get_cloud_launch(launch_id)
    assert launch["state"] == "deploy_failed" and "AllowTcpForwarding" in launch["message"]
    assert deployed == []
    await manager.close()


async def _identifier_recording_key(ssh: SshInfo, log) -> str:
    ssh.host_key = "ssh-ed25519 AAAA"  # 真实部署在首次连接时记录主机密钥
    return FAKE_MACHINE


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch, ssh_probe):
    prepare_server(tmp_path, monkeypatch, db_name="api.db")
    with TestClient(app) as test_client:
        app.state.database.set_cloud_profile("ssh", {"type": "ssh"})
        yield test_client


def test_manual_ssh_api(client: TestClient, monkeypatch) -> None:
    db: Database = app.state.database
    assert registry.get("cloud", "ssh") is ManualSshProvider
    form = {"values": {"ssh_command": "ssh -p 40022 root@gpu.example.com", "password": "pw"}}
    bad = {"values": {**form["values"], "password": "nope"}}
    assert client.put("/api/cloud/providers/ssh/credentials", json=bad).status_code == 400
    assert client.post("/api/cloud/providers/ssh/test", json=form).json()["ok"] is True
    assert client.put("/api/cloud/providers/ssh/credentials", json=form).status_code == 200

    described = next(p for p in client.get("/api/cloud/providers").json() if p["id"] == "ssh")
    assert described["configured"] is True and described["credential_mode"] == "machines"
    assert "pw" not in json.dumps(described)
    group = next(g for g in client.get("/api/cloud/instances").json() if g["provider"] == "ssh")
    instance_id = group["instances"][0]["id"]
    assert instance_id == "gpu.example.com-40022" and "forget" in group["capabilities"]
    options = client.get("/api/cloud/providers/ssh/launch-options").json()
    assert [m["id"] for m in options["methods"]] == ["connect"]
    assert options["methods"][0]["configurable_gpu"] is False

    # 平台不能关机：创建任务和绑定都强制为不自动关机。
    started: list[str] = []
    monkeypatch.setattr(app.state.cloud_launcher, "start", started.append)
    created = client.post(
        "/api/cloud/launches",
        json={
            "provider": "ssh",
            "method": "connect",
            "target_id": instance_id,
            "gpu_id": "existing",
            "services": ["asr"],
            "shutdown_mode": "always",
        },
    ).json()
    assert created["shutdown_mode"] == "off" and started == [created["id"]]
    bound = client.put(
        f"/api/cloud/bindings/{FAKE_MACHINE}",
        json={"provider": "ssh", "instance_id": instance_id, "shutdown_mode": "always"},
    ).json()
    assert bound["shutdown_mode"] == "off"
    response = client.put(
        f"/api/cloud/bindings/{FAKE_MACHINE}/shutdown-mode", json={"shutdown_mode": "always"}
    )
    assert response.status_code == 400 and "不支持自动关机" in render(response.json()["detail"])
    view = client.get("/api/status").json()["cloud"]["bindings"][FAKE_MACHINE]
    assert view["can_shutdown"] is False and view["tunnel_error"] is None

    db.set_cloud_host_key("ssh", instance_id, "ssh-ed25519 AAAA")
    db.set_cloud_profile("orphan", {"type": "not-installed"})
    assert client.post("/api/cloud/instances/orphan/x/forget").status_code == 404
    assert client.post(f"/api/cloud/instances/ssh/{instance_id}/forget").json() == {"removed": True}
    assert db.cloud_credentials("ssh") == {}
    assert (
        db.get_cloud_binding(FAKE_MACHINE) is None and db.cloud_host_key("ssh", instance_id) is None
    )


# ---------------------------------------------------------------------------
# 真实 SSH 连接：本机起一个 SSH 服务端，验证反向转发确实把请求送回主控端口
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_open_tunnel_forwards_to_local_port(monkeypatch) -> None:
    import asyncssh

    from video_turbo.orchestrator import cloud_tunnel

    class Server(asyncssh.SSHServer):
        allow_forwarding = True

        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return password == "pw"

        def server_requested(self, listen_host: str, listen_port: int) -> bool:
            return type(self).allow_forwarding

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"master:" + await reader.readline())
        await writer.drain()
        writer.close()

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    public = host_key.export_public_key().decode().strip()
    remote_port = _free_port()
    monkeypatch.setattr(cloud_tunnel, "TUNNEL_REMOTE_PORT", remote_port)
    master = await asyncio.start_server(echo, "127.0.0.1", 0)
    sshd = await asyncssh.listen("127.0.0.1", 0, server_factory=Server, server_host_keys=[host_key])
    ssh = SshInfo(host="127.0.0.1", port=sshd.get_port(), password="pw", host_key=public)
    try:
        tunnel = await cloud_tunnel.open_tunnel(
            ssh, "127.0.0.1", master.sockets[0].getsockname()[1]
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", remote_port)
        writer.write(b"hello\n")
        assert await asyncio.wait_for(reader.readline(), 5) == b"master:hello\n"
        writer.close()
        tunnel.close()
        await asyncio.wait_for(tunnel.wait_closed(), 5)

        with pytest.raises(TunnelError) as denied:  # 密码错误：不重试
            await cloud_tunnel.open_tunnel(
                ssh.model_copy(update={"password": "bad"}), "127.0.0.1", 1
            )
        assert denied.value.fatal
        other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
        with pytest.raises(TunnelError) as mismatch:  # 主机密钥不一致：拒绝连接
            await cloud_tunnel.open_tunnel(
                ssh.model_copy(update={"host_key": other}), "127.0.0.1", 1
            )
        assert mismatch.value.fatal and "主机密钥" in render(str(mismatch.value))
        with pytest.raises(TunnelError) as unknown:  # 没有记录主机密钥：不做首次信任
            await cloud_tunnel.open_tunnel(
                ssh.model_copy(update={"host_key": None}), "127.0.0.1", 1
            )
        assert unknown.value.fatal
        Server.allow_forwarding = False  # 服务端禁用端口转发
        with pytest.raises(TunnelError) as refused:
            await cloud_tunnel.open_tunnel(ssh, "127.0.0.1", 1)
        assert "AllowTcpForwarding" in render(str(refused.value)) and not refused.value.fatal
    finally:
        sshd.close()
        master.close()
