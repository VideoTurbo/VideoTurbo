#!/usr/bin/env bash
# VideoTurbo Linux NVIDIA Worker 管理脚本：安装、运行、停止、清理、状态与日志。
#
# 两个互相独立的服务：
#   字幕识别（asr）     ：WhisperJAV / faster-whisper，同机可开多路并行（cloud4090-01、-02……）
#   影片修复（restore）    ：Jasna，每台机器一个实例（cloud4090-dc）
# 两个服务都启动时串行使用 GPU（字幕优先）：影片修复只在本机字幕任务空闲时开始下一部，
# 影片修复修复期间字幕实例暂停领取，这一部修复完再继续。
#
# 交互式（适合人工）：  bash deploy/worker/linux/worker.sh
# 参数式（适合自动化）：bash deploy/worker/linux/worker.sh <命令> [服务] [选项]，详见 help。
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename -- "$0")"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
SCRIPT_CMD="bash deploy/worker/linux/$(basename -- "$0")"
VENV_DIR="$PROJECT_ROOT/.venv"
# 从 uv.lock 导出的依赖清单（精确版本 + 哈希），见 tools/export_requirements.py。
REQUIREMENTS_DIR="$PROJECT_ROOT/deploy/worker/requirements"
VENV_PYTHON="$VENV_DIR/bin/python"
WHISPERJAV_DIR="$PROJECT_ROOT/.venv-whisperjav"
WORKER_CONFIG="${VIDEO_TURBO_WORKER_CONFIG:-$PROJECT_ROOT/config/worker.yaml}"
SYSTEM_PYTHON="${VIDEO_TURBO_PYTHON:-python3}"
LOG_DIR="$PROJECT_ROOT/logs"
RUN_DIR="$PROJECT_ROOT/work"
STATE_DIR="$RUN_DIR/.state"
JASNA_ROOT="${VIDEO_TURBO_JASNA_DIR:-$PROJECT_ROOT/jasna}"
JASNA_VERSION="${VIDEO_TURBO_JASNA_VERSION:-latest}"
# 设为 0 时，已安装的 Jasna 在重新安装时保留当前版本，不查询、不下载新版本。
JASNA_UPDATE="${VIDEO_TURBO_JASNA_UPDATE:-1}"
JASNA_ARCHIVE_DIR="${VIDEO_TURBO_JASNA_ARCHIVE_DIR:-}"
# 保留用户原始设置，避免通用 Git 测速导出的前缀覆盖 Jasna 实际分卷测速。
JASNA_GITHUB_PROXY="${VIDEO_TURBO_GITHUB_PROXY:-}"
# 默认放在当前用户的家目录，root 与普通用户都可写。
DEFAULT_MODEL_DIR="${VIDEO_TURBO_DEFAULT_MODEL_DIR:-${HOME:-/opt}/models}"
# 下载源由 select_sources 测速后填入；设置 VIDEO_TURBO_* 环境变量可固定地址。
PYPI_INDEX_URL=""
HF_MIRROR_ENDPOINT=""
GITHUB_PROXY=""
STOP_TIMEOUT="${VIDEO_TURBO_STOP_TIMEOUT:-60}"

NONINTERACTIVE="${VIDEO_TURBO_NONINTERACTIVE:-0}"
ASSUME_YES=0
FORCE=0
SKIP_SELF_TEST=0
PROFILE="${VIDEO_TURBO_PROFILE:-}"
ASR_COUNT="${VIDEO_TURBO_ASR_WORKERS:-}"
FOLLOW=0
LOG_LINES=100
CLEAN_SKIPS=()
SELF_TEST_ARGS=()
LOG_TAG="VideoTurbo"

# ============================================================ 输出与交互

# 界面语言：VIDEO_TURBO_LANGUAGE（zh-Hans / en），否则看系统语言；中文系统显示中文，其他显示英文。
# 导出给本脚本调用的 Python 工具和服务，使它们用同一种语言。
case "${VIDEO_TURBO_LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in
    zh*|ZH*) UI_LANG=zh; export VIDEO_TURBO_LANGUAGE=zh-Hans ;;
    *) UI_LANG=en; export VIDEO_TURBO_LANGUAGE=en ;;
esac
# L "中文" "English"：按界面语言输出其中一种（不换行）。
L() { if [ "$UI_LANG" = zh ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }

say() { printf '%s\n' "$*"; }
info() { printf '[%s] %s\n' "$LOG_TAG" "$*" >&2; }
warn() { printf "$(L '[%s] 警告：%s\n' '[%s] Warning: %s\n')" "$LOG_TAG" "$*" >&2; }
die() { printf '%s\n' "$1" >&2; exit "${2:-1}"; }

interactive() {
    [ "$NONINTERACTIVE" != "1" ] && [ "$ASSUME_YES" != "1" ] && [ -t 0 ]
}

# ask 提示 默认值 → REPLY
ask() {
    REPLY=""
    if interactive; then
        printf "$(L '%s%s：' '%s%s: ')" "$1" "$(L "${2:+（回车默认 $2）}" "${2:+ (Enter for $2)}")" >&2
        read -r REPLY || REPLY=""
    fi
    [ -n "$REPLY" ] || REPLY="${2:-}"
}

# confirm 提示 默认(y|n)：交互时询问；--yes 时同意；其他非交互情况取默认值。
confirm() {
    local prompt="$1" default="${2:-n}" answer=""
    if [ "$ASSUME_YES" = "1" ]; then
        return 0
    fi
    if ! interactive; then
        [ "$default" = "y" ]
        return
    fi
    if [ "$default" = "y" ]; then
        printf '%s [Y/n] ' "$prompt" >&2
    else
        printf '%s [y/N] ' "$prompt" >&2
    fi
    read -r answer || answer=""
    [ -n "$answer" ] || answer="$default"
    case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

pause() {
    if interactive; then
        printf "$(L '按回车继续……' 'Press Enter to continue……')" >&2
        read -r _ || true
    fi
}

# ============================================================ 基础信息

project_version() {
    sed -n 's/^__version__ *= *"\(.*\)"/\1/p' "$PROJECT_ROOT/src/video_turbo/__init__.py" 2>/dev/null | head -n1
}

files_hash() {
    local existing=() file
    for file in "$@"; do
        [ -f "$file" ] && existing+=("$file")
    done
    [ "${#existing[@]}" -gt 0 ] || { echo "-"; return; }
    if command -v sha256sum >/dev/null 2>&1; then
        cat "${existing[@]}" | sha256sum | cut -c1-12
    else
        cat "${existing[@]}" | shasum -a 256 | cut -c1-12
    fi
}

asr_deps_hash() {
    files_hash "$PROJECT_ROOT/pyproject.toml" "$REQUIREMENTS_DIR/asr.txt" "$PROJECT_ROOT/tools/install_whisperjav.py"
}
restore_deps_hash() { files_hash "$PROJECT_ROOT/pyproject.toml" "$REQUIREMENTS_DIR/restore.txt"; }

# 按依赖清单安装（版本与 CI 测试一致），再以 editable 模式安装当前项目代码（不再解析依赖）。
install_locked() {
    local requirements="$REQUIREMENTS_DIR/$1.txt"
    [ -f "$requirements" ] || die "$(L "项目文件不完整：缺少 ${requirements#"$PROJECT_ROOT"/}。请重新下载完整的安装包。" "The project files are incomplete: ${requirements#"$PROJECT_ROOT"/} is missing. Download the complete install package again.")"
    "$VENV_PYTHON" -m pip install --require-hashes -r "$requirements"
    # 云端使用 editable 安装，始终直接加载当前 src，避免脚本与 site-packages 版本错位。
    "$VENV_PYTHON" -m pip install --no-deps --editable "$PROJECT_ROOT"
}

# 安装记录：key=value 文本，随虚拟环境一起删除。
asr_state_file() { echo "$VENV_DIR/video-turbo-asr.state"; }
restore_state_file() { echo "$VENV_DIR/video-turbo-restore.state"; }

read_state() {
    [ -f "$1" ] || return 0
    sed -n "s/^$2=//p" "$1" | head -n1
}

write_state() {
    local file="$1"
    shift
    mkdir -p "$(dirname "$file")"
    : > "$file.tmp"
    while [ "$#" -gt 0 ]; do
        printf '%s\n' "$1" >> "$file.tmp"
        shift
    done
    printf 'installed_at=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$file.tmp"
    mv "$file.tmp" "$file"
}

expected_whisperjav_version() {
    sed -n 's/^WHISPERJAV_VERSION *= *"\(.*\)"/\1/p' "$PROJECT_ROOT/src/video_turbo/worker/whisperjav.py" 2>/dev/null | head -n1
}

installed_whisperjav_version() {
    local marker="$WHISPERJAV_DIR/video-turbo-whisperjav.json"
    if [ -f "$marker" ]; then
        sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$marker" | head -n1
    fi
}

# worker.yaml 中的值：有虚拟环境时用 PyYAML 读取，否则按行读取顶层键。
config_value() {
    local key="$1"
    [ -f "$WORKER_CONFIG" ] || return 0
    if [ -x "$VENV_PYTHON" ] && "$VENV_PYTHON" -c 'import yaml' >/dev/null 2>&1; then
        "$VENV_PYTHON" - "$WORKER_CONFIG" "$key" <<'PY' 2>/dev/null || true
import sys, yaml
value = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
if isinstance(value, bool):
    value = str(value).lower()
print("" if value is None else str(value).strip())
PY
    else
        case "$key" in
            *.*) ;;
            *) sed -n "s/^$key: *//p" "$WORKER_CONFIG" | head -n1 | sed 's/ *#.*//; s/^"\(.*\)"$/\1/' ;;
        esac
    fi
}

detect_profile() {
    [ -n "$PROFILE" ] && { validate_profile; return; }
    local gpu=""
    if command -v nvidia-smi >/dev/null 2>&1; then
        gpu="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1 || true)"
    fi
    case "$gpu" in
        *3090*) PROFILE=3090 ;;
        *4090*) PROFILE=4090 ;;
        *5090*) PROFILE=5090 ;;
    esac
    if [ -z "$PROFILE" ]; then
        if interactive; then
            ask "$(L "无法从 nvidia-smi 识别显卡型号（${gpu:-无}），请选择配置模板 3090/4090/5090" "Can't detect the GPU model from nvidia-smi (${gpu:-none}); choose a profile 3090/4090/5090")" 4090
            PROFILE="$REPLY"
        else
            die "$(L "无法从 nvidia-smi 识别显卡型号（${gpu:-无}），请用 --profile 3090|4090|5090 指定。" "Can't detect the GPU model from nvidia-smi (${gpu:-none}); set it with --profile 3090|4090|5090.")" 2
        fi
    fi
    validate_profile
}

validate_profile() {
    case "$PROFILE" in
        3090|4090|5090) ;;
        *) die "$(L "--profile 只能是 3090、4090 或 5090：$PROFILE" "--profile must be 3090, 4090 or 5090: $PROFILE")" 2 ;;
    esac
}

max_asr_workers() { [ "$PROFILE" = "5090" ] && echo 4 || echo 3; }

validate_asr_count() {
    local max
    max="$(max_asr_workers)"
    case "$1" in
        ''|*[!0-9]*) die "$(L "字幕并行数必须是 1～$max 的整数：$1" "The number of subtitle services must be a whole number from 1 to $max: $1")" 2 ;;
    esac
    if [ "$1" -lt 1 ] || [ "$1" -gt "$max" ]; then
        die "$(L "RTX $PROFILE 的字幕并行数必须在 1～$max 之间：$1" "The number of subtitle services for RTX $PROFILE must be between 1 and $max: $1")" 2
    fi
}

