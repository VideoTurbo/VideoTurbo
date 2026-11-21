"""桌面版的本机识别状态：外壳定时上报，控制台在识别程序连上之前也能看到本机在做什么。

本机识别开启后要先下载运行环境、下载模型并自检，识别程序启动后才会报到、出现在“已接入的机器”里；
这之前（等待下载、安装中、出错）只有外壳知道进度。外壳每隔几秒把状态 ``PUT`` 到
``/api/auth/desktop/local-worker``（只接受本机请求和这次启动的令牌），状态放在内存里，
一段时间没有上报就不再显示。控制台点“开始下载”或“重试”时记下请求，外壳下次上报时从响应里取走。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..i18n import msg
from .auth import DESKTOP_TOKEN_HEADER, LOOPBACK, _auth, _client
from .context import app_state

# 外壳每 2 秒上报一次；超过这个时间没有上报（外壳已退出）就不再显示。
FRESH_SECONDS = 15.0

router = APIRouter()


class LocalWorkerReport(BaseModel):
    """外壳上报的本机识别状态，字段与外壳的识别状态窗口一致。"""

    model_config = ConfigDict(extra="ignore")

    phase: str = Field(max_length=32)
    enabled: bool
    # 运行环境和模型都已就绪。
    ready: bool = False
    # 安装运行环境：第几步 / 共几步和步骤名称。
    index: int | None = Field(default=None, ge=0, le=100)
    total: int | None = Field(default=None, ge=0, le=100)
    zh: str = Field(default="", max_length=200)
    en: str = Field(default="", max_length=200)
    # 安装或自检的最新一行输出。
    line: str = Field(default="", max_length=500)
    # 出错时的说明。
    message: str = Field(default="", max_length=1000)


class LocalWorkerStatus:
    def __init__(self) -> None:
        self._report: LocalWorkerReport | None = None
        self._at = 0.0
        self._download = False

    def update(self, report: LocalWorkerReport) -> bool:
        """记下外壳的上报，返回控制台是否请求了下载（取走即清除）。"""
        self._report = report
        self._at = time.monotonic()
        download, self._download = self._download, False
        return download

    def current(self) -> dict | None:
        report = self._report
        if report is None or not report.enabled or time.monotonic() - self._at > FRESH_SECONDS:
            return None
        return {**report.model_dump(), "download_requested": self._download}

    def request_download(self) -> bool:
        if self.current() is None:
            return False
        self._download = True
        return True


def local_worker_status(request: Request) -> LocalWorkerStatus:
    return app_state(request).local_worker


@router.put("/api/auth/desktop/local-worker", include_in_schema=False)
async def report_local_worker(request: Request, report: LocalWorkerReport) -> dict:
    """外壳上报本机识别状态：只接受本机请求和这次启动的令牌（请求头）。"""
    if _client(request) not in LOOPBACK or not _auth(request).check_desktop_token(
        request.headers.get(DESKTOP_TOKEN_HEADER, "")
    ):
        raise HTTPException(status_code=403, detail=msg("auth.desktopTokenInvalid"))
    return {"download": local_worker_status(request).update(report)}


@router.post("/api/desktop/local-worker/download")
async def download_local_worker(request: Request) -> dict:
    """控制台点“开始下载”或“重试”：外壳下次上报时开始。"""
    if not local_worker_status(request).request_download():
        raise HTTPException(status_code=409, detail=msg("api.compute.localWorkerUnavailable"))
    return {"requested": True}
