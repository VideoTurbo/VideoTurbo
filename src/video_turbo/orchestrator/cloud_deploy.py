"""通过 SSH 把 Worker 部署到刚开机的云实例。

登录账号不必是 root：代码、临时文件都放在登录用户的家目录（``$HOME/video-turbo``），模型目录默认为
``$HOME/models``，可在云平台配置的高级设置中修改。缺少系统依赖时 worker.sh 用 root 或免密 sudo 安装。

步骤（在实例上执行）：解压 NAS 推送的代码包 → 首次部署影片修复时先验证 NVENC（失败则停止全部部署）
→ 停止正在运行的 Worker → 写入 worker.yaml 的
server_url、worker_token → 对主控派发的每个服务（asr 字幕识别、restore 影片修复），从未部署成功过、
依赖（pyproject.toml、WhisperJAV 安装脚本）或 Jasna 许可证变化时运行 deploy/worker/linux/worker.sh
install；开启“自动更新 Jasna”且 GitHub 上有新版本时也会重新安装影片修复 → 只启动这些服务（首次启动会
自检并下载缺失的模型）。Worker 代码以 editable 方式安装，纯代码更新解压后即生效，不需要重新安装。实例上已安装但这次没选的服务保持停止。某个服务安装失败时，其余服务照常启动，
脚本以非零退出码结束。

worker_token、Jasna 许可证码等配置经 SFTP 写入权限 600 的临时文件，不出现在命令行中，读取后立即删除；
许可证码只以环境变量传给安装命令，部署记录中只保存它的指纹。
首次连接记录主机密钥，之后只接受同一把密钥。写入的 server_url 指向实例本机的隧道端口（见 cloud_tunnel）。

脚本输出由 DeployOutput 整理成日志行：进度条刷新按间隔抽样，一行长时间没结束时先显示已收到的部分，
完全没有输出时定期写一行“仍在执行”，便于区分卡住和正在跑不输出的命令。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shlex
import time
from collections.abc import Callable, Sequence

import asyncssh

from ..cloud.base import SshInfo
from ..i18n import msg
from ..machine_id import from_gpu_uuid, nvidia_uuid_command
from .worker_bundle import WorkerBundle

# 都相对登录用户的家目录：SFTP 的相对路径从家目录解析，部署脚本里拼在 $HOME 之后。
REMOTE_ROOT = "video-turbo"
REMOTE_BUNDLE = ".video-turbo-bundle.tar.gz"
REMOTE_SETTINGS = ".video-turbo-deploy.json"
SSH_READY_TIMEOUT_SECONDS = 300
DEPLOY_TIMEOUT_SECONDS = 4 * 3600
_PROFILE = re.compile(r"(3090|4090|5090)")


class DeployError(RuntimeError):
    pass


class NvencPrecheckError(DeployError):
    """首次修复部署的编码预检未通过，可由用户决定是否仅继续字幕。"""


NVENC_PRECHECK_EXIT = 6


def gpu_profile(gpu_name: str) -> str | None:
    """按 GPU 名称选择 worker.sh 的配置模板与字幕并行数上限（--profile 3090/4090/5090）。"""
    match = _PROFILE.search(gpu_name or "")
    return match.group(1) if match else None


SERVICE_LABELS = {"asr": msg("cloud.deploy.service.asr"), "restore": msg("cloud.deploy.service.restore")}


def deploy_script(
    profile: str,
    version: str,
    services: Sequence[str] = ("asr",),
    *,
    update_jasna: bool = True,
    model_dir: str = "",
    home: str | None = None,
) -> str:
    """生成在实例上执行的部署脚本：只安装、启动 services 中的服务（asr、restore）。

    update_jasna 为 False 时，已安装的 Jasna 固定在当前版本，不查询也不下载新版本。
    model_dir 为模型目录：绝对路径，或以 ~/、$HOME/ 开头；空表示 $HOME/models。每次部署都写入 worker.yaml。
    home 仅供测试指定家目录，默认使用登录用户的 $HOME。
    """
    chosen = [service for service in SERVICE_LABELS if service in set(services)]
    if not chosen:
        raise ValueError(msg("cloud.deploy.noServices"))
    q = shlex.quote
    return f"""set -euo pipefail