# ============================================================ 安装状态

asr_installed() {
    [ -x "$VENV_PYTHON" ] && { [ -f "$(asr_state_file)" ] || [ -n "$(installed_whisperjav_version)" ]; }
}

restore_installed() {
    [ -x "$VENV_PYTHON" ] && [ -x "$JASNA_ROOT/current/jasna" ] && [ "$(config_value jasna.enabled)" = "true" ]
}

jasna_installed_tag() {
    local tag
    tag="$(cat "$JASNA_ROOT/installed-tag" 2>/dev/null || true)"
    if [ -z "$tag" ] && [ -L "$JASNA_ROOT/current" ]; then
        tag="$(basename "$(readlink "$JASNA_ROOT/current")")"
    fi
    echo "$tag"
}

# 已安装的 Jasna 是否就是 tag 版本。旧脚本安装的没有 installed-tag，按目录名是否包含 tag 判断。
jasna_is_version() {
    local tag="$1" installed
    [ -n "$tag" ] && [ -x "$JASNA_ROOT/current/jasna" ] || return 1
    installed="$(jasna_installed_tag)"
    [ "$installed" = "$tag" ] && return 0
    [ ! -f "$JASNA_ROOT/installed-tag" ] && case "$installed" in *"$tag"*) return 0 ;; esac
    return 1
}

# 查询 GitHub 上最新的 Jasna 版本；离线或被限流时输出空。
jasna_latest_tag() {
    command -v curl >/dev/null 2>&1 || return 0
    curl -fsSL --max-time 8 "https://api.github.com/repos/Kruk2/jasna/releases/latest" 2>/dev/null \
        | grep -o '"tag_name": *"[^"]*"' | head -n1 | sed 's/.*"\([^"]*\)"$/\1/' || true
}

# GitHub 上有比已安装更新的 Jasna 且允许自动更新时返回 0；查询不到最新版本时视为没有更新。
jasna_outdated() {
    local latest
    [ "$JASNA_UPDATE" = "1" ] && [ "$JASNA_VERSION" = "latest" ] && [ -z "$JASNA_ARCHIVE_DIR" ] || return 1
    latest="$(jasna_latest_tag)"
    [ -n "$latest" ] && ! jasna_is_version "$latest"
}

# 一行安装状态：服务 是否已装 已装版本 / 可更新提示
asr_install_summary() {
    local current deps installed_version installed_deps wj_installed wj_expected note=""
    current="$(project_version)"
    deps="$(asr_deps_hash)"
    wj_expected="$(expected_whisperjav_version)"
    if ! asr_installed; then
        echo "$(L "未安装（可安装 v${current}，WhisperJAV ${wj_expected}）" "Not installed (v${current} available, WhisperJAV ${wj_expected})")"
        return
    fi
    installed_version="$(read_state "$(asr_state_file)" version)"
    installed_deps="$(read_state "$(asr_state_file)" deps)"
    wj_installed="$(installed_whisperjav_version)"
    if [ -z "$installed_version" ]; then
        note="$(L "旧脚本安装，建议重新安装以记录版本" "Installed by an older script; reinstall to record the version")"
    elif [ "$installed_deps" != "$deps" ] || [ "$wj_installed" != "$wj_expected" ]; then
        note="$(L "可更新到 v${current}（WhisperJAV ${wj_expected}）" "Update to v${current} available (WhisperJAV ${wj_expected})")"
    else
        note="$(L "已是最新" "Up to date")"
    fi
    echo "$(L "已安装 v${installed_version:-未知}（WhisperJAV ${wj_installed:-未安装}）· $note" "Installed v${installed_version:-unknown} (WhisperJAV ${wj_installed:-not installed}) · $note")"
}

restore_install_summary() {
    local latest="${1:-}" installed deps installed_deps note=""
    if ! restore_installed; then
        if [ -x "$JASNA_ROOT/current/jasna" ]; then
            echo "$(L "未启用（Jasna 已下载，但 worker.yaml 中 jasna.enabled 不是 true，重新安装即可启用）" "Not enabled (Jasna is downloaded, but jasna.enabled in worker.yaml isn't true; reinstall to enable it)")"
        else
            echo "$(L "未安装${latest:+（可安装 Jasna ${latest}）}" "Not installed${latest:+ (Jasna ${latest} available)}")"
        fi
        return
    fi
    installed="$(jasna_installed_tag)"
    deps="$(restore_deps_hash)"
    installed_deps="$(read_state "$(restore_state_file)" deps)"
    if [ -n "$latest" ] && ! jasna_is_version "$latest" && [ "$JASNA_VERSION" = "latest" ]; then
        note="$(L "Jasna 可更新到 $latest" "Jasna update to $latest available")"
    elif [ -n "$installed_deps" ] && [ "$installed_deps" != "$deps" ]; then
        note="$(L "程序依赖有更新，建议重新安装" "Dependencies changed; reinstalling is recommended")"
    elif [ -z "$latest" ]; then
        note="$(L "未查询最新版本" "Latest version not checked")"
    else
        note="$(L "已是最新" "Up to date")"
    fi
    local ltx=""
    [ "$(config_value jasna.ltx)" = "true" ] && ltx="$(L "，LTX 已启用" ", LTX enabled")"
    echo "$(L "已安装 Jasna ${installed:-未知}${ltx} · $note" "Installed Jasna ${installed:-unknown}${ltx} · $note")"
}

# ============================================================ 进程管理

pid_file_for() { echo "$RUN_DIR/worker.$1.pid"; }
log_file_for() { echo "$LOG_DIR/worker.$1.log"; }
role_of() { case "$1" in *-dc) echo restore ;; *) echo asr ;; esac; }

# instance_pid 实例 → 输出正在运行的守护进程 PID，未运行返回 1
instance_pid() {
    local file pid
    file="$(pid_file_for "$1")"
    [ -f "$file" ] || return 1
    pid="$(tr -d '[:space:]' < "$file")"
    case "$pid" in ''|*[!0-9]*|0|1) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

# 所有有 PID 文件的实例。
all_instances() {
    local file name
    for file in "$RUN_DIR"/worker.*.pid; do
        [ -e "$file" ] || continue
        name="$(basename "$file" .pid)"
        echo "${name#worker.}"
    done
}

running_instances() {
    local role="${1:-all}" instance
    for instance in $(all_instances); do
        [ "$role" = "all" ] || [ "$(role_of "$instance")" = "$role" ] || continue
        instance_pid "$instance" >/dev/null && echo "$instance"
    done
    return 0
}

count_running() { running_instances "$1" | grep -c . || true; }

wait_gone() {
    local pid="$1" seconds="$2" waited=0
    while kill -0 "$pid" 2>/dev/null; do
        [ "$waited" -lt $((seconds * 10)) ] || return 1
        sleep 0.1
        waited=$((waited + 1))
    done
}

stop_instance() {
    local instance="$1" force="${2:-0}" file pid command
    file="$(pid_file_for "$instance")"
    if [ ! -f "$file" ]; then
        return 0
    fi
    pid="$(tr -d '[:space:]' < "$file")"
    case "$pid" in
        ''|*[!0-9]*|0|1)
            warn "$(L "PID 文件无效，已清理：$file" "Invalid PID file removed: $file")"
            rm -f "$file"
            return 1
            ;;
    esac
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$file"
        return 0
    fi
    if [ -r "/proc/$pid/cmdline" ]; then
        command="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
        case "$command" in
            *worker.sh*|*run-worker.sh*) ;;
            *)
                warn "$(L "没有停止：编号 $pid 的程序不是 VideoTurbo 的（${command:0:80}）。" "Not stopped: process $pid isn't VideoTurbo (${command:0:80}).")"
                return 1
                ;;
        esac
    fi
    kill -TERM "$pid" 2>/dev/null || true
    say "$(L "正在停止 ${instance}，请稍候……" "Stopping ${instance}, please wait……")"
    if wait_gone "$pid" "$STOP_TIMEOUT"; then
        rm -f "$file"
        say "$(L "$instance 已停止。" "$instance stopped.")"
        return 0
    fi
    if [ "$force" = "1" ]; then
        warn "$(L "$instance 在 ${STOP_TIMEOUT} 秒内未退出，强制结束。" "$instance didn't exit within ${STOP_TIMEOUT} seconds; forcing it to stop.")"
        pkill -KILL -P "$pid" 2>/dev/null || true
        kill -KILL "$pid" 2>/dev/null || true
        rm -f "$file"
        return 0
    fi
    warn "$(L "$instance 在 ${STOP_TIMEOUT} 秒内未退出（可能仍在结束模型进程）；可稍后再查看状态，或用 stop --force 强制结束。" "$instance didn't exit within ${STOP_TIMEOUT} seconds (it may still be shutting down model processes); check the status later, or force it with stop --force.")"
    return 1
}

stop_service() {
    local role="$1" force="${2:-0}" instance stopped=0 failed=0
    for instance in $(all_instances); do
        [ "$role" = "all" ] || [ "$(role_of "$instance")" = "$role" ] || continue
        if instance_pid "$instance" >/dev/null; then
            stopped=$((stopped + 1))
        fi
        stop_instance "$instance" "$force" || failed=1
    done
    if [ "$stopped" -eq 0 ]; then
        say "$(L "$(role_label "$role")没有在运行。" "The $(role_label "$role") isn't running.")"
    fi
    return "$failed"
}

role_label() {
    case "$1" in
        asr) echo "$(L "字幕识别服务" "subtitle recognition service")" ;;
        restore) echo "$(L "影片修复服务" "video restoration service")" ;;
        *) echo "$(L "全部服务" "all services")" ;;
    esac
}

# 下载源：PyPI、PyTorch、Hugging Face、GitHub 各有官方源和镜像。用前花几秒并行测连通性、
# 文件是否存在和速度，自动选最快的；已设置 VIDEO_TURBO_* 环境变量的不测速。
# 结果导出为环境变量，本次运行内的后续步骤和子进程直接沿用。
select_sources() {
    local python="$SYSTEM_PYTHON" selected=""
    [ -x "$VENV_PYTHON" ] && python="$VENV_PYTHON"
    if [ -f "$PROJECT_ROOT/tools/select_sources.py" ]; then
        selected="$("$python" "$PROJECT_ROOT/tools/select_sources.py" --config "$WORKER_CONFIG" --format sh "$@" || true)"
    fi
    [ -n "$selected" ] && eval "$selected"
    PYPI_INDEX_URL="${VIDEO_TURBO_PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
    HF_MIRROR_ENDPOINT="${VIDEO_TURBO_HF_ENDPOINT:-${HF_ENDPOINT:-https://hf-mirror.com}}"
    GITHUB_PROXY="${VIDEO_TURBO_GITHUB_PROXY:-}"
    [ "$GITHUB_PROXY" = "direct" ] && GITHUB_PROXY=""
    return 0
}

# 访问 GitHub API：直连失败时改走加速代理。
github_api() {
    local url="https://api.github.com/$1" prefix
    for prefix in "" "$GITHUB_PROXY" "https://gh-proxy.com/"; do
        curl -fsSL --max-time 15 "$prefix$url" 2>/dev/null && return 0
    done
    return 1
}

