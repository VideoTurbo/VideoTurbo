"""翻译模型池：多个模型轮流接收请求，按失败次数暂歇和熔断，状态写回数据库。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from ..config import TranslationConfig
from ..models import utc_now_iso

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UpstreamRuntime:
    name: str
    request_config: TranslationConfig
    concurrency: int
    configured: bool = True
    enabled: bool = True
    active_requests: int = 0
    cooldown_until: float = 0.0
    selection_order: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    disabled_at: str | None = None
    last_failure_at: str | None = None
    last_error: str | None = None
    last_probe_at: str | None = None
    last_probe_status: str | None = None
    last_probe_error: str | None = None
    last_probe_model: str | None = None
    last_probe_structured_output: bool | None = None
    last_probe_seconds: float | None = None


class TranslationUpstreamPool:
    """Across-job least-loaded scheduler for an explicitly configured upstream pool."""

    failure_threshold = 8

    def __init__(
        self,
        config: TranslationConfig,
        *,
        saved_states: dict[str, dict[str, Any]] | None = None,
        state_changed: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    ):
        self.cooldown_seconds = config.upstream_cooldown_seconds
        self._condition = asyncio.Condition()
        self._selection_counter = 0
        self._upstreams: list[UpstreamRuntime] = []
        self._state_changed = state_changed
        saved_states = saved_states or {}
        for upstream in config.upstreams:
            request_config = config.model_copy(
                update={
                    "provider": upstream.provider or config.provider,
                    "base_url": config.upstream_base_url(upstream),
                    "api_mode": upstream.api_mode,
                    "model": upstream.model or config.model,
                    "api_key": config.upstream_api_key(upstream),
                    "upstreams": [],
                    **upstream.request_overrides(),
                }
            )
            configured = request_config.ready
            saved = saved_states.get(upstream.name) or {}
            enabled = configured and bool(saved.get("enabled", True))
            try:
                consecutive_failures = max(0, int(saved.get("consecutive_failures", 0)))
                failure_count = max(0, int(saved.get("failure_count", 0)))
            except (TypeError, ValueError):
                consecutive_failures = 0
                failure_count = 0
            try:
                last_probe_seconds = (
                    float(saved["last_probe_seconds"])
                    if saved.get("last_probe_seconds") is not None
                    else None
                )
            except (TypeError, ValueError):
                last_probe_seconds = None
            if consecutive_failures >= self.failure_threshold:
                enabled = False
            self._upstreams.append(
                UpstreamRuntime(
                    upstream.name,
                    request_config,
                    upstream.concurrency,
                    configured=configured,
                    enabled=enabled,
                    failure_count=failure_count,
                    consecutive_failures=consecutive_failures,
                    disabled_at=str(saved.get("disabled_at") or "") or None,
                    last_failure_at=str(saved.get("last_failure_at") or "") or None,
                    last_error=str(saved.get("last_error") or "") or None,
                    last_probe_at=str(saved.get("last_probe_at") or "") or None,
                    last_probe_status=(
                        str(saved.get("last_probe_status") or "") or None
                    ),
                    last_probe_error=str(saved.get("last_probe_error") or "") or None,
                    last_probe_model=str(saved.get("last_probe_model") or "") or None,
                    last_probe_structured_output=(
                        saved.get("last_probe_structured_output")
                        if isinstance(saved.get("last_probe_structured_output"), bool)
                        else None
                    ),
                    last_probe_seconds=last_probe_seconds,
                )
            )

    def __len__(self) -> int:
        return len(self._upstreams)

    def detach_state_persistence(self) -> None:
        """热更新替换节点池后调用：旧池只服务进行中的请求，不再写入持久化状态。"""
        self._state_changed = None

    def has_available_upstream(self) -> bool:
        now = monotonic()
        return any(
            upstream.configured
            and upstream.enabled
            and upstream.cooldown_until <= now
            for upstream in self._upstreams
        )

    def _saved_states(self) -> dict[str, dict[str, Any]]:
        return {
            upstream.name: {
                "enabled": upstream.enabled,
                "failure_count": upstream.failure_count,
                "consecutive_failures": upstream.consecutive_failures,
                "disabled_at": upstream.disabled_at,
                "last_failure_at": upstream.last_failure_at,
                "last_error": upstream.last_error,
                "last_probe_at": upstream.last_probe_at,
                "last_probe_status": upstream.last_probe_status,
                "last_probe_error": upstream.last_probe_error,
                "last_probe_model": upstream.last_probe_model,
                "last_probe_structured_output": upstream.last_probe_structured_output,
                "last_probe_seconds": upstream.last_probe_seconds,
            }
            for upstream in self._upstreams
        }

    def _persist_states(self) -> None:
        if self._state_changed is None:
            return
        try:
            self._state_changed(self._saved_states())
        except Exception:
            logger.exception("保存翻译模型状态失败。")

    async def acquire(
        self,
        excluded: set[str],
        *,
        preferred_name: str | None = None,
    ) -> UpstreamRuntime | None:
        async with self._condition:
            while True:
                candidates = [
                    item
                    for item in self._upstreams
                    if item.configured and item.enabled and item.name not in excluded
                ]
                if preferred_name:
                    candidates = [item for item in candidates if item.name == preferred_name]
                if not candidates:
                    return None
                now = monotonic()
                ready = [item for item in candidates if item.cooldown_until <= now]
                available = [item for item in ready if item.active_requests < item.concurrency]
                if available:
                    selected = min(
                        available,
                        key=lambda item: (
                            item.active_requests / item.concurrency,
                            item.selection_order,
                            item.name,
                        ),
                    )
                    self._selection_counter += 1
                    selected.selection_order = self._selection_counter
                    selected.active_requests += 1
                    return selected
                if not ready:
                    return None
                await self._condition.wait()

    async def acquire_for_probe(self, name: str) -> UpstreamRuntime | None:
        """非阻塞获取指定节点，避免控制台探测等待正在执行的全文任务。"""
        async with self._condition:
            now = monotonic()
            upstream = next(
                (
                    item
                    for item in self._upstreams
                    if item.name == name
                    and item.configured
                    and item.enabled
                    and item.cooldown_until <= now
                    and item.active_requests < item.concurrency
                ),
                None,
            )
            if upstream is None:
                return None
            upstream.active_requests += 1
            return upstream

    async def release(
        self,
        upstream: UpstreamRuntime,
        *,
        failed: bool = False,
        error: str | None = None,
        clear_failures: bool = True,
    ) -> None:
        async with self._condition:
            upstream.active_requests = max(0, upstream.active_requests - 1)
            state_changed = False
            if failed:
                state_changed = True
                upstream.failure_count += 1
                upstream.last_failure_at = utc_now_iso()
                upstream.last_error = error
                upstream.cooldown_until = max(
                    upstream.cooldown_until, monotonic() + self.cooldown_seconds
                )
                if upstream.enabled:
                    upstream.consecutive_failures += 1
                    if upstream.consecutive_failures >= self.failure_threshold:
                        upstream.enabled = False
                        upstream.disabled_at = utc_now_iso()
                        logger.error(
                            "翻译模型 %s 已连续失败 %d 次，自动停用；需在“大模型”页手动启用。",
                            upstream.name,
                            upstream.consecutive_failures,
                        )
            elif clear_failures and upstream.enabled and upstream.consecutive_failures:
                state_changed = True
                upstream.consecutive_failures = 0
                upstream.last_error = None
            if state_changed:
                self._persist_states()
            self._condition.notify_all()

    async def response_failed(
        self, upstream: UpstreamRuntime, error: str
    ) -> None:
        """记录 HTTP 200 后才发现的协议或内容结构错误。"""
        async with self._condition:
            upstream.failure_count += 1
            upstream.last_failure_at = utc_now_iso()
            upstream.last_error = error
            upstream.cooldown_until = max(
                upstream.cooldown_until, monotonic() + self.cooldown_seconds
            )
            if upstream.enabled:
                upstream.consecutive_failures += 1
                if upstream.consecutive_failures >= self.failure_threshold:
                    upstream.enabled = False
                    upstream.disabled_at = utc_now_iso()
                    logger.error(
                        "翻译模型 %s 已连续失败 %d 次，自动停用；需在“大模型”页手动启用。",
                        upstream.name,
                        upstream.consecutive_failures,
                    )
            self._persist_states()
            self._condition.notify_all()

    async def response_succeeded(self, upstream: UpstreamRuntime) -> None:
        """只有响应完成解析和业务校验后才清除连续失败。"""
        async with self._condition:
            if upstream.enabled and upstream.consecutive_failures:
                upstream.consecutive_failures = 0
                upstream.last_error = None
                self._persist_states()
            self._condition.notify_all()

    async def record_probe(
        self,
        upstream: UpstreamRuntime,
        *,
        compatible: bool,
        error: str | None = None,
        model: str | None = None,
        structured_output: bool | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        async with self._condition:
            upstream.last_probe_at = utc_now_iso()
            upstream.last_probe_status = "compatible" if compatible else "incompatible"
            upstream.last_probe_error = error
            upstream.last_probe_model = model
            upstream.last_probe_structured_output = structured_output
            upstream.last_probe_seconds = duration_seconds
            self._persist_states()
            self._condition.notify_all()

    async def enable(self, name: str) -> bool:
        async with self._condition:
            upstream = next((item for item in self._upstreams if item.name == name), None)
            if upstream is None or not upstream.configured:
                return False
            upstream.enabled = True
            upstream.consecutive_failures = 0
            upstream.cooldown_until = 0.0
            upstream.disabled_at = None
            upstream.last_error = None
            self._persist_states()
            self._condition.notify_all()
            logger.info("已从控制台重新启用翻译模型：%s", name)
            return True

    def snapshot(self) -> list[dict[str, Any]]:
        now = monotonic()
        return [
            {
                "name": upstream.name,
                "provider": upstream.request_config.provider,
                "api_mode": upstream.request_config.api_mode,
                "model": upstream.request_config.model,
                "base_url": upstream.request_config.base_url,
                "structured_output": upstream.request_config.structured_output,
                "reasoning_effort": upstream.request_config.reasoning_effort,
                "active_requests": upstream.active_requests,
                "concurrency": upstream.concurrency,
                "cooldown_seconds": round(max(0.0, upstream.cooldown_until - now)),
                "failure_count": upstream.failure_count,
                "consecutive_failures": upstream.consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "configured": upstream.configured,
                "enabled": upstream.enabled,
                "available": upstream.configured
                and upstream.enabled
                and upstream.cooldown_until <= now,
                "disabled_at": upstream.disabled_at,
                "last_failure_at": upstream.last_failure_at,
                "last_error": upstream.last_error,
                "last_probe_at": upstream.last_probe_at,
                "last_probe_status": upstream.last_probe_status,
                "last_probe_error": upstream.last_probe_error,
                "last_probe_model": upstream.last_probe_model,
                "last_probe_structured_output": upstream.last_probe_structured_output,
                "last_probe_seconds": upstream.last_probe_seconds,
            }
            for upstream in self._upstreams
        ]
