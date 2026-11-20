from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from . import languages, source_paths
from .environment import getenv
from .i18n import msg
from .models import AsrSettings


class ConfigBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ServerConfig(ConfigBase):
    """监听地址和端口与容器部署绑定，来自环境变量（见 deployment_settings）；只有日志级别在控制台修改。"""

    host: str = "0.0.0.0"
    # Worker 专用端口的监听地址；留空时与 host 相同。桌面版让控制台只听本机、接入端口对局域网开放。
    worker_host: str = ""
    # 主端口：控制台和全部接口。
    port: int = Field(default=8765, ge=1, le=65535)
    # Worker 专用端口：只提供 Worker 接口，供端口转发映射到公网。
    worker_port: int = Field(default=8766, ge=1, le=65535)
    # 反向代理的地址（逗号分隔的 IP 或网段）：只信任它们转发的客户端地址和协议（X-Forwarded-*）。
    # 留空时只信任本机回环地址。
    trusted_proxies: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_worker_port(self) -> ServerConfig:
        if self.worker_port == self.port:
            raise ValueError(msg("config.portClash"))
        return self

    @property
    def worker_listen_host(self) -> str:
        return self.worker_host or self.host


LOCAL_SOURCE = "local"
LIBRARY_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"


class MediaLibraryConfig(ConfigBase):
    # 创建时生成、之后不变：影片记录按 id 归属媒体库，改名不影响已有记录。
    id: str = Field(pattern=LIBRARY_ID_PATTERN)
    name: str = Field(min_length=1, max_length=60)
    # 来源："local" 是主程序容器内的文件夹；其余是网盘插件（媒体库来源插件）的 id。
    source: str = Field(default=LOCAL_SOURCE, min_length=1, max_length=64)
    # 来源内的绝对路径（/ 分隔，见 source_paths）。本地来源在 Docker 里是容器内路径。
    path: str
    enabled: bool = True
    # 语言代码来自内置语言表（languages.py）；扫描时按这对语言判断和创建任务。
    source_language: str = languages.DEFAULT_SOURCE_LANGUAGE
    target_language: str = languages.DEFAULT_TARGET_LANGUAGE

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(msg("config.libraryNameEmpty"))
        return value

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: object, info: ValidationInfo) -> str:
        # source 先于 path 校验：Windows 上的本地文件夹用盘符写法，其余以 / 开头。
        return source_paths.normalize(value, local=info.data.get("source", LOCAL_SOURCE) == LOCAL_SOURCE)

    @model_validator(mode="after")
    def validate_languages(self) -> MediaLibraryConfig:
        languages.validate_pair(self.source_language, self.target_language)
        return self

    @property
    def is_local(self) -> bool:
        return self.source == LOCAL_SOURCE

    @property
    def local_path(self) -> Path:
        """本地来源的目录；网盘来源调用时抛出 ValueError。"""
        if not self.is_local:
            raise ValueError(msg("config.libraryNotLocal", name=self.name))
        return Path(self.path)


def paths_overlap(first: str, second: str) -> bool:
    """两个来源内路径是否相同或互相包含。"""
    return source_paths.overlap(first, second)


class ScanConfig(ConfigBase):
    enabled: bool = True
    schedule: str = "0 3 * * *"
    settle_minutes: int = 0
    manual_scan_enabled: bool = True
    video_extensions: list[str] = Field(
        default_factory=lambda: [
            "mkv", "mp4", "m4v", "avi", "ts", "m2ts", "mts", "mov", "wmv", "asf",
            "flv", "f4v", "webm", "rmvb", "rm", "mpg", "mpeg", "vob", "3gp", "divx",
        ]
    )

    @field_validator("video_extensions")
    @classmethod
    def normalize_extensions(cls, values: list[str]) -> list[str]:
        return sorted({value.lower().lstrip(".") for value in values})


class AudioConfig(ConfigBase):
    sample_rate: int = 16000
    channels: int = 1
    codec: Literal["opus"] = "opus"
    opus_bitrate: str = "64k"


# 片名末尾 -C / -UC 表示带硬编码字幕，与语言无关；按语言区分的规则在语言表的目标语言条目里。
_BURNED_IN_SUFFIX_PATTERN = r"(?i)(?:^|[-_.])u?c$"


