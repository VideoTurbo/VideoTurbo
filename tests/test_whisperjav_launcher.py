import logging
import pickle
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from video_turbo.worker import whisperjav_launcher as launcher


@pytest.fixture
def fake_ensemble(monkeypatch):
    """Minimal stand-ins for the WhisperJAV 1.9.5 ensemble modules the launcher patches."""
    package = ModuleType("whisperjav")
    ensemble = ModuleType("whisperjav.ensemble")
    pass_worker = ModuleType("whisperjav.ensemble.pass_worker")
    orchestrator = ModuleType("whisperjav.ensemble.orchestrator")

    def run_pass_worker(payload, result_file):
        logging.getLogger("whisperjav").info("Phase 1: Extracting audio")
        return payload.pass_number

    run_pass_worker.__module__, run_pass_worker.__qualname__ = pass_worker.__name__, "run_pass_worker"
    pass_worker.run_pass_worker = orchestrator.run_pass_worker = run_pass_worker

    class EnsembleOrchestrator:
        subs_language = "native"

        def __init__(self, run):
            self.run = run

        def process_batch(self, media_files, pass1_config, pass2_config=None, merge_strategy="pass1_primary"):
            # Same call order as the 1.9.5 batch path.
            first = self._run_pass_in_subprocess(
                pass_number=1, media_files=media_files, pass_config=pass1_config, language_code="ja"
            )
            second = self._run_pass_in_subprocess(
                pass_number=2, media_files=media_files, pass_config=pass2_config, language_code="ja"
            ) if pass2_config else None
            return [first, second]

        def _run_pass_in_subprocess(self, pass_number, media_files, pass_config, language_code):
            return self.run(pass_number, pass_config, language_code)

    orchestrator.EnsembleOrchestrator = EnsembleOrchestrator
    orchestrator.resolve_language_code = lambda config, subs_language: config["language"]
    package.ensemble = ensemble
    ensemble.pass_worker, ensemble.orchestrator = pass_worker, orchestrator
    for module in (package, ensemble, pass_worker, orchestrator):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return SimpleNamespace(pass_worker=pass_worker, orchestrator=orchestrator)


def test_pass_logs_are_tagged_and_target_still_pickles_by_name(fake_ensemble, caplog):
    factory = logging.getLogRecordFactory()
    try:
        launcher.tag_pass_logs()
        target = fake_ensemble.pass_worker.run_pass_worker
        assert fake_ensemble.orchestrator.run_pass_worker is target
        assert pickle.loads(pickle.dumps(target)) is target
        with caplog.at_level(logging.INFO, logger="whisperjav"):
            assert target(SimpleNamespace(pass_number=2), "result.pkl") == 2
    finally:
        logging.setLogRecordFactory(factory)
    assert caplog.messages == ["[pass2] Phase 1: Extracting audio"]


def test_second_pass_starts_before_first_pass_finishes(fake_ensemble, caplog):
    second_started = threading.Event()
    calls = []

    def run(pass_number, config, language):
        calls.append((pass_number, config["name"], language))
        if pass_number == 2:
            second_started.set()
        else:
            assert second_started.wait(5), "pass 2 waited for pass 1"
        return f"pass{pass_number}"

    caplog.set_level(logging.INFO, logger="whisperjav")
    launcher.run_passes_in_parallel()
    orchestrator = fake_ensemble.orchestrator.EnsembleOrchestrator(run)
    results = orchestrator.process_batch(
        [{"basename": "movie"}], {"name": "qwen"}, {"name": "anime", "language": "ja2"}
    )
    assert results == ["pass1", "pass2"]
    assert sorted(calls) == [(1, "qwen", "ja"), (2, "anime", "ja2")]
    assert "Pass 2 worker finished" in caplog.messages
    # Multi-file batches keep WhisperJAV's own sequential order.
    calls.clear()
    second_started.set()
    orchestrator.process_batch([{}, {}], {"name": "qwen"}, {"name": "anime", "language": "ja2"})
    assert calls == [(1, "qwen", "ja"), (2, "anime", "ja")]
