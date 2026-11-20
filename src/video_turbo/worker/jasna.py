"""在 Worker 上调用 Jasna CLI 修复画面。"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from ..i18n import msg
from ..models import is_ltx_model
from .config import WorkerConfig
from .jasna_progress import JasnaProgress, OutputSplitter, parse_progress_line

logger = logging.getLogger(__name__)

# 发布包启动前会校验运行环境；Worker 启动脚本为识别后端设置的变量会让它拒绝运行。
_UNSAFE_ENV = ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "TQDM_DISABLE")


class JasnaCancelled(RuntimeError):
    pass


class JasnaError(RuntimeError):
    pass


def jasna_environment(config: WorkerConfig) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _UNSAFE_ENV}
    env["XDG_CONFIG_HOME"] = str(config.jasna.config_home)
    # tqdm 在非终端下按 COLUMNS 决定行宽，过窄会截掉剩余时间和速度。
    env.setdefault("COLUMNS", "200")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def jasna_ready(config: WorkerConfig) -> bool:
    executable = config.jasna.executable
    return bool(
        config.jasna.enabled
        and os.name != "nt"
        and executable is not None
        and executable.is_file()
        and os.access(executable, os.X_OK)
    )


def supported_models(config: WorkerConfig) -> list[str]:
    if not jasna_ready(config):
        return []
    return ["basicvsrpp", "ltx"] if config.jasna.ltx else ["basicvsrpp"]


def probe_nvenc(config: WorkerConfig) -> str | None:
    """用 Jasna 自带的 ffmpeg 试编码 1 秒画面，返回失败原因；None 表示 GPU 视频编码器可用。

    云 GPU 即使用同一个镜像启动，分到的机器也不一定能用 NVENC，所以每次 Worker 启动都要检查。
    """
    ffmpeg = config.jasna.executable.parent / "tools" / "ffmpeg"
    if not ffmpeg.is_file():
        return msg("worker.jasna.missingFfmpeg", ffmpeg=ffmpeg)
    try:
        completed = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc2=duration=1:size=1280x720", "-c:v", "hevc_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, env=jasna_environment(config), timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return msg("worker.jasna.checkFailed", exc=exc)
    if completed.returncode == 0:
        return None
    lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
    return "；".join(lines[:2])[:300] or msg("worker.jasna.ffmpegExit", returncode=completed.returncode)


def build_command(
    config: WorkerConfig,
    *,
    source: Path,
    output: Path,
    working_directory: Path,
    restoration_model: str,
    codec: str,
    ltx_large_canvas: bool = False,
) -> list[str]:
    command = [
        str(config.jasna.executable),
        "--input",
        str(source),
        "--output",
        str(output),
        "--restoration-model-name",
        restoration_model,
        "--codec",
        codec,
        "--device",
        f"cuda:{config.device_index}",
        "--working-directory",
        str(working_directory),
    ]
    if is_ltx_model(restoration_model):
        # 两个选项只对 LTX 有效；BasicVSR++ 任务带上它们 Jasna 会直接报错。
        if config.jasna.ltx_fast:
            command.append("--ltx-fast")
        if ltx_large_canvas:
            command.append("--ltx-large-canvas")
    if config.jasna.max_clip_size is not None:
        command += ["--max-clip-size", str(config.jasna.max_clip_size)]
    command += [str(arg) for arg in config.jasna.extra_args]
    return command


def _describe_failure(code: int, tail: list[str]) -> str:
    text = "\n".join(tail[-15:])
    hints = [
        ("Failed to open hevc encoder", msg("worker.jasna.hint.encoder")),
        ("NVIDIA_DRIVER_CAPABILITIES", msg("worker.jasna.hint.capabilities")),
        ("libGL.so.1", msg("worker.jasna.hint.libgl")),
        ("unsigned or modified Jasna runtime", msg("worker.jasna.hint.runtime")),
        ("in use on three devices", msg("worker.jasna.hint.devices")),
        ("could not reach the license service", msg("worker.jasna.hint.licenseServer")),
        ("No activation found", msg("worker.jasna.hint.notActivated")),
        ("Activate Jasna", msg("worker.jasna.hint.notActivated")),
        ("Run jasna in a terminal to download", msg("worker.jasna.hint.download")),
        ("needs an RTX 50-series", msg("worker.jasna.hint.fast")),
        ("out of memory", msg("worker.jasna.hint.memory")),
        ("CUDA out of memory", msg("worker.jasna.hint.memory")),
    ]
    for needle, hint in hints:
        if needle.lower() in text.lower():
            return msg("worker.jasna.exitHint", hint=hint, code=code, text=text)
    return msg("worker.jasna.exitText", code=code, text=text) if text else msg("worker.jasna.exitNoOutput", code=code)


def run_jasna(
    config: WorkerConfig,
    *,
    source: Path,
    output: Path,
    working_directory: Path,
    restoration_model: str,
    codec: str,
    ltx_large_canvas: bool = False,
    should_cancel: Callable[[], bool],
    on_progress: Callable[[JasnaProgress], None],
    on_stage: Callable[[str], None] = lambda _detail: None,
) -> dict[str, object]:
    if is_ltx_model(restoration_model) and not config.jasna.ltx:
        raise JasnaError(msg("worker.jasna.ltxOff"))
    working_directory.mkdir(parents=True, exist_ok=True)
    command = build_command(
        config,
        source=source,
        output=output,
        working_directory=working_directory,
        restoration_model=restoration_model,
        codec=codec,
        ltx_large_canvas=ltx_large_canvas,
    )
    logger.info("启动 Jasna：%s", " ".join(command))
    on_stage(msg("worker.jasna.preparing"))
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(Path(command[0]).parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=jasna_environment(config),
        start_new_session=True,
    )
    # 记录进程号：Worker 被强制结束（kill -9、OOM）时 Jasna 不会跟着退出，下次启动据此清理。
    pid_file = working_directory.parent / PID_FILE
    pid_file.write_text(str(process.pid))
    tail: deque[str] = deque(maxlen=60)
    splitter = OutputSplitter()

    def reader() -> None:
        assert process.stdout is not None
        while True:
            data = process.stdout.read1(65536)
            if not data:
                break
            for line in splitter.feed(data):
                progress = parse_progress_line(line)
                if progress is not None:
                    on_progress(progress)
                else:
                    tail.append(line.strip()[:500])
                    logger.info("Jasna：%s", line.strip()[:500])
        for line in splitter.flush():
            tail.append(line.strip()[:500])

    thread = threading.Thread(target=reader, name="jasna-output", daemon=True)
    thread.start()
    deadline = started + config.jasna.timeout_hours * 3600
    try:
        while process.poll() is None:
            if should_cancel():
                raise JasnaCancelled(msg("worker.jasna.cancelled"))
            if time.monotonic() > deadline:
                raise JasnaError(msg("worker.jasna.timeout", timeout_hours=f"{config.jasna.timeout_hours:g}"))
            time.sleep(1)
    except BaseException:
        _stop_process_group(process)
        raise
    finally:
        thread.join(timeout=10)
        if process.poll() is not None:
            pid_file.unlink(missing_ok=True)
    code = process.returncode
    if code != 0:
        raise JasnaError(_describe_failure(code, list(tail)))
    if not output.is_file() or output.stat().st_size == 0:
        raise JasnaError(msg("worker.jasna.noOutput") + "\n".join(list(tail)[-10:]))
    return {
        "tool": "jasna",
        "restoration_model": restoration_model,
        "codec": codec,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "max_clip_size": config.jasna.max_clip_size,
        "ltx_fast": bool(is_ltx_model(restoration_model) and config.jasna.ltx_fast),
        "ltx_large_canvas": bool(is_ltx_model(restoration_model) and ltx_large_canvas),
        "extra_args": list(config.jasna.extra_args),
    }


PID_FILE = "jasna.pid"


def _is_jasna_process(pid: int) -> bool:
    """确认进程号仍属于 Jasna，避免进程号被复用后误杀其他进程（依赖 Linux 的 /proc）。"""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return bool(argv) and Path(os.fsdecode(argv[0])).name == "jasna" and b"--input" in argv


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_orphaned_jasna(work_dir: Path | None, wait_seconds: float = 30) -> list[int]:
    """停止上次 Worker 被强制结束后遗留的 Jasna 进程组，返回被停止的进程号。

    调用方必须先持有本机影片修复锁：持有锁说明同机没有其他 Worker 实例正在运行 Jasna，
    此时仍在运行的 Jasna 都是遗留进程。
    """
    stopped: list[int] = []
    if work_dir is None or not work_dir.is_dir():
        return stopped
    for pid_file in work_dir.glob(f"*/{PID_FILE}"):
        try:
            pid = int(pid_file.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pid_file.unlink(missing_ok=True)
            continue
        if _is_jasna_process(pid):
            logger.warning("发现上次遗留的 Jasna 进程 %d（%s），正在停止。", pid, pid_file.parent.name)
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + wait_seconds
            while _process_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            if _process_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            stopped.append(pid)
        pid_file.unlink(missing_ok=True)
    return stopped


def _stop_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def jasna_version(config: WorkerConfig) -> str:
    completed = subprocess.run(
        [str(config.jasna.executable), "--version"],
        cwd=str(config.jasna.executable.parent),
        capture_output=True,
        text=True,
        env=jasna_environment(config),
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raise JasnaError(_describe_failure(completed.returncode, (completed.stdout + completed.stderr).splitlines()))
    return (completed.stdout or completed.stderr).strip().splitlines()[-1] if (completed.stdout or completed.stderr).strip() else "unknown"


def default_self_test_model(config: WorkerConfig) -> str:
    """已启用 LTX 时默认自检 LTX：下载 LTX 模型，并跳过 BasicVSR++ 15–60 分钟的编译。"""
    return "ltx" if config.jasna.ltx else "basicvsrpp"


def _run_answering_download(command: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    """运行 Jasna 并自动同意下载 LTX 模型。

    Jasna 只在标准输入是终端时询问是否下载，这里给它一个伪终端并预先写入 “y”；
    输出仍直接显示在当前终端。模型已存在时不会询问，预写的 “y” 不会被读取。
    """
    import pty

    master, slave = pty.openpty()
    try:
        os.write(master, b"y\n")
        return subprocess.run(command, cwd=str(cwd), env=env, stdin=slave, check=False).returncode
    finally:
        os.close(slave)
        os.close(master)


def self_test(config: WorkerConfig, *, model: str | None = None) -> None:
    """检查编码器并用合成短片跑一遍 Jasna。

    - LTX：自动下载缺少的 LTX 模型（约 15 GB），只处理 2 秒测试片；
    - BasicVSR++：首次运行为本机 GPU 编译模型，约 15–60 分钟。
    Jasna 的输出直接显示在终端。
    """
    if not jasna_ready(config):
        raise JasnaError(
            msg("worker.jasna.notFound", executable=config.jasna.executable)
        )
    model = model or default_self_test_model(config)
    if is_ltx_model(model) and not config.jasna.ltx:
        raise JasnaError(msg("worker.jasna.ltxDisabled"))
    env = jasna_environment(config)
    root = config.jasna.executable.parent
    logger.info("Jasna 版本：%s", jasna_version(config))
    ffmpeg = root / "tools" / "ffmpeg"
    if not ffmpeg.is_file():
        raise JasnaError(msg("worker.jasna.missingFfmpegDot", ffmpeg=ffmpeg))
    logger.info("检查 GPU 视频编码器（hevc_nvenc）。")
    check = subprocess.run(
        [str(ffmpeg), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=duration=1:size=1280x720",
         "-c:v", "hevc_nvenc", "-f", "null", "-"],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )
    if check.returncode != 0:
        raise JasnaError(
            msg("worker.jasna.nvencUnavailable", error=check.stderr.strip())
        )
    work = config.restore_dir / "self-test"
    # 清掉上次中断留下的进度，避免 Jasna 询问是否覆盖已保存的进度。
    shutil.rmtree(work / "jasna-work", ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    seconds = 2 if is_ltx_model(model) else 5
    clip = work / "sample.mp4"
    output = work / "sample-out.mp4"
    subprocess.run(
        [str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc2=duration={seconds}:size=1280x720:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "hevc_nvenc", "-c:a", "aac",
         "-shortest", str(clip)],
        check=True, env=env, timeout=300,
    )
    output.unlink(missing_ok=True)
    command = build_command(
        config, source=clip, output=output, working_directory=work / "jasna-work",
        restoration_model=model, codec="hevc",
    )
    if is_ltx_model(model):
        logger.info(
            "用 LTX 处理 %d 秒测试片；缺少 LTX 模型时自动下载（约 15 GB，可中断后重新运行续传）。"
            "已跳过 BasicVSR++ 编译，需要时运行 bash deploy/worker/linux/worker.sh self-test restore --basicvsrpp。",
            seconds,
        )
        code = _run_answering_download(command, cwd=root, env=env)
    else:
        logger.info("用 BasicVSR++ 处理 %d 秒测试片（首次需为本机 GPU 编译，约 15–60 分钟，请勿中断）。", seconds)
        code = subprocess.run(command, cwd=str(root), env=env, check=False).returncode
    if code != 0 or not output.is_file():
        raise JasnaError(msg("worker.jasna.selfTestFailed", code=code))
    shutil.rmtree(work, ignore_errors=True)
    logger.info("Jasna 自检通过：%s", model)