# 运行环境：模型下载源、CUDA 动态库路径。前台自检和后台守护进程共用。
prepare_runtime_env() {
    # large-v3 等模型首次下载及缓存缺失文件走测速选出的 Hugging Face 地址。
    select_sources hf
    export HF_ENDPOINT="$HF_MIRROR_ENDPOINT"
    # 必须在 Python 导入 huggingface_hub 前设置，避免镜像下载跳转到 Xet CAS 后 401。
    export HF_HUB_DISABLE_XET="${VIDEO_TURBO_HF_HUB_DISABLE_XET:-1}"
    # 云镜像可能预设为隐藏 Hugging Face/tqdm 进度条；本项目需要显示模型下载速度。
    unset HF_HUB_DISABLE_PROGRESS_BARS TQDM_DISABLE
    # faster-whisper/CTranslate2 使用 pip 安装的 CUDA 12 cuBLAS 和 cuDNN 9，需放入加载路径。
    local cuda_paths
    cuda_paths="$("$VENV_PYTHON" -c 'from video_turbo.worker.gpu import nvidia_runtime_library_paths; print(":".join(str(path) for path in nvidia_runtime_library_paths()))')"
    if [ -n "$cuda_paths" ]; then
        export LD_LIBRARY_PATH="$cuda_paths${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
    cd "$PROJECT_ROOT"
}

# 后台守护进程（仅供 start 内部调用）：Worker 意外退出后 15 秒自动重启，NAS 或网络中断后可自动重连。
SUP_PID_FILE=""
SUP_STOP_REQUESTED=0
SUP_CHILD_PID=""

sup_request_stop() {
    SUP_STOP_REQUESTED=1
    if [ -n "$SUP_CHILD_PID" ] && kill -0 "$SUP_CHILD_PID" 2>/dev/null; then
        kill -TERM "$SUP_CHILD_PID" 2>/dev/null || true
    fi
}

sup_cleanup_pid_file() {
    if [ -n "$SUP_PID_FILE" ] && [ -f "$SUP_PID_FILE" ] && [ "$(tr -d '[:space:]' < "$SUP_PID_FILE")" = "$$" ]; then
        rm -f "$SUP_PID_FILE"
    fi
}

supervise() {
    local role="$1" instance="$2" suffix="$3" count="$4" log_file exit_code
    log_file="$(log_file_for "$instance")"
    export VIDEO_TURBO_WORKER_ROLE="$role"
    export VIDEO_TURBO_WORKER_INSTANCE="$instance"
    # Worker 名称 = 由 GPU 硬件生成的机器标识 + 该后缀（01、02… 或 dc），无需手工命名。
    export VIDEO_TURBO_WORKER_SUFFIX="$suffix"
    export VIDEO_TURBO_WORKER_COUNT="$count"
    export VIDEO_TURBO_WORKER_CONFIG="$WORKER_CONFIG"
    prepare_runtime_env

    if instance_pid "$instance" >/dev/null; then
        echo "$(L "$instance 已经在运行。" "$instance is already running.")" >&2
        exit 1
    fi
    trap sup_request_stop INT TERM
    trap sup_cleanup_pid_file EXIT
    rm -f "$(pid_file_for "$instance")"
    # 避免同时启动时覆盖已有守护进程的 PID。
    if ! (set -o noclobber; printf '%s\n' "$$" > "$(pid_file_for "$instance")") 2>/dev/null; then
        echo "$(L "$instance 正在启动。" "$instance is starting.")" >&2
        exit 1
    fi
    SUP_PID_FILE="$(pid_file_for "$instance")"

    while true; do
        "$VENV_PYTHON" -m video_turbo.worker.main --role "$role" --config "$WORKER_CONFIG" --log-file "$log_file" &
        SUP_CHILD_PID=$!
        if wait "$SUP_CHILD_PID"; then
            exit_code=0
        else
            exit_code=$?
        fi
        if [ "$SUP_STOP_REQUESTED" -eq 1 ]; then
            # wait 可能被信号提前打断，等子进程完成清理后再删除守护进程 PID。
            wait "$SUP_CHILD_PID" 2>/dev/null || true
            echo "$(L "$instance 已停止。" "$instance stopped.")" >&2
            exit 0
        fi
        SUP_CHILD_PID=""
        echo "$(L "$instance 已退出（状态码 ${exit_code}），15 秒后重启。" "$instance exited (status ${exit_code}); restarting in 15 seconds.")" >&2
        sleep 15 &
        SUP_CHILD_PID=$!
        if ! wait "$SUP_CHILD_PID" && [ "$SUP_STOP_REQUESTED" -eq 1 ]; then
            SUP_CHILD_PID=""
            echo "$(L "$instance 已停止。" "$instance stopped.")" >&2
            exit 0
        fi
        SUP_CHILD_PID=""
    done
}

start_instance() {
    local role="$1" instance="$2" suffix="$3" count="$4" pid log started attempt name
    if [ "$role" = restore ]; then name="$(role_label "$role")"; else name="$(L "字幕识别 $suffix" "subtitle recognition $suffix")"; fi
    if pid="$(instance_pid "$instance")"; then
        say "$(L "$name 已经在运行。" "$name is already running.")"
        return 0
    fi
    rm -f "$(pid_file_for "$instance")"
    log="$(log_file_for "$instance")"
    nohup bash "$SCRIPT_PATH" __supervise "$role" "$instance" "$suffix" "$count" >>"$log" 2>&1 </dev/null &
    started=$!
    for attempt in $(seq 1 50); do
        if pid="$(instance_pid "$instance")"; then
            say "$(L "已启动 ${name}；日志：logs/$(basename "$log")" "Started ${name}; log: logs/$(basename "$log")")"
            return 0
        fi
        if ! kill -0 "$started" 2>/dev/null; then
            wait "$started" || true
            echo "$(L "$name 启动失败，请查看日志：$log" "$name failed to start; see the log: $log")" >&2
            tail -n 5 "$log" >&2 2>/dev/null || true
            return 1
        fi
        sleep 0.1
    done
    kill -TERM "$started" 2>/dev/null || true
    wait "$started" || true
    echo "$(L "$name 启动超时，请查看日志：$log" "$name timed out while starting; see the log: $log")" >&2
    return 1
}

# ============================================================ 自检（首次启动时自动运行）

asr_self_test_key() { echo "$(asr_deps_hash) whisperjav=$(installed_whisperjav_version)"; }
restore_self_test_key() {
    echo "jasna=$(jasna_installed_tag) ltx=$(config_value jasna.ltx) fast=$(config_value jasna.ltx_fast)"
}

run_foreground_worker() {
    (
        prepare_runtime_env
        exec "$VENV_PYTHON" -m video_turbo.worker.main --config "$WORKER_CONFIG" "$@"
    )
}

# self_test 服务 [强制]：未通过过或安装变化后运行一次，结果记录在 work/.state。
self_test() {
    local role="$1" forced="${2:-0}" key marker status=0
    if [ "$role" = "asr" ]; then
        key="$(asr_self_test_key)"
    else
        key="$(restore_self_test_key)"
    fi
    marker="$STATE_DIR/self-test-$role"
    if [ "$forced" != "1" ] && [ "$(cat "$marker" 2>/dev/null || true)" = "$key" ]; then
        return 0
    fi
    if [ "$forced" != "1" ] && [ "$SKIP_SELF_TEST" = "1" ]; then
        warn "$(L "已跳过$(role_label "$role")首次自检（--skip-self-test），缺失的模型会在第一个任务时下载。" "Skipped the first self-test of the $(role_label "$role") (--skip-self-test); missing models are downloaded with the first job.")"
        return 0
    fi
    # 强制重检失败或中断时，不能沿用上次的成功标记。
    rm -f "$marker" || return $?
    LOG_TAG="$(L "$(role_label "$role")自检" "$(role_label "$role") self-test")"
    if [ "$(count_running all)" -gt 0 ]; then
        warn "$(L "本机有服务正在运行，自检会与之争用 GPU，可能变慢或显存不足。" "Services are running on this machine; the self-test competes for the GPU and may be slow or run out of VRAM.")"
    fi
    if [ "$role" = "asr" ]; then
        info "$(L "检查 GPU 并下载/验证 Qwen3-ASR、anime-whisper、两遍 VAD 和合并（首次下载时间较长）。" "Checking the GPU and downloading/verifying Qwen3-ASR, anime-whisper, both VADs and the merge (the first download takes a while).")"
        run_foreground_worker --self-test ${SELF_TEST_ARGS[@]+"${SELF_TEST_ARGS[@]}"} || status=$?
    else
        if [ "$(config_value jasna.ltx)" = "true" ]; then
            info "$(L "检查 GPU 视频编码器并用 LTX 处理 2 秒测试片；缺少的 LTX 模型会自动下载（约 15 GB）。" "Checking the GPU video encoder and processing a 2-second test clip with LTX; missing LTX models are downloaded automatically (about 15 GB).")"
        else
            info "$(L "检查显卡视频编码功能，并为这块显卡准备 BasicVSR++（约 15–60 分钟，请勿中断）。" "Checking the GPU video encoder and preparing BasicVSR++ for this GPU (about 15–60 minutes; don't interrupt).")"
        fi
        run_foreground_worker --self-test --restore ${SELF_TEST_ARGS[@]+"${SELF_TEST_ARGS[@]}"} || status=$?
    fi
    # start all 通过 || 汇总各服务错误，调用链中的 set -e 不会生效。
    if [ "$status" -ne 0 ]; then
        warn "$(L "自检失败（代码 ${status}），不会启动$(role_label "$role")。排查后重新运行：$SCRIPT_CMD self-test $role" "The self-test failed (code ${status}), so the $(role_label "$role") won't start. After fixing it, run: $SCRIPT_CMD self-test $role")"
        LOG_TAG="VideoTurbo"
        return "$status"
    fi
    mkdir -p "$STATE_DIR" || return $?
    echo "$key" > "$marker" || return $?
    info "$(L "自检通过。" "Self-test passed.")"
    LOG_TAG="VideoTurbo"
}

# ============================================================ 启动

require_config() {
    [ -f "$WORKER_CONFIG" ] || die "$(L "找不到 ${WORKER_CONFIG}，请先安装（$SCRIPT_CMD install）。" "${WORKER_CONFIG} not found; install first ($SCRIPT_CMD install).")"
    if [ -z "$(config_value worker_token)" ]; then
        die "$(L "${WORKER_CONFIG} 中 worker_token 未填写（从主程序控制台“设置 → 云主机 → 接入密钥”复制），或重新运行安装填写。" "worker_token in ${WORKER_CONFIG} is empty (copy it from Settings → Cloud hosts → Access key in the main app dashboard), or run the installer again to fill it in.")"
    fi
}

start_asr() {
    local count="$1" running index
    asr_installed || die "$(L "字幕识别服务未安装，请先运行：$SCRIPT_CMD install asr" "The subtitle recognition service isn't installed; run first: $SCRIPT_CMD install asr")"
    require_config
    detect_profile
    validate_asr_count "$count"
    self_test asr || return $?
    running="$(count_running asr)"
    if [ "$running" -gt 0 ] && [ "$running" -ne "$count" ]; then
        say "$(L "字幕识别正在以 $running 路运行，改为 $count 路需要重启（正在识别的任务会由主程序重新排队）。" "Subtitle recognition is running $running services; changing to $count needs a restart (jobs being recognized are queued again by the main app).")"
        confirm "$(L "确认重启字幕识别服务？" "Restart the subtitle recognition service?")" y || { say "$(L "已取消。" "Cancelled.")"; return 0; }
        stop_service asr || die "$(L "字幕识别服务未能全部停止，请稍后重试。" "Not all subtitle recognition services stopped; try again later.")"
    fi
    mkdir -p "$LOG_DIR" "$RUN_DIR"
    for index in $(seq 1 "$count"); do
        start_instance asr "$(printf '%s-%02d' "$PROFILE" "$index")" "$(printf '%02d' "$index")" "$count"
    done
    say "$(L "字幕识别：$count 路准确模式；日志：logs/worker.$PROFILE-*.log" "Subtitle recognition: $count services in accurate mode; logs: logs/worker.$PROFILE-*.log")"
    if [ "$PROFILE" = "5090" ]; then
        say "$(L "观察 nvidia-smi：若显存仍有至少 8 GB 空闲且 GPU 长期低于 90%，可改为 4 路（start asr --workers 4）。" "Watch nvidia-smi: if at least 8 GB of VRAM stays free and the GPU stays under 90%, you can use 4 services (start asr --workers 4).")"
    else
        say "$(L "观察 nvidia-smi：若出现显存不足，可减少路数（start asr --workers N）。" "Watch nvidia-smi: if VRAM runs out, use fewer services (start asr --workers N).")"
    fi
}

start_restore() {
    restore_installed || die "$(L "影片修复服务未安装或未启用，请先运行：$SCRIPT_CMD install restore" "The video restoration service isn't installed or enabled; run first: $SCRIPT_CMD install restore")"
    require_config
    detect_profile
    self_test restore || return $?
    mkdir -p "$LOG_DIR" "$RUN_DIR"
    start_instance restore "$PROFILE-dc" dc 1
    if [ "$(count_running asr)" -gt 0 ]; then
        say "$(L "字幕识别也在运行：两者串行使用 GPU，字幕优先，影片修复在字幕空闲时开始下一部。" "Subtitle recognition is also running: both share the GPU one at a time, subtitles first; restoration starts the next video when subtitles are idle.")"
    fi
}

default_asr_count() {
    local running
    if [ -n "$ASR_COUNT" ]; then
        echo "$ASR_COUNT"
        return
    fi
    running="$(count_running asr)"
    if [ "$running" -gt 0 ]; then
        echo "$running"
    else
        echo 3
    fi
}

cmd_start() {
    local service="${1:-}" failed=0
    if [ -z "$service" ]; then
        # 未指定时启动所有已安装的服务。
        if asr_installed && restore_installed; then
            service=all
        elif restore_installed; then
            service=restore
        elif asr_installed; then
            service=asr
        else
            die "$(L "还没有安装任何服务，请先运行：$SCRIPT_CMD install asr|restore|all" "No service is installed yet; run first: $SCRIPT_CMD install asr|restore|all")"
        fi
    fi
    detect_profile
    case "$service" in
        asr) start_asr "$(default_asr_count)" ;;
        restore) start_restore ;;
        all)
            start_asr "$(default_asr_count)" || failed=1
            start_restore || failed=1
            return "$failed"
            ;;
        *) die "$(L "未知服务：${service}（可选 asr、restore、all）" "Unknown service: ${service} (choose asr, restore or all)")" 2 ;;
    esac
}

