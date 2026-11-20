"""各类插件共用的配置字段、合并规则与基类。

插件的配置（地址、API Key 等）和插件设置在控制台填写，保存在主程序数据库；密钥类字段不回显，留空表示沿用已保存的值。
地址类字段标记 ``binds_secrets``：地址变化而密钥沿用旧值时拒绝保存，避免已保存的密钥被发往新地址。
"""

from __future__ import annotations

import base64
import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from importlib.resources import files
from typing import Any, ClassVar, Self
from urllib.parse import unquote, urlsplit

import httpx

from ..i18n import localized, msg


class PluginConfigError(ValueError):
    """插件配置未通过校验；message 直接展示给用户。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PluginCallError(RuntimeError):
    """插件调用外部服务失败；message 直接展示给用户。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


FieldValue = str | int | float | bool
# 字段控件：单行文本（secret 时为密码框）、多行文本、数字、下拉框、开关。
# 给用户看的文字：字符串，或 {语言代码: 文字}（例如 {"zh-Hans": "令牌", "en": "Token"}）。
LocalizedText = str | Mapping[str, str]
FIELD_TYPES = ("text", "textarea", "number", "select", "switch")


@dataclass(frozen=True)
class CredentialField:
    """控制台表单中的一个配置项。插件的每份配置（``credential_fields``）和插件设置（``settings_fields``）都用它声明。

    文本类（text、textarea）的值是字符串，number 是 int（``integer=False`` 时为 float），switch 是 bool，
    select 是选中项的 value。只有文本类字段可以是密钥（``secret``），其他类型一律按非密钥处理。
    """

    key: str
    label: LocalizedText
    secret: bool = True
    required: bool = True
    placeholder: LocalizedText = ""
    help: LocalizedText = ""
    trim: bool = True
    default: FieldValue | None = None
    type: str = "text"
    # select 的选项：(value, 显示文字)。
    options: tuple[tuple[str, LocalizedText], ...] = ()
    # number 的范围与是否只能是整数。
    min: float | None = None
    max: float | None = None
    integer: bool = True
    # 地址类字段：值变化时，必须同时重新填写所有已保存的密钥字段。
    binds_secrets: bool = False

    def __post_init__(self) -> None:
        # 文字类属性可以写成 {"zh-Hans": "…", "en": "…"}，按查看者的语言显示（见 i18n.localized）。
        for name in ("label", "placeholder", "help"):
            object.__setattr__(self, name, localized(getattr(self, name)))
        if self.type not in FIELD_TYPES:
            raise ValueError(
                f"字段 {self.key} 的类型 {self.type!r} 无效，可选：{'、'.join(FIELD_TYPES)}"
            )
        if self.type not in {"text", "textarea"}:
            object.__setattr__(self, "secret", False)
        if self.type == "switch":
            object.__setattr__(self, "required", False)
            object.__setattr__(self, "default", bool(self.default))
        if self.type == "select":
            if not self.options:
                raise ValueError(f"下拉框字段 {self.key} 没有选项。")
            object.__setattr__(
                self, "options", tuple((str(value), localized(label)) for value, label in self.options)
            )
        if self.default is None and self.type in {"text", "textarea"}:
            object.__setattr__(self, "default", "")

    def describe(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = [{"value": value, "label": label} for value, label in self.options]
        return data

    def coerce(self, value: object) -> FieldValue | None:
        """表单提交的值转成字段的类型；None 表示清空。不合法时抛出 PluginConfigError。"""
        if value is None:
            return None
        if self.type == "switch":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                return value.lower() == "true"
            raise PluginConfigError(msg("plugins.field.switch", label=self.label))
        if isinstance(value, bool):
            raise PluginConfigError(msg("plugins.field.invalid", label=self.label))
        if self.type == "number":
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return None
            try:
                number = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise PluginConfigError(msg("plugins.field.number", label=self.label)) from None
            if not math.isfinite(number):
                raise PluginConfigError(msg("plugins.field.number", label=self.label))
            if self.integer and not number.is_integer():
                raise PluginConfigError(msg("plugins.field.integer", label=self.label))
            if self.min is not None and number < self.min:
                raise PluginConfigError(msg("plugins.field.min", label=self.label, min=f"{self.min:g}"))
            if self.max is not None and number > self.max:
                raise PluginConfigError(msg("plugins.field.max", label=self.label, max=f"{self.max:g}"))
            return int(number) if self.integer else number
        if not isinstance(value, str | int | float):
            raise PluginConfigError(msg("plugins.field.invalid", label=self.label))
        text = str(value)
        if self.trim:
            text = text.strip()
        if self.type == "select" and text and text not in {item[0] for item in self.options}:
            raise PluginConfigError(msg("plugins.field.option", label=self.label))
        return text or None


def _filled(value: object) -> bool:
    return value is not None and value != ""


def merge_values(
    fields: tuple[CredentialField, ...], saved: dict[str, Any], values: dict[str, Any]
) -> dict[str, Any]:
    """表单提交的值叠加到已保存的值上。

    没有提交的字段沿用已保存的值；密钥字段提交空值也表示沿用；其他字段提交空值表示清空。
    缺少必填项、值不合法，或地址变化后沿用旧密钥时抛出 PluginConfigError。
    """
    merged = dict(saved)
    submitted: dict[str, FieldValue] = {}
    cleared: set[str] = set()
    for field in fields:
        if field.key not in values:
            continue
        value = field.coerce(values[field.key])
        if value is not None:
            submitted[field.key] = value
        elif not field.secret:
            cleared.add(field.key)
    rebound = [
        field.label
        for field in fields
        if field.binds_secrets
        and field.key in submitted
        and _filled(saved.get(field.key))
        and str(submitted[field.key]).rstrip("/") != str(saved[field.key]).rstrip("/")
    ]
    kept = [
        field.label
        for field in fields
        if field.secret and field.key not in submitted and _filled(saved.get(field.key))
    ]
    if rebound and kept:
        raise PluginConfigError(msg("plugins.field.rebound", changed=rebound, fields=kept))
    for key in cleared:
        merged.pop(key, None)
    merged.update(submitted)
    missing = [
        field.label for field in fields if field.required and not _filled(merged.get(field.key))
    ]
    if missing:
        raise PluginConfigError(msg("plugins.field.required", fields=missing))
    return merged


# 已保存的密钥只返回固定掩码，不回显任何字符（Webhook 地址的末尾往往就是令牌）。
SECRET_MASK = "••••••"


def masked_values(fields: tuple[CredentialField, ...], values: dict[str, Any]) -> dict[str, Any]:
    """给控制台的已保存值：密钥字段只返回掩码（未保存时为空字符串）。"""
    result: dict[str, Any] = {}
    for field in fields:
        value = values.get(field.key)
        if field.secret:
            result[field.key] = SECRET_MASK if _filled(value) else ""
        elif value is not None:
            result[field.key] = value
    return result


def with_defaults(fields: tuple[CredentialField, ...], values: dict[str, Any]) -> dict[str, Any]:
    """没保存过的字段取默认值（插件声明了默认值时）。"""
    result = dict(values)
    for field in fields:
        if field.key not in result and _filled(field.default):
            result[field.key] = field.default
    return result


def missing_required(fields: tuple[CredentialField, ...], values: dict[str, Any]) -> list[str]:
    """还没填写的必填项名称。"""
    return [
        field.label for field in fields if field.required and not _filled(values.get(field.key))
    ]


def normalize_base_url(value: str, label: str = msg("plugins.field.address")) -> str:
    """http(s) 服务地址：去掉末尾斜杠，不允许带账号密码、查询参数。"""
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PluginConfigError(msg("plugins.field.urlScheme", label=label))
    if parts.username or parts.password:
        raise PluginConfigError(msg("plugins.field.urlCredentials", label=label))
    if parts.query or parts.fragment:
        raise PluginConfigError(msg("plugins.field.urlQuery", label=label))
    return value


def response_error(response: httpx.Response, service: str) -> PluginCallError | None:
    """把非 2xx 响应转成给用户看的错误；2xx 返回 None。"""
    if response.is_success:
        return None
    if response.status_code in {401, 403}:
        return PluginCallError(
            msg("plugins.call.forbidden", service=service, status_code=response.status_code)
        )
    if response.is_redirect:
        return PluginCallError(
            msg("plugins.call.redirect", service=service, status_code=response.status_code)
        )
    return PluginCallError(msg("plugins.call.error", service=service, status_code=response.status_code))


ICON_MAX_LENGTH = 64 * 1024
# 远程图标文件的大小上限：转成 base64 data URI 后不超过 ICON_MAX_LENGTH。
ICON_MAX_BYTES = ICON_MAX_LENGTH * 3 // 4 - 64
_ICON_DATA_URI = re.compile(
    r"data:image/(png|jpeg|gif|webp|svg\+xml)(;base64)?,(.*)", re.IGNORECASE | re.DOTALL
)
_SVG_START = re.compile(r"\s*(<\?xml[^>]*\?>\s*)?(<!--.*?-->\s*)*<svg[\s>/]", re.DOTALL)
# SVG 中的脚本、事件属性、内嵌 HTML、实体声明和外部引用（只允许 #id 形式的内部引用）。
_SVG_UNSAFE = re.compile(
    r"<\s*script|<\s*foreignObject|<!\s*(?:ENTITY|DOCTYPE)|\bon[a-z]+\s*=|javascript:|@import"
    r"""|\b(?:xlink:)?href\s*=\s*(?!["']?\s*#)|url\(\s*(?!["']?\s*#)""",
    re.IGNORECASE,
)
_RASTER_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _svg_problem(text: str) -> str:
    if not _SVG_START.match(text):
        return msg("plugins.icon.notSvg")
    if _SVG_UNSAFE.search(text):
        return msg("plugins.icon.unsafeSvg")
    return ""


def icon_from_bytes(data: bytes) -> str:
    """远程下载的图标文件转成 data URI：按文件头识别 PNG、JPEG、GIF、WebP、SVG，其他内容拒绝。

    SVG 拒绝含脚本、事件属性、内嵌 HTML 或外部引用的文件。图标只经 ``<img>`` 显示，浏览器在这种方式下
    本来就不执行 SVG 脚本、不加载外部资源；这里再拒绝一次，避免图标被挪作他用时出问题。不合格时抛出 ValueError。
    """
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(msg("plugins.icon.tooLarge", size=ICON_MAX_BYTES // 1024))
    kind = next((name for magic, name in _RASTER_MAGIC if data.startswith(magic)), "")
    if not kind and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "webp"
    if not kind:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError(msg("plugins.icon.badFormat")) from None
        if problem := _svg_problem(text):
            raise ValueError(problem)
        kind = "svg+xml"
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


def icon_uri(icon: object) -> str:
    """插件图标转成 ``<img>`` 可用的 data URI。

    支持内联 SVG 源码（``<svg …>``）或 ``data:image/…`` URI，最长 64 KB；远程地址和其他内容一律忽略，
    避免控制台加载插件作者控制的外部资源（模块级 ``PLUGIN_ICON`` 的远程地址由主程序在安装时下载，见 installer）。
    图标只经 ``<img>`` 显示，SVG 中的脚本不会执行；含脚本或外部引用的 SVG 仍然忽略。
    """
    if not isinstance(icon, str):
        return ""
    icon = icon.strip()
    if icon.startswith(("<svg", "<?xml")):
        icon = "data:image/svg+xml;base64," + base64.b64encode(icon.encode()).decode()
    match = _ICON_DATA_URI.match(icon)
    if len(icon) > ICON_MAX_LENGTH or not match:
        return ""
    if match.group(1).lower() == "svg+xml":
        try:
            body = match.group(3)
            text = base64.b64decode(body).decode() if match.group(2) else unquote(body)
        except (ValueError, UnicodeDecodeError):
            return ""
        if _svg_problem(text):
            return ""
    return icon


VERSION_MAX_LENGTH = 32


def version_text(version: object) -> str:
    """插件声明的版本号；不是字符串或过长时视为没有声明。"""
    if not isinstance(version, str):
        return ""
    version = version.strip()
    return version if len(version) <= VERSION_MAX_LENGTH else ""


def builtin_icon(name: str) -> str:
    """内置插件的图标：``plugins/icons/`` 下的 SVG 源码。"""
    return (files(__package__) / "icons" / name).read_text(encoding="utf-8")


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, (PluginCallError, PluginConfigError)):
        return exc.message
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return msg("plugins.call.timeout")
    if isinstance(exc, httpx.HTTPError):
        return msg("plugins.call.network", error=exc or type(exc).__name__)
    return f"{type(exc).__name__}: {exc}"


# 插件状态序列化后的上限：令牌、Cookie 之类，远小于这个值。
STATE_MAX_BYTES = 256 * 1024
StateCallback = Callable[[dict[str, Any]], None]


def localize_plugin(plugin: type) -> type:
    """插件类的 label、description 可以写成 {语言: 文字}；加载时统一转成按查看者语言显示的文字。"""
    for name in ("label", "description"):
        value = getattr(plugin, name, None)
        if value is not None and not isinstance(value, str):
            setattr(plugin, name, localized(value))
    return plugin


class PluginInfo:
    """在控制台配置的插件共有的说明、配置字段和插件设置（媒体服务器、通知、媒体库来源）。

    - ``credential_fields``：每份配置各自填写的项（例如每台 Emby 的地址和 API Key）。
    - ``settings_fields``：插件设置，所有配置共用，在“设置 → 插件”的插件卡片上点设置按钮填写。
      插件运行时从 ``self.settings`` 读取（只读）。
    - 插件状态：插件自己保存的数据（登录后得到的令牌、Cookie 等），不在表单里显示，从 ``self.state`` 读取，
      用 :meth:`save_state` 保存。用户修改插件设置后自动清空，避免换了账号还在用旧令牌。
    """

    id: ClassVar[str]
    # label、description 和字段的文字可以是字符串，也可以是 {"zh-Hans": "…", "en": "…"}。
    label: ClassVar[LocalizedText]
    homepage: ClassVar[str] = ""
    # 图标：内联 SVG 源码或 data:image/… URI，见 icon_uri。
    icon: ClassVar[str] = ""
    # 插件版本号，如 "1.2.0"；显示在插件列表和更新确认中。
    version: ClassVar[str] = ""
    # 一句话说明，展示在添加表单和设置弹窗中。
    description: ClassVar[LocalizedText] = ""
    credential_fields: ClassVar[tuple[CredentialField, ...]] = ()
    settings_fields: ClassVar[tuple[CredentialField, ...]] = ()
    # 插件设置与插件状态的保存位置，由主程序在加载时设置：同一插件文件里的插件类共用一份，
    # 按插件文件的 id 保存；为空时按插件类的 id。插件不需要设置它。
    plugin_key: ClassVar[str] = ""

    settings: dict[str, Any]
    state: dict[str, Any]

    def _attach(
        self,
        settings: Mapping[str, Any] | None,
        state: Mapping[str, Any] | None,
        on_state: StateCallback | None,
    ) -> None:
        self.settings = dict(settings or {})
        self.state = dict(state or {})
        self._on_state = on_state

    def save_state(self, changes: Mapping[str, Any]) -> None:
        """保存插件状态：changes 中的项写入 ``self.state``，值为 None 表示删除该项。

        值必须能转成 JSON。在主程序上立即写入数据库；在 GPU 机器上，下载结束后回传主程序保存。
        """
        updated = dict(self.state)
        for key, value in changes.items():
            if not isinstance(key, str):
                raise TypeError(msg("plugins.state.keyType"))
            if value is None:
                updated.pop(key, None)
            else:
                updated[key] = value
        try:
            size = len(json.dumps(updated, ensure_ascii=False).encode())
        except (TypeError, ValueError) as exc:
            raise TypeError(msg("plugins.state.notJson", exc=exc)) from exc
        if size > STATE_MAX_BYTES:
            raise ValueError(msg("plugins.state.tooLarge", size=STATE_MAX_BYTES // 1024))
        self.state = updated
        if self._on_state is not None:
            self._on_state(dict(changes))

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "id": cls.id,
            "label": cls.label,
            "homepage": cls.homepage,
            "icon": icon_uri(cls.icon),
            "description": cls.description,
            "fields": [field.describe() for field in cls.credential_fields],
            "settings_fields": [field.describe() for field in cls.settings_fields],
        }

    @classmethod
    def merge_values(cls, saved: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        merged = merge_values(cls.credential_fields, saved, values)
        return cls.validate_values(
            {field.key: merged[field.key] for field in cls.credential_fields if field.key in merged}
        )

    @classmethod
    def validate_values(cls, values: dict[str, Any]) -> dict[str, Any]:
        """合并后的配置做格式校验或规范化；不合法时抛出 PluginConfigError。"""
        return values

    @classmethod
    def merge_settings(cls, saved: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        merged = merge_values(cls.settings_fields, saved, values)
        return cls.validate_settings(
            {field.key: merged[field.key] for field in cls.settings_fields if field.key in merged}
        )

    @classmethod
    def validate_settings(cls, settings: dict[str, Any]) -> dict[str, Any]:
        """合并后的插件设置做格式校验或规范化；不合法时抛出 PluginConfigError。"""
        return settings

    @classmethod
    def summary(cls, values: dict[str, Any]) -> str:
        """列表中显示的简短说明，不能包含密钥。默认是第一个非密钥文本字段。"""
        return next(
            (
                str(values.get(field.key) or "")
                for field in cls.credential_fields
                if not field.secret and field.type in {"text", "textarea"}
            ),
            "",
        )


class ConfigurablePlugin(PluginInfo, ABC):
    """媒体服务器、通知等插件的基类：一份配置对应一个实例，按次创建，用 ``async with`` 管理连接。

    ``self.values`` 是这份配置，``self.settings`` 是插件设置（所有配置共用）。
    不跟随重定向：带着密钥的请求不会被转发到配置之外的地址。
    """

    def __init__(
        self,
        values: dict[str, Any],
        *,
        settings: Mapping[str, Any] | None = None,
        state: Mapping[str, Any] | None = None,
        on_state: StateCallback | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15,
    ) -> None:
        self.values = values
        self._attach(settings, state, on_state)
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @abstractmethod
    async def test(self) -> str:
        """验证配置，返回给用户看的一句话结果。失败时抛出 PluginCallError。"""
