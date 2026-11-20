"""本机在局域网里的地址：给“添加显卡电脑”提供候选的接入地址。

标准库没有列出网卡的接口，这里用两个办法凑出常用的地址：向外网地址“连接”一个 UDP 套接字（不发送任何数据）
得到默认出口的地址，再加上本机主机名解析出的地址。只保留局域网、Tailscale 等私有网段的 IPv4 地址。
Docker 里拿到的是容器内部地址，对其他电脑没有用，调用方在 Docker 版里不使用它。
"""

from __future__ import annotations

import ipaddress
import socket

# 100.64.0.0/10 是运营商级 NAT 网段，Tailscale 等组网工具也用它。
_SHARED = ipaddress.ip_network("100.64.0.0/10")


def _usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return False
    return ip.is_private or ip in _SHARED


def _outbound_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # 文档保留地址，不会真的发包
            return probe.getsockname()[0]
    except OSError:
        return None


def _hostname_addresses() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


def lan_addresses() -> list[str]:
    """默认出口的地址排在最前，其余按出现顺序，去重。"""
    found = [_outbound_address(), *_hostname_addresses()]
    return list(dict.fromkeys(address for address in found if address and _usable(address)))
