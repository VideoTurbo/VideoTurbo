from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .i18n import msg


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class JobState(StrEnum):
    # 扫描发现、等待用户手动提交；提交后进入 DISCOVERED 排队。
    PENDING = "PENDING"
    DISCOVERED = "DISCOVERED"
    EXTRACTING = "EXTRACTING"
    WAITING_ASR = "WAITING_ASR"
    TRANSCRIBING = "TRANSCRIBING"
    ASR_COMPLETED = "ASR_COMPLETED"
    WAITING_TRANSLATION = "WAITING_TRANSLATION"
    TRANSLATING = "TRANSLATING"
    VALIDATING = "VALIDATING"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class SourceKind(StrEnum):
    AUDIO = "audio"
    EMBEDDED_TEXT = "embedded_text"
    EXTERNAL_TEXT = "external_text"
    # 扫描时就已判定的影片（已有目标字幕记为已完成，找不到可用来源记为失败），没有识别来源，不能重新处理。
    NONE = "none"


AsrMode = Literal["whisperjav"]
DeviceType = Literal["cuda", "mps", "cpu"]


class AsrSettings(BaseModel):
    mode: AsrMode = "whisperjav"
    whisperjav_ensemble: bool = True


class UiSettings(BaseModel):
    # 列表海报列默认开启；关闭后不再读取海报和生成缩略图。
    show_posters: bool = True
    # 新提醒（云实例关机等）时在浏览器播放提示音。
    notice_sound: bool = True


class TransferSettings(BaseModel):
    # 主控上行（Worker 下载影片修复原片和字幕音频）的总限速，单位 MB/s（1 MB = 1024 KB）；None 为不限速。
    upload_limit_mb: float | None = Field(default=None, gt=0, le=10000)


class Cue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cue_id: int
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    # 原文与译文；语言由任务的语言对决定。
    source: str
    target: str | None = None
    confidence: float | None = None


class WorkerRegistration(BaseModel):
    name: str
    # Worker 运行的 VideoTurbo 代码版本，控制台据此提示与主控不一致。
    version: str = Field(min_length=1, max_length=64)
    gpu_name: str
    driver_version: str | None = None
    memory_total_mb: int
    model: str
    compute_type: str
    device_type: DeviceType = "cuda"
    # 只做影片修复的 Worker 上报空列表。
    supported_asr_modes: list[AsrMode]
    supports_whisperjav_ensemble: bool = False
    supports_restore: bool = False
    # 有 ffmpeg，可以领取网盘媒体库的字幕任务：直接从网盘下载影片抽音频。
    supports_media_fetch: bool = False
    # "basicvsrpp" 与 "ltx"（代表全部 LTX 质量档，需要已激活的许可证）。
    restore_models: list[Literal["basicvsrpp", "ltx"]] = Field(default_factory=list)
    # 已启用影片修复但本次启动不可用时的原因（例如 GPU 视频编码器检查失败），供控制台显示。
    restore_unavailable_reason: str | None = Field(default=None, max_length=500)


class WhisperJavPassProgress(BaseModel):
    """Observed state of one WhisperJAV pass; the step is one of the five shown stages."""

    pass_number: int = Field(ge=1, le=2)
    model: str
    state: Literal["waiting", "running", "done"] = "waiting"
    stage: str | None = None
    step: int | None = Field(default=None, ge=1, le=5)
    completed_scenes: int | None = Field(default=None, ge=0)
    total_scenes: int | None = Field(default=None, ge=1)
    current_scene: int | None = Field(default=None, ge=1)
    # Seconds since the job's recognition started, on the Worker's elapsed clock.
    started_seconds: float | None = Field(default=None, ge=0)
    finished_seconds: float | None = Field(default=None, ge=0)


class WhisperJavProgress(BaseModel):
    """Observed progress within one WhisperJAV phase, never a whole-job estimate."""

    stage: str
    model: str | None = None
    pass_number: int | None = Field(default=None, ge=1, le=2)
    total_passes: Literal[1, 2] = 1
    completed_scenes: int | None = Field(default=None, ge=0)
    total_scenes: int | None = Field(default=None, ge=1)
    current_scene: int | None = Field(default=None, ge=1)
    # Per-pass detail. The flat fields above mirror the leading pass for the Worker log.
    passes: list[WhisperJavPassProgress] = Field(default_factory=list)


