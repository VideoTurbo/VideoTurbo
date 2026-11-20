"""字幕生成与影片修复两个列表对外统一使用的状态。

内部阶段（提取、识别、翻译、下载原片……）只作为 step 文字给界面看，
列表、筛选和计数都只按下面 6 个状态。
"""

from __future__ import annotations

from typing import Literal

from .i18n import msg
from .models import JobState, RestoreState

ListStatus = Literal["pending", "queued", "running", "paused", "failed", "completed"]

# 列表筛选 → 包含的状态；"all" 不筛选。
VIEW_STATUSES: dict[str, set[str]] = {
    "active": {"queued", "running", "paused"},
    "failed": {"failed"},
    "pending": {"pending"},
    "completed": {"completed"},
}
LIST_VIEWS = ("all", *VIEW_STATUSES)
LIST_VIEW_PATTERN = f"^({'|'.join(LIST_VIEWS)})$"

# 用户在待处理里选“不生成字幕”后的说明；带这条说明、没有字幕的已完成任务可以再提交。
SKIPPED_NOTE = msg("status.skipped")

JOB_STATUS: dict[JobState, ListStatus] = {
    JobState.PENDING: "pending",
    JobState.DISCOVERED: "queued",
    JobState.WAITING_ASR: "queued",
    JobState.ASR_COMPLETED: "queued",
    JobState.WAITING_TRANSLATION: "queued",
    JobState.EXTRACTING: "running",
    JobState.TRANSCRIBING: "running",
    JobState.TRANSLATING: "running",
    JobState.VALIDATING: "running",
    JobState.PUBLISHING: "running",
    JobState.PAUSED: "paused",
    JobState.FAILED: "failed",
    JobState.COMPLETED: "completed",
}
JOB_STEPS: dict[JobState, str] = {
    JobState.WAITING_ASR: msg("status.step.waitingMachine"),
    JobState.ASR_COMPLETED: msg("status.step.waitingTranslation"),
    JobState.WAITING_TRANSLATION: msg("status.step.waitingTranslation"),
    JobState.EXTRACTING: msg("status.step.extracting"),
    JobState.TRANSCRIBING: msg("status.step.transcribing"),
    JobState.TRANSLATING: msg("status.step.translating"),
    JobState.VALIDATING: msg("status.step.validating"),
    JobState.PUBLISHING: msg("status.step.publishing"),
}

# 影片修复：已取消的任务不单独成状态，影片回到待处理。
RESTORE_STATUS: dict[RestoreState, ListStatus] = {
    RestoreState.WAITING: "queued",
    RestoreState.PREPARING: "running",
    RestoreState.DOWNLOADING: "running",
    RestoreState.PROCESSING: "running",
    RestoreState.UPLOADING: "running",
    RestoreState.FINALIZING: "running",
    RestoreState.FAILED: "failed",
    RestoreState.COMPLETED: "completed",
    RestoreState.CANCELLED: "pending",
}
RESTORE_STEPS: dict[RestoreState, str] = {
    RestoreState.WAITING: msg("status.step.waitingMachine"),
    RestoreState.PREPARING: msg("status.step.preparing"),
    RestoreState.DOWNLOADING: msg("status.step.downloading"),
    RestoreState.PROCESSING: msg("status.step.restoring"),
    RestoreState.UPLOADING: msg("status.step.uploading"),
    RestoreState.FINALIZING: msg("status.step.finalizing"),
}


def job_states_for(view: str) -> set[JobState] | None:
    """筛选对应的字幕任务内部状态；"all" 返回 None。"""
    wanted = VIEW_STATUSES.get(view)
    if wanted is None:
        return None
    return {state for state, status in JOB_STATUS.items() if status in wanted}


def view_counts(status_counts: dict[str, int]) -> dict[str, int]:
    """按状态的数量 → 各筛选的数量。"""
    counts = {view: sum(status_counts.get(s, 0) for s in wanted) for view, wanted in VIEW_STATUSES.items()}
    counts["all"] = sum(status_counts.values())
    return counts


def job_view_counts(state_counts: dict[str, int]) -> dict[str, int]:
    """字幕任务按内部状态的数量 → 各筛选的数量。"""
    status_counts: dict[str, int] = {}
    for state, count in state_counts.items():
        status = JOB_STATUS[JobState(state)]
        status_counts[status] = status_counts.get(status, 0) + count
    return view_counts(status_counts)
