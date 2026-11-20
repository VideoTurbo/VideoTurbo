import os
from pathlib import Path

import pytest
from conftest import raises_rendered

from video_turbo.worker.config import JasnaConfig, WorkerConfig
from video_turbo.worker.jasna import JasnaError, default_self_test_model, self_test

# 影片修复（Jasna）只在 Linux 显卡电脑上运行
pytestmark = pytest.mark.skipif(os.name == "nt", reason="影片修复（Jasna）只在 Linux 显卡电脑上运行")

FAKE_JASNA = """#!/bin/bash
if [ "$1" = "--version" ]; then echo "jasna 0.12.3"; exit 0; fi
echo "$*" > "$JASNA_TEST_LOG/args"
out=""; model=""
while [ $# -gt 0 ]; do
  case "$1" in --output) out="$2"; shift;; --restoration-model-name) model="$2"; shift;; esac
  shift
done
echo "$model" > "$JASNA_TEST_LOG/model"
# 与真实 Jasna 一样：只有标准输入是终端时才询问是否下载。
if [ -t 0 ]; then read -r answer; echo "$answer" > "$JASNA_TEST_LOG/answer"; else echo notty > "$JASNA_TEST_LOG/answer"; fi
echo restored > "$out"
"""
FAKE_FFMPEG = """#!/bin/bash
last="${@: -1}"
[ "$last" = "-" ] || echo clip > "$last"
"""


def _config(tmp_path: Path, monkeypatch, *, ltx: bool) -> tuple[WorkerConfig, Path]:
    root = tmp_path / "jasna"
    (root / "tools").mkdir(parents=True)
    for path, body in ((root / "jasna", FAKE_JASNA), (root / "tools" / "ffmpeg", FAKE_FFMPEG)):
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "log"
    log.mkdir()
    monkeypatch.setenv("JASNA_TEST_LOG", str(log))
    config = WorkerConfig(
        worker_token="t", cache_dir=tmp_path / "work",
        jasna=JasnaConfig(enabled=True, ltx=ltx, executable=root / "jasna", config_home=tmp_path / "cfg"),
    )
    return config, log


def test_ltx_worker_self_tests_ltx_and_answers_download(tmp_path: Path, monkeypatch) -> None:
    config, log = _config(tmp_path, monkeypatch, ltx=True)
    assert default_self_test_model(config) == "ltx"
    self_test(config)
    assert (log / "model").read_text().strip() == "ltx"
    assert (log / "answer").read_text().strip() == "y"
    assert not (tmp_path / "work" / "self-test").exists()


def test_basicvsrpp_by_default_without_ltx(tmp_path: Path, monkeypatch) -> None:
    config, log = _config(tmp_path, monkeypatch, ltx=False)
    self_test(config)
    assert (log / "model").read_text().strip() == "basicvsrpp"
    self_test(config, model="basicvsrpp")
    with raises_rendered(JasnaError, "jasna.ltx"):
        self_test(config, model="ltx")


def test_ltx_worker_can_still_compile_basicvsrpp(tmp_path: Path, monkeypatch) -> None:
    config, log = _config(tmp_path, monkeypatch, ltx=True)
    self_test(config, model="basicvsrpp")
    assert (log / "model").read_text().strip() == "basicvsrpp"


def test_ltx_only_flags(tmp_path: Path) -> None:
    from video_turbo.worker.jasna import build_command

    config = WorkerConfig(worker_token="t", jasna=JasnaConfig(executable=tmp_path / "jasna", ltx_fast=True))
    common = {"source": tmp_path / "in.mp4", "output": tmp_path / "out.mp4", "working_directory": tmp_path / "w",
              "codec": "hevc"}
    ltx = build_command(config, restoration_model="ltx", ltx_large_canvas=True, **common)
    assert "--ltx-fast" in ltx and "--ltx-large-canvas" in ltx
    # BasicVSR++ 任务绝不带 LTX 参数，否则 Jasna 会报错退出。
    basic = build_command(config, restoration_model="basicvsrpp", ltx_large_canvas=True, **common)
    assert "--ltx-fast" not in basic and "--ltx-large-canvas" not in basic
    config.jasna.ltx_fast = False
    assert "--ltx-fast" not in build_command(config, restoration_model="ltx-4step", **common)