cmd_stop() {
    local service="${1:-all}"
    case "$service" in asr|restore|all) ;; *) die "$(L "未知服务：${service}（可选 asr、restore、all）" "Unknown service: ${service} (choose asr, restore or all)")" 2 ;; esac
    stop_service "$service" "$FORCE"
}

# ============================================================ 安装

# 能否安装系统包：root，或当前用户有免密 sudo（不会停下来等密码，自动部署时不卡住）。
can_apt() {
    [ "$(id -u)" -eq 0 ] || { command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; }
}

run_apt() {
    if [ "$(id -u)" -eq 0 ]; then
        DEBIAN_FRONTEND=noninteractive apt-get "$@"
    else
        sudo -n DEBIAN_FRONTEND=noninteractive apt-get "$@"
    fi
}

# 安装缺失的系统包。参数：必需的包… -- 可选的包（失败时继续）…
apt_install() {
    local required=() optional=() seen_separator=0 package
    for package in "$@"; do
        if [ "$package" = "--" ]; then
            seen_separator=1
        elif [ "$seen_separator" = "1" ]; then
            optional+=("$package")
        else
            required+=("$package")
        fi
    done
    [ "${#required[@]}" -gt 0 ] || [ "${#optional[@]}" -gt 0 ] || return 0
    info "$(L "缺少系统组件：${required[*]:-}${optional[*]:+ ${optional[*]}}" "Missing system components: ${required[*]:-}${optional[*]:+ ${optional[*]}}")"
    if [ "${VIDEO_TURBO_SKIP_APT:-0}" = "1" ] || ! command -v apt-get >/dev/null 2>&1; then
        if [ "${#required[@]}" -gt 0 ]; then
            die "$(L "请先手动安装：${required[*]}（Ubuntu/Debian：apt-get install -y ${required[*]}）。" "Install these by hand first: ${required[*]} (Ubuntu/Debian: apt-get install -y ${required[*]}).")"
        fi
        info "$(L "未自动安装 ${optional[*]}，将继续使用可用工具或其他安装方式。" "${optional[*]} wasn't installed automatically; continuing with the available tools or other install methods.")"
        return 0
    fi
    if ! can_apt; then
        if [ "${#required[@]}" -gt 0 ]; then
            die "$(L "当前账号不是 root，也不能免密码使用 sudo，没法自动安装系统组件。请让管理员先运行：sudo apt-get install -y ${required[*]}" "This account isn't root and can't use sudo without a password, so system components can't be installed automatically. Ask an administrator to run: sudo apt-get install -y ${required[*]}")"
        fi
        info "$(L "当前用户不是 root，也没有免密 sudo，未安装 ${optional[*]}，将继续使用可用工具或其他安装方式。" "Not root and no passwordless sudo, so ${optional[*]} wasn't installed; continuing with the available tools or other install methods.")"
        return 0
    fi
    info "$(L "用 apt-get 安装缺少的系统组件。" "Installing missing system components with apt-get.")"
    if ! run_apt update; then
        [ "${#required[@]}" -eq 0 ] || die "$(L "apt-get update 失败；请手动安装：${required[*]} ${optional[*]:-}" "apt-get update failed; install by hand: ${required[*]} ${optional[*]:-}")"
        warn "$(L "apt-get update 失败，跳过可选组件：${optional[*]:-}" "apt-get update failed; skipping optional components: ${optional[*]:-}")"
        return 0
    fi
    if [ "${#required[@]}" -gt 0 ]; then
        run_apt install -y --no-install-recommends "${required[@]}" || die "$(L "安装系统组件失败：${required[*]}" "Failed to install system components: ${required[*]}")"
    fi
    # conda/自编译 Python 没有对应 apt 包；失败时继续，后续会用 ensurepip/uv 等方式补齐 pip。
    if [ "${#optional[@]}" -gt 0 ] && ! run_apt install -y --no-install-recommends "${optional[@]}"; then
        info "$(L "未能安装 ${optional[*]}，将继续使用可用工具或其他安装方式。" "Couldn't install ${optional[*]}; continuing with the available tools or other install methods.")"
    fi
}

install_env() {
    # pip/uv 使用测速选出的源，并清除云镜像预设的 extra-index。
    select_sources pypi torch hf github
    export PIP_INDEX_URL="$PYPI_INDEX_URL"
    export UV_DEFAULT_INDEX="$PYPI_INDEX_URL"
    # 不保留 pip/uv 下载缓存：CUDA 版 PyTorch 等 wheel 缓存会额外占用数 GB。
    export PIP_NO_CACHE_DIR=1
    export UV_NO_CACHE=1
    export HF_ENDPOINT="$HF_MIRROR_ENDPOINT"
    # 国内镜像可用时 hf-xet 仍可能绕过镜像访问 Xet CAS 并返回 401，默认禁用。
    export HF_HUB_DISABLE_XET="${VIDEO_TURBO_HF_HUB_DISABLE_XET:-1}"
    unset PIP_EXTRA_INDEX_URL UV_EXTRA_INDEX_URL UV_INDEX
    if ! interactive; then
        export VIDEO_TURBO_NONINTERACTIVE=1
    fi
}

