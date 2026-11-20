"""主程序的部署方式：提示文案和诊断信息据此说明"去哪里改"。

- ``docker``：官方镜像（Dockerfile 设置），设置在 docker-compose.yml 里改。
- ``desktop``：桌面版安装包（外壳启动主程序时设置），设置在托盘菜单里改。
- ``source``：从源码运行（未设置或无法识别时），提示文案与 Docker 相同。
"""

from __future__ import annotations

from .environment import getenv

DEPLOYMENT_ENV = "VIDEO_TURBO_DEPLOYMENT"
KINDS = ("docker", "desktop", "source")


def deployment() -> str:
    value = (getenv(DEPLOYMENT_ENV) or "").strip().lower()
    return value if value in KINDS else "source"


def is_desktop() -> bool:
    return deployment() == "desktop"
