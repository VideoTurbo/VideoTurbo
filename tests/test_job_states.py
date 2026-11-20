from pathlib import Path

import pytest
from conftest import force_job_state

from video_turbo import job_states
from video_turbo.database import Database
from video_turbo.languages import LanguagePair
from video_turbo.models import JobState, SourceKind

USER_ONLY = {JobState.PENDING, JobState.PAUSED, JobState.COMPLETED, JobState.FAILED}


def test_every_state_is_covered_by_the_pipeline_table() -> None:
    # 自动处理中的每个状态都要在表里登记出路；只能由用户操作改变的状态不能出现在表的左侧。
    assert set(job_states.PIPELINE_TRANSITIONS) == set(JobState) - USER_ONLY
    for state, targets in job_states.PIPELINE_TRANSITIONS.items():
        # 任何处理中的阶段出错都要能记为失败。
        assert JobState.FAILED in targets, state
        assert JobState.PENDING not in targets and JobState.PAUSED not in targets, state


def test_every_pipeline_state_is_reachable() -> None:
    reachable = {JobState.DISCOVERED}  # 用户提交后进入
    frontier = [JobState.DISCOVERED]
    while frontier:
        for target in job_states.PIPELINE_TRANSITIONS.get(frontier.pop(), ()):
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    # 正在识别（TRANSCRIBING）由机器领取进入，见 claim_asr_job。
    assert reachable | {JobState.TRANSCRIBING} == set(JobState) - {JobState.PENDING, JobState.PAUSED}


def test_user_action_sets_are_consistent() -> None:
    assert JobState.PUBLISHING not in job_states.PAUSABLE
    assert job_states.CANCELLABLE == job_states.PAUSABLE | {JobState.PAUSED}
    assert not job_states.PAUSABLE & USER_ONLY


@pytest.fixture
def job(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "app.db")
    database.initialize()
    media_id, _ = database.upsert_media(
        library_id="lib", path=str(tmp_path / "movie.mkv"), size=1, mtime_ns=1, duration=60.0,
        probe={"streams": []}, decision="process", reason="",
    )
    job_id, _ = database.create_or_reset_job(
        media_id=media_id, source_kind=SourceKind.AUDIO, source_stream_index=1, source_path=None,
        changed=True, pair=LanguagePair(),
    )
    return database, job_id


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (JobState.DISCOVERED, JobState.EXTRACTING, True),
        (JobState.TRANSLATING, JobState.VALIDATING, True),
        (JobState.PUBLISHING, JobState.COMPLETED, True),
        # 跳过阶段、从用户控制的状态推进、失败后继续写入，都会被拒绝。
        (JobState.DISCOVERED, JobState.TRANSLATING, False),
        (JobState.PAUSED, JobState.COMPLETED, False),
        (JobState.PENDING, JobState.ASR_COMPLETED, False),
        (JobState.FAILED, JobState.COMPLETED, False),
        (JobState.COMPLETED, JobState.FAILED, False),
    ],
)
def test_set_job_state_follows_the_table(job, current, target, allowed) -> None:
    database, job_id = job
    force_job_state(database, job_id, current)
    assert job_states.can_advance(current, target) is allowed
    assert database.set_job_state(job_id, target) is allowed
    assert database.get_job(job_id)["state"] == (target if allowed else current)
