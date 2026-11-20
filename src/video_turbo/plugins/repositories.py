"""插件仓库：用户添加的仓库地址，仓库根目录的 ``plugins.json`` 列出其中的插件，控制台按类型分类显示、点击安装。

仓库只提供插件列表；安装仍走 installer 的两步流程（下载、静态检查、用户核对 SHA-256 后才执行），
列表中的信息（名称、版本、图标等）只用于展示，以安装时从插件文件读出的为准。

``plugins.json`` 的格式::

    {
      "name": "仓库名称",
      "plugins": [
        {
          "id": "chenyu",
          "name": "晨羽智云",
          "version": "1.0.0",
          "description": "一句话说明",
          "kinds": ["cloud"],
          "path": "cloud_gpu/chenyu.py",
          "sha256": "…",
          "icon": "data:image/png;base64,…",
          "homepage": "https://…"
        }
      ]
    }

``path`` 相对于 ``plugins.json`` 所在目录，也可以用 ``url`` 写完整地址；``icon`` 只接受 data URI 或内联 SVG，
控制台不访问仓库之外的图片地址。缺少必填项（id、kinds、path/url）或类型都不认识的条目跳过。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..database import Database
from ..i18n import msg
from ..models import utc_now_iso
from .base import icon_uri, version_text
from .installer import PluginError, fetch_text, kinds_of, normalize_url

INDEX_NAME = "plugins.json"
MAX_REPOSITORIES = 20
MAX_ENTRIES = 500
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GITHUB_REPO = re.compile(r"/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+)(/.*)?)?/?")


def index_url(url: str) -> str:
    """仓库地址 → plugins.json 的地址。

    支持 GitHub 仓库页面（``https://github.com/owner/repo``，可带 ``/tree/分支/子目录``）和
    任意 ``.json`` 文件的 https 地址（GitHub 的文件页面地址换成原始文件地址）。
    """
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PluginError(msg("plugins.repo.urlScheme"))
    if parts.username or parts.password:
        raise PluginError(msg("plugins.repo.urlCredentials"))
    if parts.path.lower().endswith(".json"):
        return normalize_url(url, msg("plugins.repo.what"))
    if parts.hostname == "github.com" and (match := _GITHUB_REPO.fullmatch(parts.path)):
        owner, repo, ref, folder = match.groups()
        folder = (folder or "").strip("/")
        prefix = f"{folder}/" if folder else ""
        return (
            f"https://raw.githubusercontent.com/{owner}/{repo}/{ref or 'HEAD'}/{prefix}{INDEX_NAME}"
        )
    raise PluginError(
        msg("plugins.repo.badUrl", INDEX_NAME=INDEX_NAME)
    )


def _text(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _entry(item: object, base: str) -> dict[str, Any] | None:
    """校验 plugins.json 中的一个条目；不合格时返回 None。"""
    if not isinstance(item, dict):
        return None
    plugin_id = item.get("id")
    if not isinstance(plugin_id, str) or not _ID_PATTERN.fullmatch(plugin_id):
        return None
    kinds = item.get("kinds")
    if isinstance(kinds, str):
        kinds = [kinds]
    known = (
        kinds_of(kind for kind in kinds or () if isinstance(kind, str))
        if isinstance(kinds, list)
        else []
    )
    if not known:
        return None
    target = item.get("url") or item.get("path")
    if not isinstance(target, str) or not target.strip():
        return None
    try:
        url = normalize_url(urljoin(base, target.strip()))
    except PluginError:
        return None
    sha256 = item.get("sha256")
    homepage = _text(item.get("homepage"), 500)
    return {
        "id": plugin_id,
        "label": _text(item.get("name"), 100) or plugin_id,
        "version": version_text(item.get("version")),
        "description": _text(item.get("description"), 500),
        "kinds": known,
        "url": url,
        "sha256": sha256.lower()
        if isinstance(sha256, str) and _SHA256.fullmatch(sha256.lower())
        else "",
        "icon": icon_uri(item.get("icon")),
        "homepage": homepage if homepage.startswith(("https://", "http://")) else "",
    }


def parse_index(text: str, url: str) -> dict[str, Any]:
    """解析 plugins.json。格式不对时抛出 PluginError；单个条目不合格时跳过。"""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PluginError(msg("plugins.repo.badIndex", INDEX_NAME=INDEX_NAME, exc=exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise PluginError(msg("plugins.repo.noPlugins", INDEX_NAME=INDEX_NAME))
    plugins: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data["plugins"][:MAX_ENTRIES]:
        entry = _entry(item, url)
        if entry and entry["id"] not in seen:
            seen.add(entry["id"])
            plugins.append(entry)
    return {
        "name": _text(data.get("name"), 100),
        "description": _text(data.get("description"), 500),
        "plugins": plugins,
        "skipped": min(len(data["plugins"]), MAX_ENTRIES) - len(plugins),
    }


async def fetch_index(url: str, transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    return parse_index(await fetch_text(url, transport, what=msg("plugins.repo.what")), url)


def _fallback_name(record: dict[str, str]) -> str:
    parts = urlsplit(record.get("url", ""))
    return (parts.hostname or "") + parts.path.rstrip("/")


async def _catalog_of(
    record: dict[str, str], transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any]:
    try:
        index = await fetch_index(record["index_url"], transport)
    except PluginError as exc:
        return {
            **record,
            "name": record.get("name") or _fallback_name(record),
            "error": exc.message,
            "plugins": [],
        }
    return {
        **record,
        **index,
        "name": index["name"] or record.get("name") or _fallback_name(record),
        "error": None,
    }


async def catalog(
    database: Database, transport: httpx.AsyncBaseTransport | None
) -> list[dict[str, Any]]:
    """全部仓库及其插件列表（每次重新下载 plugins.json）。单个仓库失败时带上原因，不影响其他仓库。"""
    records = database.plugin_repositories()
    return list(await asyncio.gather(*(_catalog_of(record, transport) for record in records)))


async def add(
    database: Database, url: str, transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any]:
    """添加仓库：先下载并解析 plugins.json，确认是插件仓库才保存。"""
    target = index_url(url)
    records = database.plugin_repositories()
    if any(record.get("index_url") == target for record in records):
        raise PluginError(msg("plugins.repo.duplicate"), conflict=True)
    if len(records) >= MAX_REPOSITORIES:
        raise PluginError(msg("plugins.repo.tooMany", MAX_REPOSITORIES=MAX_REPOSITORIES), conflict=True)
    index = await fetch_index(target, transport)
    record = {
        "id": uuid.uuid4().hex[:12],
        "url": url.strip(),
        "index_url": target,
        "name": index["name"],
        "added_at": utc_now_iso(),
    }
    database.set_plugin_repositories([*records, record])
    return {**record, **index, "name": index["name"] or _fallback_name(record), "error": None}


def remove(database: Database, repository_id: str) -> None:
    records = database.plugin_repositories()
    kept = [record for record in records if record.get("id") != repository_id]
    if len(kept) == len(records):
        raise PluginError(msg("plugins.repo.notFound"))
    database.set_plugin_repositories(kept)
