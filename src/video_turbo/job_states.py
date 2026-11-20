"""字幕任务的状态流转规则（唯一出处）。

任务状态有两类变化：

- **自动处理推进**：主程序和机器按流程把任务往下推（提取音频 → 等待识别 → … → 已完成），
  统一经过 ``Database.set_job_state``，只允许 ``PIPELINE_TRANSITIONS`` 中列出的变化；
  不在表中的变化（例如任务已被用户暂停、取消，或已失败）不会写入，``set_job_state`` 返回 False。
- **用户操作**：开始、暂停、继续、取消、重试、重新处理、标为已完成等，由 job_store 中对应的方法完成，
  各自限定可操作的状态（见下面的 ``PAUSABLE`` 等集合）。界面显示哪些按钮也按这些集合判断（processor.job_actions）。

新增状态或调整流程时改这里，并补上 tests/test_job_states.py 中的用例。
"""

from __future__ import annotations

from collections.abc import Mapping

from .models import JobState

S = JobState

# 自动处理的各阶段：都可以暂停；PUBLISHING 很快结束且会替换字幕文件，不能打断。
PAUSABLE: frozenset[JobState] = frozenset(
    {
        S.DISCOVERED,
        S.EXTRACTING,
        S.WAITING_ASR,
        S.TRANSCRIBING,
        S.ASR_COMPLETED,
        S.WAITING_TRANSLATION,
        S.TRANSLATING,
        S.VALIDATING,
    }
)
# 取消：回到待处理并保留已有进度。
CANCELLABLE: frozenset[JobState] = PAUSABLE | {S.PAUSED}
# 重新处理（继续、重新翻译、重新识别、从头处理）。
REPROCESSABLE: frozenset[JobState] = frozenset(
    {S.ASR_COMPLETED, S.WAITING_TRANSLATION, S.FAILED, S.PAUSED, S.COMPLETED}
)

# 自动处理推进：当前状态 → 可以进入的状态。PENDING、PAUSED、COMPLETED、FAILED 只能由用户操作改变。
PIPELINE_TRANSITIONS: Mapping[JobState, frozenset[JobState]] = {
    S.DISCOVERED: frozenset({S.EXTRACTING, S.FAILED}),
    # 本地影片抽出音频后等待识别；网盘影片交给机器下载；已有文本字幕时直接进入识别完成。
    S.EXTRACTING: frozenset({S.WAITING_ASR, S.ASR_COMPLETED, S.FAILED}),
    S.WAITING_ASR: frozenset({S.FAILED}),
    # 机器领取后（领取本身见 claim_asr_job）：传回结果，或网盘影片无法下载。
    S.TRANSCRIBING: frozenset({S.ASR_COMPLETED, S.FAILED}),
    # 翻译未配置或主力模型全部不可用时等待翻译。
    S.ASR_COMPLETED: frozenset({S.WAITING_TRANSLATION, S.TRANSLATING, S.FAILED}),
    # 停在等待翻译时会更新原因（同状态写入）。
    S.WAITING_TRANSLATION: frozenset({S.WAITING_TRANSLATION, S.TRANSLATING, S.FAILED}),
    # 翻译出错可重试时回到等待翻译；目标位置已有字幕时直接记为已完成。
    S.TRANSLATING: frozenset({S.VALIDATING, S.WAITING_TRANSLATION, S.COMPLETED, S.FAILED}),
    S.VALIDATING: frozenset({S.PUBLISHING, S.WAITING_TRANSLATION, S.COMPLETED, S.FAILED}),
    S.PUBLISHING: frozenset({S.COMPLETED, S.WAITING_TRANSLATION, S.FAILED}),
}


def pipeline_sources(target: JobState) -> frozenset[JobState]:
    """自动处理中可以进入 ``target`` 的所有当前状态。"""
    return frozenset(state for state, targets in PIPELINE_TRANSITIONS.items() if target in targets)


def can_advance(current: JobState, target: JobState) -> bool:
    return target in PIPELINE_TRANSITIONS.get(current, frozenset())