class WorkerHeartbeat(BaseModel):
    gpu_utilization: int | None = None
    memory_free_mb: int | None = None
    current_job_id: str | None = None
    status: str = "online"
    asr_completed_chunks: int | None = Field(default=None, ge=0)
    asr_total_chunks: int | None = Field(default=None, ge=1)
    asr_elapsed_seconds: float | None = Field(default=None, ge=0)
    asr_eta_seconds: float | None = Field(default=None, ge=0)
    asr_mode: AsrMode | None = None
    asr_stage: str | None = None
    asr_whisperjav_progress: WhisperJavProgress | None = None
    device_type: DeviceType | None = None
    asr_device: DeviceType | None = None
    # None 时 current_job_id 按字幕识别任务处理。
    task_kind: Literal["asr", "restore"] | None = None
    restore_progress: RestoreProgress | None = None
    # 流水线 Worker 同时持有的全部影片修复任务（含 current_job_id 之外的预取与回传）。
    restore_jobs: list[RestoreJobReport] = Field(default_factory=list, max_length=8)


class JobPriorityUpdate(BaseModel):
    priority: int = Field(ge=-100, le=100)


class ManualScanRequest(BaseModel):
    library_ids: list[str] = Field(min_length=1)


class JobReprocessRequest(BaseModel):
    mode: Literal["continue", "retranslate", "reasr", "full"] = "continue"


class BatchJobAction(BaseModel):
    # “直接标为已完成”必须一次提交全部选中任务，才能在数据库中做原子校验和更新。
    # 控制台单次最多展示 2000 条任务，因此这里与该上限保持一致。
    job_ids: list[str] = Field(min_length=1, max_length=2000)
    action: Literal[
        "start",
        "skip",
        "pause",
        "resume",
        "cancel",
        "priority_high",
        "priority_normal",
        "continue",
        "retranslate",
        "complete",
    ]


class AsrArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass1: str = Field(min_length=1, max_length=10_000_000)
    pass2: str = Field(min_length=1, max_length=10_000_000)
    merged: str = Field(min_length=1, max_length=10_000_000)


class AsrResult(BaseModel):
    job_id: str
    worker_name: str
    language: str
    language_probability: float | None = None
    duration_seconds: float | None = Field(default=None, gt=0)
    cues: list[Cue]
    metadata: dict[str, Any] = Field(default_factory=dict)
    asr_artifacts: AsrArtifacts | None = None


class AsrFailure(BaseModel):
    worker_name: str = Field(min_length=1, max_length=200)
    error: str = Field(min_length=1, max_length=8000)


class MediaFetch(BaseModel):
    """网盘影片由 GPU 机器直接下载抽音频：加载哪个插件、用什么设置、抽哪条音轨。"""

    plugin_id: str
    # 插件源码的 SHA-256；GPU 机器按它从主程序下载插件并核对。内置来源为空字符串。
    plugin_sha256: str
    # 内置来源（WebDAV、Samba）：GPU 机器用自带的代码加载，不下载源码。
    builtin: bool = False
    # 直连网盘失败时经主程序中转下载影片的接口路径（相对主程序地址）。
    relay_path: str = ""
    # 插件设置（含密钥）和插件状态（令牌等），只保存在 GPU 机器的内存中。
    settings: dict[str, Any]
    state: dict[str, Any]
    path: str
    stream_index: int
    channels: int
    sample_rate: int
    opus_bitrate: str


class PluginStateReport(BaseModel):
    """GPU 机器回传插件在下载影片时保存的状态（令牌等）；值为 None 表示删除该项。"""

    worker_name: str
    changes: dict[str, Any]


