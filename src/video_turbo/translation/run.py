"""逐批翻译一部影片的字幕：分批并发请求、失败重试、改用备用模型、拆小重发和保存进度。

一次翻译由 ``_TranslationRun`` 完成，从上到下分为几层：

- ``run_batches``：按批次大小切分，跳过中间进度里已翻译的部分，各批并发；
- ``translate_range``：翻译一批，失败且批次还够大时拆成两半重发；
- ``request_batch``：先用主力模型（或主力模型池），失败后改用备用模型；
- ``request_model``：向一个模型（或模型池中的一个模型）发请求，按需重试；
  接口不接受 JSON Schema 时自动降级后立即重发。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from ..config import TranslationConfig
from ..i18n import msg
from ..languages import LanguagePair
from ..models import Cue
from .api import (
    build_http_payload,
    extract_json,
    extract_responses_content,
    post_request,
    read_response_payload,
    request_endpoint,
    translation_schema,
    validate_translation_items,
)
from .checkpoint import load_checkpoint, write_checkpoint
from .errors import (
    TranslationBatchError,
    TranslationError,
    TranslationFatalError,
    TranslationNodeResponseError,
    TranslationNotConfigured,
    TranslationTransportBatchError,
    describe_http_error,
    describe_request_error,
    is_enum_schema_rejection,
    is_schema_rejection,
)
from .metrics import TranslationMetrics
from .pool import TranslationUpstreamPool, UpstreamRuntime
from .prompts import DEFAULT_PAIR

logger = logging.getLogger(__name__)

PRIMARY_UNAVAILABLE = msg("translation.run.primaryUnavailable")
BAD_RESPONSE = msg("translation.badResponse")
# 一批的翻译结果：{cue_id: 译文}、实际使用的模型名、模型（节点）名。
BatchResult = tuple[dict[int, str], str, str]


@dataclass(slots=True)
class _Sent:
    """一次请求：用了哪个模型，收到了什么响应（网络错误时 ``network_error`` 有值）。"""

    config: TranslationConfig
    profile: str
    runtime: UpstreamRuntime | None
    upstream_name: str
    structured: bool
    cue_id_enum: bool
    response: Any = None
    payload: dict[str, Any] | None = None
    network_error: TranslationError | None = None


@dataclass(slots=True)
class _Outcome:
    """处理一次响应的结论：拿到结果、降级后立即重发，或者失败（``split`` 表示可拆小重发）。"""

    result: BatchResult | None = None
    retry_now: bool = False
    error: TranslationError | None = None
    split: bool = False


class _TranslationRun:
    def __init__(
        self,
        cues: list[Cue],
        config: TranslationConfig,
        *,
        checkpoint_path: Path | None,
        request_semaphore: asyncio.Semaphore | None,
        upstream_pool: TranslationUpstreamPool | None,
        fallback_upstream_pool: TranslationUpstreamPool | None,
        metrics: TranslationMetrics | None,
        pair: LanguagePair,
    ) -> None:
        self.cues = cues
        self.config = config
        self.pair = pair
        self.checkpoint_path = checkpoint_path
        self.translated = [cue.model_copy(deep=True) for cue in cues]
        self.by_id = {cue.cue_id: cue for cue in self.translated}
        self.index_by_id = {cue.cue_id: index for index, cue in enumerate(cues)}
        self.resumed: dict[int, str] = {}
        self.model_by_cue_id: dict[int, str] = {}
        self.upstream_by_cue_id: dict[int, str] = {}
        self.resumed_upstream_failures: dict[str, int] = {}
        if checkpoint_path is not None:
            self._resume(checkpoint_path)

        self.semaphore = request_semaphore or asyncio.Semaphore(config.concurrency)
        self.fallback_config = config.fallback_request_config()
        self.fallback_pool_config = config.fallback_pool_config()
        if config.upstreams and upstream_pool is None:
            upstream_pool = TranslationUpstreamPool(config)
        if self.fallback_pool_config is not None and fallback_upstream_pool is None:
            fallback_upstream_pool = TranslationUpstreamPool(self.fallback_pool_config)
        self.upstream_pool = upstream_pool
        self.fallback_upstream_pool = fallback_upstream_pool
        self.structured_states, self.cue_id_enum_states = self._initial_output_states()
        self.progress = metrics or TranslationMetrics(len(cues), config.concurrency)
        self.checkpoint_batches = 0
        self.last_checkpoint_at = monotonic()

    # ---- 准备 ----

    def _resume(self, checkpoint_path: Path) -> None:
        (
            self.resumed,
            self.model_by_cue_id,
            self.upstream_by_cue_id,
            self.resumed_upstream_failures,
        ) = load_checkpoint(checkpoint_path, self.cues, self.config, self.pair)
        for cue_id, text in self.resumed.items():
            self.by_id[cue_id].target = text
        if self.resumed:
            logger.info("从翻译中间进度恢复 %d/%d 条字幕。", len(self.resumed), len(self.cues))

    def _initial_output_states(self) -> tuple[dict[str, bool], dict[str, bool]]:
        """各模型是否使用 JSON Schema 和 cue_id enum；接口不接受时在运行中关闭。"""
        primary = self.config.structured_output
        structured = {
            "primary": primary,
            "fallback": self.fallback_config.structured_output if self.fallback_config else False,
        }
        cue_id_enum = {profile: True for profile in structured}
        # 节点可单独覆盖结构化输出；未覆盖时 request_config 已继承节点池的值。
        for pool, default in (
            (self.upstream_pool, primary),
            (self.fallback_upstream_pool, self.config.fallback_structured_output),
        ):
            if pool is None:
                continue
            for item in pool.snapshot():
                key = f"upstream:{item['name']}"
                structured[key] = item.get("structured_output", default)
                cue_id_enum[key] = True
        return structured, cue_id_enum

    # ---- 进度 ----

    def save_progress(self, *, force_checkpoint: bool = False) -> None:
        now = monotonic()
        should_checkpoint = self.checkpoint_path is not None and (
            force_checkpoint
            or self.checkpoint_batches >= self.config.checkpoint_every_batches
            or now - self.last_checkpoint_at >= self.config.checkpoint_interval_seconds
        )
        progress = self.progress
        if should_checkpoint and self.checkpoint_path is not None:
            write_checkpoint(
                self.checkpoint_path,
                self.translated,
                self.config,
                self.model_by_cue_id,
                self.upstream_by_cue_id,
                progress.upstream_failure_counts,
                self.pair,
            )
            self.checkpoint_batches = 0
            self.last_checkpoint_at = now
            progress.checkpoint_write_count += 1
        logger.info(
            "翻译进度：%d/%d（%.1f%%），请求 %d，重试 %d，用备用模型 %d，拆小重发 %d。",
            progress.completed_cues,
            progress.total_cues,
            progress.completed_cues / progress.total_cues * 100 if progress.total_cues else 100,
            progress.request_count,
            progress.retry_count,
            progress.fallback_count,
            progress.split_count,
        )

    # ---- 单个模型：发送、判断、重试 ----

    async def request_model(
        self,
        client: Any,
        batch: Sequence[Cue],
        context: Sequence[Cue],
        request_config: TranslationConfig | None,
        *,
        profile: str,
        max_retries: int,
        pool: TranslationUpstreamPool | None = None,
        attempted_upstreams: set[str] | None = None,
    ) -> BatchResult:
        attempt = 0
        pinned_upstream: str | None = None
        while True:
            sent = await self._send(
                client, batch, context, request_config, profile, pool, attempted_upstreams, pinned_upstream
            )
            if sent.network_error is not None:
                outcome = _Outcome(error=sent.network_error, split=True)
            else:
                outcome = await self._handle_response(sent, batch, pool)
            if outcome.result is not None:
                return outcome.result
            if outcome.retry_now:
                # 降级后在同一个模型上立即重发，不计入重试次数。
                pinned_upstream = sent.runtime.name if sent.runtime else None
                continue
            assert outcome.error is not None
            if attempt >= max_retries:
                raise self._final_error(outcome, sent, profile)
            await self._wait_before_retry(batch, attempt, max_retries, outcome.error)
            attempt += 1

    async def _send(
        self,
        client: Any,
        batch: Sequence[Cue],
        context: Sequence[Cue],
        request_config: TranslationConfig | None,
        profile: str,
        pool: TranslationUpstreamPool | None,
        attempted_upstreams: set[str] | None,
        pinned_upstream: str | None,
    ) -> _Sent:
        queued_at = monotonic()
        async with self.semaphore:
            runtime: UpstreamRuntime | None = None
            effective_profile = profile
            if pool is not None:
                attempted = attempted_upstreams if attempted_upstreams is not None else set()
                excluded = attempted - ({pinned_upstream} if pinned_upstream else set())
                runtime = await pool.acquire(excluded, preferred_name=pinned_upstream)
                if runtime is None:
                    raise TranslationTransportBatchError(msg("translation.run.noModel"))
                attempted.add(runtime.name)
                effective_profile = f"upstream:{runtime.name}"
                base_config = runtime.request_config
            elif request_config is None:
                raise TranslationError(msg("translation.run.noConfig"))
            else:
                base_config = request_config
            config = base_config.model_copy(
                update={"structured_output": self.structured_states[effective_profile]}
            )
            payload = build_http_payload(
                config,
                batch,
                context,
                cue_id_enum=self.cue_id_enum_states[effective_profile],
                pair=self.pair,
            )
            structured = translation_schema(payload) is not None
            sent = _Sent(
                config=config,
                profile=effective_profile,
                runtime=runtime,
                upstream_name=runtime.name if runtime else profile,
                structured=structured,
                cue_id_enum=structured and self.cue_id_enum_states[effective_profile],
            )
            wait_seconds = monotonic() - queued_at
            self.progress.request_started(batch, wait_seconds, str(config.model), sent.upstream_name)
            logger.info(
                "翻译请求开始：%s（模型 %s），第 %d-%d 条，共 %d 条，排队等待 %.2f 秒。",
                sent.upstream_name,
                config.model,
                batch[0].cue_id,
                batch[-1].cue_id,
                len(batch),
                wait_seconds,
            )
            failed = False
            error: str | None = None
            started_at = monotonic()
            try:
                try:
                    response = await post_request(client, request_endpoint(config), config, payload)
                except httpx.RequestError as exc:
                    failed = True
                    duration = monotonic() - started_at
                    error = describe_request_error(exc, config.request_timeout_seconds)
                    self.progress.request_finished(
                        duration_seconds=duration, status_code=None, error=error
                    )
                    logger.warning(
                        "翻译请求失败：%s（模型 %s），第 %d-%d 条，%s，耗时 %.2f 秒。",
                        sent.upstream_name,
                        config.model,
                        batch[0].cue_id,
                        batch[-1].cue_id,
                        error,
                        duration,
                    )
                    sent.network_error = TranslationError(error)
                    return sent
                duration = monotonic() - started_at
                sent.response = response
                sent.payload = read_response_payload(response, config)
                schema_rejected = structured and is_schema_rejection(
                    response.status_code, sent.payload, response.text
                )
                # 节点池里的任意 HTTP 错误都可能来自余额、账号或服务故障；
                # 只有明确的 JSON Schema 不兼容会在同一节点自动降级。
                failed = response.status_code >= 400 and not schema_rejected
                if failed:
                    error = describe_http_error(response.status_code, sent.payload, response.text)
                self.progress.request_finished(
                    duration_seconds=duration,
                    status_code=response.status_code,
                    response_payload=sent.payload,
                )
                logger.info(
                    "翻译请求完成：%s（模型 %s），第 %d-%d 条，HTTP %d，耗时 %.2f 秒。",
                    sent.upstream_name,
                    config.model,
                    batch[0].cue_id,
                    batch[-1].cue_id,
                    response.status_code,
                    duration,
                )
                return sent
            finally:
                if runtime is not None:
                    assert pool is not None
                    await pool.release(runtime, failed=failed, error=error, clear_failures=False)

    async def _handle_response(
        self, sent: _Sent, batch: Sequence[Cue], pool: TranslationUpstreamPool | None
    ) -> _Outcome:
        response = sent.response
        if response.status_code >= 400:
            return self._handle_http_error(sent)
        error: TranslationError
        try:
            if sent.payload is None:
                raise TranslationNodeResponseError(BAD_RESPONSE)
            content, _ = extract_responses_content(sent.payload, allow_legacy_chat=True)
            items = extract_json(content)
            model = str(sent.payload.get("model") or sent.config.model)
            validated = validate_translation_items(items, batch)
            if sent.runtime is not None:
                assert pool is not None
                await pool.response_succeeded(sent.runtime)
            return _Outcome(result=(validated, model, sent.upstream_name))
        except TranslationError as exc:
            error = exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            error = TranslationNodeResponseError(BAD_RESPONSE)
            error.__cause__ = exc
        if sent.runtime is not None and isinstance(error, TranslationNodeResponseError):
            assert pool is not None
            await pool.response_failed(sent.runtime, str(error))
        return _Outcome(error=error)

    def _handle_http_error(self, sent: _Sent) -> _Outcome:
        response = sent.response
        status = response.status_code
        if sent.cue_id_enum and is_enum_schema_rejection(status, sent.payload, response.text):
            self.cue_id_enum_states[sent.profile] = False
            self.progress.schema_fallback_count += 1
            logger.warning(
                "模型 %s 的网关不接受 cue_id enum，改用不含 enum 的 JSON Schema。", sent.config.model
            )
            return _Outcome(retry_now=True)
        if sent.structured and is_schema_rejection(status, sent.payload, response.text):
            self.structured_states[sent.profile] = False
            self.progress.schema_fallback_count += 1
            logger.warning(
                "模型 %s 的网关不接受 JSON Schema，自动退回普通 JSON 输出。", sent.config.model
            )
            return _Outcome(retry_now=True)
        # 模型池中的任何 HTTP 错误都可以换模型；单个模型只有超时、限流和服务端错误值得重试。
        recoverable = sent.runtime is not None or status in {408, 429} or status >= 500
        error_type = TranslationFatalError if 400 <= status < 500 and not recoverable else TranslationError
        return _Outcome(
            error=error_type(describe_http_error(status, sent.payload, response.text)),
            split=recoverable,
        )

    def _final_error(self, outcome: _Outcome, sent: _Sent, profile: str) -> TranslationError:
        """重试用完后要抛出的错误：带上模型名；可拆小重发的错误换成 TranslationTransportBatchError。"""
        error = outcome.error
        assert error is not None
        self.progress.upstream_failed(sent.runtime.name if sent.runtime else profile)
        if sent.runtime is not None:
            tagged = type(error)(msg("translation.run.tagged", name=sent.runtime.name, error=error))
            tagged.__cause__ = error.__cause__
            error = tagged
        if isinstance(error, TranslationFatalError) or not outcome.split:
            return error
        wrapped = TranslationTransportBatchError(str(error))
        wrapped.__cause__ = error
        return wrapped

    async def _wait_before_retry(
        self, batch: Sequence[Cue], attempt: int, max_retries: int, error: TranslationError
    ) -> None:
        delay = min(2**attempt, 4)
        self.progress.retry_count += 1
        self.progress.last_error = str(error)
        logger.warning(
            "翻译请求重试：cue %d-%d，第 %d/%d 次，%d 秒后继续。原因：%s",
            batch[0].cue_id,
            batch[-1].cue_id,
            attempt + 1,
            max_retries,
            delay,
            error,
        )
        await asyncio.sleep(delay)

    # ---- 主力模型与备用模型 ----

    async def request_batch(
        self, client: Any, batch: Sequence[Cue], context: Sequence[Cue]
    ) -> BatchResult:
        result, primary_error = await self._try_primary(client, batch, context)
        if result is not None:
            return result
        if primary_error is None:
            primary_error = TranslationTransportBatchError(msg("translation.run.noPrimary"))
        if self.upstream_pool is not None and not self.upstream_pool.has_available_upstream():
            raise TranslationFatalError(PRIMARY_UNAVAILABLE) from primary_error
        if self.fallback_config is None and self.fallback_upstream_pool is None:
            raise primary_error
        self._log_fallback(batch, primary_error)
        return await self._try_fallback(client, batch, context)

    async def _try_pool(
        self,
        client: Any,
        batch: Sequence[Cue],
        context: Sequence[Cue],
        pool: TranslationUpstreamPool,
        *,
        profile: str,
        attempt_limit: int,
        label: str,
        reraise_fatal: bool,
    ) -> tuple[BatchResult | None, TranslationError | None, set[str]]:
        """依次换模型池中的模型，直到成功或没有新模型可试。"""
        attempted: set[str] = set()
        error: TranslationError | None = None
        for _ in range(min(attempt_limit, len(pool))):
            attempted_before = len(attempted)
            try:
                result = await self.request_model(
                    client,
                    batch,
                    context,
                    None,
                    profile=profile,
                    max_retries=0,
                    pool=pool,
                    attempted_upstreams=attempted,
                )
                return result, None, attempted
            except TranslationError as exc:
                if reraise_fatal and isinstance(exc, TranslationFatalError):
                    raise
                error = exc
                self.progress.last_error = str(exc)
                logger.warning(
                    "%s %s 处理第 %d-%d 条失败：%s",
                    label,
                    sorted(attempted),
                    batch[0].cue_id,
                    batch[-1].cue_id,
                    exc,
                )
                if len(attempted) == attempted_before:
                    break
        return None, error, attempted

    async def _try_primary(
        self, client: Any, batch: Sequence[Cue], context: Sequence[Cue]
    ) -> tuple[BatchResult | None, TranslationError | None]:
        if self.upstream_pool is None:
            try:
                result = await self.request_model(
                    client,
                    batch,
                    context,
                    self.config,
                    profile="primary",
                    max_retries=self.config.max_retries,
                )
                return result, None
            except TranslationFatalError:
                raise
            except TranslationError as exc:
                return None, exc
        result, error, attempted = await self._try_pool(
            client,
            batch,
            context,
            self.upstream_pool,
            profile="primary",
            attempt_limit=self.config.max_upstream_attempts,
            label="翻译模型",
            reraise_fatal=True,
        )
        if result is None and not attempted:
            raise TranslationFatalError(PRIMARY_UNAVAILABLE)
        return result, error

    def _log_fallback(self, batch: Sequence[Cue], primary_error: TranslationError) -> None:
        self.progress.fallback_count += 1
        self.progress.last_error = str(primary_error)
        if self.fallback_pool_config is not None:
            fallback_model = self.fallback_pool_config.model
        elif self.fallback_config is not None:
            fallback_model = self.fallback_config.model
        else:
            fallback_model = "备用模型"
        logger.warning(
            "主力模型处理第 %d-%d 条失败，这批改用备用模型 %s：%s",
            batch[0].cue_id,
            batch[-1].cue_id,
            fallback_model,
            primary_error,
        )

    async def _try_fallback(
        self, client: Any, batch: Sequence[Cue], context: Sequence[Cue]
    ) -> BatchResult:
        if self.fallback_upstream_pool is not None:
            result, error, _ = await self._try_pool(
                client,
                batch,
                context,
                self.fallback_upstream_pool,
                profile="fallback",
                attempt_limit=self.config.max_fallback_upstream_attempts,
                label="备用模型",
                reraise_fatal=False,
            )
            if result is not None:
                return result
            error = error or TranslationTransportBatchError(msg("translation.run.noFallback"))
            raise TranslationTransportBatchError(msg("translation.run.bothFailed", error=error)) from error
        assert self.fallback_config is not None
        try:
            return await self.request_model(
                client, batch, context, self.fallback_config, profile="fallback", max_retries=0
            )
        except TranslationFatalError:
            raise
        except TranslationTransportBatchError as error:
            raise TranslationTransportBatchError(msg("translation.run.bothFailed", error=error)) from error
        except TranslationError as error:
            raise TranslationBatchError(msg("translation.run.bothFailed", error=error)) from error

    # ---- 分批与拆分 ----

    async def translate_range(self, client: Any, batch: Sequence[Cue]) -> None:
        config = self.config
        first_index = self.index_by_id[batch[0].cue_id]
        last_index = self.index_by_id[batch[-1].cue_id]
        context = self.cues[
            max(0, first_index - config.context_cues) : min(
                len(self.cues), last_index + 1 + config.context_cues
            )
        ]
        try:
            result, successful_model, successful_upstream = await self.request_batch(
                client, batch, context
            )
        except TranslationBatchError as exc:
            if len(batch) <= config.min_batch_size:
                raise
            await self._split_and_retry(client, batch, exc)
            return
        for cue_id, text in result.items():
            self.by_id[cue_id].target = text
        for cue in batch:
            self.model_by_cue_id[cue.cue_id] = successful_model
            self.upstream_by_cue_id[cue.cue_id] = successful_upstream
        self.progress.batch_completed(len(batch), successful_model, successful_upstream)
        self.checkpoint_batches += 1
        self.save_progress()

    async def _split_and_retry(
        self, client: Any, batch: Sequence[Cue], exc: TranslationBatchError
    ) -> None:
        middle = len(batch) // 2
        self.progress.batch_split()
        # 网络类失败（多半是请求太大或太慢）时两半依次发，避免同时加重负担。
        transport_failure = isinstance(exc, TranslationTransportBatchError)
        logger.warning(
            "翻译批次 cue %d-%d 失败，自动拆分为 %d 和 %d 条%s重试。",
            batch[0].cue_id,
            batch[-1].cue_id,
            middle,
            len(batch) - middle,
            "顺序" if transport_failure else "并行",
        )
        if transport_failure:
            await self.translate_range(client, batch[:middle])
            await self.translate_range(client, batch[middle:])
            return
        child_results = await asyncio.gather(
            self.translate_range(client, batch[:middle]),
            self.translate_range(client, batch[middle:]),
            return_exceptions=True,
        )
        for child_result in child_results:
            if isinstance(child_result, BaseException):
                raise child_result

    async def run_batches(self, client: Any) -> None:
        size = self.config.batch_size
        tasks = []
        for start in range(0, len(self.cues), size):
            pending = [
                cue for cue in self.cues[start : start + size] if not self.by_id[cue.cue_id].target
            ]
            if pending:
                tasks.append(self.translate_range(client, pending))
        self.progress.initialize_batches(
            len(tasks),
            len(self.resumed),
            [self.model_by_cue_id[i] for i in self.resumed if i in self.model_by_cue_id],
            [self.upstream_by_cue_id[i] for i in self.resumed if i in self.upstream_by_cue_id],
            self.resumed_upstream_failures,
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise errors[0]

    async def run(self, http_client: Any | None) -> list[Cue]:
        try:
            if http_client is not None:
                await self.run_batches(http_client)
            else:
                async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
                    await self.run_batches(client)
        except BaseException as exc:
            self.progress.finish(str(exc) or type(exc).__name__)
            raise
        else:
            self.progress.finish()
        finally:
            self.save_progress(force_checkpoint=True)
        return self.translated


async def translate_cues(
    cues: list[Cue],
    config: TranslationConfig,
    *,
    checkpoint_path: Path | None = None,
    request_semaphore: asyncio.Semaphore | None = None,
    upstream_pool: TranslationUpstreamPool | None = None,
    fallback_upstream_pool: TranslationUpstreamPool | None = None,
    http_client: Any | None = None,
    metrics: TranslationMetrics | None = None,
    pair: LanguagePair = DEFAULT_PAIR,
) -> list[Cue]:
    if not config.ready:
        raise TranslationNotConfigured(msg("translation.run.notConfigured"))
    if config.provider not in {"openai_compatible", "vertex_ai"}:
        raise TranslationError(msg("translation.run.unsupported", provider=config.provider))
    run = _TranslationRun(
        cues,
        config,
        checkpoint_path=checkpoint_path,
        request_semaphore=request_semaphore,
        upstream_pool=upstream_pool,
        fallback_upstream_pool=fallback_upstream_pool,
        metrics=metrics,
        pair=pair,
    )
    return await run.run(http_client)
