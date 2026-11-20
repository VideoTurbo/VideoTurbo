"""deploy/worker/linux/worker.sh：单一脚本承担安装、启动、停止、清理、状态（参数式运行）。"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from video_turbo.i18n import render
from video_turbo.worker.config import WorkerConfig

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = "deploy/worker/linux/worker.sh"


def wait_for(predicate, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


# 测的是 Linux 显卡电脑的安装脚本。
pytestmark = pytest.mark.skipif(os.name == "nt", reason="测的是 Linux 显卡电脑的安装脚本")

FAKE_VENV_PYTHON = '''import json, os, signal, sys, time
from pathlib import Path

root = Path(__file__).parent
args = sys.argv[1:]
if args and args[0] in ("-c", "-"):
    if args[0] == "-c" and "nvidia_runtime_library_paths" in args[1]:
        print("/test/cuda")
        sys.exit(0)
    os.execv(sys.executable, [sys.executable, *args])
if args[:2] == ["-m", "pip"]:
    with open(root / "pip.log", "a") as log:
        log.write(" ".join(args[2:]) + "\\n")
    sys.exit(0)
if args and args[0].endswith("cleanup_worker.py"):
    with open(root / "cleanup.log", "a") as log:
        log.write(" ".join(args[1:]) + "\\n")
    print("[字幕任务临时目录] 1 项，共 1.0 MB")
    print("合计 " + os.environ.get("TEST_CLEAN_TOTAL", "1.0 MB") + "。")
    sys.exit(0)
if args[:2] == ["-m", "video_turbo.worker.main"]:
    instance = os.environ.get("VIDEO_TURBO_WORKER_INSTANCE", "foreground")
    if "--self-test" in args:
        with open(root / "self-test.log", "a") as log:
            log.write(" ".join(args[2:]) + "\\n")
        role = "restore" if "--restore" in args else "asr"
        sys.exit(1 if os.environ.get("TEST_FAIL_SELF_TEST") in ("1", role) else 0)
    capture = root / (instance + ".json")
    capture.write_text(json.dumps({
        "args": args[2:], "pid": os.getpid(), "library_path": os.environ.get("LD_LIBRARY_PATH"),
        "suffix": os.environ.get("VIDEO_TURBO_WORKER_SUFFIX"), "count": os.environ.get("VIDEO_TURBO_WORKER_COUNT"),
        "role": os.environ.get("VIDEO_TURBO_WORKER_ROLE"),
    }))
    def stop(*_):
        time.sleep(0.1)
        capture.with_suffix(".stopped").touch()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(0.05)
sys.exit(0)
'''


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "project"
    for name in (
        SCRIPT,
        "pyproject.toml",
        "deploy/worker/requirements/asr.txt",
        "deploy/worker/requirements/restore.txt",
        "src/video_turbo/__init__.py",
        "src/video_turbo/worker/whisperjav.py",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT / name, target)
    (root / "config").mkdir()
    (root / "config/worker.yaml").write_text("name: cloud5090\nworker_token: secret\n")
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    fake = python.parent / "fake_python.py"
    fake.write_text(FAKE_VENV_PYTHON)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    nvidia = bin_dir / "nvidia-smi"
    nvidia.write_text('#!/bin/sh\necho "NVIDIA GeForce RTX 5090, 610.43.02, 32607"\n')
    nvidia.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("VIDEO_TURBO_")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["VIDEO_TURBO_STOP_TIMEOUT"] = "5"
    # 脚本按语言输出；断言按中文写。
    env["VIDEO_TURBO_LANGUAGE"] = "zh-Hans"

    def run(*args, **overrides):
        return subprocess.run(
            ["bash", str(root / SCRIPT), *args], env={**env, **overrides}, cwd=root,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
        )

    yield root, run
    run("stop", "--force")


def mark_asr_installed(root: Path) -> None:
    (root / ".venv/video-turbo-asr.state").write_text("version=0.0.1\ndeps=x\n")


def mark_restore_installed(root: Path) -> None:
    jasna = root / "jasna/jasna-linux-nvidia-v1/jasna"
    jasna.parent.mkdir(parents=True)
    jasna.write_text("#!/bin/sh\n")
    jasna.chmod(0o755)
    (root / "jasna/current").symlink_to(jasna.parent)
    (root / "jasna/installed-tag").write_text("v1\n")
    config = root / "config/worker.yaml"
    config.write_text(config.read_text() + "jasna:\n  enabled: true\n")


def capture(root: Path, instance: str) -> dict:
    path = root / ".venv/bin" / f"{instance}.json"
    wait_for(path.exists)
    return json.loads(path.read_text())


def test_help_and_argument_errors(deployment):
    _, run = deployment
    result = run("help")
    assert result.returncode == 0
    assert "install asr|restore|all" in result.stdout and "start" in result.stdout
    assert run().returncode == 2  # 非交互终端不进入菜单，只提示用法
    assert run("bogus").returncode == 2
    assert run("start", "--bogus").returncode == 2
    assert run("install").returncode == 2


def test_start_requires_installation(deployment):
    _, run = deployment
    result = run("start")
    assert result.returncode == 1
    assert "还没有安装任何服务" in render(result.stderr)
    assert "字幕识别服务未安装" in render(run("start", "asr").stderr)
    assert "影片修复服务未安装" in render(run("start", "restore").stderr)


def test_asr_start_runs_self_test_once_and_manages_instances(deployment):
    root, run = deployment
    mark_asr_installed(root)
    result = run("start", "asr", "--workers", "2")
    assert result.returncode == 0, result.stderr
    assert (root / ".venv/bin/self-test.log").read_text().splitlines() == [
        f"--config {root}/config/worker.yaml --self-test"
    ]
    for index in (1, 2):
        worker = capture(root, f"5090-0{index}")
        assert worker["args"][:2] == ["--role", "asr"]
        assert worker["suffix"] == f"0{index}"  # 名称 = 硬件机器标识 + 后缀，由 Worker 自己拼接
        assert worker["count"] == "2"
        assert worker["library_path"].startswith("/test/cuda")
        assert (root / f"logs/worker.5090-0{index}.log").is_file()
    status = run("status")
    assert "字幕识别服务：2 路运行中" in render(status.stdout)
    assert "5090-01：运行中" in render(status.stdout) and "5090-02：运行中" in render(status.stdout)

    again = run("start", "asr", "--workers", "2")
    assert "字幕识别 01 已经在运行" in render(again.stdout)
    # 自检只在首次（或安装变化后）运行。
    assert len((root / ".venv/bin/self-test.log").read_text().splitlines()) == 1

    # 改变路数会重启全部字幕实例，使每个实例拿到新的并行数。
    resized = run("start", "asr", "--workers", "3")
    assert resized.returncode == 0, resized.stderr
    assert (root / ".venv/bin/5090-01.stopped").exists()
    wait_for(lambda: json.loads((root / ".venv/bin/5090-03.json").read_text())["count"] == "3")

    stopped = run("stop", "asr")
    assert stopped.returncode == 0, stopped.stderr
    assert not list((root / "work").glob("worker.*.pid"))
    assert (root / ".venv/bin/5090-03.stopped").exists()


def test_failed_self_test_does_not_start(deployment):
    root, run = deployment
    mark_asr_installed(root)
    result = run("start", "asr", TEST_FAIL_SELF_TEST="1")
    assert result.returncode != 0
    assert not list((root / "work").glob("worker.*.pid"))
    assert not (root / "work/.state/self-test-asr").exists()
    skipped = run("start", "asr", "--skip-self-test")
    assert skipped.returncode == 0, skipped.stderr
    assert "已跳过字幕识别服务首次自检" in render(skipped.stderr)


@pytest.mark.parametrize("service", ["all", None])
@pytest.mark.parametrize("failed_role", ["asr", "restore"])
def test_start_all_propagates_self_test_failure_and_retries(deployment, service, failed_role):
    root, run = deployment
    mark_asr_installed(root)
    mark_restore_installed(root)
    args = ("start", service) if service else ("start",)
    result = run(*args, TEST_FAIL_SELF_TEST=failed_role)
    assert result.returncode != 0, result.stderr
    failed_suffix = "01" if failed_role == "asr" else "dc"
    assert not (root / f"work/worker.5090-{failed_suffix}.pid").exists()
    assert not (root / f".venv/bin/5090-{failed_suffix}.json").exists()
    assert not (root / f"work/.state/self-test-{failed_role}").exists()
    label = "字幕识别服务" if failed_role == "asr" else "影片修复服务"
    assert f"[{label}自检] 自检通过。" not in render(result.stderr)
    assert "自检失败" in render(result.stderr)
    # The independent service can still start; the combined command must fail.
    good_suffix = "dc" if failed_role == "asr" else "01"
    assert capture(root, f"5090-{good_suffix}")
    assert len((root / ".venv/bin/self-test.log").read_text().splitlines()) == 2

    retried = run("start", failed_role, "--workers", "1")
    assert retried.returncode == 0, retried.stderr
    assert capture(root, f"5090-{failed_suffix}")
    assert (root / f"work/.state/self-test-{failed_role}").exists()
    assert len((root / ".venv/bin/self-test.log").read_text().splitlines()) == 3


@pytest.mark.parametrize("role", ["asr", "restore"])
def test_failed_forced_self_test_removes_previous_success_marker(deployment, role):
    root, run = deployment
    mark_asr_installed(root)
    mark_restore_installed(root)
    assert run("self-test", role).returncode == 0
    marker = root / f"work/.state/self-test-{role}"
    assert marker.exists()
    failed = run("self-test", role, TEST_FAIL_SELF_TEST="1")
    assert failed.returncode != 0
    assert not marker.exists()
    assert "自检通过" not in render(failed.stderr)
    assert run("start", role, "--workers", "1", TEST_FAIL_SELF_TEST="1").returncode != 0
    assert len((root / ".venv/bin/self-test.log").read_text().splitlines()) == 3


@pytest.mark.parametrize(("profile", "count"), [("5090", "5"), ("4090", "4"), ("5090", "0"), ("5090", "two")])
def test_invalid_asr_concurrency(deployment, profile, count):
    root, run = deployment
    mark_asr_installed(root)
    result = run("start", "asr", "--profile", profile, "--workers", count)
    assert result.returncode == 2
    assert "字幕并行数" in render(result.stderr)


def test_restore_runs_as_separate_dc_instance(deployment):
    root, run = deployment
    mark_asr_installed(root)
    mark_restore_installed(root)
    result = run("start")  # 不指定服务：启动所有已安装的服务
    assert result.returncode == 0, result.stderr
    restore = capture(root, "5090-dc")
    assert restore["args"][:2] == ["--role", "restore"]
    assert restore["suffix"] == "dc"
    assert restore["role"] == "restore"
    assert [capture(root, f"5090-0{i}")["args"][:2] for i in (1, 2, 3)] == [["--role", "asr"]] * 3
    assert "--self-test --restore" in (root / ".venv/bin/self-test.log").read_text()

    assert run("stop", "restore").returncode == 0
    assert (root / ".venv/bin/5090-dc.stopped").exists()
    assert not (root / "work/worker.5090-dc.pid").exists()
    assert (root / "work/worker.5090-01.pid").exists()  # 只停影片修复
    assert "影片修复服务：0 路运行中" in render(run("status").stdout)


def test_stop_handles_invalid_pid_files(deployment):
    root, run = deployment
    (root / "work").mkdir()
    (root / "work/worker.5090-01.pid").write_text("abc\n")
    result = run("stop")
    assert "PID 文件无效" in render(result.stderr)
    assert not (root / "work/worker.5090-01.pid").exists()


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="需要 /proc 校验进程命令行")
def test_stop_refuses_unrelated_process(deployment):
    root, run = deployment
    process = subprocess.Popen(["sleep", "30"])
    try:
        (root / "work").mkdir()
        (root / "work/worker.5090-01.pid").write_text(f"{process.pid}\n")
        result = run("stop")
        assert "没有停止" in render(result.stderr)
        assert process.poll() is None
    finally:
        process.kill()
        (root / "work/worker.5090-01.pid").unlink()


def test_clean_lists_by_default_and_stops_services_with_yes(deployment):
    root, run = deployment
    mark_asr_installed(root)
    assert run("start", "asr", "--workers", "1", "--skip-self-test").returncode == 0
    listed = run("clean")
    assert "风险提示" in render(listed.stdout) and "强制停止" in render(listed.stdout)
    assert "已取消清理" in render(listed.stdout)
    assert (root / "work/worker.5090-01.pid").exists()
    assert (root / ".venv/bin/cleanup.log").read_text().splitlines() == [f"--config {root}/config/worker.yaml"]

    cleaned = run("clean", "--yes", "--skip", "pip")
    assert cleaned.returncode == 0, cleaned.stderr
    assert (root / ".venv/bin/5090-01.stopped").exists()
    assert not (root / "work/worker.5090-01.pid").exists()
    assert (root / ".venv/bin/cleanup.log").read_text().splitlines()[-1] == (
        f"--config {root}/config/worker.yaml --skip pip --yes"
    )


def test_clean_with_nothing_to_remove(deployment):
    root, run = deployment
    result = run("clean", "--yes", TEST_CLEAN_TOTAL="0 B")
    assert result.returncode == 0
    assert "没有需要清理的内容" in render(result.stdout)
    assert len((root / ".venv/bin/cleanup.log").read_text().splitlines()) == 1


def test_status_reports_install_versions(deployment):
    root, run = deployment
    result = run("status")
    assert "字幕识别：未安装" in render(result.stdout) and "影片修复：未安装" in render(result.stdout)
    (root / ".venv/video-turbo-asr.state").write_text("version=0.0.1\ndeps=old\n")
    assert "已安装 v0.0.1" in render(run("status").stdout)
    assert "可更新" in render(run("status").stdout)


# ------------------------------------------------------------------ 安装


def install_sandbox(root: Path, bin_dir: Path, *, model_dir: Path) -> None:
    for name in (
        "tools/install_whisperjav.py", "tools/configure_worker.py", "tools/configure_jasna.py",
        "tools/download_jasna.py", "tools/select_sources.py",
        "config/worker.example.yaml", "src/video_turbo/__init__.py",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT / name, target)
    (root / "config/worker.yaml").unlink()


def system_python(bin_dir: Path, *, libcxx: bool = True, ensurepip: bool = True) -> Path:
    failing = "|".join(name for name, ok in (("*ctypes*", libcxx), ("*ensurepip*", ensurepip)) if not ok)
    path = bin_dir / "python3"
    path.write_text(f"""#!/bin/sh