class SubtitleConfig(ConfigBase):
    format: Literal["srt"] = "srt"
    overwrite_existing: bool = False
    skip_existing_target: bool = True
    # 目标是简体中文时已有繁体也算已有目标字幕，反之亦然；不分简繁的 zh、chi 始终算已有。
    accept_other_chinese_variant: bool = True
    burned_in_target_path_patterns: list[str] = Field(
        default_factory=lambda: [_BURNED_IN_SUFFIX_PATTERN]
    )
    unlabeled_embedded_subtitle_policy: Literal["ignore", "skip"] = "ignore"
    # 每行字符数和阅读速度上限随目标语言变化，见语言表的 layout。
    max_lines: int = 2
    min_duration_ms: int = 800
    max_duration_ms: int = 7000

    @field_validator("burned_in_target_path_patterns")
    @classmethod
    def validate_burned_in_target_path_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(msg("config.hardsubRegex", value=value, exc=exc)) from exc
        return values


TranslationProvider = Literal["openai_compatible", "vertex_ai"]
VERTEX_AI_DEFAULT_BASE_URL = "https://aiplatform.googleapis.com/v1"
_VERTEX_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def vertex_ai_base_url(project: str | None, location: str | None) -> str:
    """无项目时使用 express mode 全局地址；有项目时拼出项目和地区路径。"""
    if not project:
        return VERTEX_AI_DEFAULT_BASE_URL
    location = location or "global"
    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )
    return f"https://{host}/v1/projects/{project}/locations/{location}"


def _validate_vertex_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _VERTEX_ID_PATTERN.fullmatch(value):
        raise ValueError(msg("config.vertexFormat", value=value))
    return value


