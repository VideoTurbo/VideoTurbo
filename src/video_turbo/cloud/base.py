"""云 GPU 平台插件接口。

每个平台一个 :class:`CloudProvider` 子类，声明自己支持的能力（``capabilities``）和
需要的凭据字段（``credential_fields``）。主控只依赖这里的统一接口，不关心具体平台：
凭据在控制台填写并保存在 NAS 数据库，平台差异全部留在插件内部。

内置只有“手动 SSH”。其他平台以单个 ``.py`` 文件发布，在控制台“设置 → 插件”输入地址安装，
也可以通过 Python entry point ``video_turbo.cloud_providers`` 注册，见 ``video_turbo.plugins``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self

import httpx
from pydantic import BaseModel, Field

from ..i18n import msg

# CredentialField 属于插件接口，云平台插件从本模块导入。
from ..plugins.base import CredentialField, PluginConfigError, icon_uri, merge_values

# 插件接口版本，插件文件用 ``PLUGIN_API = <版本>`` 声明自己适配的版本，
# 主程序加载 PLUGIN_API_MIN_VERSION 到 PLUGIN_API_VERSION 之间的插件。
# - 只新增（新的可选方法、字段）：PLUGIN_API_VERSION 加一，MIN 不变，旧插件照常加载；
# - 删改已有接口：PLUGIN_API_VERSION 加一，MIN 改成与它相同，旧插件需要作者更新。
# 历史：+1 支持 label、description 和字段文字写成 {语言: 文字}（多语言）。
#       +1 字段支持 visible_when（按其他字段的值显示）。
PLUGIN_API_VERSION = 3
PLUGIN_API_MIN_VERSION = 1

# 能力标识。核心逻辑只在插件声明了对应能力时才调用相应方法，界面也据此显示按钮。
CAP_LIST = "list"  # 列出实例及状态
CAP_SHUTDOWN = "shutdown"  # 关机（停止计费，保留实例）
CAP_DESTROY = "destroy"  # 永久销毁已关机实例
CAP_SAVE_IMAGE = "save_image"  # 保存实例为私有镜像
CAP_IDLE_CLOSE = "idle_close"  # 平台侧空闲自动关机，作为 NAS 失联时的保底
CAP_LAUNCH = "launch"  # 开机或新建实例（具体方式见 launch_methods）
CAP_SSH = "ssh"  # 实例信息中带 SSH 登录方式，可自动部署 Worker
CAP_FORGET = "forget"  # 实例记录保存在主控（而非平台账号下），可以从主控移除


class CloudErrorKind(StrEnum):
    AUTH = "auth"  # 凭据无效或无权限：重试无意义
    RATE_LIMIT = "rate_limit"  # 请求过于频繁：退避后重试
    OUT_OF_STOCK = "out_of_stock"  # 缺货：可以继续重试
    TRANSIENT = "transient"  # 网络错误、平台临时故障：稍后重试
    FATAL = "fatal"  # 余额不足、参数错误等：重试无意义


class CloudError(Exception):
    def __init__(self, kind: CloudErrorKind, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.kind in {
            CloudErrorKind.RATE_LIMIT,
            CloudErrorKind.OUT_OF_STOCK,
            CloudErrorKind.TRANSIENT,
        }


InstanceStatus = Literal["running", "starting", "stopping", "stopped", "unknown"]

INSTANCE_STATUS_TEXT: dict[InstanceStatus, str] = {
    "running": msg("cloud.base.status.running"),
    "starting": msg("cloud.base.status.starting"),
    "stopping": msg("cloud.base.status.stopping"),
    "stopped": msg("cloud.base.status.stopped"),
    "unknown": msg("cloud.base.status.unknown"),
}
LAUNCH_METHOD_LABELS = {
    "startup": msg("cloud.base.method.startup"),
    "image": msg("cloud.base.method.image"),
    "public": msg("cloud.base.method.public"),
    "community": msg("cloud.base.method.community"),
    "pod": msg("cloud.base.method.pod"),
    "connect": msg("cloud.base.method.connect"),
}


def instance_status_text(status: InstanceStatus, detail: str = "") -> str:
    """同一生命周期使用统一名称，平台特有阶段或异常保留为补充说明。"""
    label = INSTANCE_STATUS_TEXT[status]
    if status == "unknown":
        return detail or label
    return msg("cloud.base.statusDetail", label=label, detail=detail) if detail else label


class SshInfo(BaseModel):
    host: str
    port: int = 22
    username: str = "root"
    password: str | None = None
    # 已信任的主机公钥（OpenSSH 格式）。None 表示首次连接：连接成功后由部署代码写入，主控保存后下次校验。
    host_key: str | None = None


@dataclass(frozen=True)
class LaunchMethod:
    """一种开机方式。target 为要开机的实例、镜像或 Pod；searchable 表示目标需要按关键字搜索。"""

    id: str
    label: str
    target_label: str
    searchable: bool = False
    # 新建实例（True）还是开启已有实例（False）：新建时请求超时要按实例列表核对是否已创建。
    creates_instance: bool = True
    configurable_gpu: bool = True
    # 平台限制、计费方式等差异放在说明中，同类操作的 label 保持一致。
    help: str = ""


class LaunchTarget(BaseModel):
    id: str
    label: str
    detail: str = ""


class GpuConfiguration(BaseModel):
    gpu_count: int
    available: bool | None = None
    # 所选卡数的合计小时价（人民币元），不能当作单卡价再乘卡数。
    price_per_hour: float | None = None
    detail: str = ""


class GpuOffer(BaseModel):
    id: str
    label: str
    # 平台给出的库存标记，仅供参考，以开机结果为准；None 表示平台未提供。
    available: bool | None = None
    price_per_hour: float | None = None
    # 平台按卡数分别定价/提供库存时使用。
    configurations: list[GpuConfiguration] = Field(default_factory=list)


class CloudInstance(BaseModel):
    id: str
    title: str = ""
    status: InstanceStatus
    # 使用 instance_status_text 统一生命周期名称，平台特有状态保留补充说明。
    status_text: str = ""
    gpu_name: str = ""
    gpu_count: int = 0
    region: str = ""
    # 秒级时间戳；平台不提供时为 None。
    started_at: float | None = None
    created_at: float | None = None
    gpu_id: str = ""
    # 含密码，不出现在接口输出中。
    ssh: SshInfo | None = Field(default=None, exclude=True)


class CloudProvider(ABC):
    """一个云平台账号的客户端。按次创建，用 ``async with`` 管理连接。"""

    id: ClassVar[str]
    label: ClassVar[str]
    homepage: ClassVar[str] = ""
    # 图标：内联 SVG 源码或 data:image/… URI，见 plugins.base.icon_uri。
    icon: ClassVar[str] = ""
    # 插件版本号，如 "1.2.0"；显示在插件列表和更新确认中。
    version: ClassVar[str] = ""
    capabilities: ClassVar[frozenset[str]] = frozenset()
    credential_fields: ClassVar[tuple[CredentialField, ...]] = ()
    credential_mode: ClassVar[str] = "key"
    # 平台空闲关机支持的分钟数（不含 0，0 表示取消）。
    idle_close_choices: ClassVar[tuple[int, ...]] = ()
    launch_methods: ClassVar[tuple[LaunchMethod, ...]] = ()
    # 可选的 GPU 数量。
    gpu_counts: ClassVar[tuple[int, ...]] = (1,)
    # 自动部署时的默认模型目录；空表示登录用户的 $HOME/models。平台有持久化数据盘时指向数据盘。
    default_model_dir: ClassVar[str] = ""

    def __init__(
        self,
        credentials: dict[str, str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15,
    ) -> None:
        self.credentials = credentials
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "id": cls.id,
            "label": cls.label,
            "homepage": cls.homepage,
            "icon": icon_uri(cls.icon),
            "credential_mode": cls.credential_mode,
            "capabilities": sorted(cls.capabilities),
            "idle_close_choices": list(cls.idle_close_choices),
            "launch_methods": [
                {
                    "id": method.id,
                    "label": method.label,
                    "target_label": method.target_label,
                    "searchable": method.searchable,
                    "configurable_gpu": method.configurable_gpu,
                    "help": method.help,
                }
                for method in cls.launch_methods
            ],
            "gpu_counts": list(cls.gpu_counts),
            "default_model_dir": cls.default_model_dir,
            "fields": [field.describe() for field in cls.credential_fields],
        }

    @classmethod
    def credentials_configured(cls, values: dict[str, str]) -> bool:
        return bool(values) and all(
            values.get(field.key) for field in cls.credential_fields if field.required
        )

    @classmethod
    def merge_credentials(cls, saved: dict[str, str], values: dict[str, str]) -> dict[str, str]:
        try:
            return merge_values(cls.credential_fields, saved, values)
        except PluginConfigError as exc:
            raise CloudError(CloudErrorKind.AUTH, exc.message) from exc

    async def prepare_credentials(self) -> dict[str, str]:
        """保存前换取持久授权；默认平台直接保存表单凭据。"""
        return dict(self.credentials)

    @classmethod
    def forget_instance(cls, saved: dict[str, str], instance_id: str) -> dict[str, str] | None:
        """从已保存的凭据中移除一台实例的记录，返回新的凭据（None 表示清空）。仅 CAP_FORGET 插件实现。"""
        raise CloudError(CloudErrorKind.FATAL, msg("cloud.base.noForget", label=cls.label))

    @abstractmethod
    async def list_instances(self) -> list[CloudInstance]:
        """列出账号下的全部实例。"""

    async def shutdown(self, instance_id: str) -> None:
        raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.noShutdown", label=self.label))

    async def destroy(self, instance_id: str) -> None:
        raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.noDestroy", label=self.label))

    async def save_image(self, instance_id: str, image_name: str = "") -> None:
        """保存私有镜像；空名称使用平台默认值。"""
        raise CloudError(CloudErrorKind.FATAL, msg("cloud.service.noImage", label=self.label))

    async def set_idle_close(self, instance_id: str, minutes: int) -> None:
        """设置平台侧空闲关机；minutes=0 表示取消。"""
        raise CloudError(CloudErrorKind.FATAL, msg("cloud.base.noBackstop", label=self.label))

    async def gpu_offers(self) -> list[GpuOffer]:
        """可选的 GPU 型号。"""
        return []

    async def launch_targets(self, method: str, query: str = "") -> list[LaunchTarget]:
        """某种开机方式可选的目标（已关机实例、私有镜像、Pod 等）。"""
        return []

    async def launch(self, method: str, target_id: str, gpu_id: str, gpu_count: int) -> str | None:
        """发起开机。返回实例 ID；平台已接受但未返回 ID 时返回 None。缺货抛出 OUT_OF_STOCK。"""
        raise CloudError(CloudErrorKind.FATAL, msg("cloud.base.noLaunch", label=self.label))

    async def test(self) -> str:
        """验证凭据，返回给用户看的一句话结果。"""
        instances = await self.list_instances()
        running = sum(1 for instance in instances if instance.status == "running")
        return msg("cloud.base.testOk", running=running, count=len(instances))
