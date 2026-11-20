"""Start the WhisperJAV CLI with Worker-side runtime options.

Keep project imports out of this module: it is executed as a standalone script
by the backend interpreter. Ensemble passes run in ``spawn`` processes, which
re-execute this file as ``__mp_main__``, so module-level patches reach them too.
"""

from __future__ import annotations

import concurrent.futures
import functools
import logging
import os
import sys

FIRERED_VAD_GPU_ENV = "VIDEO_TURBO_FIRERED_VAD_GPU"
PARALLEL_PASSES_ENV = "VIDEO_TURBO_WHISPERJAV_PARALLEL_PASSES"


def load_hub_fixes():
    """Load the sibling whisperjav_hub.py by path (this script runs standalone)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisperjav_hub.py")
    spec = importlib.util.spec_from_file_location("video_turbo_whisperjav_hub", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enable_firered_vad_gpu() -> None:
    # WhisperJAV 1.9.5 exposes FireRedVAD's use_gpu only through its own YAML.
    from whisperjav.modules.speech_segmentation.backends.firered_vad import (
        FireRedVadSpeechSegmenter,
    )

    original = FireRedVadSpeechSegmenter.__init__

    @functools.wraps(original)
    def init(self, *args, **kwargs):
        kwargs["use_gpu"] = True
        original(self, *args, **kwargs)

    FireRedVadSpeechSegmenter.__init__ = init


def tag_pass_logs() -> None:
    """Prefix each pass worker's log records with ``[passN]``.

    The single-file ensemble path never logs which pass is running, and parallel
    passes interleave their output; the tag keeps every line attributable.
    """
    from whisperjav.ensemble import orchestrator, pass_worker

    original = pass_worker.run_pass_worker

    # wraps() keeps the module and qualified name, so spawn pickles the target by
    # name and the child resolves it to its own patched copy.
    @functools.wraps(original)
    def run_pass_worker(payload, result_file):
        tag = f"[pass{payload.pass_number}] "
        make_record = logging.getLogRecordFactory()

        def record(*args, **kwargs):
            item = make_record(*args, **kwargs)
            item.msg = tag + str(item.msg)
            return item

        logging.setLogRecordFactory(record)
        return original(payload, result_file)

    pass_worker.run_pass_worker = orchestrator.run_pass_worker = run_pass_worker


def run_passes_in_parallel() -> None:
    """Start pass 2 together with pass 1 for single-file ensembles.

    WhisperJAV 1.9.5 waits for pass 1 before spawning pass 2, yet each pass is
    an independent process with its own temp directory and only the merge reads
    both results.
    """
    from whisperjav.ensemble import orchestrator

    cls = orchestrator.EnsembleOrchestrator
    original_batch = cls.process_batch
    original_run = cls._run_pass_in_subprocess

    @functools.wraps(original_batch)
    def process_batch(self, media_files, pass1_config, pass2_config=None, merge_strategy="pass1_primary"):
        # Multi-file serial batches run passes per file; leave them sequential.
        if pass2_config and len(media_files) == 1:
            language = orchestrator.resolve_language_code(pass2_config, self.subs_language)
            self._video_turbo_pass2 = (pass2_config, language)
        try:
            return original_batch(self, media_files, pass1_config, pass2_config, merge_strategy)
        finally:
            self._video_turbo_pass2 = None

    def run_second_pass(self, media_files, pass2_config, pass2_language):
        try:
            return original_run(self, 2, media_files, pass2_config, pass2_language)
        finally:
            # WhisperJAV reports pass 2 only after pass 1 returns; say when it really ends.
            logging.getLogger("whisperjav").info("Pass 2 worker finished")

    @functools.wraps(original_run)
    def run_pass(self, pass_number, media_files, pass_config, language_code):
        pending = getattr(self, "_video_turbo_pass2", None)
        if pass_number == 1 and isinstance(pending, tuple):
            pass2_config, pass2_language = pending
            pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="pass2")
            self._video_turbo_pass2 = pool.submit(
                run_second_pass, self, media_files, pass2_config, pass2_language
            )
            pool.shutdown(wait=False)
        elif pass_number == 2 and isinstance(pending, concurrent.futures.Future):
            self._video_turbo_pass2 = None
            return pending.result()
        return original_run(self, pass_number, media_files, pass_config, language_code)

    cls.process_batch = process_batch
    cls._run_pass_in_subprocess = run_pass


if os.environ.get(FIRERED_VAD_GPU_ENV) == "1":
    enable_firered_vad_gpu()

if __name__ == "__mp_main__":
    # A spawned pass worker: patch before it unpickles its target by name.
    load_hub_fixes().patch_hub_downloads()
    tag_pass_logs()
    load_hub_fixes().ignore_missing_hub_files()

if __name__ == "__main__":
    # Install before the CLI captures download functions during heavy imports.
    load_hub_fixes().patch_hub_downloads()
    from whisperjav.main import main

    tag_pass_logs()
    load_hub_fixes().ignore_missing_hub_files()
    if os.environ.get(PARALLEL_PASSES_ENV) == "1":
        run_passes_in_parallel()
    sys.exit(main())