class TranslationUpstreamConfig(ConfigBase):
    name: str
    # openai_compatible 节点必填；vertex_ai 节点可改用 project/location 或继承上级地址。
    base_url: str | None = None
    # 为空时继承所在配置的 provider；vertex_ai 节点忽略 api_mode。
    provider: TranslationProvider | None = None
    api_mode: Literal["responses", "chat_completions"] = "responses"
    project: str | None = None
    location: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    concurrency: int = Field(default=2, ge=1, le=8)
    # 以下两项只有显式设置时才覆盖所在节点池的同名配置（None 也是有效的显式值）。
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    structured_output: bool | None = None

    def request_overrides(self) -> dict:
        """节点显式设置、需要覆盖节点池默认值的请求参数。"""
        return {
            field: getattr(self, field)
            for field in ("reasoning_effort", "structured_output")
            if field in self.model_fields_set
            and not (field == "structured_output" and self.structured_output is None)
        }

    @field_validator("name", "base_url", "api_key_env")
    @classmethod
    def strip_text_fields(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("project", "location")
    @classmethod
    def validate_vertex_ids(cls, value: str | None) -> str | None:
        return _validate_vertex_id(value)


def _resolve_upstream_base_url(
    upstream: TranslationUpstreamConfig, parent: TranslationConfig
) -> str | None:
    """节点自己的 project/base_url 优先，其次继承上级的 Vertex AI 地址。"""
    if (upstream.provider or parent.provider) != "vertex_ai":
        return upstream.base_url
    location = upstream.location or parent.location
    if upstream.project:
        return vertex_ai_base_url(upstream.project, location)
    if upstream.base_url:
        return upstream.base_url
    if parent.project:
        return vertex_ai_base_url(parent.project, location)
    if parent.provider == "vertex_ai" and parent.base_url:
        return parent.base_url
    return VERTEX_AI_DEFAULT_BASE_URL


def _validate_upstream_base_urls(
    upstreams: list[TranslationUpstreamConfig],
    provider: str | None,
    field_name: str,
) -> None:
    for upstream in upstreams:
        if (upstream.provider or provider) != "vertex_ai" and not upstream.base_url:
            raise ValueError(msg("config.upstreamNoUrl", field_name=field_name, name=upstream.name))


class TranslationConfig(ConfigBase):
    enabled: bool = True
    provider: TranslationProvider | None = "openai_compatible"
    api_mode: Literal["responses", "chat_completions"] = "responses"
    base_url: str | None = "https://api.deepseek.com"
    # 仅 vertex_ai：填写 project 后按 project/location 拼出地址，忽略 base_url。
    project: str | None = None
    location: str | None = "global"
    model: str | None = "deepseek-v4-flash-0731"
    api_key: str | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    structured_output: bool = False
    fallback_provider: TranslationProvider | None = None
    fallback_base_url: str | None = None
    fallback_project: str | None = None
    fallback_location: str | None = None
    fallback_model: str | None = None
    fallback_api_key: str | None = None
    fallback_reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    fallback_structured_output: bool = False
    upstreams: list[TranslationUpstreamConfig] = Field(default_factory=list)
    fallback_upstreams: list[TranslationUpstreamConfig] = Field(default_factory=list)
    max_upstream_attempts: int = Field(default=2, ge=1, le=8)
    max_fallback_upstream_attempts: int = Field(default=2, ge=1, le=8)
    upstream_cooldown_seconds: int = Field(default=30, ge=1, le=600)
    batch_size: int = Field(default=16, ge=1, le=256)
    context_cues: int = Field(default=4, ge=0, le=64)
    # 所有影片共享的同时请求上限；节点池模式下由各节点的并发数相加得出（见 build_translation_config）。
    concurrency: int = Field(default=1, ge=1)
    job_concurrency: int = Field(default=1, ge=1, le=4)
    max_retries: int = Field(default=0, ge=0, le=5)
    min_batch_size: int = Field(default=1, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=256)
    request_timeout_seconds: int = Field(default=90, ge=30, le=3600)
    checkpoint_every_batches: int = Field(default=3, ge=1, le=100)
    checkpoint_interval_seconds: int = Field(default=10, ge=1, le=600)
    @field_validator("project", "location", "fallback_project", "fallback_location")
    @classmethod
    def validate_vertex_ids(cls, value: str | None) -> str | None:
        return _validate_vertex_id(value)

    @model_validator(mode="after")
    def validate_upstream_names(self) -> TranslationConfig:
        names = [
            upstream.name for upstream in [*self.upstreams, *self.fallback_upstreams]
        ]
        if len(names) != len(set(names)):
            raise ValueError(msg("config.duplicateUpstreams"))
        if self.provider == "vertex_ai" and (
            self.project or "base_url" not in self.model_fields_set
        ):
            self.base_url = vertex_ai_base_url(self.project, self.location)
        _validate_upstream_base_urls(self.upstreams, self.provider, "translation.upstreams")
        _validate_upstream_base_urls(
            self.fallback_upstreams,
            self.fallback_provider or self.provider,
            "translation.fallback_upstreams",
        )
        return self

    @property
    def ready(self) -> bool:
        if self.upstreams:
            return bool(
                self.enabled
                and self.provider
                and any(
                    _resolve_upstream_base_url(upstream, self)
                    and (upstream.model or self.model)
                    and self.upstream_api_key(upstream)
                    for upstream in self.upstreams
                )
            )
        return bool(
            self.enabled and self.provider and self.base_url and self.model and self.api_key
        )

    def upstream_api_key(self, upstream: TranslationUpstreamConfig) -> str | None:
        if upstream.api_key:
            return upstream.api_key
        if upstream.api_key_env:
            return None
        return self.api_key

    def upstream_base_url(self, upstream: TranslationUpstreamConfig) -> str | None:
        return _resolve_upstream_base_url(upstream, self)

    @property
    def ready_upstream_count(self) -> int:
        return sum(
            bool(
                _resolve_upstream_base_url(upstream, self)
                and (upstream.model or self.model)
                and self.upstream_api_key(upstream)
            )
            for upstream in self.upstreams
        )

    @property
    def fallback_ready(self) -> bool:
        if self.fallback_upstreams:
            return bool(
                self.enabled
                and self.provider
                and any(
                    _resolve_upstream_base_url(upstream, self._fallback_view())
                    and (upstream.model or self.fallback_model)
                    and self.fallback_upstream_api_key(upstream)
                    for upstream in self.fallback_upstreams
                )
            )
        return bool(
            self.fallback_model
            and self._fallback_base_url()
            and (self.fallback_api_key or self.api_key)
        )

    def _fallback_base_url(self) -> str | None:
        provider = self.fallback_provider or self.provider
        if provider != "vertex_ai":
            return self.fallback_base_url or self.base_url
        if self.fallback_project:
            return vertex_ai_base_url(
                self.fallback_project, self.fallback_location or self.location
            )
        if self.fallback_base_url:
            return self.fallback_base_url
        if self.provider == "vertex_ai":
            return self.base_url
        return VERTEX_AI_DEFAULT_BASE_URL

    def _fallback_view(self) -> TranslationConfig:
        """兜底层级的 provider、项目和地址，供兜底节点继承。"""
        return self.model_copy(
            update={
                "provider": self.fallback_provider or self.provider,
                "project": self.fallback_project or self.project,
                "location": self.fallback_location or self.location,
                "base_url": self._fallback_base_url(),
            }
        )

    def fallback_upstream_api_key(
        self, upstream: TranslationUpstreamConfig
    ) -> str | None:
        if upstream.api_key:
            return upstream.api_key
        if upstream.api_key_env:
            return None
        return self.fallback_api_key or self.api_key

    @property
    def ready_fallback_upstream_count(self) -> int:
        fallback_view = self._fallback_view()
        return sum(
            bool(
                _resolve_upstream_base_url(upstream, fallback_view)
                and (upstream.model or self.fallback_model)
                and self.fallback_upstream_api_key(upstream)
            )
            for upstream in self.fallback_upstreams
        )

    def fallback_request_config(self) -> TranslationConfig | None:
        if self.fallback_upstreams or not self.fallback_ready:
            return None
        return self.model_copy(
            update={
                "provider": self.fallback_provider or self.provider,
                "project": self.fallback_project or self.project,
                "location": self.fallback_location or self.location,
                "base_url": self._fallback_base_url(),
                "model": self.fallback_model,
                "api_key": self.fallback_api_key or self.api_key,
                "reasoning_effort": self.fallback_reasoning_effort,
                "structured_output": self.fallback_structured_output,
                "upstreams": [],
                "fallback_upstreams": [],
            }
        )

    def fallback_pool_config(self) -> TranslationConfig | None:
        if not self.fallback_upstreams:
            return None
        return self.model_copy(
            update={
                "provider": self.fallback_provider or self.provider,
                "project": self.fallback_project or self.project,
                "location": self.fallback_location or self.location,
                "base_url": self._fallback_base_url(),
                "model": self.fallback_model,
                "api_key": self.fallback_api_key or self.api_key,
                "reasoning_effort": self.fallback_reasoning_effort,
                "structured_output": self.fallback_structured_output,
                "upstreams": self.fallback_upstreams,
                "fallback_upstreams": [],
            }
        )


class RestoreConfig(ConfigBase):
    """影片修复：Worker 用 Jasna 修复后回传，主控备份原片并把新片及附属文件改名。"""

    # 原片备份目录（主控容器内路径，来自环境变量），必须位于所有媒体库之外；未设置时不能提交影片修复任务。
    backup_dir: Path | None = None
    # 文件主名或直接父目录匹配任意一项即视为已修复或无需修复的影片，不显示“影片修复”按钮。
    skip_path_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?i)(?:^|[-_.])uc?$",
            r"(?i)(?:^|[-_.])restored(?:$|[-_.])",
        ]
    )
    # 新片时长与原片相差超过该秒数时拒绝替换，防止回传残缺文件。
    max_duration_difference_seconds: float = Field(default=3.0, ge=0.1, le=600)
    # 影片修复任务的 Worker 租约；下载/修复大文件时由心跳续租。
    lease_seconds: int = Field(default=300, ge=60, le=3600)
    # 中断的任务（Worker 重启、掉线）为原机器保留的分钟数：原机器上有已下载的原片和 LTX 进度，
    # 这段时间内只派回原机器；超时后任何 Worker 都可领取。0 表示不保留。
    resume_reservation_minutes: int = Field(default=30, ge=0, le=1440)

    @field_validator("skip_path_patterns")
    @classmethod
    def validate_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(msg("config.skipRegex", value=value, exc=exc)) from exc
        return values