case "$1" in */configure_worker.py|*/configure_jasna.py) exec "{sys.executable}" "$@" ;; esac
case "$2" in
  {failing or "__never__"}) exit 1 ;;
  *'print("%d.%d"'*) echo 3.12 ;;
esac
exit 0
""")
    path.chmod(0o755)
    return path


def bare_bin(tmp_path: Path) -> Path:
    """只有基础命令的 PATH：git、ffmpeg、libc++、venv 都缺失。"""
    bin_dir = tmp_path / "bare-bin"
    bin_dir.mkdir()
    for tool in ("bash", "env", "dirname", "basename", "cp", "mkdir", "cat", "sed", "head", "tail", "tr",
                 "date", "mv", "cut", "grep", "seq", "sleep", "ls", "sort", "rm", "chmod", "readlink",
                 "awk", "df", "sha256sum", "shasum", "perl", "touch", "wc"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    for name, body in (
        ("nvidia-smi", "echo 'NVIDIA GeForce RTX 5090, 610.43.02, 32607'\n"),
        ("id", "echo 0\n"),
        ("apt-get", f'echo "$*" >> "{tmp_path}/apt.log"\n'),
    ):
        (bin_dir / name).write_text("#!/bin/sh\n" + body)
        (bin_dir / name).chmod(0o755)
    return bin_dir


def run_install(root: Path, bin_dir: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(bin_dir / "bash"), str(root / SCRIPT), "install", *args],
        env={"PATH": str(bin_dir), "VIDEO_TURBO_PYTHON": str(bin_dir / "python3"), "HOME": str(root),
             "VIDEO_TURBO_DEFAULT_MODEL_DIR": str(root.parent / "models"), "VIDEO_TURBO_LANGUAGE": "zh-Hans", **extra},
        cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
    )


def test_install_asr_installs_missing_system_packages_with_apt(deployment, tmp_path):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir, libcxx=False, ensurepip=False)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    result = run_install(root, bin_dir, "asr", "--yes")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "apt.log").read_text().splitlines() == [
        "update",
        "install -y --no-install-recommends git ffmpeg libc++1",
        "install -y --no-install-recommends python3.12-venv",
    ]
    assert "缺少系统组件：git ffmpeg libc++1 python3.12-venv" in render(result.stderr)
    assert "NVENC" not in result.stderr  # 字幕安装不再检查影片修复用的编码器
    pip = (root / ".venv/bin/pip.log").read_text()
    assert f"--require-hashes -r {root}/deploy/worker/requirements/asr.txt" in pip
    assert f"--no-deps --editable {root}" in pip and "[worker]" not in pip
    state = (root / ".venv/video-turbo-asr.state").read_text()
    assert "version=" in state and "deps=" in state
    assert (tmp_path / "models").is_dir()
    assert "start asr" in result.stdout


def test_install_skip_apt_reports_missing_packages(deployment, tmp_path):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir, libcxx=False)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    result = run_install(root, bin_dir, "asr", "--yes", VIDEO_TURBO_SKIP_APT="1")
    assert result.returncode == 1
    assert not (tmp_path / "apt.log").exists()
    assert "apt-get install -y git ffmpeg libc++1" in result.stderr


def non_root(bin_dir: Path, *, sudo: bool) -> None:
    """把 id 桩改成普通用户；sudo=True 时提供免密 sudo（记录调用后照常执行）。"""
    (bin_dir / "id").write_text("#!/bin/sh\necho 1000\n")
    if sudo:
        path = bin_dir / "sudo"
        path.write_text(
            '#!/bin/sh\n[ "$1" = -n ] || exit 1\nshift\n[ "$1" != true ] || exit 0\n'
            'case "$1" in *=*) shift ;; esac\nexec "$@"\n'
        )
        path.chmod(0o755)


def test_install_as_non_root_uses_passwordless_sudo(deployment, tmp_path):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir, libcxx=False)
    non_root(bin_dir, sudo=True)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    result = run_install(root, bin_dir, "asr", "--yes")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "apt.log").read_text().splitlines()[:2] == [
        "update",
        "install -y --no-install-recommends git ffmpeg libc++1",
    ]


def test_install_as_non_root_without_sudo_lists_packages(deployment, tmp_path):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir, libcxx=False)
    non_root(bin_dir, sudo=False)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    result = run_install(root, bin_dir, "asr", "--yes")
    assert result.returncode == 1
    assert not (tmp_path / "apt.log").exists()
    assert "不能免密码使用 sudo" in render(result.stderr)
    assert "sudo apt-get install -y git ffmpeg libc++1" in result.stderr


def test_default_model_dir_is_under_home(deployment, tmp_path):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    home = tmp_path / "home"
    home.mkdir()
    result = run_install(
        root, bin_dir, "asr", "--yes", HOME=str(home), VIDEO_TURBO_DEFAULT_MODEL_DIR="",
    )
    assert result.returncode == 0, result.stderr
    assert f"download_root: \"{home}/models\"" in (root / "config/worker.yaml").read_text(), result.stderr


@pytest.mark.parametrize("preserve_config", [False, True])
def test_install_selects_profile_template_and_preserves_config(deployment, tmp_path, preserve_config):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir)
    for tool in ("git", "ffmpeg", "ffprobe"):
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / tool).chmod(0o755)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    config = root / "config/worker.yaml"
    if preserve_config:
        config.write_text("name: cloud5090-existing\nworker_token: keep-this-token\n")
    before = config.read_text() if preserve_config else (
        (root / "config/worker.example.yaml").read_text()
        .replace("download_root: ../models", f'download_root: "{tmp_path / "models"}"')
    )
    result = run_install(root, bin_dir, "asr")
    assert result.returncode == 0, result.stderr
    assert config.read_text() == before
    assert not (tmp_path / "apt.log").exists()
    assert "[5090安装]" in render(result.stderr)
    assert "immediate_worker_prefixes" not in result.stdout


@pytest.mark.parametrize("missing", ["src/video_turbo/__init__.py", "config/worker.example.yaml"])
def test_install_rejects_incomplete_upload(deployment, tmp_path, missing):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    (root / missing).unlink()
    result = run_install(root, bin_dir, "asr", "--yes")
    assert result.returncode == 1
    assert f"缺少 {root}/{missing}" in render(result.stderr)


NVENC_ENCODERS = 'if [ "$2" = "-encoders" ]; then echo " V....D hevc_nvenc  NVIDIA NVENC hevc encoder"; exit 0; fi\n'


def run_restore_install(deployment, tmp_path, ffmpeg: str | None):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    system_python(bin_dir)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    calls = tmp_path / "curl-called"
    for name, body in (
        ("nvidia-smi", 'case "$*" in *name*) echo "NVIDIA GeForce RTX 5090, 610.43.02, 32607" ;; *) echo "610.43.02" ;; esac\n'),
        ("curl", f'touch "{calls}"; exit 22\n'),  # 模拟无法访问 GitHub，脚本随后退出
        ("wget", "exit 0\n"),
        ("zstd", "exit 0\n"),
    ):
        (bin_dir / name).write_text("#!/bin/sh\n" + body)
        (bin_dir / name).chmod(0o755)
    if ffmpeg is not None:
        (bin_dir / "ffmpeg").write_text("#!/bin/sh\n" + ffmpeg)
        (bin_dir / "ffmpeg").chmod(0o755)
    result = run_install(root, bin_dir, "restore", "--yes", VIDEO_TURBO_SKIP_APT="1")
    return result, calls, root


def test_restore_precheck_stops_before_download_when_nvenc_fails(deployment, tmp_path):
    result, calls, _ = run_restore_install(
        deployment, tmp_path, NVENC_ENCODERS + 'echo "OpenEncodeSessionEx failed: unsupported device (2)" >&2; exit 1\n'
    )
    assert result.returncode == 1
    assert "未下载 Jasna" in render(result.stderr) and "unsupported device" in result.stderr
    assert not calls.exists()


def test_restore_install_skips_asr_dependencies(deployment, tmp_path):
    result, calls, root = run_restore_install(deployment, tmp_path, NVENC_ENCODERS + "exit 0\n")
    assert calls.exists()
    assert "无法访问 GitHub API" in render(result.stderr)
    pip = (root / ".venv/bin/pip.log").read_text()
    assert f"--require-hashes -r {root}/deploy/worker/requirements/restore.txt" in pip
    assert "asr.txt" not in pip and "[worker]" not in pip


@pytest.mark.parametrize("download_ok", [True, False])
def test_restore_only_extracts_validated_download_list(deployment, tmp_path, download_ok):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    python = system_python(bin_dir)
    install_sandbox(root, bin_dir, model_dir=tmp_path / "models")
    # 验证 worker.sh 调用下载器、传入原始用户配置，以及失败时不进入解压阶段。
    python.write_text(python.read_text().replace(
        "*/configure_worker.py|*/configure_jasna.py)",
        "*/configure_worker.py|*/configure_jasna.py|*/download_jasna.py)",
    ))
    (bin_dir / "ln").symlink_to(shutil.which("ln"))
    for name, body in (
        ("nvidia-smi", 'case "$*" in *name*) echo "NVIDIA GeForce RTX 5090, 610.43.02, 32607" ;; *) echo "610.43.02" ;; esac\n'),
        ("curl", "echo '{\"tag_name\": \"v1\", \"assets\": []}'\n"),
        ("wget", "exit 0\n"),
        ("ffmpeg", NVENC_ENCODERS + "exit 0\n"),
        ("zstd", "cat\n"),
    ):
        (bin_dir / name).write_text("#!/bin/sh\n" + body)
        (bin_dir / name).chmod(0o755)
    helper = root / "tools/download_jasna.py"
    helper.write_text(f'''import json, sys
from pathlib import Path
assert json.load(sys.stdin)["tag_name"] == "v1"
assert sys.argv[-2:] == ["--proxy", "direct"]
directory = Path(sys.argv[sys.argv.index("--output-dir") + 1])
directory.mkdir(parents=True, exist_ok=True)
part = directory / "jasna-linux-nvidia-1.tar.zst.part000"
part.write_bytes(b"validated bytes")
(directory / (part.name + ".aria2")).write_bytes(b"must not be extracted")
print(part)
sys.exit({0 if download_ok else 1})
''')
    tar = bin_dir / "tar"
    executable = "#!/bin/sh\n" + NVENC_ENCODERS + "exit 0\n"
    tar.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
Path({str(tmp_path / "extracted")!r}).write_bytes(sys.stdin.buffer.read())
directory = Path(sys.argv[sys.argv.index("-C") + 1]) / "jasna-linux-nvidia-1"
(directory / "tools").mkdir(parents=True)
for name in ("jasna", "tools/ffmpeg"):
    p = directory / name
    p.write_text({executable!r})
    p.chmod(0o755)
''')
    tar.chmod(0o755)
    result = run_install(root, bin_dir, "restore", "--yes", VIDEO_TURBO_SKIP_APT="1",
                         VIDEO_TURBO_GITHUB_PROXY="direct")
    if download_ok:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "extracted").read_bytes() == b"validated bytes"
        assert (root / "jasna/installed-tag").read_text().strip() == "v1"
    else:
        assert result.returncode == 1
        assert "Jasna 下载或完整性检查失败" in render(result.stderr)
        assert not (tmp_path / "extracted").exists()


@pytest.mark.parametrize("ffmpeg", [None, 'echo " V....D libx264"; exit 0\n'])
def test_restore_precheck_skipped_without_system_nvenc(deployment, tmp_path, ffmpeg):
    result, calls, _ = run_restore_install(deployment, tmp_path, ffmpeg)
    assert "跳过下载前的编码器检查" in render(result.stderr)
    assert calls.exists()


# ------------------------------------------------------------------ 配置模板


@pytest.mark.parametrize("mode", [
    "pass", "encode-fails", "missing", "no-encoder", "apt-fails", "apt-no-encoder", "apt-disabled",
])
def test_cloud_restore_precheck_before_worker_dependencies(deployment, tmp_path, mode):
    root, _ = deployment
    bin_dir = bare_bin(tmp_path)
    # 隔离系统路径，测试不依赖宿主机是否安装 ffmpeg，也不调用真实 apt。
    system_ffmpeg = bin_dir / "system-ffmpeg"
    script = root / SCRIPT
    script.write_text(script.read_text().replace("/usr/bin/ffmpeg", str(system_ffmpeg)))
    ffmpeg = bin_dir / "ffmpeg"
    if mode not in {"missing", "apt-fails", "apt-no-encoder", "apt-disabled"}:
        ffmpeg.write_text("#!/bin/sh\n" + (
            'echo "libx264"; exit 0\n' if mode == "no-encoder" else
            NVENC_ENCODERS + ('echo "unsupported device" >&2; exit 1\n' if mode == "encode-fails" else "exit 0\n")
        ))
        ffmpeg.chmod(0o755)
    apt = bin_dir / "apt-get"
    apt.write_text(
        f'#!/bin/sh\necho "$*" >> "{tmp_path}/apt.log"\n'
        + ("exit 1\n" if mode == "apt-fails" else
           f'if [ "$1" = install ]; then cp "{tmp_path}/new-ffmpeg" "{system_ffmpeg}"; fi\n')
    )
    new_ffmpeg = tmp_path / "new-ffmpeg"
    new_ffmpeg.write_text("#!/bin/sh\n" + (
        'echo "libx264"; exit 0\n' if mode == "apt-no-encoder" else NVENC_ENCODERS + "exit 0\n"
    ))
    new_ffmpeg.chmod(0o755)
    result = subprocess.run(
        [str(bin_dir / "bash"), str(script), "__restore_precheck"],
        env={"PATH": str(bin_dir), "VIDEO_TURBO_LANGUAGE": "zh-Hans", "VIDEO_TURBO_SKIP_APT": "1" if mode == "apt-disabled" else "0"},
        text=True, capture_output=True, check=False, timeout=30,
    )
    assert (result.returncode == 0) == (mode in {"pass", "missing", "no-encoder"}), result.stderr
    assert not (root / ".venv/bin/pip.log").exists()
    if mode in {"missing", "no-encoder"}:
        assert (tmp_path / "apt.log").read_text().splitlines() == [
            "update", "install -y --no-install-recommends --reinstall ffmpeg",
        ]
        assert "GPU 视频编码（NVENC）测试通过" in render(result.stderr)
    elif mode == "encode-fails":
        assert "NVENC）不可用" in render(result.stderr)
        assert not (tmp_path / "apt.log").exists()
    elif mode == "apt-no-encoder":
        assert "安装后 ffmpeg 仍不支持" in render(result.stderr)
    elif mode == "apt-disabled":
        assert "无法安装 NVENC 检测工具" in render(result.stderr)
        assert not (tmp_path / "apt.log").exists()


def test_cloud_restore_precheck_skips_successfully_installed_restore(deployment, tmp_path):
    root, _ = deployment
    mark_restore_installed(root)
    (root / ".venv/video-turbo-restore.state").write_text("version=1\n")
    bin_dir = bare_bin(tmp_path)
    result = subprocess.run(
        [str(bin_dir / "bash"), str(root / SCRIPT), "__restore_precheck"],
        env={"PATH": str(bin_dir), "VIDEO_TURBO_LANGUAGE": "zh-Hans"}, text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "跳过首次安装的 GPU 视频编码（NVENC）检查" in render(result.stderr)
    assert not (tmp_path / "apt.log").exists()


def test_shared_template_defaults_to_dedicated_accurate_mode() -> None:
    payload = yaml.safe_load((PROJECT / "config/worker.example.yaml").read_text(encoding="utf-8"))
    assert payload["server_url"] == "" and payload["worker_token"] == ""
    payload["worker_token"] = "test-token"
    config = WorkerConfig.model_validate(payload)
    assert "name" not in payload
    assert config.device == "auto"
    assert config.idle.enabled is False
    assert config.asr.model == "large-v3"
    assert config.asr.compute_type == "float16"
    assert config.asr.chunk_seconds == 300
    assert config.asr.download_root == Path("../models")
    assert config.jasna.enabled is False



def test_script_is_strict_and_executable() -> None:
    script = PROJECT / SCRIPT
    assert script.stat().st_mode & 0o111
    text = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "HF_HUB_DISABLE_XET" in text and "nvidia_runtime_library_paths" in text
    assert sorted(path.name for path in (PROJECT / "deploy/worker/linux").iterdir()) == ["worker.sh"]
