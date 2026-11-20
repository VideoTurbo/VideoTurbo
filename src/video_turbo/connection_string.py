"""接入串：把主程序的接入地址和接入密钥合成一段文本，显卡电脑粘贴一次就能连上。

格式是 ``VT1:`` 加 JSON（``{"u": 地址, "t": 密钥}``）的 base64url 编码（去掉末尾的 ``=``）。
开头的版本号以后可以换成别的格式，旧的识别机仍能认出新旧版本。接入串里带着密钥，与密钥一样要保密。
桌面版识别机应用（Rust）按同一格式解析，见 desktop/crates/shell-core/src/connection.rs。
"""

from __future__ import annotations

import base64
import binascii
import json
from urllib.parse import urlsplit

from .i18n import msg

PREFIX = "VT1:"


def _check_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(msg("connection.badUrl", url=url))
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(msg("connection.badUrl", url=url))
    return url


def encode(url: str, token: str) -> str:
    if not token.strip():
        raise ValueError(msg("connection.noToken"))
    payload = json.dumps({"u": _check_url(url), "t": token.strip()}, separators=(",", ":"))
    return PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def looks_like(text: str) -> bool:
    return text.strip().upper().startswith(PREFIX)


def decode(text: str) -> tuple[str, str]:
    """返回（主程序地址，接入密钥）；不是有效的接入串时抛出 ValueError。"""
    text = "".join(text.split())  # 复制时可能带上换行或空格
    if not looks_like(text):
        raise ValueError(msg("connection.invalid"))
    body = text[len(PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(msg("connection.invalid")) from exc
    fields = payload if isinstance(payload, dict) else {}
    url, token = fields.get("u"), fields.get("t")
    if not (isinstance(url, str) and isinstance(token, str)):
        raise ValueError(msg("connection.invalid"))  # noqa: TRY004 - 对调用方来说就是“值不对”
    if not token.strip():
        raise ValueError(msg("connection.noToken"))
    return _check_url(url), token.strip()
