"""翻译进度与统计：请求数、重试、拆分、各模型用量，供控制台显示。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from ..models import Cue, utc_now_iso

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TranslationMetrics:
    total_cues: int
    max_concurrency: int
    started_at: str = field(default_factory=utc_now_iso)
    total_batches: int = 0
    completed_batches: int = 0
    resumed_cues: int = 0
    completed_cues: int = 0
    inflight_requests: int = 0
    request_count: int = 0
    retry_count: int = 0
    fallback_count: int = 0
    split_count: int = 0
    schema_fallback_count: int = 0
    checkpoint_write_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    model_cue_counts: dict[str, int] = field(default_factory=dict)
    upstream_cue_counts: dict[str, int] = field(default_factory=dict)
    upstream_failure_counts: dict[str, int] = field(default_factory=dict)
    request_seconds: float = 0.0
    semaphore_wait_seconds: float = 0.0
    last_batch: str | None = None
    last_request_seconds: float | None = None
    last_http_status: int | None = None
    last_model: str | None = None
    last_upstream: str | None = None
    last_error: str | None = None
    finished_at: str | None = None
    _started_monotonic: float = field(default_factory=monotonic, repr=False)
    _finished_monotonic: float | None = field(default=None, repr=False)

    def initialize_batches(
        self,
        total_batches: int,
        resumed_cues: int,
        resumed_models: Sequence[str] = (),
        resumed_upstreams: Sequence[str] = (),
        resumed_upstream_failures: dict[str, int] | None = None,
    ) -> None:
        self.total_batches = total_batches
        self.resumed_cues = resumed_cues
        self.completed_cues = resumed_cues
        self.model_cue_counts.clear()
        self.upstream_cue_counts.clear()
        self.upstream_failure_counts = dict(resumed_upstream_failures or {})
        for model in resumed_models:
            self.model_cue_counts[model] = self.model_cue_counts.get(model, 0) + 1
        for upstream in resumed_upstreams:
            self.upstream_cue_counts[upstream] = self.upstream_cue_counts.get(upstream, 0) + 1

    def request_started(
        self, batch: Sequence[Cue], wait_seconds: float, model: str, upstream: str
    ) -> None:
        self.inflight_requests += 1
        self.request_count += 1
        self.semaphore_wait_seconds += wait_seconds
        self.last_batch = f"{batch[0].cue_id}-{batch[-1].cue_id}"
        self.last_model = model
        self.last_upstream = upstream

    def request_finished(
        self,
        *,
        duration_seconds: float,
        status_code: int | None,
        response_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.inflight_requests = max(0, self.inflight_requests - 1)
        self.request_seconds += duration_seconds
        self.last_request_seconds = duration_seconds
        self.last_http_status = status_code
        if error:
            self.last_error = error
        usage = response_payload.get("usage") or {} if isinstance(response_payload, dict) else {}
        if isinstance(usage, dict):
            try:
                self.prompt_tokens += int(
                    usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                )
                self.completion_tokens += int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                )
                output_details = usage.get("output_tokens_details") or {}
                if isinstance(output_details, dict):
                    self.reasoning_tokens += int(output_details.get("reasoning_tokens") or 0)
            except (TypeError, ValueError):
                logger.debug("翻译接口返回的 Token 用量不是整数：%r", usage)

    def batch_completed(self, cue_count: int, model: str, upstream: str = "primary") -> None:
        self.completed_batches += 1
        self.completed_cues = min(self.total_cues, self.completed_cues + cue_count)
        self.model_cue_counts[model] = self.model_cue_counts.get(model, 0) + cue_count
        self.upstream_cue_counts[upstream] = (
            self.upstream_cue_counts.get(upstream, 0) + cue_count
        )
        self.last_error = None

    def upstream_failed(self, upstream: str) -> None:
        self.upstream_failure_counts[upstream] = (
            self.upstream_failure_counts.get(upstream, 0) + 1
        )

    def batch_split(self) -> None:
        # 一个失败批次替换为两个子批次，因此预计请求总数净增加一个。
        self.total_batches += 1
        self.split_count += 1

    def finish(self, error: str | None = None) -> None:
        self.inflight_requests = 0
        self.last_error = error or self.last_error
        self.finished_at = utc_now_iso()
        self._finished_monotonic = monotonic()

    def model_usage(self) -> dict[str, Any]:
        provider_cue_counts: dict[str, int] = {}
        for model, cue_count in self.model_cue_counts.items():
            normalized = model.casefold()
            if "grok" in normalized:
                provider = "grok"
            elif "deepseek" in normalized:
                provider = "deepseek"
            elif "gemini" in normalized:
                provider = "gemini"
            else:
                provider = "other"
            provider_cue_counts[provider] = provider_cue_counts.get(provider, 0) + cue_count
        attributed_cues = sum(self.model_cue_counts.values())
        unknown_cues = max(0, self.completed_cues - attributed_cues)
        if unknown_cues:
            provider_cue_counts["unknown"] = unknown_cues
        denominator = self.completed_cues
        attributed_upstream_cues = sum(self.upstream_cue_counts.values())
        upstream_cue_counts = dict(sorted(self.upstream_cue_counts.items()))
        unknown_upstream_cues = max(0, denominator - attributed_upstream_cues)
        if unknown_upstream_cues:
            upstream_cue_counts["unknown"] = unknown_upstream_cues
        return {
            "total_cues": denominator,
            "model_cue_counts": dict(sorted(self.model_cue_counts.items())),
            "provider_cue_counts": provider_cue_counts,
            "provider_percentages": {
                provider: round(cue_count / denominator * 100, 1) if denominator else 0.0
                for provider, cue_count in provider_cue_counts.items()
            },
            "upstream_cue_counts": upstream_cue_counts,
            "upstream_percentages": {
                upstream: round(cue_count / denominator * 100, 1) if denominator else 0.0
                for upstream, cue_count in upstream_cue_counts.items()
            },
            "upstream_failure_counts": dict(sorted(self.upstream_failure_counts.items())),
        }

    def snapshot(self) -> dict[str, Any]:
        end = self._finished_monotonic or monotonic()
        elapsed = max(0.0, end - self._started_monotonic)
        translated_now = max(0, self.completed_cues - self.resumed_cues)
        rate = translated_now / elapsed if elapsed > 0 else 0.0
        remaining = max(0, self.total_cues - self.completed_cues)
        eta = remaining / rate if rate > 0 else None
        return {
            **self.model_usage(),
            "total_cues": self.total_cues,
            "completed_cues": self.completed_cues,
            "progress_percent": round(
                self.completed_cues / self.total_cues * 100 if self.total_cues else 100, 1
            ),
            "total_batches": self.total_batches,
            "completed_batches": self.completed_batches,
            "inflight_requests": self.inflight_requests,
            "max_concurrency": self.max_concurrency,
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "split_count": self.split_count,
            "schema_fallback_count": self.schema_fallback_count,
            "checkpoint_write_count": self.checkpoint_write_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "elapsed_seconds": round(elapsed, 1),
            "average_request_seconds": round(
                self.request_seconds / self.request_count if self.request_count else 0.0, 1
            ),
            "semaphore_wait_seconds": round(self.semaphore_wait_seconds, 1),
            "eta_seconds": round(eta) if eta is not None else None,
            "last_batch": self.last_batch,
            "last_request_seconds": (
                round(self.last_request_seconds, 1)
                if self.last_request_seconds is not None
                else None
            ),
            "last_http_status": self.last_http_status,
            "last_model": self.last_model,
            "last_upstream": self.last_upstream,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
