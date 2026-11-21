"""在控制台安装的插件（云平台、媒体服务器、通知、媒体库来源）：按地址下载，或上传本地 ``.py`` 文件。

插件是单个 ``.py`` 文件：声明 ``PLUGIN_API``，定义一个或多个插件类（某类插件基类的子类），每个类的类型由它继承的
基类决定，一个文件可以同时提供多种类型。只有一个插件类时，插件的 id、名称、版本就是这个类的；有多个时用模块常量
声明整个插件的信息::

    PLUGIN_API = {"media_source": 2, "notifier": 1}  # 只有一种类型时可以写成整数
    PLUGIN_ID = "mycloud"
    PLUGIN_NAME = "我的网盘"
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_ICON = "https://example.com/icon.png"     # 可选：内联 SVG、data URI 或 https 地址
    PLUGIN_HOMEPAGE = "https://example.com"          # 可选
    PLUGIN_SETTINGS = (CredentialField(...), ...)    # 可选：文件内全部插件类共用的插件设置

同一文件里的插件类共用一份插件设置和插件状态，按插件 id 保存。

安装和更新都分两步：先下载（或接收上传的源码）并只做静态检查（不执行），把源码的 SHA-256 交给用户核对；用户确认后
带着这个 SHA-256 再次请求，主控重新下载（或再次接收源码），内容与确认的一致才执行、保存到数据库并注册，立即可用，
不需要重启。主控启动时从数据库重新加载。按地址安装的插件只在用户点击时更新，不会自动拉取；本地上传的插件没有地址可查，
重新上传同一插件的新文件即为更新。``PLUGIN_ICON`` 是 https 地址时，图标同样在第一步由主控下载、检查，指纹一并核对，
确认后保存到数据库；浏览器只显示保存的图标，不访问图标地址。

插件代码在主控进程内运行，可以读取全部凭据和数据，只应安装信任的来源。为此：

- 只从公网 https 地址下载：地址（以及每一次重定向的目标）解析到回环、内网、链路本地等地址时拒绝，
  避免借安装插件探测内网。解析与实际连接之间仍有 DNS 重绑定的理论窗口，最终防线是用户核对 SHA-256。
- 环境变量 ``VIDEO_TURBO_SAFE_MODE=1`` 时启动不加载任何在控制台安装的插件，也不能安装或更新，只能卸载；
  用于插件导致主控异常时恢复。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import ipaddress
import logging
import os
import re
import socket
import sys
import types
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..database import Database
from ..deployment import is_desktop
from ..i18n import localized, msg
from ..models import utc_now_iso
from ..plugin_store import CONNECTION_KEYS
from . import registry
from .base import (
    ICON_MAX_BYTES,
    CredentialField,
    PluginInfo,
    check_conditions,
    icon_from_bytes,
    icon_uri,
    localize_plugin,
    version_text,
)

logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5
# 临时网络错误（超时、连接中断）共尝试的次数，以及重试前等待的基数（第 n 次重试等 n 倍）。
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0
_TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
SAFE_MODE_ENV = "VIDEO_TURBO_SAFE_MODE"
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_GITHUB_BLOB = re.compile(r"/([^/]+)/([^/]+)/blob/(.+)")
_MODULE_PREFIX = "video_turbo_plugin_"
_UPLOAD_PREFIX = "upload:"


@dataclass(frozen=True)
class _Loaded:
    module: str
    # 文件中的插件类：((类型, 插件类 id), ...)
    members: tuple[tuple[str, str], ...]


# 已加载的插件：{插件 id: _Loaded}。
_loaded: dict[str, _Loaded] = {}
# 启动时没有加载的插件：{插件 id: 原因}。
_errors: dict[str, str] = {}


class PluginError(Exception):
    """安装或加载失败；message 直接展示给用户。"""

    def __init__(self, message: str, *, conflict: bool = False, mismatch: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.conflict = conflict
        # 重新下载的内容与用户确认的 SHA-256 不一致：来源可能在两次请求之间被替换。
        self.mismatch = mismatch


def safe_mode() -> bool:
    return os.getenv(SAFE_MODE_ENV) == "1"


def _require_normal_mode() -> None:
    if safe_mode():
        raise PluginError(
            msg(
                "plugins.install.safeMode.desktop" if is_desktop() else "plugins.install.safeMode",
                SAFE_MODE_ENV=SAFE_MODE_ENV,
            ),
            conflict=True,
        )


def normalize_url(url: str, what: str = msg("plugins.install.what.file")) -> str:
    """校验地址；GitHub 的文件页面地址换成原始文件地址。"""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PluginError(msg("plugins.install.urlScheme", what=what))
    if parts.username or parts.password:
        raise PluginError(msg("plugins.install.urlCredentials", what=what))
    if parts.hostname == "github.com" and (match := _GITHUB_BLOB.fullmatch(parts.path)):
        owner, repo, rest = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return url


async def _resolve(host: str, port: int) -> list[str]:
    """域名解析得到的全部地址。测试中替换为固定结果。"""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _check_public(url: str, what: str) -> None:
    """只允许公网 https 地址：拒绝回环、内网、链路本地、保留地址等。"""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host:
        raise PluginError(msg("plugins.install.urlHttps", what=what))
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await _resolve(host, parts.port or 443)
        except OSError as exc:
            raise PluginError(
                msg("plugins.install.hostNotFound", what=what, host=host)
            ) from exc
    if not addresses or not all(_is_public(address) for address in addresses):
        raise PluginError(
            msg("plugins.install.privateHost", host=host, what=what)
        )


async def fetch(
    url: str,
    transport: httpx.AsyncBaseTransport | None,
    *,
    what: str = msg("plugins.install.what.plugin"),
    limit: int = MAX_SOURCE_BYTES,
) -> bytes:
    """从公网 https 地址下载。手动跟随重定向，每一跳都重新检查是否为公网 https 地址。

    超时、连接中断这类临时网络错误自动重试；服务器明确返回的错误（如 404）不重试。
    """
    async with httpx.AsyncClient(
        transport=transport, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                return await _fetch_once(client, url, what, limit)
            except _TRANSIENT_ERRORS as exc:
                if attempt + 1 < DOWNLOAD_ATTEMPTS:
                    logger.info("下载 %s 失败（%s），重试", url, _http_error_text(exc))
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                raise PluginError(msg("plugins.install.downloadFailed", what=what, error=_http_error_text(exc))) from exc
            except httpx.HTTPError as exc:
                raise PluginError(msg("plugins.install.downloadFailed", what=what, error=_http_error_text(exc))) from exc
    raise AssertionError("unreachable")


async def _fetch_once(client: httpx.AsyncClient, url: str, what: str, limit: int) -> bytes:
    for _ in range(MAX_REDIRECTS + 1):
        await _check_public(url, what)
        async with client.stream("GET", url) as response:
            if response.is_redirect and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code != 200:
                raise PluginError(msg("plugins.install.downloadFailed", what=what, error=_status_text(response.status_code)))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > limit:
                    raise PluginError(msg("plugins.install.tooLarge", what=what, limit=_size_text(limit)))
            return bytes(body)
    raise PluginError(msg("plugins.install.tooManyRedirects", what=what))


def _status_text(status: int) -> str:
    if status == 404:
        return msg("plugins.install.http404")
    if status in (401, 403):
        return msg("plugins.install.httpForbidden", status=status)
    if status == 429:
        return msg("plugins.install.http429")
    if status >= 500:
        return msg("plugins.install.httpServer", status=status)
    return msg("plugins.install.httpOther", status=status)


def _http_error_text(exc: httpx.HTTPError) -> str:
    """httpx 的超时等异常文字常为空，换成说明；其余用异常文字，为空时用异常类型名。"""
    if isinstance(exc, httpx.TimeoutException):
        return msg("plugins.install.timeout")
    if isinstance(exc, httpx.ConnectError):
        return msg("plugins.install.connectFailed")
    return str(exc) or type(exc).__name__


def _size_text(size: int) -> str:
    return f"{size // (1024 * 1024)} MB" if size >= 1024 * 1024 else f"{size // 1024} KB"


async def fetch_text(
    url: str, transport: httpx.AsyncBaseTransport | None, *, what: str = msg("plugins.install.what.plugin")
) -> str:
    try:
        return (await fetch(url, transport, what=what)).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PluginError(msg("plugins.install.unreadable", what=what, kind=what)) from exc


def _origin(plugin: dict[str, Any], fallback: str) -> str:
    """插件来源的文字说明：按地址安装的是地址，本地上传的是 ``upload:文件名``。"""
    if plugin.get("url"):
        return str(plugin["url"])
    if plugin.get("filename"):
        return _UPLOAD_PREFIX + str(plugin["filename"])
    return fallback


def upload_name(filename: str) -> str:
    """校验上传的文件名，只保留文件名部分。"""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name.lower().endswith(".py") or len(name) <= 3:
        raise PluginError(msg("plugins.install.uploadPy"))
    if len(name) > 255:
        raise PluginError(msg("plugins.install.nameTooLong"))
    return name


def _check_size(source: str) -> None:
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise PluginError(msg("plugins.install.fileTooLarge"))


def _digest(source: str | bytes) -> str:
    return hashlib.sha256(source.encode() if isinstance(source, str) else source).hexdigest()


# ---- 静态检查（不执行） ----

_MISSING = object()


def _assigned(tree: ast.Module, name: str) -> ast.expr | None:
    """模块顶层 ``name = …`` 的值表达式。"""
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return node.value
    return None


def _declared_api(tree: ast.Module) -> object:
    """模块顶层 ``PLUGIN_API = <整数或 {类型: 整数}>`` 的值；没有这样的声明时返回 _MISSING。"""
    value = _assigned(tree, "PLUGIN_API")
    if value is None:
        return _MISSING
    try:
        return ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _literal(node: ast.expr) -> object:
    """常量，或 {语言: 文字} 这样只含字符串的字典（多语言名称）；其他写法读不到，返回 None。"""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, dict) and not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return None
    return value


def _constants(tree: ast.Module) -> dict[str, object]:
    """模块顶层 ``NAME = <常量>`` 的值，用于解析插件类属性引用的模块常量（例如图标源码）。"""
    found: dict[str, object] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.Constant, ast.Dict))
        ):
            found[node.targets[0].id] = _literal(node.value)
    return found


def _class_attrs(node: ast.ClassDef, constants: dict[str, object]) -> dict[str, object]:
    """类体中 ``name = <常量或模块常量名>`` 的值。"""
    attrs: dict[str, object] = {}
    for item in node.body:
        target = (
            item.targets[0]
            if isinstance(item, ast.Assign) and len(item.targets) == 1
            else item.target
            if isinstance(item, ast.AnnAssign)
            else None
        )
        if not isinstance(target, ast.Name):
            continue
        value = getattr(item, "value", None)
        if isinstance(value, (ast.Constant, ast.Dict)):
            attrs[target.id] = _literal(value)
        elif isinstance(value, ast.Name) and value.id in constants:
            attrs[target.id] = constants[value.id]
    return attrs


def _base_names(node: ast.ClassDef) -> set[str]:
    return {
        base.id if isinstance(base, ast.Name) else base.attr
        for base in node.bases
        if isinstance(base, (ast.Name, ast.Attribute))
    }


def _text(value: object) -> str:
    if isinstance(value, dict):
        return localized(value)
    return value.strip() if isinstance(value, str) else ""


def _homepage(value: object) -> str:
    text = _text(value)
    return text if text.startswith(("https://", "http://")) else ""


def remote_icon(value: object) -> str:
    """``PLUGIN_ICON`` 是 https 地址时返回该地址，否则为空。"""
    text = _text(value)
    return text if text.startswith("https://") else ""


def kinds_of(kind_ids: Any) -> list[dict[str, str]]:
    """类型 id 列表 → [{id, label}]，按 registry.KINDS 的顺序。"""
    ids = set(kind_ids)
    return [
        {"id": kind.id, "label": kind.label} for kind in registry.KINDS.values() if kind.id in ids
    ]


def declared_info(tree: ast.Module) -> dict[str, Any]:
    """静态读取插件声明的信息（名称、类型、版本、主页、图标），不执行代码，供用户安装前查看。

    只认字符串常量；以变量、函数等方式提供的值读不到，留空。实际信息以执行后为准。
    """
    kinds = {kind.base.__name__: kind for kind in registry.KINDS.values()}
    constants = _constants(tree)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    members = []
    for node in classes:
        kind = next((kinds[name] for name in _base_names(node) if name in kinds), None)
        if kind:
            attrs = _class_attrs(node, constants)
            members.append((kind, attrs))
    if not members:
        node = next((node for node in classes if "label" in _class_attrs(node, constants)), None)
        if node:
            members = [(None, _class_attrs(node, constants))]
    single = members[0][1] if len(members) == 1 else {}

    def declared(module_key: str, class_key: str) -> object:
        value = constants.get(module_key)
        filled = (isinstance(value, str) and value.strip()) or (isinstance(value, dict) and value)
        return value if filled else single.get(class_key)

    icon = declared("PLUGIN_ICON", "icon")
    return {
        "id": _text(declared("PLUGIN_ID", "id")),
        "label": _text(declared("PLUGIN_NAME", "label")),
        "version": version_text(declared("PLUGIN_VERSION", "version")),
        "description": _text(declared("PLUGIN_DESCRIPTION", "description")),
        "homepage": _homepage(declared("PLUGIN_HOMEPAGE", "homepage")),
        "icon": icon_uri(icon),
        "icon_url": remote_icon(icon),
        "kinds": kinds_of(kind.id for kind, _ in members if kind),
        "members": [
            {
                "id": _text(attrs.get("id")),
                "kind": kind.id,
                "kind_label": kind.label,
                "label": _text(attrs.get("label")),
            }
            for kind, attrs in members
            if kind
        ],
    }


def _check_declared_api(declared: object) -> None:
    """静态检查 PLUGIN_API：整数须在某类插件支持的版本内，字典的每一项须在对应类型支持的版本内。"""
    if declared is _MISSING:
        raise PluginError(msg("plugins.install.notPlugin"))
    if isinstance(declared, dict):
        if not declared:
            raise PluginError(msg("plugins.install.emptyApi"))
        for kind_id, version in declared.items():
            kind = registry.KINDS.get(kind_id) if isinstance(kind_id, str) else None
            if kind is None:
                raise PluginError(msg("plugins.install.unknownKind", kind_id=f"{kind_id!r}", known=list(registry.KINDS)))
            _check_api(version, set(kind.api_versions), kind.label)
        return
    _check_api(declared, {v for kind in registry.KINDS.values() for v in kind.api_versions})


def _check_api(declared: object, expected: set[int], kind_label: str = "") -> None:
    if isinstance(declared, bool) or not isinstance(declared, int) or declared not in expected:
        prefix = msg("plugins.install.kindPlugin", kind_label=kind_label) if kind_label else msg("plugins.install.what.plugin")
        raise PluginError(
            msg("plugins.install.incompatible", prefix=prefix, declared=f"{declared!r}", wanted=sorted(expected))
        )


def _compile(source: str, origin: str) -> tuple[types.CodeType, dict[str, Any]]:
    """静态检查，不执行：不是插件的文件、接口版本不匹配的插件在这里就被拒绝。返回编译结果和静态读取的插件信息。"""
    try:
        tree = ast.parse(source, origin)
        code = compile(tree, origin, "exec")
    except (SyntaxError, ValueError) as exc:
        raise PluginError(msg("plugins.install.syntax", exc=exc)) from exc
    _check_declared_api(_declared_api(tree))
    return code, declared_info(tree)


# ---- 执行后检查 ----


@dataclass(frozen=True)
class _Plugin:
    """执行后的插件文件。icon 是模块或插件类声明的图标原文（可能是远程地址）。"""

    id: str
    label: str
    version: str
    homepage: str
    icon: str
    members: tuple[tuple[registry.PluginKind, type], ...]


def _module_text(module: types.ModuleType, name: str, *, localizable: bool = False) -> str:
    """模块常量的文字。``localizable`` 时也可以写成 {语言: 文字}（见 i18n.localized）。"""
    value = vars(module).get(name)
    if localizable and isinstance(value, Mapping):
        return localized(value)
    if value is not None and not isinstance(value, str):
        raise PluginError(msg("plugins.install.notString", name=name))
    return (value or "").strip()


def _check_member(module: types.ModuleType, kind: registry.PluginKind, plugin: type) -> None:
    declared = vars(module).get("PLUGIN_API", _MISSING)
    if declared is _MISSING:
        raise PluginError(msg("plugins.install.notPlugin"))
    if isinstance(declared, dict):
        if kind.id not in declared:
            raise PluginError(msg("plugins.install.missingKindApi", label=kind.label, id=kind.id))
        _check_api(declared[kind.id], set(kind.api_versions), kind.label)
    else:
        _check_api(declared, set(kind.api_versions), kind.label)
    plugin_id = getattr(plugin, "id", None)
    if not isinstance(plugin_id, str) or not _ID_PATTERN.fullmatch(plugin_id):
        raise PluginError(
            msg("plugins.install.badId", cls=plugin.__name__)
        )
    if not isinstance(getattr(plugin, "label", None), str) or not plugin.label.strip():
        raise PluginError(msg("plugins.install.noLabel", cls=plugin.__name__))
    if kind.id == "media_source" and getattr(plugin, "credential_fields", ()):
        raise PluginError(
            msg("plugins.install.sourceCredentials")
        )


def _check_conditions(plugin: type) -> None:
    """字段的显示条件只能引用同一组字段（每份配置或插件设置）里的其他字段。"""
    for fields in (getattr(plugin, "credential_fields", ()), getattr(plugin, "settings_fields", ())):
        for field_key, missing in check_conditions(tuple(fields)):
            raise PluginError(
                msg("plugins.install.badCondition", cls=plugin.__name__, field=field_key, key=missing)
            )


def _apply_settings(
    module: types.ModuleType, members: list[tuple[registry.PluginKind, type]]
) -> None:
    """模块级 PLUGIN_SETTINGS 作为文件内全部插件类共用的插件设置。"""
    configurable = [plugin for _, plugin in members if issubclass(plugin, PluginInfo)]
    declared = vars(module).get("PLUGIN_SETTINGS")
    if declared is None:
        if len(members) > 1 and any(plugin.settings_fields for plugin in configurable):
            raise PluginError(
                msg("plugins.install.sharedSettings")
            )
        return
    if not isinstance(declared, (tuple, list)) or not all(
        isinstance(field, CredentialField) for field in declared
    ):
        raise PluginError(msg("plugins.install.settingsType"))
    if not configurable:
        raise PluginError(msg("plugins.install.settingsKinds"))
    fields = tuple(declared)
    for plugin in configurable:
        if plugin.settings_fields and tuple(plugin.settings_fields) != fields:
            raise PluginError(
                msg("plugins.install.settingsConflict", cls=plugin.__name__)
            )
        plugin.settings_fields = fields


def _plugin_of(module: types.ModuleType) -> _Plugin:
    found = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and value.__module__ == module.__name__
        and not inspect.isabstract(value)
        and registry.kind_of(value) is not None
    ]
    if not found:
        raise PluginError(
            msg("plugins.install.noClass")
        )
    members: list[tuple[registry.PluginKind, type]] = []
    seen: set[tuple[str, str]] = set()
    for plugin in found:
        kind = registry.kind_of(plugin)
        assert kind is not None
        _check_member(module, kind, plugin)
        if (kind.id, plugin.id) in seen:
            raise PluginError(msg("plugins.install.duplicateId", label=kind.label, id=plugin.id))
        seen.add((kind.id, plugin.id))
        members.append((kind, plugin))
    _apply_settings(module, members)
    for _, plugin in members:
        _check_conditions(plugin)
    for _, plugin in members:
        localize_plugin(plugin)
        try:
            plugin.describe()
        except Exception as exc:
            raise PluginError(
                msg("plugins.install.badInfo", exc=exc, cls=plugin.__name__, error_type=type(exc).__name__)
            ) from exc
    single = members[0][1] if len(members) == 1 else None
    plugin_id = _module_text(module, "PLUGIN_ID") or (single.id if single else "")
    label = _module_text(module, "PLUGIN_NAME", localizable=True) or (single.label.strip() if single else "")
    if not plugin_id or not label:
        raise PluginError(
            msg("plugins.install.needModuleId")
        )
    if not _ID_PATTERN.fullmatch(plugin_id):
        raise PluginError(msg("plugins.install.badModuleId"))
    version = version_text(vars(module).get("PLUGIN_VERSION")) or (
        version_text(single.version) if single else ""
    )
    homepage = _homepage(vars(module).get("PLUGIN_HOMEPAGE")) or (
        _homepage(single.homepage) if single else ""
    )
    icon = _module_text(module, "PLUGIN_ICON") or (
        _text(getattr(single, "icon", "")) if single else ""
    )
    return _Plugin(plugin_id, label, version, homepage, icon, tuple(members))


def _load(source: str, origin: str) -> tuple[_Plugin, str]:
    """执行插件源码，返回插件和模块名。失败时不留下模块。"""
    code, _ = _compile(source, origin)
    name = _MODULE_PREFIX + uuid.uuid4().hex
    module = types.ModuleType(name)
    module.__file__ = origin
    # pydantic 和 dataclass 解析类型标注时要按模块名找到命名空间。
    sys.modules[name] = module
    try:
        try:
            exec(code, module.__dict__)  # noqa: S102 - 只执行用户按 SHA-256 确认过的代码
        except Exception as exc:
            raise PluginError(msg("plugins.install.startFailed", exc=exc, error_type=type(exc).__name__)) from exc
        return _plugin_of(module), name
    except PluginError:
        sys.modules.pop(name, None)
        raise


def _owner(kind: str, plugin_id: str) -> str | None:
    """注册表中 (类型, id) 的插件类来自哪个在控制台安装的插件。"""
    return next(
        (file_id for file_id, loaded in _loaded.items() if (kind, plugin_id) in loaded.members),
        None,
    )


def _conflict(plugin: _Plugin) -> str:
    """插件（或其中某个插件类）的 id 与内置插件、Python 包插件或其他已安装插件的冲突说明；没有冲突时为空。"""
    for kind, member in plugin.members:
        if registry.get(kind.id, member.id) is not None and _owner(kind.id, member.id) != plugin.id:
            other = _owner(kind.id, member.id)
            where = msg("plugins.install.wherePlugin", other=other) if other else msg("plugins.install.whereBuiltin")
            return msg("plugins.install.idTaken", where=where, id=member.id)
    # 插件 id 也是插件设置的保存位置，不能与内置插件、Python 包插件的 id 相同。
    for kind in registry.KINDS.values():
        if registry.get(kind.id, plugin.id) is not None and _owner(kind.id, plugin.id) is None:
            return msg("plugins.install.builtinTaken", id=plugin.id)
    return ""


def _activate(plugin: _Plugin, module_name: str, icon: str) -> None:
    """注册文件中的插件类。icon 是保存的图标 data URI，补给没有自己图标的插件类。"""
    old = _loaded.get(plugin.id)
    members = tuple((kind.id, member.id) for kind, member in plugin.members)
    if old:
        for kind_id, member_id in old.members:
            if (kind_id, member_id) not in members:
                registry.unregister(kind_id, member_id)
    for kind, member in plugin.members:
        member.plugin_key = plugin.id
        if icon and not icon_uri(getattr(member, "icon", "")):
            member.icon = icon
        registry.register(kind.id, member)
    _loaded[plugin.id] = _Loaded(module_name, members)
    _errors.pop(plugin.id, None)
    if old and old.module != module_name:
        sys.modules.pop(old.module, None)


def _deactivate(plugin_id: str) -> None:
    _errors.pop(plugin_id, None)
    if loaded := _loaded.pop(plugin_id, None):
        for kind_id, member_id in loaded.members:
            registry.unregister(kind_id, member_id)
        sys.modules.pop(loaded.module, None)


def members(plugin_id: str) -> list[tuple[str, type]]:
    """插件中的插件类 [(类型, 插件类)]：在控制台安装的按插件 id 查；内置和 Python 包的插件按插件类 id 查。"""
    if loaded := _loaded.get(plugin_id):
        return [
            (kind_id, plugin)
            for kind_id, member_id in loaded.members
            if (plugin := registry.get(kind_id, member_id)) is not None
        ]
    if plugin_id in _errors:
        return []
    found = registry.find(plugin_id)
    return [found] if found else []


def load_installed(database: Database) -> list[tuple[str, str]]:
    """主控启动时调用：按数据库中保存的源码加载全部已安装插件。单个插件失败不影响其他插件。

    返回没有加载的插件 [(插件 id, 原因)]，由调用方写入提醒。
    """
    for plugin_id in list(_loaded):
        _deactivate(plugin_id)
    _errors.clear()
    installed = database.installed_plugins()
    if safe_mode():
        for plugin_id in installed:
            _errors[plugin_id] = msg("plugins.install.safeModeSkipped", SAFE_MODE_ENV=SAFE_MODE_ENV)
        return list(_errors.items())
    for plugin_id, record in installed.items():
        try:
            plugin, module_name = _load(record.get("source", ""), _origin(record, plugin_id))
            problem = (
                msg("plugins.install.idChanged", id=plugin.id, plugin_id=plugin_id)
                if plugin.id != plugin_id
                else _conflict(plugin)
            )
            if problem:
                sys.modules.pop(module_name, None)
                raise PluginError(problem)
        except PluginError as exc:
            logger.warning("加载插件 %s 失败：%s", plugin_id, exc.message)
            _errors[plugin_id] = exc.message
            continue
        _activate(plugin, module_name, record.get("icon", ""))
    return list(_errors.items())


def _usage(database: Database, plugin_members: list[tuple[str, str]]) -> int:
    """使用这些插件类的配置数量。媒体库来源插件按媒体库计数，由接口层补上。"""
    count = 0
    for kind, member_id in plugin_members:
        if kind == "cloud":
            records: Any = database.cloud_profiles().values()
        elif kind in CONNECTION_KEYS:
            records = database.plugin_connections(kind).values()
        else:
            continue
        count += sum(1 for record in records if record.get("type") == member_id)
    return count


def _member_info(kind_id: str, member_id: str) -> dict[str, str]:
    kind = registry.KINDS.get(kind_id)
    plugin = registry.get(kind_id, member_id) if kind else None
    return {
        "id": member_id,
        "kind": kind_id,
        "kind_label": kind.label if kind else kind_id,
        "label": plugin.label if plugin else member_id,
    }


def describe(database: Database) -> list[dict[str, Any]]:
    result = []
    for plugin_id, record in database.installed_plugins().items():
        loaded = _loaded.get(plugin_id)
        stored = [tuple(item) for item in record.get("members") or () if len(item) == 2]
        plugin_members = list(loaded.members) if loaded else stored
        result.append(
            {
                "id": plugin_id,
                "kinds": kinds_of(kind for kind, _ in plugin_members),
                "members": [_member_info(kind, member) for kind, member in plugin_members],
                "source": "url" if record.get("url") else "upload",
                "label": record.get("label") or plugin_id,
                "version": record.get("version", ""),
                "homepage": record.get("homepage", ""),
                "icon": record.get("icon", ""),
                "url": record.get("url", ""),
                "filename": record.get("filename", ""),
                "sha256": record.get("sha256", ""),
                "installed_at": record.get("installed_at"),
                "updated_at": record.get("updated_at"),
                "error": _errors.get(plugin_id),
                "usage": _usage(database, plugin_members),
            }
        )
    return result


def describe_all(database: Database) -> list[dict[str, Any]]:
    """全部已安装的插件：内置的、Python 包（entry point）提供的，以及在控制台安装的（按地址或本地上传）。

    前两种随主控代码一起安装，不能在控制台更新或卸载。
    """
    result = []
    for kind in registry.KINDS.values():
        builtin = {plugin.id for plugin in kind.builtin}
        for plugin in registry.providers(kind.id).values():
            if _owner(kind.id, plugin.id) is not None:
                continue
            result.append(
                {
                    "id": plugin.id,
                    "kinds": kinds_of([kind.id]),
                    "members": [_member_info(kind.id, plugin.id)],
                    "source": "builtin" if plugin.id in builtin else "package",
                    "label": plugin.label,
                    "homepage": plugin.homepage,
                    "icon": icon_uri(plugin.icon),
                    "usage": _usage(database, [(kind.id, plugin.id)]),
                }
            )
    return result + describe(database)


# ---- 图标 ----


@dataclass(frozen=True)
class _Icon:
    """插件图标：data URI，以及远程图标的地址和文件指纹（内联图标两者为空）。"""

    uri: str = ""
    url: str = ""
    sha256: str = ""


async def _remote_icon(url: str, transport: httpx.AsyncBaseTransport | None) -> _Icon:
    url = normalize_url(url, msg("plugins.install.what.icon"))
    data = await fetch(url, transport, what=msg("plugins.install.what.icon"), limit=ICON_MAX_BYTES)
    try:
        uri = icon_from_bytes(data)
    except ValueError as exc:
        raise PluginError(msg("plugins.install.iconUnusable", exc=exc)) from exc
    return _Icon(uri, url, _digest(data))


async def _preview_icon(
    info: dict[str, Any], transport: httpx.AsyncBaseTransport | None
) -> dict[str, str]:
    """预览时下载远程图标。图标下载失败不影响安装，只是没有图标。"""
    if not info.get("icon_url"):
        return {"icon_sha256": ""}
    try:
        icon = await _remote_icon(info["icon_url"], transport)
    except PluginError as exc:
        return {"icon": "", "icon_sha256": "", "icon_error": exc.message}
    return {"icon": icon.uri, "icon_url": icon.url, "icon_sha256": icon.sha256}


async def _confirmed_icon(
    plugin: _Plugin, icon_sha256: str, transport: httpx.AsyncBaseTransport | None
) -> _Icon:
    """安装时的图标：远程图标重新下载，与用户确认的指纹一致才保存；确认时没有图标（下载失败）则不保存。"""
    url = remote_icon(plugin.icon)
    if not url:
        return _Icon(icon_uri(plugin.icon))
    if not icon_sha256:
        return _Icon()
    icon = await _remote_icon(url, transport)
    if icon.sha256 != icon_sha256.strip().lower():
        raise PluginError(
            msg("plugins.install.iconChanged"),
            conflict=True,
            mismatch=True,
        )
    return icon


# ---- 安装、更新、卸载 ----


def _confirmed(source: str, sha256: str, *, upload: bool = False) -> None:
    if _digest(source) != sha256.strip().lower():
        where = msg("plugins.install.where.upload") if upload else msg("plugins.install.what.file")
        raise PluginError(
            msg("plugins.install.fileChanged", where=where),
            conflict=True,
            mismatch=True,
        )


def _result(plugin: _Plugin, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": plugin.id,
        "kinds": kinds_of(kind.id for kind, _ in plugin.members),
        "label": plugin.label,
        "version": plugin.version,
        "url": record.get("url", ""),
        "filename": record.get("filename", ""),
        "sha256": record.get("sha256", ""),
        "icon_url": record.get("icon_url", ""),
        "icon_sha256": record.get("icon_sha256", ""),
    }


async def _store(
    database: Database,
    url: str,
    source: str,
    *,
    expected_id: str | None,
    icon_sha256: str,
    transport: httpx.AsyncBaseTransport | None,
    filename: str = "",
) -> dict[str, Any]:
    """执行已确认的源码，保存并注册。url 为空表示本地上传，filename 是上传的文件名。

    同一来源（同一地址，或都是本地上传）安装已有 id 的插件即为更新；来源不同时要求先卸载。
    """
    installed = database.installed_plugins()
    plugin, module_name = _load(source, _origin({"url": url, "filename": filename}, ""))
    problem = None
    if expected_id and plugin.id != expected_id:
        problem = PluginError(msg("plugins.install.otherPlugin"))
    elif conflict := _conflict(plugin):
        problem = PluginError(conflict, conflict=True)
    elif (
        not expected_id
        and plugin.id in installed
        and (installed[plugin.id].get("url") or "") != url
    ):
        other = msg("plugins.install.source.otherUrl") if installed[plugin.id].get("url") else msg("plugins.install.source.upload")
        problem = PluginError(
            msg("plugins.install.installedElsewhere", label=plugin.label, other=other),
            conflict=True,
        )
    if problem is None:
        try:
            icon = await _confirmed_icon(plugin, icon_sha256, transport)
        except PluginError as exc:
            problem = exc
    if problem:
        sys.modules.pop(module_name, None)
        raise problem
    now = utc_now_iso()
    sha256 = _digest(source)
    previous = installed.get(plugin.id)
    changed = not (
        previous
        and plugin.id in _loaded
        and previous.get("sha256") == sha256
        and previous.get("icon_sha256", "") == icon.sha256
    )
    record = {
        "kinds": [kind["id"] for kind in kinds_of(kind.id for kind, _ in plugin.members)],
        "members": [[kind.id, member.id] for kind, member in plugin.members],
        "label": plugin.label,
        "url": url,
        **({"filename": filename} if filename else {}),
        "sha256": sha256,
        "version": plugin.version,
        "homepage": plugin.homepage,
        "icon": icon.uri,
        "icon_url": icon.url,
        "icon_sha256": icon.sha256,
        "source": source,
        "installed_at": (previous or {}).get("installed_at") or now,
        "updated_at": now,
    }
    database.set_installed_plugin(plugin.id, record)
    _activate(plugin, module_name, icon.uri)
    return {**_result(plugin, record), "changed": changed, "updated": previous is not None}


def _preview_result(source: str, info: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**info, **extra, "sha256": _digest(source), "size": len(source.encode())}


async def preview(url: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """安装的第一步：下载并静态检查，不执行。返回静态读取的插件信息和 SHA-256，由用户核对后交给 install。"""
    _require_normal_mode()
    url = normalize_url(url)
    source = await fetch_text(url, transport)
    _, info = _compile(source, url)
    return _preview_result(source, {**info, **await _preview_icon(info, transport)}, url=url)


async def install(
    database: Database,
    url: str,
    sha256: str,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    icon_sha256: str = "",
) -> dict[str, Any]:
    """安装的第二步：重新下载，内容与用户确认的 SHA-256 一致才执行。"""
    _require_normal_mode()
    url = normalize_url(url)
    source = await fetch_text(url, transport)
    _confirmed(source, sha256)
    return await _store(
        database, url, source, expected_id=None, icon_sha256=icon_sha256, transport=transport
    )


async def preview_upload(
    filename: str, source: str, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """上传安装的第一步：只做静态检查，不执行。返回静态读取的插件信息和 SHA-256，由用户核对后交给 install_upload。"""
    _require_normal_mode()
    filename = upload_name(filename)
    _check_size(source)
    _, info = _compile(source, _UPLOAD_PREFIX + filename)
    return _preview_result(
        source, {**info, **await _preview_icon(info, transport)}, url="", filename=filename
    )


async def install_upload(
    database: Database,
    filename: str,
    source: str,
    sha256: str,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    icon_sha256: str = "",
) -> dict[str, Any]:
    """上传安装的第二步：内容与用户确认的 SHA-256 一致才执行。已从本地上传安装的同一插件会被替换（更新）。"""
    _require_normal_mode()
    filename = upload_name(filename)
    _check_size(source)
    _confirmed(source, sha256, upload=True)
    return await _store(
        database,
        "",
        source,
        expected_id=None,
        icon_sha256=icon_sha256,
        transport=transport,
        filename=filename,
    )


def _installed(database: Database, plugin_id: str) -> dict[str, Any]:
    plugin = database.installed_plugins().get(plugin_id)
    if plugin is None:
        raise PluginError(msg("plugins.install.notInstalled"))
    return plugin


def _by_url(database: Database, plugin_id: str) -> dict[str, Any]:
    """按地址安装的插件记录；本地上传的插件没有地址，不能检查更新。"""
    plugin = _installed(database, plugin_id)
    if not plugin.get("url"):
        raise PluginError(msg("plugins.install.uploadNoUpdate"))
    return plugin


def _unchanged(plugin_id: str, plugin: dict[str, Any], source: str) -> bool:
    return plugin_id in _loaded and plugin.get("sha256") == _digest(source)


async def check_update(
    database: Database, plugin_id: str, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """更新的第一步：从安装时的地址重新下载并静态检查，不执行。返回新内容静态读取的插件信息和 SHA-256。

    源码没有变化时不下载图标；有变化时远程图标一并重新下载，供用户核对。
    """
    _require_normal_mode()
    plugin = _by_url(database, plugin_id)
    source = await fetch_text(plugin["url"], transport)
    _, info = _compile(source, plugin["url"])
    changed = not _unchanged(plugin_id, plugin, source)
    icon = await _preview_icon(info, transport) if changed else {}
    return _preview_result(
        source, {**info, **icon}, id=plugin_id, url=plugin["url"], changed=changed
    )


async def update(
    database: Database,
    plugin_id: str,
    sha256: str,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    icon_sha256: str = "",
) -> dict[str, Any]:
    """更新的第二步：内容与用户确认的 SHA-256 一致才执行。源码没有变化时不重新加载。"""
    _require_normal_mode()
    plugin = _by_url(database, plugin_id)
    source = await fetch_text(plugin["url"], transport)
    _confirmed(source, sha256)
    if _unchanged(plugin_id, plugin, source):
        return {"id": plugin_id, "sha256": _digest(source), "changed": False}
    return await _store(
        database,
        plugin["url"],
        source,
        expected_id=plugin_id,
        icon_sha256=icon_sha256,
        transport=transport,
    )


def uninstall(database: Database, plugin_id: str) -> dict[str, Any]:
    """卸载并返回被卸载的记录（不含源码和图标），供调用方写入提醒。"""
    plugin = _installed(database, plugin_id)
    database.set_installed_plugin(plugin_id, None)
    _deactivate(plugin_id)
    return {key: value for key, value in plugin.items() if key not in {"source", "icon"}}
