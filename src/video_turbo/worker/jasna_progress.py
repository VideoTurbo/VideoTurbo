"""解析 Jasna CLI 的 tqdm 进度输出。

Jasna 在非终端下仍向 stderr 写 tqdm 进度条，用 ``\\r`` 刷新同一行。已知格式：

- ``Processing video:  45%|████ |Processed: 1:23 (2848f) | Remaining: 0:10 (123f) | Speed: 35.0fps``
- ``LTX 2/4 denoise:  30%|██ | 12/40 [00:10<00:30,  1.20frame/s]``（LTX 分 4 遍）
- ``LTX download:  12%|█ | 1.80G/15.0G [01:00<07:00, 31.2MB/s]``
- ``Mosaic scan:  50%|█ | 100/200 [00:05<00:05, 20.0sample/s]``

无法识别的行不影响任务，只作为日志尾部保留，失败时上报。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..i18n import msg

_BAR = re.compile(r"(?P<desc>[^\r\n|]*?):\s*(?P<pct>\d{1,3}(?:\.\d+)?)%\|")
_REMAINING = re.compile(r"Remaining:\s*(?P<value>\d+(?::\d{2}){1,2})")
_SPEED_FPS = re.compile(r"Speed:\s*(?P<value>\d+(?:\.\d+)?)fps")
_TQDM_TIMES = re.compile(r"\[(?P<elapsed>[\d:]+)<(?P<remaining>[\d:]+|\?),\s*(?P<rate>[^\]]*)\]")
_LTX_STAGE = re.compile(r"(?P<number>\d)/(?P<count>\d)\s+(?P<stage>[A-Za-z_]+)")
_RATE_FRAMES = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:frame|f|it)/s")

_LTX_STAGE_NAMES = {
    "scan": msg("worker.jasna.stage.scan"),
    "prepare": msg("worker.jasna.stage.prepare"),
    "encode": msg("worker.jasna.stage.encode"),
    "denoise": msg("worker.jasna.stage.denoise"),
    "decode": msg("worker.jasna.stage.decode"),
    "save": msg("worker.jasna.stage.save"),
    "compose": msg("worker.jasna.stage.compose"),
}


@dataclass(frozen=True, slots=True)
class JasnaProgress:
    detail: str
    percent: float
    fps: float | None = None
    eta_seconds: float | None = None


def _clock_seconds(value: str) -> float | None:
    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds)


def _describe(desc: str) -> str:
    desc = desc.strip()
    lowered = desc.lower()
    if lowered.startswith("processing video"):
        return msg("worker.jasna.stage.restore")
    if "download" in lowered:
        return msg("worker.jasna.stage.download")
    if lowered.startswith("mosaic scan"):
        return msg("worker.jasna.stage.scanAreas")
    stage = _LTX_STAGE.search(desc)
    if stage:
        name = _LTX_STAGE_NAMES.get(stage["stage"].lower(), stage["stage"])
        return msg("worker.jasna.stage.ltxPass", name=name, number=stage['number'], count=stage['count'])
    return desc[:80] or msg("worker.jasna.stage.working")


def parse_progress_line(line: str) -> JasnaProgress | None:
    match = _BAR.search(line)
    if not match:
        return None
    percent = max(0.0, min(100.0, float(match["pct"])))
    fps: float | None = None
    eta: float | None = None
    remaining = _REMAINING.search(line)
    if remaining:
        eta = _clock_seconds(remaining["value"])
    speed = _SPEED_FPS.search(line)
    if speed:
        fps = float(speed["value"])
    times = _TQDM_TIMES.search(line)
    if times:
        if eta is None and times["remaining"] != "?":
            eta = _clock_seconds(times["remaining"])
        rate = _RATE_FRAMES.search(times["rate"])
        if fps is None and rate:
            fps = float(rate["value"])
    return JasnaProgress(detail=_describe(match["desc"]), percent=percent, fps=fps, eta_seconds=eta)


class OutputSplitter:
    """把 \\r 与 \\n 分隔的字节流切成行，保留未结束的半行。"""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, data: bytes) -> list[str]:
        text = self._pending + data.decode("utf-8", errors="replace")
        parts = re.split(r"[\r\n]", text)
        self._pending = parts.pop()
        return [part for part in parts if part.strip()]

    def flush(self) -> list[str]:
        pending, self._pending = self._pending, ""
        return [pending] if pending.strip() else []