HOME_DIR={q(home) if home else '"${HOME}"'}
ROOT="${{HOME_DIR}}/{REMOTE_ROOT}"
PROFILE={q(profile)}
VERSION={q(version)}
SERVICES={q(" ".join(chosen))}
UPDATE_JASNA={"1" if update_jasna else "0"}
BUNDLE="${{HOME_DIR}}/{REMOTE_BUNDLE}"
SETTINGS="${{HOME_DIR}}/{REMOTE_SETTINGS}"
MODEL_DIR={q(model_dir)}
case "${{MODEL_DIR}}" in
    "") MODEL_DIR="${{HOME_DIR}}/models" ;;
    "~"|"~/"*) MODEL_DIR="${{HOME_DIR}}${{MODEL_DIR:1}}" ;;
    '$HOME'|'$HOME/'*) MODEL_DIR="${{HOME_DIR}}${{MODEL_DIR:5}}" ;;
esac
trap 'rm -f "${{SETTINGS}}"' EXIT
mkdir -p "${{ROOT}}"
cd "${{ROOT}}"
WORKER_SH="deploy/worker/linux/worker.sh"
echo "[安装] 解压程序（版本 ${{VERSION}}）到 ${{ROOT}}"
tar -xzf "${{BUNDLE}}" -C "${{ROOT}}"
rm -f "${{BUNDLE}}"
case " ${{SERVICES}} " in
    *" restore "*)
        echo "[安装] 先检查显卡视频编码功能（NVENC，影片修复需要）"
        if ! VIDEO_TURBO_NONINTERACTIVE=1 bash "${{WORKER_SH}}" __restore_precheck </dev/null; then
            echo "[安装] 显卡视频编码检查未通过（原因见上面的日志），还没有安装或启动任何服务"
            exit {NVENC_PRECHECK_EXIT}
        fi
        ;;
esac
if [ -f "${{WORKER_SH}}" ] && [ -f config/worker.yaml ] && [ -x .venv/bin/python ]; then
    echo "[安装] 停止正在运行的服务"
    bash "${{WORKER_SH}}" stop --force --yes </dev/null || true
fi
# Jasna 许可证码只在内存中传给安装命令（环境变量），不写入磁盘上的部署记录，只记录指纹。
JASNA_LICENSE="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("jasna_license") or "")' "${{SETTINGS}}")"
LICENSE_MARK="$(printf '%s' "${{JASNA_LICENSE}}" | python3 -c 'import hashlib, sys; d = sys.stdin.buffer.read(); print(hashlib.sha256(d).hexdigest()[:12] if d else "-")')"
echo "[安装] 模型目录：${{MODEL_DIR}}"
if ! mkdir -p "${{MODEL_DIR}}" 2>/dev/null || [ ! -w "${{MODEL_DIR}}" ]; then
    echo "[安装] 模型目录不可写：${{MODEL_DIR}}（登录账号 $(id -un)）。请在云平台配置的“高级设置”中换一个目录"
    exit 5
fi
echo "[安装] 写入 config/worker.yaml"
python3 tools/configure_worker.py --config config/worker.yaml \\
    --template "config/worker.example.yaml" \\
    --default-download-root "${{MODEL_DIR}}" \\
    --download-root "${{MODEL_DIR}}" \\
    --apply-json "${{SETTINGS}}"