# 两个服务共用：项目文件、GPU、Python、系统依赖、worker.yaml、虚拟环境。
install_common() {
    local want_asr="$1" want_restore="$2" required packages=() optional=()
    LOG_TAG="$(L "${PROFILE}安装" "${PROFILE} install")"
    info "$(L "项目目录：$PROJECT_ROOT" "Project folder: $PROJECT_ROOT")"
    info "$(L "Python 组件下载地址：$PYPI_INDEX_URL" "Python package index: $PYPI_INDEX_URL")"
    # 上传不完整时 pip 只会报 egg_base 'src' 不存在，提前给出明确提示。
    for required in pyproject.toml src/video_turbo/__init__.py tools/install_whisperjav.py \
        tools/configure_worker.py "config/worker.example.yaml"; do
        if [ ! -f "$PROJECT_ROOT/$required" ]; then
            echo "$(L "项目文件不完整：缺少 $PROJECT_ROOT/$required" "The project files are incomplete: $PROJECT_ROOT/$required is missing")" >&2
            die "$(L "请把安装包（dist/video-turbo）里的全部内容合并上传到项目根目录，包括 src、config、deploy、tools。" "Merge everything from the install package (dist/video-turbo) into the project root, including src, config, deploy and tools.")"
        fi
    done
    info "$(L "检查 NVIDIA GPU 和驱动。" "Checking the NVIDIA GPU and driver.")"
    command -v nvidia-smi >/dev/null 2>&1 || die "$(L "找不到 nvidia-smi，请确认这台机器有 NVIDIA 显卡（RTX $PROFILE）并已装好驱动。" "nvidia-smi not found; make sure this machine has an NVIDIA GPU (RTX $PROFILE) with the driver installed.")"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    info "$(L "检查 Python 版本：$SYSTEM_PYTHON" "Checking the Python version: $SYSTEM_PYTHON")"
    command -v "$SYSTEM_PYTHON" >/dev/null 2>&1 || die "$(L "找不到 ${SYSTEM_PYTHON}。需要 Python 3.11 或 3.12。" "${SYSTEM_PYTHON} not found. Python 3.11 or 3.12 is required.")"
    "$SYSTEM_PYTHON" -c 'import sys; assert (3, 11) <= sys.version_info[:2] < (3, 13), "需要 Python 3.11 或 3.12"'

    # 字幕：git（安装 WhisperJAV）、ffmpeg/ffprobe（识别）、libc++.so.1（TEN VAD）。
    # 影片修复：curl/wget/zstd（下载解压 Jasna）、libGL/libglib（Jasna 运行库）。
    # 两者都需要 venv/ensurepip。设置 VIDEO_TURBO_SKIP_APT=1 可禁止自动安装，仅提示缺失项。
    info "$(L "检查系统组件。" "Checking system components.")"
    if [ "$want_asr" = "1" ]; then
        command -v git >/dev/null 2>&1 || packages+=(git)
        if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
            packages+=(ffmpeg)
        fi
        # 与 Worker 运行时一致：不继承镜像的 LD_LIBRARY_PATH，确认系统能直接加载 libc++。
        if ! env -u LD_LIBRARY_PATH "$SYSTEM_PYTHON" -c 'import ctypes; ctypes.CDLL("libc++.so.1")' >/dev/null 2>&1; then
            packages+=(libc++1)
        fi
    fi
    if [ "$want_restore" = "1" ]; then
        local tool
        for tool in curl wget zstd; do
            command -v "$tool" >/dev/null 2>&1 || packages+=("$tool")
        done
        # aria2 可选：安装失败或无 apt 权限时，下载器回退到 wget。
        command -v aria2c >/dev/null 2>&1 || optional+=(aria2)
        if command -v ldconfig >/dev/null 2>&1; then
            ldconfig -p 2>/dev/null | grep -q 'libGL.so.1' || packages+=(libgl1)
            ldconfig -p 2>/dev/null | grep -q 'libglib-2.0.so.0' || packages+=(libglib2.0-0)
        fi
    fi
    if ! "$SYSTEM_PYTHON" -c 'import ensurepip, venv' >/dev/null 2>&1; then
        optional+=("python$("$SYSTEM_PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')-venv")
    fi
    apt_install "${packages[@]+"${packages[@]}"}" -- "${optional[@]+"${optional[@]}"}"

    # 在耗时的依赖安装前填写配置：首次安装复制模板，交互式终端中依次询问
    # server_url、worker_token 和模型目录，回车使用默认值。Worker 名称由显卡自动生成。
    info "$(L "配置 ${WORKER_CONFIG}。" "Configuring ${WORKER_CONFIG}.")"
    local configure=(
        "$PROJECT_ROOT/tools/configure_worker.py"
        --config "$WORKER_CONFIG"
        --template "$PROJECT_ROOT/config/worker.example.yaml"
        --default-download-root "$DEFAULT_MODEL_DIR"
    )
    if interactive; then
        "$SYSTEM_PYTHON" "${configure[@]}"
    else
        "$SYSTEM_PYTHON" "${configure[@]}" --non-interactive
    fi
    MODEL_DIR="$("$SYSTEM_PYTHON" "${configure[@]}" --print-download-root)"

    if [ ! -x "$VENV_PYTHON" ]; then
        info "$(L "创建程序运行环境：$VENV_DIR" "Creating the program environment: $VENV_DIR")"
        "$SYSTEM_PYTHON" -m venv "$VENV_DIR" || info "$(L "程序运行环境没有完整创建，正在尝试补装安装工具 pip。" "The program environment wasn't fully created; trying to add the pip installer.")"
    fi
    [ -x "$VENV_PYTHON" ] || die "$(L "无法创建程序运行环境。Ubuntu 可先安装与 Python 版本对应的 python3.X-venv 后重试。" "Can't create the program environment. On Ubuntu, install the python3.X-venv package matching your Python version and retry.")"
    if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
        info "$(L "运行环境缺少安装工具 pip，正在自动修复。" "The program environment is missing pip; fixing it automatically.")"
        if "$VENV_PYTHON" -m ensurepip --upgrade; then
            info "$(L "已通过 ensurepip 修复。" "Fixed with ensurepip.")"
        elif "$SYSTEM_PYTHON" -m pip --version >/dev/null 2>&1 \
            && "$SYSTEM_PYTHON" -m pip --python "$VENV_PYTHON" install --upgrade pip wheel; then
            info "$(L "已通过系统 pip 修复。" "Fixed with the system pip.")"
        elif command -v uv >/dev/null 2>&1 && uv pip install --python "$VENV_PYTHON" pip wheel; then
            info "$(L "已通过 uv 修复。" "Fixed with uv.")"
        else
            die "$(L "无法自动安装 pip。请以 root 或 sudo 执行 apt-get update && apt-get install -y python3-venv 后重试。" "Can't install pip automatically. As root or with sudo, run apt-get update && apt-get install -y python3-venv, then retry.")"
        fi
    fi
    info "$(L "更新安装工具。" "Updating the installer tools.")"
    "$VENV_PYTHON" -m pip install --upgrade pip wheel
    mkdir -p "$LOG_DIR" "$RUN_DIR"
}

install_asr() {
    LOG_TAG="$(L "字幕安装" "subtitle install")"
    info "$(L "模型下载地址：${HF_MIRROR_ENDPOINT}" "Model download address: ${HF_MIRROR_ENDPOINT}")"
    # 镜像内的 CUDA Toolkit 和预装 PyTorch 不参与；按宿主机驱动选择 PyTorch 的 cu128/cu126 轮子。
    # 驱动不满足（如 5090 驱动低于 570）时在耗时的依赖安装前退出。
    info "$(L "检查显卡驱动，选择合适的组件版本。" "Checking the GPU driver to choose matching components.")"
    "$SYSTEM_PYTHON" "$PROJECT_ROOT/tools/install_whisperjav.py" --check-cuda
    info "$(L "安装识别需要的组件；第一次下载时间较长。" "Installing recognition components; the first download takes a while.")"
    install_locked asr
    info "$(L "安装 WhisperJAV 独立环境（需要 git、ffmpeg、ffprobe）。" "Installing the WhisperJAV environment (needs git, ffmpeg, ffprobe).")"
    "$VENV_PYTHON" "$PROJECT_ROOT/tools/install_whisperjav.py"
    info "$(L "准备持久化模型目录：$MODEL_DIR" "Preparing the persistent model folder: $MODEL_DIR")"
    mkdir -p "$MODEL_DIR"
    [ -w "$MODEL_DIR" ] || die "$(L "模型目录不可写：$MODEL_DIR" "The model folder isn't writable: $MODEL_DIR")"
    write_state "$(asr_state_file)" "version=$(project_version)" "deps=$(asr_deps_hash)"
    info "$(L "字幕识别服务安装完成，模型存放目录：$MODEL_DIR" "Subtitle recognition service installed; models are stored in: $MODEL_DIR")"
}

nvenc_test() {
    env -u LD_LIBRARY_PATH -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD "$1" -hide_banner -loglevel error \
        -f lavfi -i testsrc2=duration=1:size=1280x720 -c:v hevc_nvenc -f null -
}

# 云端首次部署先执行此命令；在字幕安装、Jasna 下载、Python 环境准备之前验证编码。
restore_deploy_precheck() {
    local encoder="" candidate encoders
    if [ -x "$VENV_PYTHON" ] && [ -x "$JASNA_ROOT/current/jasna" ] \
        && { [ -f "$PROJECT_ROOT/.video-turbo-deployed-restore" ] || [ -f "$(restore_state_file)" ]; }; then
        info "$(L "影片修复已安装过，跳过首次安装的 GPU 视频编码（NVENC）检查。" "Restoration was installed before; skipping the first-install GPU video encoding (NVENC) check.")"
        return 0
    fi
    for candidate in "$JASNA_ROOT/current/tools/ffmpeg" "$(command -v ffmpeg || true)" /usr/bin/ffmpeg; do
        [ -x "$candidate" ] || continue
        encoders="$(env -u LD_LIBRARY_PATH -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD \
            "$candidate" -hide_banner -encoders 2>/dev/null)" || continue
        if [[ "$encoders" == *hevc_nvenc* ]]; then
            encoder="$candidate"
            break
        fi
    done
    if [ -z "$encoder" ]; then
        info "$(L "缺少支持 NVENC 的 ffmpeg，先尝试安装系统 ffmpeg。" "No ffmpeg with NVENC support; trying to install the system ffmpeg first.")"
        if [ "${VIDEO_TURBO_SKIP_APT:-0}" = "1" ] || ! command -v apt-get >/dev/null 2>&1 || ! can_apt; then
            die "$(L "无法安装 NVENC 检测工具：需要 apt-get 和 root 或免密 sudo，请手动安装支持 hevc_nvenc 的 ffmpeg。" "Can't install the NVENC check tool: apt-get and root or passwordless sudo are required; install an ffmpeg with hevc_nvenc by hand.")"
        fi
        run_apt update && run_apt install -y --no-install-recommends --reinstall ffmpeg \
            || die "$(L "安装 NVENC 检测工具 ffmpeg 失败。" "Failed to install the NVENC check tool ffmpeg.")"
        hash -r
        # /usr/bin 优先，避免 PATH 中旧的自编译 ffmpeg 遮住刚安装的系统版本。
        for candidate in /usr/bin/ffmpeg "$(command -v ffmpeg || true)"; do
            [ -x "$candidate" ] || continue
            encoders="$(env -u LD_LIBRARY_PATH -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD \
                "$candidate" -hide_banner -encoders 2>/dev/null)" || continue
            if [[ "$encoders" == *hevc_nvenc* ]]; then
                encoder="$candidate"
                break
            fi
        done
        [ -n "$encoder" ] || die "$(L "安装后 ffmpeg 仍不支持 hevc_nvenc，无法检测 NVENC 编码能力。" "ffmpeg still doesn't support hevc_nvenc after installing, so NVENC can't be checked.")"
    fi
    info "$(L "首次安装影片修复：实际测试 GPU 视频编码（NVENC，${encoder}）。" "First restoration install: testing GPU video encoding (NVENC, ${encoder}).")"
    nvenc_test "$encoder" || die "$(L "GPU 视频编码器（NVENC）不可用。请联系平台开放视频编码能力（NVIDIA_DRIVER_CAPABILITIES=all）或更换机器。" "The GPU video encoder (NVENC) is unavailable. Ask the platform to enable video encoding (NVIDIA_DRIVER_CAPABILITIES=all) or use another machine.")"
    info "$(L "GPU 视频编码（NVENC）测试通过。" "GPU video encoding (NVENC) test passed.")"
}

install_restore() {
    local driver driver_major nvenc_help part_dir parts="" part before extracted tag="" release api license ltx_flag=""
    local part_paths=()
    LOG_TAG="$(L "影片修复安装" "restoration install")"
    case "$JASNA_ROOT$PROJECT_ROOT" in
        *[!A-Za-z0-9._/-]*) die "$(L "Jasna 要求路径只含英文字母、数字和 ._-/：$JASNA_ROOT" "Jasna requires a path with only English letters, digits and ._-/: $JASNA_ROOT")" ;;
    esac
    case "$JASNA_ROOT" in
        /workspace*|/mnt/*) warn "$(L "Jasna 不能从网络盘运行（如 RunPod 的 /workspace），请确认 $JASNA_ROOT 在本机磁盘。" "Jasna can't run from a network disk (such as /workspace on RunPod); make sure $JASNA_ROOT is on a local disk.")" ;;
    esac
    info "$(L "检查 NVIDIA 驱动（Jasna 需要 580 及以上）。" "Checking the NVIDIA driver (Jasna needs 580 or newer).")"
    driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d ' ')"
    driver_major="${driver%%.*}"
    case "$driver_major" in ''|*[!0-9]*) die "$(L "无法读取驱动版本：$driver" "Can't read the driver version: $driver")" ;; esac
    [ "$driver_major" -ge 580 ] || die "$(L "驱动 $driver 低于 580，Jasna 无法运行。请更换驱动更新的机器。" "Driver $driver is older than 580, so Jasna can't run. Use a machine with a newer driver.")"
    if [ -n "${NVIDIA_DRIVER_CAPABILITIES:-}" ] && [ "${NVIDIA_DRIVER_CAPABILITIES}" != "all" ]; then
        warn "$(L "NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES}，Jasna 需要 all 才能读写视频（在租用平台的模板或云主机设置中修改）。" "NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES}; Jasna needs all to read and write video (change it in the rental platform's template or the cloud host settings).")"
    fi
    nvenc_help="$(L "GPU 视频编码器（NVENC）不可用，Jasna 无法保存视频。常见原因：容器未开放视频能力（平台需设置 NVIDIA_DRIVER_CAPABILITIES=all），或宿主机限制了编码器。请联系平台或更换机器；本机仍可运行字幕识别服务。" "The GPU video encoder (NVENC) is unavailable, so Jasna can't save video. Common causes: the container doesn't expose video capabilities (the platform must set NVIDIA_DRIVER_CAPABILITIES=all), or the host restricts the encoder. Contact the platform or use another machine; this machine can still run subtitle recognition.")"
    # 在下载约 5 GB 发布包之前，先用系统 ffmpeg 检查 NVENC；系统 ffmpeg 不含 NVENC 时跳过，下载后再检查。
    if [ "${VIDEO_TURBO_JASNA_SKIP_PRECHECK:-0}" != "1" ] && command -v ffmpeg >/dev/null 2>&1 \
        && env -u LD_LIBRARY_PATH ffmpeg -hide_banner -encoders 2>/dev/null | grep -q hevc_nvenc; then
        info "$(L "下载前检查 GPU 视频编码器（系统 ffmpeg）。" "Checking the GPU video encoder before downloading (system ffmpeg).")"
        nvenc_test ffmpeg || die "$(L "${nvenc_help}（未下载 Jasna。跳过预检可设置 VIDEO_TURBO_JASNA_SKIP_PRECHECK=1）" "${nvenc_help} (Jasna not downloaded. To skip the pre-check, set VIDEO_TURBO_JASNA_SKIP_PRECHECK=1)")"
    else
        info "$(L "系统 ffmpeg 不可用或不含 NVENC 编码器，跳过下载前的编码器检查（下载后仍会检查）。" "The system ffmpeg is unavailable or lacks NVENC; skipping the pre-download encoder check (it's still checked after downloading).")"
    fi

    info "$(L "安装影片修复服务需要的组件（不含字幕识别模型）。" "Installing the restoration service components (without the subtitle models).")"
    install_locked restore

    mkdir -p "$JASNA_ROOT/downloads" "$JASNA_ROOT/config"
    chmod 700 "$JASNA_ROOT/config"
    if [ -n "$JASNA_ARCHIVE_DIR" ]; then
        info "$(L "使用已下载的分卷：$JASNA_ARCHIVE_DIR" "Using the downloaded parts: $JASNA_ARCHIVE_DIR")"
        part_dir="$JASNA_ARCHIVE_DIR"
    elif [ "$JASNA_UPDATE" != "1" ] && [ "$JASNA_VERSION" = "latest" ] && [ -x "$JASNA_ROOT/current/jasna" ]; then
        tag="$(jasna_installed_tag)"
        info "$(L "未开启 Jasna 自动更新（VIDEO_TURBO_JASNA_UPDATE=0），保留已安装的 ${tag}。" "Jasna auto-update is off (VIDEO_TURBO_JASNA_UPDATE=0); keeping the installed ${tag}.")"
    else
        if [ "$JASNA_VERSION" = "latest" ]; then
            api="repos/Kruk2/jasna/releases/latest"
        else
            api="repos/Kruk2/jasna/releases/tags/$JASNA_VERSION"
        fi
        info "$(L "查询 Jasna 发布：https://api.github.com/$api" "Checking Jasna releases: https://api.github.com/$api")"
        release="$(github_api "$api")" || die "$(L "无法访问 GitHub API。可设置 VIDEO_TURBO_JASNA_ARCHIVE_DIR 使用手动下载的分卷。" "Can't reach the GitHub API. Set VIDEO_TURBO_JASNA_ARCHIVE_DIR to use manually downloaded parts.")"
        tag="$(printf '%s' "$release" | grep -o '"tag_name": *"[^"]*"' | head -n1 | sed 's/.*"\([^"]*\)"$/\1/' || true)"
        [ -n "$tag" ] || die "$(L "GitHub 发布信息中没有版本号。" "The GitHub release has no version number.")"
        part_dir="$JASNA_ROOT/downloads/${tag:-unknown}"
    fi
    if jasna_is_version "$tag"; then
        info "$(L "Jasna $tag 已是最新，跳过下载。" "Jasna $tag is up to date; skipping the download.")"
    else
        if [ -z "$JASNA_ARCHIVE_DIR" ]; then
            info "$(L "下载 Jasna ${tag}（分段下载，断了能接着下，检查完整后解压）。" "Downloading Jasna ${tag} (in resumable parts, unpacked after the integrity check).")"
            [ -f "$PROJECT_ROOT/tools/download_jasna.py" ] || die "$(L "缺少 tools/download_jasna.py，请上传完整安装包。" "tools/download_jasna.py is missing; upload the complete install package.")"
            parts="$(printf '%s' "$release" | "$SYSTEM_PYTHON" "$PROJECT_ROOT/tools/download_jasna.py" \
                --output-dir "$part_dir" --proxy "$JASNA_GITHUB_PROXY")" \
                || die "$(L "Jasna 下载或完整性检查失败；已下载的部分会保留，重新安装就能接着下。" "The Jasna download or integrity check failed; downloaded parts are kept, and reinstalling resumes.")"
        else
            # 手动下载的分卷保持兼容；在线下载只解压发布清单中已通过校验的文件。
            # shellcheck disable=SC2012
            parts="$(ls "$part_dir"/jasna-linux-nvidia-*.part[0-9][0-9][0-9] 2>/dev/null | sort -V || true)"
        fi
        [ -n "$parts" ] || die "$(L "在 $part_dir 中找不到 jasna-linux-nvidia-*.part* 分卷。" "No jasna-linux-nvidia-*.part* parts in $part_dir.")"
        while IFS= read -r part; do part_paths+=("$part"); done <<< "$parts"
        info "$(L "解压到 ${JASNA_ROOT}。" "Unpacking to ${JASNA_ROOT}.")"
        before="$(ls -d "$JASNA_ROOT"/jasna-linux-nvidia-*/ 2>/dev/null || true)"
        cat "${part_paths[@]}" | zstd -dc | tar -xpf - --same-owner -C "$JASNA_ROOT"
        extracted="$(ls -d "$JASNA_ROOT"/jasna-linux-nvidia-*/ 2>/dev/null | sort -V | tail -n1 || true)"
        [ -n "$extracted" ] || die "$(L "解压后找不到 jasna-linux-nvidia-* 目录。" "No jasna-linux-nvidia-* folder after unpacking.")"
        extracted="${extracted%/}"
        [ -x "$extracted/jasna" ] || die "$(L "解压目录中没有可执行的 jasna：$extracted" "No executable jasna in the unpacked folder: $extracted")"
        ln -sfn "$extracted" "$JASNA_ROOT/current"
        printf '%s\n' "${tag:-$(basename "$extracted")}" > "$JASNA_ROOT/installed-tag"
        info "$(L "当前版本：$extracted" "Current version: $extracted")"
        if [ -n "$before" ]; then
            info "$(L "旧版本目录仍保留，确认新版本正常后可手动删除。升级前可把旧版本的 model_weights 复制到新版本，免得再等 15–60 分钟重新准备。" "The old version folder is kept; delete it by hand once the new version works. Before upgrading, you can copy the old model_weights into the new version to skip another 15–60 minutes of preparation.")"
        fi
    fi

    info "$(L "检查 GPU 视频编码器。" "Checking the GPU video encoder.")"
    nvenc_test "$JASNA_ROOT/current/tools/ffmpeg" || die "$nvenc_help"

    license="${VIDEO_TURBO_JASNA_LICENSE:-}"
    if [ -z "$license" ] && interactive; then
        if [ "$(config_value jasna.ltx)" = "true" ]; then
            printf "$(L '输入 Jasna 许可证码重新激活 LTX（直接回车保持当前激活）：' 'Enter the Jasna license code to reactivate LTX (press Enter to keep the current activation): ')" >&2
        else
            printf "$(L '输入 Jasna 许可证码以启用 LTX（直接回车跳过，只使用免费 BasicVSR++）：' 'Enter the Jasna license code to enable LTX (press Enter to skip and use only the free BasicVSR++): ')" >&2
        fi
        read -r license || license=""
    fi
    if [ -n "$license" ]; then
        # 同一许可证码最多 3 台设备同时使用；租用 GPU、容器内的 Linux 只在 Jasna 运行时计入，关机后释放。
        info "$(L "激活许可证（需要联网；租用 GPU 只在 Jasna 运行时占用 3 台名额中的 1 台）。完成后可能停顿约 1 分钟，请勿按 Ctrl+C。" "Activating the license (needs internet; a rented GPU uses 1 of the 3 seats only while Jasna runs). It may pause for about a minute afterwards; don't press Ctrl+C.")"
        if (cd "$JASNA_ROOT/current" && env -u LD_LIBRARY_PATH -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD \
            XDG_CONFIG_HOME="$JASNA_ROOT/config" ./jasna --license-code "$license"); then
            ltx_flag="true"
            info "$(L "许可证已激活，LTX 已启用；首次启动影片修复服务时会自动下载 LTX 模型（约 15 GB）。" "License activated and LTX enabled; the LTX model (about 15 GB) is downloaded automatically when the restoration service first starts.")"
        else
            ltx_flag="false"
            warn "$(L "许可证激活失败，暂只启用 BasicVSR++。可稍后重新安装影片修复服务。" "License activation failed; only BasicVSR++ is enabled for now. You can reinstall the restoration service later.")"
        fi
    fi
    if [ -n "$ltx_flag" ]; then
        "$SYSTEM_PYTHON" "$PROJECT_ROOT/tools/configure_jasna.py" --config "$WORKER_CONFIG" --ltx "$ltx_flag"
    else
        "$SYSTEM_PYTHON" "$PROJECT_ROOT/tools/configure_jasna.py" --config "$WORKER_CONFIG"
    fi
    write_state "$(restore_state_file)" "version=$(project_version)" "deps=$(restore_deps_hash)" \
        "jasna=$(jasna_installed_tag)"
    info "$(L "影片修复服务安装完成。原片备份文件夹在主程序的 docker-compose.yml 里设置。" "Video restoration service installed. The original backup folder is set in the main app's docker-compose.yml.")"
}

