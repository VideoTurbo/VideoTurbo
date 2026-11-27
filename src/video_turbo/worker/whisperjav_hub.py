"""Hugging Face download fixes shared by the WhisperJAV launcher and self-test.

Keep project imports out of this module: both standalone scripts load it by
path inside the isolated WhisperJAV interpreter, and importing it has no side
effects.
"""

from __future__ import annotations

import functools
import logging
import os

# 408/429 are transient (timeout / rate limit) and may succeed on a mirror.
_RETRYABLE_CLIENT_STATUSES = {408, 429}
_ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")


def _retryable(error: BaseException) -> bool:
    if is_missing_on_hub(error):
        return False
    from huggingface_hub import errors

    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (errors.FileMetadataError, errors.LocalEntryNotFoundError,
                                ConnectionError, TimeoutError)):
            return True
        if isinstance(current, errors.HfHubHTTPError):
            status = getattr(getattr(current, "response", None), "status_code", None)
            if status in _RETRYABLE_CLIENT_STATUSES or (status is not None and status >= 500):
                return True
        # requests/httpx/urllib errors differ between hub versions. Inspect the
        # chain as well as the outer exception (transformers often wraps it).
        if any(word in str(current).lower() for word in (
            "ssl", "certificate", "connection", "timeout", "timed out", "network",
            "socket", "getaddrinfo", "name resolution", "urlopen error",
        )):
            return True
        current = current.__cause__ or current.__context__
    return False


def make_resilient_wrapper(original, name):
    """Use each endpoint once, preserving cache, offline and explicit options."""
    if getattr(original, "_video_turbo_downloads", False):
        return original

    @functools.wraps(original)
    def download(*args, **kwargs):
        from huggingface_hub import constants

        if kwargs.get("local_files_only") or constants.HF_HUB_OFFLINE:
            return original(*args, **kwargs)
        primary = (kwargs.get("endpoint") or os.environ.get("VIDEO_TURBO_HF_ENDPOINT")
                   or os.environ.get("HF_ENDPOINT") or constants.ENDPOINT).rstrip("/")
        endpoints = list(dict.fromkeys((primary, *_ENDPOINTS)))
        logger = logging.getLogger(__name__)
        first_error = None
        for index, endpoint in enumerate(endpoints):
            try:
                return original(*args, **{**kwargs, "endpoint": endpoint})
            except Exception as exc:
                if not _retryable(exc):
                    raise
                if first_error is None:
                    first_error = exc
                logger.warning("[HF Download] %s 在 %s 失败：%s: %s", name, endpoint, type(exc).__name__, exc)
            # A local-only snapshot lookup in Hub 0.x only checks whether the
            # directory exists, not whether the requested weights are present.
            # Do not let a partial snapshot short-circuit the alternate source.
            # Online downloads still reuse cached files; explicit offline/local
            # calls above retain the caller's chosen behaviour.
            if index == 0 and name != "snapshot_download" and not kwargs.get("force_download"):
                try:
                    return original(*args, **{**kwargs, "endpoint": endpoint, "local_files_only": True})
                except Exception as exc:  # noqa: BLE001 - a cache miss must allow the alternate endpoint
                    logger.debug("[HF Download] 本地缓存未能加载：%s", exc)
            if index + 1 < len(endpoints):
                logger.info("[HF Download] 自动切换下载源：%s", endpoints[index + 1])
        raise first_error

    download._video_turbo_downloads = True
    return download


def patch_hub_downloads() -> None:
    """Install before WhisperJAV/transformers import their download functions.

    Replace the pinned upstream wrapper factory too: the CLI installs its own
    patch later, and must reuse these wrappers rather than nesting retries.
    The model catalog also runs this without WhisperJAV installed.
    """
    import huggingface_hub

    try:
        from whisperjav.utils import model_loader
    except ImportError:
        pass
    else:
        model_loader._make_resilient_wrapper = make_resilient_wrapper
    for name in ("hf_hub_download", "snapshot_download"):
        setattr(huggingface_hub, name, make_resilient_wrapper(getattr(huggingface_hub, name), name))


def is_missing_on_hub(error: BaseException) -> bool:
    """True when the hub answered that a repo, revision or file does not exist.

    transformers probes optional files such as ``custom_generate/generate.py``
    and treats a 404 as "absent"; such answers are not network failures.
    """
    try:
        from huggingface_hub import errors
    except ImportError:
        return False
    # Some hub versions raise these without a response attached; the type is enough.
    missing = tuple(
        getattr(errors, name) for name in (
            "EntryNotFoundError", "RemoteEntryNotFoundError",
            "RepositoryNotFoundError", "RevisionNotFoundError",
        ) if isinstance(getattr(errors, name, None), type)
    )
    current: BaseException | None = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # Hub 0.x makes a local cache miss a subclass of EntryNotFoundError.
        # Only a remote 404 should suppress switching endpoints.
        if isinstance(current, errors.LocalEntryNotFoundError):
            current = current.__cause__ or current.__context__
            continue
        if missing and isinstance(current, missing):
            return True
        if isinstance(current, errors.HfHubHTTPError):
            response = getattr(current, "response", None)
            status = getattr(response, "status_code", None)
            if status is not None and 400 <= status < 500 and status not in _RETRYABLE_CLIENT_STATUSES:
                return True
        current = current.__cause__ or current.__context__
    return False


def ignore_missing_hub_files() -> None:
    """Stop WhisperJAV's download wrapper from treating hub 404s as network errors.

    WhisperJAV 1.9.5 classifies errors by message text, so a 404 for an
    optional file falls through to the cache and hf-mirror.com retries (with
    connect timeouts) and logs a failure summary, although transformers then
    ignores the re-raised 404 and loads the model normally.
    """
    try:
        from whisperjav.utils import model_loader
    except ImportError:
        return
    original = getattr(model_loader, "_is_network_error", None)
    if not callable(original) or getattr(original, "_video_turbo_patched", False):
        return

    @functools.wraps(original)
    def is_network_error(error):
        if is_missing_on_hub(error):
            logging.getLogger(__name__).debug("Hub 返回文件不存在，不按网络错误重试：%s", error)
            return False
        return original(error)

    is_network_error._video_turbo_patched = True
    # The download wrapper looks this name up in the module at call time.
    model_loader._is_network_error = is_network_error