class WorkersConfig(ConfigBase):
    # 所有在线 Worker 平等接单，按任务优先级和入队时间领取。
    heartbeat_timeout_seconds: int = 90
    lease_seconds: int = 120
    # 离线超过该分钟数的 Worker 自动从列表删除；0 表示不自动删除。
    offline_cleanup_minutes: int = Field(default=60, ge=0)


class AppConfig(ConfigBase):
    server: ServerConfig = Field(default_factory=ServerConfig)
    media_libraries: list[MediaLibraryConfig] = Field(default_factory=list)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    asr: AsrSettings = Field(default_factory=AsrSettings)
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    restore: RestoreConfig = Field(default_factory=RestoreConfig)

    @model_validator(mode="after")
    def validate_libraries(self) -> AppConfig:
        ids: set[str] = set()
        names: set[str] = set()
        for index, library in enumerate(self.media_libraries):
            if library.id in ids:
                raise ValueError(msg("config.duplicateLibraryId", id=library.id))
            if library.name in names:
                raise ValueError(msg("config.duplicateLibraryName", name=library.name))
            ids.add(library.id)
            names.add(library.name)
            for other in self.media_libraries[:index]:
                if other.source == library.source and paths_overlap(other.path, library.path):
                    raise ValueError(
                        msg("config.libraryOverlap", name=library.name, other=other.name)
                    )
        return self

    def library(self, library_id: str | None) -> MediaLibraryConfig | None:
        return next((item for item in self.media_libraries if item.id == library_id), None)

    @model_validator(mode="after")
    def validate_restore_backup_dir(self) -> AppConfig:
        backup = self.restore.backup_dir
        if backup is None:
            return self
        if not backup.is_absolute():
            raise ValueError(msg("config.backupAbsolute"))
        resolved = Path(os.path.abspath(backup))
        for library in self.media_libraries:
            if not library.is_local:
                continue
            root = Path(os.path.abspath(library.path))
            if resolved == root or root in resolved.parents or resolved in root.parents:
                raise ValueError(
                    msg("config.backupInsideLibrary", name=library.name)
                )
        return self

    def warnings(self) -> list[dict[str, str]]:
        """控制台顶部的提醒：message 是提示文字，target 是点击后跳转的位置（页面，或 settings:分类）。"""
        warnings: list[dict[str, str]] = []
        if not self.media_libraries:
            warnings.append({"message": msg("config.warn.noLibrary"), "target": "settings:media"})
        if not self.translation.ready:
            warnings.append({"message": msg("config.warn.noTranslation"), "target": "translate"})
        missing = [
            upstream.name
            for upstream in self.translation.upstreams
            if not self.translation.upstream_api_key(upstream)
        ] + [
            upstream.name
            for upstream in self.translation.fallback_upstreams
            if not self.translation.fallback_upstream_api_key(upstream)
        ]
        if self.translation.ready and missing:
            warnings.append({"message": msg("config.warn.missingKey", models=list(missing)), "target": "translate"})
        return warnings