cmd_install() {
    local service="${1:-}" want_asr=0 want_restore=0 restart=() role
    case "$service" in
        asr) want_asr=1 ;;
        restore) want_restore=1 ;;
        all) want_asr=1; want_restore=1 ;;
        '') die "$(L "请指定要安装的服务：$SCRIPT_CMD install asr|restore|all" "Choose the services to install: $SCRIPT_CMD install asr|restore|all")" 2 ;;
        *) die "$(L "未知服务：${service}（可选 asr、restore、all）" "Unknown service: ${service} (choose asr, restore or all)")" 2 ;;
    esac
    detect_profile
    install_env
    # 安装会替换依赖和 Jasna，先停止对应服务，装完后恢复运行。
    for role in asr restore; do
        if { [ "$role" = asr ] && [ "$want_asr" = 1 ]; } || { [ "$role" = restore ] && [ "$want_restore" = 1 ]; }; then
            if [ "$(count_running "$role")" -gt 0 ]; then
                say "$(L "$(role_label "$role")正在运行，安装前需要先停止（正在处理的任务会中断：字幕由主程序重新排队，影片修复保留进度续跑）。" "The $(role_label "$role") is running and must stop before installing (running jobs are interrupted: subtitles are queued again by the main app, restorations keep their progress and resume).")"
                confirm "$(L "停止并在安装完成后重新启动？" "Stop it and restart after the installation?")" y || die "$(L "已取消安装。" "Installation cancelled.")"
                stop_service "$role" 1
                restart+=("$role")
            fi
        fi
    done
    install_common "$want_asr" "$want_restore"
    [ "$want_asr" = 0 ] || install_asr
    [ "$want_restore" = 0 ] || install_restore
    LOG_TAG="VideoTurbo"
    say
    say "$(L "安装完成。下一步：" "Installed. Next:")"
    say "$(L "1. 确认 ${WORKER_CONFIG} 中的 server_url、worker_token 已填写（重新安装可再次交互修改）。" "1. Make sure server_url and worker_token are filled in ${WORKER_CONFIG} (run the installer again to change them).")"
    say "$(L "2. 启动：$SCRIPT_CMD start ${service}（首次启动会先自检并下载模型）。" "2. Start: $SCRIPT_CMD start ${service} (the first start runs a self-test and downloads models).")"
    for role in "${restart[@]+"${restart[@]}"}"; do
        say "$(L "恢复运行安装前已启动的$(role_label "$role")。" "Restarting the $(role_label "$role") that was running before the installation.")"
        cmd_start "$role"
    done
}