def test_ltx_fast_self_test_downloads_fast_model(tmp_path: Path, monkeypatch) -> None:
    config, log = _config(tmp_path, monkeypatch, ltx=True)
    config.jasna.ltx_fast = True
    self_test(config)
    assert "--ltx-fast" in (log / "args").read_text()
    assert (log / "answer").read_text().strip() == "y"


def test_run_jasna_records_and_removes_pid(tmp_path: Path, monkeypatch) -> None:
    from video_turbo.worker.jasna import PID_FILE, run_jasna

    config, _log = _config(tmp_path, monkeypatch, ltx=False)
    job_dir = tmp_path / "work" / "job1"
    run_jasna(
        config, source=tmp_path / "in.mp4", output=job_dir / "out.mp4", working_directory=job_dir / "jasna-work",
        restoration_model="basicvsrpp", codec="hevc",
        should_cancel=lambda: False,
        on_progress=lambda _p: None,
    )
    assert not (job_dir / PID_FILE).exists()


def test_stale_pid_file_is_removed(tmp_path: Path) -> None:
    from video_turbo.worker.jasna import PID_FILE, kill_orphaned_jasna

    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    (job_dir / PID_FILE).write_text("999999")
    assert kill_orphaned_jasna(tmp_path) == []
    assert not (job_dir / PID_FILE).exists()


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="依赖 Linux /proc")
def test_orphaned_jasna_is_stopped(tmp_path: Path) -> None:
    import subprocess
    import time

    from video_turbo.worker.jasna import PID_FILE, kill_orphaned_jasna

    # 真实的 Jasna 是独立可执行文件，命令行第一项就是 jasna；shell 脚本的第一项会是 /bin/sh。
    # 用 sh 冒充：argv[0] 设为 jasna，命令末尾的 true 防止 sh 直接 exec 成 sleep 改掉命令行。
    process = subprocess.Popen(
        [str(tmp_path / "bin" / "jasna"), "-c", "sleep 300; true", "--input"],
        executable="/bin/sh",
        start_new_session=True,
    )
    job_dir = tmp_path / "work" / "job1"
    job_dir.mkdir(parents=True)
    (job_dir / PID_FILE).write_text(str(process.pid))
    time.sleep(0.2)
    assert kill_orphaned_jasna(tmp_path / "work", wait_seconds=5) == [process.pid]
    assert process.wait(timeout=5) is not None


def test_recover_orphans_skips_when_another_instance_is_restoring(tmp_path: Path) -> None:
    import asyncio

    from video_turbo.worker.jasna import PID_FILE
    from video_turbo.worker.restore_pipeline import RestorePipeline
    from video_turbo.worker.restore_task import MachineLock

    executable = tmp_path / "jasna"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    work = tmp_path / "cache" / "restore"
    (work / "job1").mkdir(parents=True)
    (work / "job1" / PID_FILE).write_text("999999")
    config = WorkerConfig(worker_token="t", cache_dir=tmp_path / "cache", jasna=JasnaConfig(enabled=True, executable=executable))
    other = MachineLock(work)
    assert other.acquire()  # 同机另一个实例正在影片修复
    pipeline = RestorePipeline(config, None, report_error=None)
    assert asyncio.run(pipeline.recover_orphans()) == []
    assert (work / "job1" / PID_FILE).exists()  # 不动它的进程记录
    other.release()
    assert asyncio.run(pipeline.recover_orphans()) == []
    assert not (work / "job1" / PID_FILE).exists() and not pipeline.lock_held
