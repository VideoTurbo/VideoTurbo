"""Readable ASR failure reports, retaining the backend's diagnostic output."""

from __future__ import annotations

import re

import httpx

from ..i18n import clip, msg, render


def describe_asr_failure(error: Exception, stage: str | None) -> str:
    detail = str(error).strip() or type(error).__name__
    # 判断错误类别时看中文原文（错误信息里可能有多语言标记）。
    lower = render(detail).lower()
    if "audio integrity:" in lower:
        gaps = re.search(r"(\d+) gap\(s\) in the audio track, ([\d.]+) s in all", detail)
        summary = (
            msg("worker.error.gaps", count=gaps[1], seconds=gaps[2])
            if gaps else msg("worker.error.incomplete")
        )
        summary += msg("worker.error.checkSource")
    elif "out of memory" in lower or "cuda_error_out_of_memory" in lower:
        summary = msg("worker.error.memory")
    elif isinstance(error, httpx.TimeoutException):
        summary = msg("worker.error.serverTimeout")
    elif isinstance(error, httpx.HTTPStatusError):
        summary = msg("worker.error.serverStatus", status_code=error.response.status_code)
    elif isinstance(error, httpx.TransportError):
        summary = msg("worker.error.serverFailed")
    elif "超时" in lower or "timeout" in lower or "timed out" in lower:
        summary = msg("worker.error.tooSlow")
    else:
        summary = msg("worker.error.failed")
    context = msg("worker.error.stage", stage=clip(stage or msg("worker.error.preparing"), 200))
    # Keep the beginning and the final backend summary when a traceback is large.
    if len(detail) > 7000:
        detail = detail[:3500] + "\n…（中间日志已省略）…\n" + detail[-3500:]
    return msg("worker.error.report", context=context, summary=summary, detail=detail, error_type=type(error).__name__)