# ============================================================ 清理

cmd_clean() {
    local args=(--config "$WORKER_CONFIG") skip listing running
    for skip in "${CLEAN_SKIPS[@]+"${CLEAN_SKIPS[@]}"}"; do
        args+=(--skip "$skip")
    done
    [ -x "$VENV_PYTHON" ] || die "$(L "还没安装程序（找不到 ${VENV_PYTHON}），请先安装。" "The program isn't installed (${VENV_PYTHON} not found); install it first.")"
    say "$(L "检查可清理的内容（只列出，不删除）……" "Checking what can be cleaned up (list only, nothing is deleted)……")"
    listing="$("$VENV_PYTHON" "$PROJECT_ROOT/tools/cleanup_worker.py" "${args[@]}" 2>&1 || true)"
    printf '%s\n' "$listing" | sed -e '/请先停止所有服务/,$d' -e '/Stop all services first/,$d'
    if printf '%s\n' "$listing" | grep -qE '^(合计|Total) 0 B'; then
        say "$(L "没有需要清理的内容。" "Nothing to clean up.")"
        return 0
    fi
    running="$(running_instances all | tr '\n' ' ')"
    say
    say "$(L "风险提示：" "Warning:")"
    say "$(L "  - 删除后不可恢复；已下载完成的模型、配置、日志和 Jasna 许可证不会删除。" "  - Deleted items can't be recovered; downloaded models, config, logs and the Jasna license are kept.")"
    say "$(L "  - 删除影片修复中断任务后，同一任务回到本机需要重新下载原片、重新修复。" "  - After deleting interrupted restoration jobs, the same job on this machine downloads and restores the original again.")"
    if [ -n "$running" ]; then
        say "$(L "  - 以下服务正在运行，清理前会被强制停止：$running" "  - These services are running and will be stopped before cleaning: $running")"
        say "$(L "    正在识别的字幕任务会中断并由主程序重新排队；正在修复的影片修复任务会中断，其本地进度将被删除。" "    Subtitle jobs being recognized are interrupted and queued again by the main app; restoration jobs being processed are interrupted and their local progress is deleted.")"
    fi
    if ! confirm "$(L "确认清理？" "Clean up?")" n; then
        say "$(L "已取消清理。" "Cleanup cancelled.")"
        return 0
    fi
    if [ -n "$running" ]; then
        stop_service all 1 || true
    fi
    "$VENV_PYTHON" "$PROJECT_ROOT/tools/cleanup_worker.py" "${args[@]}" --yes
    if [ -n "$running" ]; then
        say "$(L "服务已停止，需要时运行：$SCRIPT_CMD start" "Services stopped; when needed run: $SCRIPT_CMD start")"
    fi
}

# ============================================================ 状态与日志

instance_line() {
    local instance="$1" pid name
    name="$instance"
    if pid="$(instance_pid "$instance")"; then
        say "$(L "  ${name}：运行中 · 日志 logs/worker.$instance.log" "  ${name}: running · log logs/worker.$instance.log")"
    else
        say "$(L "  ${name}：未运行" "  ${name}: not running")"
    fi
}

cmd_status() {
    local instance any=0 role
    detect_profile_quiet
    say "$(L "== 服务 ==" "== Services ==")"
    for role in asr restore; do
        say "$(L "$(role_label "$role")：$(count_running "$role") 路运行中" "$(role_label "$role"): $(count_running "$role") running")"
        for instance in $(all_instances); do
            [ "$(role_of "$instance")" = "$role" ] || continue
            any=1
            instance_line "$instance"
        done
    done
    [ "$any" = 1 ] || say "$(L "  （还没有启动过）" "  (never started)")"
    say
    say "$(L "== 安装 ==" "== Installation ==")"
    say "$(L "字幕识别：$(asr_install_summary)" "Subtitle recognition: $(asr_install_summary)")"
    say "$(L "影片修复：$(restore_install_summary)" "Video restoration: $(restore_install_summary)")"
    say "$(L "配置：$WORKER_CONFIG$([ -f "$WORKER_CONFIG" ] || echo '（不存在）')" "Config: $WORKER_CONFIG$([ -f "$WORKER_CONFIG" ] || echo ' (missing)')")"
    if [ -f "$WORKER_CONFIG" ]; then
        say "  server_url=$(config_value server_url)  worker_token=$([ -n "$(config_value worker_token)" ] && L 已填写 set || L 未填写 empty)"
    fi
    say
    say "== GPU =="
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=name,driver_version,utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null || say "$(L "nvidia-smi 查询失败" "nvidia-smi query failed")"
    else
        say "$(L "找不到 nvidia-smi" "nvidia-smi not found")"
    fi
    say
    say "$(L "== 磁盘 ==" "== Disk ==")"
    df -h "$PROJECT_ROOT" 2>/dev/null | tail -n +2 | awk "$(L '{print "项目目录：已用 "$3" / "$2"，剩余 "$4}' '{print "Project folder: used "$3" / "$2", free "$4}')" || true
    local models
    models="$(config_value asr.download_root)"
    if [ -n "$models" ] && [ -d "$models" ]; then
        df -h "$models" 2>/dev/null | tail -n +2 | awk -v dir="$models" "$(L '{print "模型目录 "dir"：剩余 "$4}' '{print "Model folder "dir": free "$4}')" || true
    fi
}

detect_profile_quiet() {
    [ -n "$PROFILE" ] && return 0
    local gpu=""
    command -v nvidia-smi >/dev/null 2>&1 && gpu="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1 || true)"
    case "$gpu" in *3090*) PROFILE=3090 ;; *4090*) PROFILE=4090 ;; *5090*) PROFILE=5090 ;; esac
    return 0
}

# logs [实例|asr|restore]：asr/restore 取该服务的第一个实例。
cmd_logs() {
    local target="${1:-}" instance="" candidate file
    case "$target" in
        ''|asr|restore)
            for candidate in $(all_instances); do
                if [ -z "$target" ] || [ "$(role_of "$candidate")" = "$target" ]; then
                    instance="$candidate"
                    break
                fi
            done
            if [ -z "$instance" ]; then
                file="$(ls -t "$LOG_DIR"/worker*.log 2>/dev/null | head -n1 || true)"
            else
                file="$(log_file_for "$instance")"
            fi
            ;;
        *) file="$(log_file_for "$target")" ;;
    esac
    [ -n "$file" ] && [ -f "$file" ] || die "$(L "找不到日志${target:+：$target}。可用日志：" "Log not found${target:+: $target}. Available logs: ")$(ls "$LOG_DIR" 2>/dev/null | tr '\n' ' ')"
    say "== $file =="
    if [ "$FOLLOW" = "1" ]; then
        tail -n "$LOG_LINES" -f "$file"
    else
        tail -n "$LOG_LINES" "$file"
    fi
}

# ============================================================ 交互菜单

menu_choose() {
    # menu_choose 标题 选项… → REPLY 为选择的编号（0 返回）
    local title="$1" index=1
    shift
    say
    say "---- $title ----"
    for option in "$@"; do
        say "  $index. $option"
        index=$((index + 1))
    done
    say "$(L "  0. 返回" "  0. Back")"
    printf "$(L '请选择：' 'Choose: ')" >&2
    read -r REPLY || REPLY=0
}

menu_install() {
    local latest
    say "$(L "正在查询 Jasna 最新版本……" "Checking the latest Jasna version……")"
    latest="$(jasna_latest_tag)"
    say
    say "$(L "代码版本：v$(project_version)" "Code version: v$(project_version)")"
    say "$(L "字幕识别服务：$(asr_install_summary)" "Subtitle recognition service: $(asr_install_summary)")"
    say "$(L "影片修复服务：" "Video restoration service: ")$(restore_install_summary "$latest")"
    menu_choose "$(L "安装 / 更新" "Install / update")" "$(L "字幕识别服务（WhisperJAV，可多路并行）" "Subtitle recognition service (WhisperJAV, several in parallel)")" "$(L "影片修复服务（Jasna，需要驱动 ≥ 580 和 NVENC）" "Video restoration service (Jasna, needs driver ≥ 580 and NVENC)")" "$(L "两个都安装" "Install both")"
    case "$REPLY" in
        1) cmd_install asr ;;
        2) cmd_install restore ;;
        3) cmd_install all ;;
        *) return 0 ;;
    esac
}