rm -f "${{SETTINGS}}"
needs_install() {{
    local mark=".video-turbo-deployed-$1"
    [ -f "${{mark}}" ] && [ "$(cat "${{mark}}")" = "$2" ] || return 0
    case "$1" in
        asr) [ -x .venv/bin/python ] && [ -x .venv-whisperjav/bin/python ] || return 0 ;;
        restore) [ -x .venv/bin/python ] && [ -x jasna/current/jasna ] || return 0 ;;
    esac
    return 1
}}
READY=""
FAILED=""
for SERVICE in ${{SERVICES}}; do
    # 依赖指纹没变时不重新安装：代码是 editable 安装，解压后已经是新版本。
    DEPS="$(bash "${{WORKER_SH}}" __deps_key "${{SERVICE}}" </dev/null || true)"
    [ -n "${{DEPS}}" ] || DEPS="${{VERSION}}"
    case "${{SERVICE}}" in
        asr) LABEL="字幕识别服务"; MARK="${{DEPS}}"; LICENSE_VALUE="" ;;
        restore) LABEL="影片修复服务"; MARK="${{DEPS}} license=${{LICENSE_MARK}}"; LICENSE_VALUE="${{JASNA_LICENSE}}" ;;
    esac
    REASON=""
    if needs_install "${{SERVICE}}" "${{MARK}}"; then
        REASON="首次安装，或有更新"
    elif [ "${{SERVICE}}" = restore ] && [ "${{UPDATE_JASNA}}" = 1 ] \\
        && bash "${{WORKER_SH}}" __jasna_outdated </dev/null; then
        REASON="Jasna 有新版本"
    fi
    if [ -n "${{REASON}}" ]; then
        echo "[安装] 安装${{LABEL}}（${{REASON}}）"
        if VIDEO_TURBO_JASNA_LICENSE="${{LICENSE_VALUE}}" VIDEO_TURBO_JASNA_UPDATE="${{UPDATE_JASNA}}" \\
            VIDEO_TURBO_NONINTERACTIVE=1 \\
            bash "${{WORKER_SH}}" install "${{SERVICE}}" --profile "${{PROFILE}}" --yes </dev/null; then
            echo "${{MARK}}" > ".video-turbo-deployed-${{SERVICE}}"
        else
            echo "[安装] ${{LABEL}}安装失败，详见上方日志"
            rm -f ".video-turbo-deployed-${{SERVICE}}"
            FAILED="${{FAILED:+${{FAILED}}、}}${{LABEL}}"
            continue
        fi
    else
        echo "[安装] ${{LABEL}}已是最新，跳过安装"
    fi
    READY="${{READY:+${{READY}} }}${{SERVICE}}"
done
if [ -n "${{READY}}" ]; then
    case "${{READY}}" in
        "asr restore") START=all; START_LABEL="字幕识别服务和影片修复服务" ;;
        asr) START=asr; START_LABEL="字幕识别服务" ;;
        restore) START=restore; START_LABEL="影片修复服务" ;;
    esac
    echo "[安装] 启动${{START_LABEL}}（首次启动会先自检并下载缺失的模型，耗时较长）"
    if ! VIDEO_TURBO_NONINTERACTIVE=1 bash "${{WORKER_SH}}" start "${{START}}" --profile "${{PROFILE}}" --yes </dev/null; then
        echo "[安装] 启动${{START_LABEL}}失败，详见上方日志"
        exit 4
    fi
fi
if [ -n "${{FAILED}}" ]; then
    echo "[安装] ${{FAILED}}安装失败${{READY:+，其余服务已启动}}"
    exit 3
