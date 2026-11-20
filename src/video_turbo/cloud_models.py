"""云主机相关的数据结构：控制台请求、设置与状态取值。存取见 cloud_store。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .i18n import msg

ShutdownMode = Literal["off", "always"]
# watching：监视空闲；draining：停止派单并确认无任务；stopping：已发出关机；stopped：已关机；failed：关机失败待重试。
BindingState = Literal["watching", "draining", "stopping", "stopped", "failed"]
NoticeLevel = Literal["info", "success", "warning", "error"]
# grabbing：抢卡中；booting：等待开机；deploying：SSH 部署中；running：已完成；
# nvenc_failed：首次修复部署的编码检查未通过，等待用户决定是否仅部署字幕。
# deploy_failed：部署失败（实例仍在运行）；failed：开机失败；cancelled：已停止。
LaunchState = Literal[
    "grabbing", "booting", "deploying", "running", "deploy_failed", "nvenc_failed", "failed", "cancelled"
]
ACTIVE_LAUNCH_STATES = ("grabbing", "booting", "deploying")
# 开机后自动部署并启动的服务：asr 字幕识别，restore 影片修复；都不选表示只开机。
DeployService = Literal["asr", "restore"]
DEPLOY_SERVICES: tuple[str, ...] = ("asr", "restore")


class CloudSettings(BaseModel):
    # 机器空闲（无任务、无待处理任务）持续多少分钟后关机。
    idle_minutes: int = Field(default=10, ge=3, le=240)
    # 平台侧空闲关机保底（分钟，0 关闭）：NAS 失联时由平台按 GPU 空闲关机。
    backstop_minutes: int = Field(default=60, ge=0, le=1440)
    # 自动部署时是否把影片修复的 Jasna 更新到最新发布；关闭后已安装的实例固定在当前版本。
    update_jasna: bool = True


class CloudSettingsUpdate(CloudSettings):
    # Jasna 许可证码（启用 LTX）：单独保存且不回显；省略或留空表示保持原值。
    jasna_license: str | None = Field(default=None, max_length=200)


def normalize_services(services: Any) -> list[str]:
    chosen = {str(item) for item in services or []}
    return [service for service in DEPLOY_SERVICES if service in chosen]


class LaunchRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    method: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=300)
    target_label: str = Field(default="", max_length=300)
    gpu_id: str = Field(min_length=1, max_length=200)
    gpu_label: str = Field(default="", max_length=200)
    gpu_count: int = Field(default=1, ge=1, le=8)
    interval_seconds: float = Field(default=2, ge=0.5, le=300)
    # 开机后要部署的服务；留空表示只开机不部署。
    services: list[DeployService] = Field(default_factory=lambda: ["asr"])
    shutdown_mode: ShutdownMode = "always"

    @model_validator(mode="after")
    def _resolve_services(self) -> LaunchRequest:
        self.services = normalize_services(self.services)  # type: ignore[assignment]
        return self

    @property
    def deploy(self) -> bool:
        return bool(self.services)


class CloudBindingRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    instance_id: str = Field(min_length=1, max_length=200)
    instance_title: str | None = Field(default=None, max_length=300)
    shutdown_mode: ShutdownMode = "always"


class ShutdownModeUpdate(BaseModel):
    shutdown_mode: ShutdownMode


class SaveImageRequest(BaseModel):
    image_name: str = Field(default="", max_length=300)


class CredentialUpdate(BaseModel):
    # 空字符串或省略表示保留原值；清除凭据请用 DELETE。
    values: dict[str, str] = Field(default_factory=dict)


class CloudProfileUpdate(CredentialUpdate):
    name: str = Field(min_length=1, max_length=100)
    # 自动部署时实例上的模型目录；留空使用平台默认目录（见 CloudProvider.default_model_dir）。
    model_dir: str = Field(default="", max_length=300)


class CloudProfileCreate(CloudProfileUpdate):
    type: str = Field(min_length=1, max_length=64)


def normalize_model_dir(value: str) -> str:
    """校验配置中的模型目录：绝对路径，或以 ~/、$HOME/ 开头（部署时按登录用户的家目录展开）。"""
    value = value.strip()
    if not value:
        return ""
    home_relative = value in {"~", "$HOME"} or value.startswith(("~/", "$HOME/"))
    if any(ord(char) < 32 for char in value) or not (value.startswith("/") or home_relative):
        raise ValueError(msg("cloud.modelDirFormat"))
    return value