menu_main() {
    local choice count
    detect_profile
    while true; do
        say
        say "$(L "==== VideoTurbo 服务管理（RTX $PROFILE · $(project_version)） ====" "==== VideoTurbo service manager (RTX $PROFILE · $(project_version)) ====")"
        say "$(L "  字幕识别：" "  Subtitle recognition: ")$(asr_installed && L "已安装 · $(count_running asr) 路运行中" "installed · $(count_running asr) running" || L 未安装 "not installed")"
        say "$(L "  影片修复：    " "  Video restoration: ")$(restore_installed && L "已安装 · $(count_running restore) 个运行中" "installed · $(count_running restore) running" || L 未安装 "not installed")"
        say "$(L "  1. 安装 / 更新" "  1. Install / update")"
        say "$(L "  2. 启动" "  2. Start")"
        say "$(L "  3. 停止" "  3. Stop")"
        say "$(L "  4. 清理" "  4. Clean up")"
        say "$(L "  5. 状态与日志" "  5. Status and logs")"
        say "$(L "  0. 退出" "  0. Exit")"
        printf "$(L '请选择：' 'Choose: ')" >&2
        read -r choice || choice=0
        case "$choice" in
            1) (menu_install) || true; pause ;;
            2)
                say
                say "$(L "字幕识别服务：" "Subtitle recognition service: ")$(asr_installed && L "已安装，$(count_running asr) 路运行中" "installed, $(count_running asr) running" || L 未安装 "not installed")"
                say "$(L "影片修复服务：" "Video restoration service: ")$(restore_installed && L "已安装，$(count_running restore) 个运行中" "installed, $(count_running restore) running" || L 未安装 "not installed")"
                say "$(L "两个都启动时串行使用 GPU：字幕优先，影片修复在字幕空闲时开始下一部；影片修复进行中字幕暂停领取。" "When both run they share the GPU one at a time: subtitles first, restoration starts the next video when subtitles are idle; subtitles pause claiming while a restoration runs.")"
                menu_choose "$(L "启动" "Start")" "$(L "字幕识别服务" "Subtitle recognition service")" "$(L "影片修复服务" "Video restoration service")" "$(L "两个都启动" "Start both")"
                choice="$REPLY"
                case "$choice" in
                    1|3)
                        ask "$(L "字幕并行路数（1～$(max_asr_workers)）" "Parallel subtitle services (1–$(max_asr_workers))")" "$(default_asr_count)"
                        count="$REPLY"
                        ;;
                esac
                case "$choice" in
                    1) (ASR_COUNT="$count"; cmd_start asr) || true ;;
                    2) (cmd_start restore) || true ;;
                    3) (ASR_COUNT="$count"; cmd_start all) || true ;;
                esac
                pause
                ;;
            3)
                say
                say "$(L "字幕识别服务：$(count_running asr) 路运行中；影片修复服务：$(count_running restore) 个运行中" "Subtitle recognition: $(count_running asr) running; video restoration: $(count_running restore) running")"
                menu_choose "$(L "停止" "Stop")" "$(L "字幕识别服务" "Subtitle recognition service")" "$(L "影片修复服务" "Video restoration service")" "$(L "全部停止" "Stop all")"
                case "$REPLY" in
                    1) (cmd_stop asr) || true ;;
                    2) (cmd_stop restore) || true ;;
                    3) (cmd_stop all) || true ;;
                esac
                pause
                ;;
            4) (cmd_clean) || true; pause ;;
            5)
                (cmd_status) || true
                while true; do
                    local instances=() instance
                    for instance in $(all_instances); do instances+=("$instance"); done
                    if [ "${#instances[@]}" -eq 0 ]; then
                        pause
                        break
                    fi
                    menu_choose "$(L "查看日志（实时跟踪按 Ctrl+C 返回菜单）" "View logs (press Ctrl+C to return to the menu when following)")" \
                        "${instances[@]/%/ $(L "最近 100 行" "last 100 lines")}" "${instances[@]/%/ $(L "实时跟踪" "follow")}"
                    case "$REPLY" in
                        ''|*[!0-9]*|0) break ;;
                    esac
                    if [ "$REPLY" -le "${#instances[@]}" ]; then
                        (FOLLOW=0; cmd_logs "${instances[$((REPLY - 1))]}") || true
                    elif [ "$REPLY" -le $(( ${#instances[@]} * 2 )) ]; then
                        (trap 'exit 0' INT; FOLLOW=1; cmd_logs "${instances[$((REPLY - ${#instances[@]} - 1))]}") || true
                    fi
                done
                ;;
            0|q|Q) return 0 ;;
            '') ;;
            *) say "$(L "无效选择：${choice}" "Invalid choice: ${choice}")" ;;
        esac
    done
}

# ============================================================ 命令行

usage() {
    if [ "$UI_LANG" = zh ]; then
        cat <<EOF
用法：
  $SCRIPT_CMD                         进入交互式菜单
  $SCRIPT_CMD <命令> [服务] [选项]    直接运行（适合自动化）

服务：asr 字幕识别（可多路并行） | restore 影片修复（每台一个） | all 两者

命令：
  install asr|restore|all   安装或更新（重复运行即更新；运行中的服务会先停止、装完后恢复）
  start   [asr|restore|all] 启动；不指定时启动所有已安装的服务。首次启动会先自检并下载模型
  stop    [asr|restore|all] 停止（默认 all），等待正在处理的任务退出
  restart [asr|restore|all] 重启
  status                     服务、安装版本、GPU、磁盘状态
  logs    [名称|asr|restore] 查看日志，例如 logs 4090-01 -f
  self-test asr|restore     重新运行自检（影片修复可加 --ltx 或 --basicvsrpp）
  clean                      列出可清理的残留；加 --yes 强制停止所有服务后删除
  help                       显示本帮助

选项：
  -y, --yes            不提问，按默认值执行；clean 时确认删除
  --profile 3090|4090|5090  显卡配置模板（默认从 nvidia-smi 识别）
  -n, --workers N      字幕并行路数（3090/4090 为 1～3，5090 为 1～4，默认 3 或当前运行数）
  --skip-self-test     启动时跳过首次自检
  --force              stop 超时（${STOP_TIMEOUT} 秒）后强制结束
  --skip asr|restore|models|pip  clean 时跳过某一类，可重复
  -f, --follow         logs 实时跟踪
  --lines N            logs 显示行数（默认 100）

高级选项（环境变量）：VIDEO_TURBO_LANGUAGE=zh-Hans|en、VIDEO_TURBO_NONINTERACTIVE=1、VIDEO_TURBO_SKIP_APT=1、VIDEO_TURBO_PYPI_INDEX_URL、
  VIDEO_TURBO_TORCH_INDEX_URL、VIDEO_TURBO_HF_ENDPOINT、VIDEO_TURBO_JASNA_LICENSE、VIDEO_TURBO_JASNA_VERSION、
  VIDEO_TURBO_JASNA_UPDATE=0（重新安装时保留已装的 Jasna 版本）、VIDEO_TURBO_JASNA_ARCHIVE_DIR、VIDEO_TURBO_GITHUB_PROXY、VIDEO_TURBO_WORKER_CONFIG
  VIDEO_TURBO_JASNA_CONNECTIONS=4（每卷 1–16）、VIDEO_TURBO_JASNA_PARALLEL=2（同时 1–3 卷）、
  VIDEO_TURBO_JASNA_MIN_SPEED=131072（字节/秒，0 关闭）、VIDEO_TURBO_JASNA_SLOW_SECONDS=30
EOF

    else
        cat <<EOF
Usage:
  $SCRIPT_CMD                         open the interactive menu
  $SCRIPT_CMD <command> [service] [options]   run directly (for automation)

Services: asr subtitle recognition (several in parallel) | restore video restoration (one per machine) | all both

Commands:
  install asr|restore|all   install or update (running it again updates; running services stop first and restart afterwards)
  start   [asr|restore|all] start; without a service, starts every installed service. The first start runs a self-test and downloads models
  stop    [asr|restore|all] stop (default all), waiting for running jobs to exit
  restart [asr|restore|all] restart
  status                     services, installed versions, GPU and disk
  logs    [name|asr|restore] show logs, e.g. logs 4090-01 -f
  self-test asr|restore     run the self-test again (restoration also takes --ltx or --basicvsrpp)
  clean                      list leftovers that can be removed; with --yes, stop all services and delete them
  help                       show this help

Options:
  -y, --yes            don't ask, use defaults; confirms deletion for clean
  --profile 3090|4090|5090  GPU profile (detected from nvidia-smi by default)
  -n, --workers N      parallel subtitle services (1–3 for 3090/4090, 1–4 for 5090; default 3 or the current number)
  --skip-self-test     skip the first self-test on start
  --force              force stop after the stop timeout (${STOP_TIMEOUT} s)
  --skip asr|restore|models|pip  skip a category for clean; can repeat
  -f, --follow         follow logs
  --lines N            number of log lines (default 100)

Advanced (environment variables): VIDEO_TURBO_LANGUAGE=zh-Hans|en, VIDEO_TURBO_NONINTERACTIVE=1, VIDEO_TURBO_SKIP_APT=1, VIDEO_TURBO_PYPI_INDEX_URL,
  VIDEO_TURBO_TORCH_INDEX_URL, VIDEO_TURBO_HF_ENDPOINT, VIDEO_TURBO_JASNA_LICENSE, VIDEO_TURBO_JASNA_VERSION,
  VIDEO_TURBO_JASNA_UPDATE=0 (keep the installed Jasna version when reinstalling), VIDEO_TURBO_JASNA_ARCHIVE_DIR, VIDEO_TURBO_GITHUB_PROXY, VIDEO_TURBO_WORKER_CONFIG
  VIDEO_TURBO_JASNA_CONNECTIONS=4 (1–16 per part), VIDEO_TURBO_JASNA_PARALLEL=2 (1–3 parts at once),
  VIDEO_TURBO_JASNA_MIN_SPEED=131072 (bytes/s, 0 turns it off), VIDEO_TURBO_JASNA_SLOW_SECONDS=30
EOF
    fi
}

main() {
    local command="" positional=()
    if [ "${1:-}" = "__supervise" ]; then
        shift
        supervise "$@"
        return
    fi
    # 供主控的自动部署脚本调用：依赖指纹没变时跳过安装；Jasna 有新版本时重新安装。
    case "${1:-}" in
        __restore_precheck)
            restore_deploy_precheck
            return
            ;;
        __deps_key)
            case "${2:-}" in
                asr) asr_deps_hash ;;
                restore) restore_deps_hash ;;
                *) return 2 ;;
            esac
            return
            ;;
        __jasna_outdated)
            jasna_outdated || return 1
            return 0
            ;;
    esac
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -y|--yes) ASSUME_YES=1 ;;
            --profile) PROFILE="${2:-}"; shift ;;
            --profile=*) PROFILE="${1#*=}" ;;
            -n|--workers) ASR_COUNT="${2:-}"; shift ;;
            --workers=*) ASR_COUNT="${1#*=}" ;;
            --skip-self-test) SKIP_SELF_TEST=1 ;;
            --force) FORCE=1 ;;
            --skip) CLEAN_SKIPS+=("${2:-}"); shift ;;
            -f|--follow) FOLLOW=1 ;;
            --lines) LOG_LINES="${2:-100}"; shift ;;
            --ltx|--basicvsrpp) SELF_TEST_ARGS+=("$1") ;;
            -h|--help) command=help ;;
            -*) die "$(L "未知选项：$1（$SCRIPT_CMD help 查看用法）" "Unknown option: $1 (see $SCRIPT_CMD help)")" 2 ;;
            *) positional+=("$1") ;;
        esac
        shift
    done
    if [ -z "$command" ]; then
        command="${positional[0]:-}"
    fi
    local service="${positional[1]:-}"
    case "$command" in
        '')
            if [ -t 0 ] && [ "$NONINTERACTIVE" != "1" ] && [ "$ASSUME_YES" != "1" ]; then
                menu_main
            else
                usage
                exit 2
            fi
            ;;
        install) cmd_install "$service" ;;
        start) cmd_start "$service" ;;
        stop) cmd_stop "${service:-all}" ;;
        restart)
            cmd_stop "${service:-all}" || die "$(L "未能全部停止，已取消启动。" "Not all services stopped; start cancelled.")"
            cmd_start "$service"
            ;;
        status) cmd_status ;;
        logs) cmd_logs "$service" ;;
        self-test)
            case "$service" in
                asr) asr_installed || die "$(L "字幕识别服务未安装。" "The subtitle recognition service isn't installed.")" ;;
                restore) restore_installed || die "$(L "影片修复服务未安装或未启用。" "The video restoration service isn't installed or enabled.")" ;;
                *) die "$(L "请指定：$SCRIPT_CMD self-test asr|restore" "Choose one: $SCRIPT_CMD self-test asr|restore")" 2 ;;
            esac
            require_config
            self_test "$service" 1
            ;;
        clean) cmd_clean ;;
        help) usage ;;
        *) die "$(L "未知命令：${command}（$SCRIPT_CMD help 查看用法）" "Unknown command: ${command} (see $SCRIPT_CMD help)")" 2 ;;
    esac
}

main "$@"