def deployment_settings() -> dict:
    """与容器部署绑定的项：监听地址、端口和原片备份目录。

    只来自环境变量（docker-compose.yml 中已按容器内路径设好），不在控制台修改，也不写入数据库。
    """
    # 本地预览只监听本机，避免临时数据库和测试口令暴露到局域网。
    local_preview = getenv("VIDEO_TURBO_LOCAL_PREVIEW") == "1"
    return {
        "server": {
            "host": "127.0.0.1" if local_preview else getenv("VIDEO_TURBO_HOST", "0.0.0.0"),
            "worker_host": "127.0.0.1" if local_preview else (getenv("VIDEO_TURBO_WORKER_HOST", "") or "").strip(),
            "port": int(getenv("VIDEO_TURBO_PORT", "8765")),
            "worker_port": int(getenv("VIDEO_TURBO_WORKER_PORT", "8766")),
            "trusted_proxies": (getenv("VIDEO_TURBO_TRUSTED_PROXIES", "") or "").strip(),
        },
        "restore": {
            "backup_dir": getenv("VIDEO_TURBO_RESTORE_BACKUP", "/app/backups") or None
        },
    }


def apply_local_preview(config: AppConfig) -> None:
    if getenv("VIDEO_TURBO_LOCAL_PREVIEW") == "1":
        # 本地只预览控制台和测试节点：在内存里关闭扫描等副作用，不写回任何配置。
        # 媒体库照常显示，但不会被扫描。媒体服务器刷新和通知推送由应用启动时按同一环境变量关闭。
        config.server.host = "127.0.0.1"
        config.server.worker_host = "127.0.0.1"
        config.scan.enabled = False
        config.scan.manual_scan_enabled = False
