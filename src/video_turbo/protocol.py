"""主程序与 GPU 机器之间的通信协议版本。

GPU 机器在每个请求的 ``X-VideoTurbo-Protocol`` 请求头中带上本版本号，主程序版本号不同时一律拒绝
（HTTP 426），并提示把两端升级到同一版本。因此 Worker 接口改动时不需要兼容旧版 GPU 机器或旧版主程序。

以下改动在任何一端单独升级后会导致两端无法正确协作，需要把版本号加一：

- 增删或改名 ``worker_gateway.WORKER_ROUTES`` 中的接口；
- 这些接口的请求、响应字段增删、改名或含义变化（包括注册、心跳、领取任务的字段）；
- 任务状态、结果文件格式等两端共同依赖的约定发生变化。

只改主程序控制台、只改 GPU 机器内部实现时不需要加一，两端可以各自升级。
"""

from __future__ import annotations

from .i18n import msg

WORKER_PROTOCOL_VERSION = 3
PROTOCOL_HEADER = "X-VideoTurbo-Protocol"
# 版本不一致时的 HTTP 状态码：426 Upgrade Required。
PROTOCOL_MISMATCH_STATUS = 426


def mismatch_message(server: int, worker: str) -> str:
    worker_text = worker or msg("protocol.unknownVersion")
    return (
        msg("protocol.mismatch", server=server, worker_text=worker_text)
    )