class JobClaim(BaseModel):
    job_id: str
    # 本地媒体库：主程序抽好的音频。网盘媒体库为空，改为 media。
    audio_url: str | None = None
    audio_sha256: str | None = None
    media: MediaFetch | None = None
    # 影片文件名（不含扩展名），Worker 用它给缓存目录命名。
    title: str = ""
    # 任务源语言的识别代码（语言表的 asr_code），两种识别后端都按它识别。
    language: str
    asr_mode: AsrMode
    # 只对支持双遍的源语言（日语）生效；其他语言主控始终派发单遍。
    whisperjav_ensemble: bool


class RestoreState(StrEnum):
    PREPARING = "PREPARING"  # 主控计算原片校验值
    WAITING = "WAITING"  # 等待 Worker 领取
    DOWNLOADING = "DOWNLOADING"  # Worker 下载原片
    PROCESSING = "PROCESSING"  # Worker 运行 Jasna
    UPLOADING = "UPLOADING"  # Worker 回传结果
    FINALIZING = "FINALIZING"  # 主控校验、备份原片并改名
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


RESTORE_ACTIVE_STATES = {
    RestoreState.PREPARING,
    RestoreState.WAITING,
    RestoreState.DOWNLOADING,
    RestoreState.PROCESSING,
    RestoreState.UPLOADING,
    RestoreState.FINALIZING,
}
RESTORE_WORKER_STATES = {
    RestoreState.DOWNLOADING,
    RestoreState.PROCESSING,
    RestoreState.UPLOADING,
}

RestorationModel = Literal[
    "basicvsrpp", "ltx-basic", "ltx", "ltx-4step", "ltx-8step", "ltx-undistilled"
]
RESTORATION_MODEL_LABELS: dict[str, str] = {
    "basicvsrpp": msg("restore.model.basicvsrpp"),
    "ltx-basic": msg("restore.model.ltxBasic"),
    "ltx": msg("restore.model.ltx"),
    "ltx-4step": msg("restore.model.ltx4"),
    "ltx-8step": msg("restore.model.ltx8"),
    "ltx-undistilled": msg("restore.model.ltxTop"),
}
VideoCodec = Literal["hevc", "h264", "av1"]


def is_ltx_model(model: str) -> bool:
    return model.startswith("ltx")


# 影片修复输出固定为 HEVC（Jasna 默认编码）。
RESTORE_CODEC: VideoCodec = "hevc"


class RestoreSettings(BaseModel):
    restoration_model: RestorationModel = "basicvsrpp"
    ltx_large_canvas: bool = False


class RestoreRequest(BaseModel):
    media_ids: list[int] = Field(min_length=1, max_length=2000)
    restoration_model: RestorationModel = "basicvsrpp"
    # LTX 大面积细节增强（--ltx-large-canvas）：大面积修复区域更多细节，整片最多约慢 3 倍；仅 LTX 有效。
    ltx_large_canvas: bool = False


class RestoreBatchAction(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=2000)
    action: Literal["cancel", "retry", "purge_original"]


class RestoreProgress(BaseModel):
    """Worker 观测到的影片修复进度；传输与处理阶段分别填写对应字段。"""

    # queued：预取的原片已下载完成，等待本机 GPU 空出。
    stage: Literal["downloading", "processing", "uploading", "preparing", "queued"]
    detail: str | None = Field(default=None, max_length=200)
    transferred_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    bytes_per_second: float | None = Field(default=None, ge=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    fps: float | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)


class RestoreJobReport(BaseModel):
    """流水线中的一个影片修复任务（预取下载、修复或后台回传）及其进度。"""

    job_id: str = Field(min_length=1, max_length=64)
    progress: RestoreProgress | None = None


class RestoreClaim(BaseModel):
    job_id: str
    source_url: str
    upload_url: str
    source_size: int
    source_sha256: str
    # 影片文件名（不含扩展名），Worker 用它给缓存目录命名。
    title: str = ""
    source_extension: str
    output_extension: str
    restoration_model: RestorationModel
    codec: VideoCodec
    ltx_large_canvas: bool = False


class RestoreUploadComplete(BaseModel):
    worker_name: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RestoreFailure(BaseModel):
    worker_name: str = Field(min_length=1, max_length=200)
    error: str = Field(min_length=1, max_length=8000)


WorkerHeartbeat.model_rebuild()