fi
echo "[安装] 完成"
"""


HOST_KEY_MISMATCH = msg("cloud.deploy.hostKeyName")

# 一行超过这么久没结束先写出已收到的部分；完全没有输出时，每隔这么久写一行“仍在执行”。
STALL_SECONDS = 30
# 进度条（tqdm 等用 \r 原地刷新）每隔多久记一次，最新一次不会被丢掉。
PROGRESS_LOG_SECONDS = 10
# 残行超过这个长度直接当一行写出，避免不换行的输出无限积压。
MAX_PARTIAL_CHARS = 4000
_LINE_END = re.compile(r"\r\n|\r|\n")


def _duration(seconds: float) -> str:
    if seconds < 120:
        return msg("cloud.deploy.seconds", seconds=int(seconds))
    return msg("cloud.deploy.minutes", minutes=int(seconds // 60))


class DeployOutput:
    """把部署脚本的原始输出整理成日志行。

    \n 结束的是普通行，立即写出；\r 结束的是进度刷新（同一行原地覆盖），最多每
    PROGRESS_LOG_SECONDS 记一次，之后若有普通行到来或刷新停住，再补记最新的那次。
    feed() 接收输出，tick() 需要定期调用（建议每秒），处理残行和“仍在执行”提示。
    """

    def __init__(self, log: Callable[[str], None], *, clock: Callable[[], float] = time.monotonic):
        self._log = log
        self._clock = clock
        now = clock()
        self._partial = ""  # 还没遇到 \r / \n 的内容
        self._partial_shown = ""  # 上次作为残行写出时的内容
        self._progress = ""  # 被抽样跳过、还没写入的最新一次进度刷新
        self._progress_at = -math.inf  # 上次写入进度刷新的时间
        self._after_cr = False  # 上一块以 \r 结尾
        self._in_refresh = False  # 残行跟在 \r 后面，是进度条正在刷新的那一行
        self._last_line = ""
        self._received_at = now  # 上次收到输出的时间
        self._written_at = now  # 上次写出任何一行的时间
        self.step = ""  # 最近一个“[安装]”步骤

    def _emit(self, line: str) -> None:
        self._written_at = self._clock()
        self._last_line = line
        self._log(line)

    def _flush_progress(self) -> None:
        if self._progress:
            if self._progress != self._last_line:  # tick() 可能已经提前写过这次刷新
                self._emit(self._progress)
            self._progress = ""
            self._progress_at = self._clock()

    def _line(self, line: str) -> None:
        self._flush_progress()
        self._progress_at = -math.inf  # 下一个进度条的第一次刷新立即显示
        if not line.strip():
            return
        if line.startswith("[安装]"):
            self.step = line.removeprefix("[安装]").strip()
        self._emit(line)

    def _refresh(self, line: str) -> None:
        if not line.strip():
            return
        if self._clock() - self._progress_at >= PROGRESS_LOG_SECONDS:
            self._progress = line
            self._flush_progress()
        else:
            self._progress = line

    def feed(self, text: str) -> None:
        self._received_at = self._clock()
        data = self._partial + text
        if self._after_cr and data.startswith("\n"):
            # 上一块末尾的 \r 其实是被拆开的 \r\n：那一行按进度刷新处理过，这里把它补记下来。
            data = data[1:]
            self._line("")
        position = 0
        for match in _LINE_END.finditer(data):
            segment = data[position : match.start()]
            position = match.end()
            self._in_refresh = match.group() == "\r"
            if self._in_refresh and not segment.startswith("[安装]"):
                self._refresh(segment)
            else:
                self._line(segment)
        self._after_cr = bool(data) and position == len(data) and data.endswith("\r")
        self._partial = data[position:]
        if position:  # 之前的残行已经结束
            self._partial_shown = ""
        if len(self._partial) > MAX_PARTIAL_CHARS:
            self._line(self._partial)
            self._partial = self._partial_shown = ""

    def tick(self) -> None:
        now = self._clock()
        if self._progress and now - self._progress_at >= PROGRESS_LOG_SECONDS:
            self._flush_progress()
        partial = self._partial
        # 进度条停在某一次刷新上（下一次 \r 还没来）：按进度抽样间隔直接写出这一次。
        if (
            self._in_refresh
            and partial.strip()
            and partial != self._partial_shown
            and now - self._progress_at >= PROGRESS_LOG_SECONDS
        ):
            self._partial_shown = partial
            self._emit(partial)
            self._progress_at = now
            return
        if now - self._written_at < STALL_SECONDS:
            return
        if partial.strip() and partial != self._partial_shown:
            self._partial_shown = partial
            self._emit(msg("cloud.deploy.partialLine", partial=partial))
        elif now - self._received_at >= STALL_SECONDS:
            step = msg("cloud.deploy.stepSuffix", step=self.step) if self.step else ""
            self._emit(msg("cloud.deploy.waiting", step=step, time=_duration(now - self._received_at)))
        else:
            self._written_at = now

    def close(self) -> None:
        """输出结束：写出最后的进度刷新和没有换行的末尾内容。"""
        partial, self._partial = self._partial, ""
        self._line(partial)


async def _connect(ssh: SshInfo, log: Callable[[str], None]) -> asyncssh.SSHClientConnection:
    """首次连接记录主机密钥（写入 ssh.host_key），之后只接受同一把密钥，防止连接被中间人冒充。"""
    deadline = time.monotonic() + SSH_READY_TIMEOUT_SECONDS
    trusted = None
    if ssh.host_key:
        try:
            trusted = ([asyncssh.import_public_key(ssh.host_key)], [], [])
        except asyncssh.KeyImportError as exc:
            raise DeployError(msg("cloud.deploy.hostKeyUnreadable", exc=exc, name=HOST_KEY_MISMATCH)) from exc
    while True:
        try:
            connection = await asyncssh.connect(
                ssh.host,
                port=ssh.port,
                username=ssh.username,
                password=ssh.password,
                known_hosts=trusted,
                client_keys=None,
                agent_path=None,
                connect_timeout=20,
                keepalive_interval=30,
            )
            if trusted is None:
                key = connection.get_server_host_key()
                if key is not None:
                    ssh.host_key = key.export_public_key().decode().strip()
                    log(f"[安装] 首次连接，已记录{HOST_KEY_MISMATCH} {key.get_fingerprint()}")
            return connection
        except asyncssh.HostKeyNotVerifiable as exc:
            # 不重试：密钥变化要么是实例重装了系统，要么是连接被劫持，都需要人来确认。
            raise DeployError(
                msg("cloud.deploy.hostKeyChanged", host=ssh.host, port=ssh.port, name=HOST_KEY_MISMATCH)
            ) from exc
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise DeployError(msg("cloud.deploy.sshFailed", host=ssh.host, port=ssh.port, exc=exc)) from exc
            log(f"[安装] 云主机还在启动，10 秒后再连（{exc}）")
            await asyncio.sleep(10)


async def query_machine_id(ssh: SshInfo, log: Callable[[str], None]) -> str:
    """通过 SSH 读取实例 GPU 的 UUID，得到与 Worker 自己生成的完全相同的机器标识。"""
    connection = await _connect(ssh, log)
    async with connection:
        result = await connection.run(shlex.join(nvidia_uuid_command(0)), check=False)
    output = str(result.stdout or "").strip().splitlines()
    if result.exit_status != 0 or not output:
        raise DeployError(msg("cloud.deploy.gpuUuidFailed", output=str(result.stderr or '').strip() or msg('cloud.deploy.noOutput')))
    return from_gpu_uuid(output[0])


async def _pump_output(stream: asyncssh.SSHReader[str], output: DeployOutput) -> None:
    """按块读取输出交给 DeployOutput，没有新输出时每秒调用一次 tick()。"""
    reading: asyncio.Task[str] | None = None
    try:
        while True:
            if reading is None:
                reading = asyncio.ensure_future(stream.read(65536))
            # 不取消读任务：超时只是去做 tick()，读到的数据留给下一轮。
            done, _ = await asyncio.wait({reading}, timeout=1)
            if done:
                chunk, reading = reading.result(), None
                if not chunk:
                    break
                output.feed(chunk)
            output.tick()
    finally:
        if reading is not None:
            reading.cancel()
        output.close()


async def deploy_worker(
    ssh: SshInfo,
    *,
    bundle: WorkerBundle,
    profile: str,
    settings: dict[str, str],
    log: Callable[[str], None],
    services: Sequence[str] = ("asr",),
    update_jasna: bool = True,
    model_dir: str = "",
) -> None:
    log(f"[安装] 连接 {ssh.username}@{ssh.host}:{ssh.port}")
    connection = await _connect(ssh, log)
    async with connection:
        log(f"[安装] 上传程序文件（{len(bundle.data) // 1024} KB，{bundle.file_count} 个文件）")
        async with connection.start_sftp_client() as sftp:
            async with sftp.open(REMOTE_BUNDLE, "wb") as handle:
                await handle.write(bundle.data)
            async with sftp.open(REMOTE_SETTINGS, "w") as handle:
                await handle.write(json.dumps(settings, ensure_ascii=False))
            await sftp.chmod(REMOTE_SETTINGS, 0o600)
        script = deploy_script(
            profile, bundle.version, services, update_jasna=update_jasna, model_dir=model_dir
        )
        output = DeployOutput(log)
        try:
            async with asyncio.timeout(DEPLOY_TIMEOUT_SECONDS):
                async with connection.create_process(
                    "bash -s", stderr=asyncssh.STDOUT, encoding="utf-8", errors="replace"
                ) as process:
                    process.stdin.write(script)
                    process.stdin.write_eof()
                    await _pump_output(process.stdout, output)
                    await process.wait()
        except TimeoutError as exc:
            raise DeployError(msg("cloud.deploy.timeout", hours=DEPLOY_TIMEOUT_SECONDS // 3600)) from exc
        last_step = output.step
        if process.exit_status != 0:
            detail = msg("cloud.deploy.stepSuffix", step=last_step) if last_step else ""
            if process.exit_status == NVENC_PRECHECK_EXIT:
                raise NvencPrecheckError(msg("cloud.deploy.nvencFailed", detail=detail))
            raise DeployError(msg("cloud.deploy.scriptFailed", exit_status=process.exit_status, detail=detail))
