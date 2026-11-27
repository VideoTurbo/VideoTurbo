from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..environment import getenv
from ..i18n import msg
from ..machine_id import detect_machine_id, worker_name


class WorkerConfigBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class IdleConfig(WorkerConfigBase):
    enabled: bool = True
    max_gpu_utilization: int = Field(default=15, ge=0, le=100)
    min_free_memory_mb: int = Field(default=8500, ge=0)
    consecutive_checks: int = Field(default=3, ge=1)
    check_interval_seconds: int = Field(default=20, ge=5)


class AsrConfig(WorkerConfigBase):
    max_cue_duration_seconds: float = Field(default=6.0, ge=1.0, le=15.0)
    download_root: Path = Path("models")
    # 桌面版开启：只报告模型已下载的识别模式，识别时不联网下载模型（模型由用户在模型清单里下载）。
    local_models_only: bool = False


class WhisperJavConfig(WorkerConfigBase):
    python_executable: Path | None = None
    model: str = "Qwen/Qwen3-ASR-1.7B"
    speech_segmenter: Literal["firered-vad", "ten", "whisperseg"] = "firered-vad"
    sensitivity: Literal["conservative", "balanced", "aggressive"] = "aggressive"
    # Only applies when the Worker runs WhisperJAV on CUDA.
    firered_vad_gpu: bool = True
    # Run the two ensemble passes at once on CUDA GPUs with enough memory.
    parallel_passes: bool = True
    timeout_seconds: int = Field(default=14400, ge=60, le=86400)


class JasnaConfig(WorkerConfigBase):
    """影片修复（Jasna）。只在 Linux NVIDIA Worker 上使用，需先运行 bash deploy/worker/linux/worker.sh install restore。"""

    enabled: bool = False
    # Jasna 可执行文件；默认 <项目>/jasna/current/jasna（安装脚本创建的链接）。
    executable: Path | None = None
    # Jasna 的 XDG_CONFIG_HOME，保存许可证激活信息；默认 <项目>/jasna/config。
    config_home: Path | None = None
    # 已用许可证激活后设为 true，才会领取 LTX 系列任务。
    ltx: bool = False
    # LTX 快速模式（--ltx-fast）：约快 1.4 倍、细节略少，仅 RTX 50 系可用，首次需多下载约 8 GB。
    # 只对 LTX 任务生效；开启后运行 bash deploy/worker/linux/worker.sh self-test restore 下载快速模式模型。
    ltx_fast: bool = False
    # 显存不足时调小，例如 60；null 使用 Jasna 默认值 90。
    max_clip_size: int | None = Field(default=None, ge=10, le=500)
    extra_args: list[str] = Field(default_factory=list)
    timeout_hours: float = Field(default=48, gt=0, le=240)
    upload_chunk_mb: int = Field(default=64, ge=1, le=256)
    # 下载原片的连接数（1–32），占用的是 NAS 家宽上行；1 为单连接。
    download_connections: int = Field(default=16, ge=1, le=32)
    # 回传结果的连接数（1–32），占用的是云服务器上行和 NAS 下行；1 为单连接。
    upload_connections: int = Field(default=32, ge=1, le=32)
    # 单机流水线：修复当前影片时预取下一部原片，并在后台回传上一部结果。
    # 本机磁盘需能同时容纳约 3 部影片（回传中、修复中、预取中）。
    pipeline: bool = True
    # 中断任务的原片/结果保留时长，同一任务回到本机时可续传；超时自动清理。
    keep_partial_hours: int = Field(default=72, ge=1, le=720)


class WorkerConfig(WorkerConfigBase):
    server_url: str = ""
    worker_token: str = ""
    # 运行时由硬件自动生成（见 machine_id），不读取配置文件；旧配置里的 name 会被忽略。
    name: str = ""
    device: Literal["auto", "cuda", "mps", "cpu"] = "auto"
    device_index: int = 0
    poll_interval_seconds: int = Field(default=15, ge=3)
    heartbeat_interval_seconds: int = Field(default=30, ge=5)
    # 唯一的缓存根目录，同机所有 Worker 实例共用；null 使用 <项目>/cache。
    # 字幕识别的音频放在 asr/<片名>-<短ID>/（不与同机主控的 subtitles/ 共用），影片修复的原片和结果放在 restore/<片名>-<短ID>/。
    # 影片修复要求该目录在本机磁盘且路径只含英文字符。
    cache_dir: Path | None = None
    # 同机字幕识别与影片修复互斥使用 GPU 的锁文件；null 使用 <缓存目录>/.gpu.lock。
    gpu_lock_file: Path | None = None
    idle: IdleConfig = Field(default_factory=IdleConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    whisperjav: WhisperJavConfig = Field(default_factory=WhisperJavConfig)
    jasna: JasnaConfig = Field(default_factory=JasnaConfig)

    @property
    def cache_root(self) -> Path:
        # load_worker_config 会填好绝对路径；直接构造配置时退回当前目录下的 cache。
        return self.cache_dir or Path("cache")

    @property
    def asr_dir(self) -> Path:
        return self.cache_root / "asr"

    @property
    def restore_dir(self) -> Path:
        return self.cache_root / "restore"


def load_worker_config(path: str | Path, *, require_token: bool = True) -> WorkerConfig:
    config_path = Path(path)
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = WorkerConfig.model_validate(loaded)
    if require_token and not config.worker_token.strip():
        raise ValueError(msg("worker.config.tokenMissing"))
    if require_token and not config.server_url.strip():
        raise ValueError(msg("worker.config.urlMissing"))
    # worker.sh 为同机多实例传入后缀 01、02… 或 dc；单实例（Windows、Mac）不带后缀。
    config.name = worker_name(
        detect_machine_id(config.device, config.device_index),
        getenv("VIDEO_TURBO_WORKER_SUFFIX", "").strip() or None,
    )
    base = config_path.resolve().parent
    if not config.asr.download_root.is_absolute():
        config.asr.download_root = (base / config.asr.download_root).resolve()
    python_path = config.whisperjav.python_executable
    if python_path is None:
        executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        python_path = base.parent / ".venv-whisperjav" / executable
    elif not python_path.is_absolute():
        python_path = base / python_path
    # venv/bin/python is normally a symlink. Resolving it would run the system
    # interpreter and bypass both the isolated packages and readiness marker.
    config.whisperjav.python_executable = Path(os.path.abspath(python_path.expanduser()))
    jasna = config.jasna
    project = base.parent
    jasna.executable = _resolve(jasna.executable, base, project / "jasna" / "current" / "jasna")
    jasna.config_home = _resolve(jasna.config_home, base, project / "jasna" / "config")
    config.cache_dir = _resolve(config.cache_dir, base, project / "cache")
    config.gpu_lock_file = _resolve(config.gpu_lock_file, base, config.cache_dir / ".gpu.lock")
    return config


def _resolve(value: Path | None, base: Path, default: Path) -> Path:
    if value is None:
        return Path(os.path.abspath(default))
    value = value.expanduser()
    # 不解析符号链接：current 链接在升级 Jasna 时会切到新版本目录。
    return Path(os.path.abspath(value if value.is_absolute() else base / value))
