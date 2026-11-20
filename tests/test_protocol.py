import asyncio

import httpx
from conftest import raises_rendered

from video_turbo.protocol import PROTOCOL_MISMATCH_STATUS
from video_turbo.worker.main import ProtocolMismatch, _check_protocol


def test_worker_turns_protocol_rejection_into_readable_error() -> None:
    request = httpx.Request("POST", "http://server/api/workers/register")
    rejected = httpx.Response(PROTOCOL_MISMATCH_STATUS, json={"detail": "请升级"}, request=request)
    with raises_rendered(ProtocolMismatch, "请升级"):
        asyncio.run(_check_protocol(rejected))
    # 其他状态码交给调用方自己的 raise_for_status 处理。
    asyncio.run(_check_protocol(httpx.Response(500, request=request)))
