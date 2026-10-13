import asyncio
import inspect
import json
import logging
import random
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx
import litellm

from app.analytics.collector import track_token_usage
from app.analytics.tracer import _optional_span
from app.db.repository import AgentConfigRepository
from app.llm.providers import resolve_provider_params
from app.models.agent import AgentConfig
from app.security.redaction import redact_sensitive_values

try:
    from litellm.exceptions import Timeout as LiteLLMTimeout
except ImportError:
    LiteLLMTimeout = None

try:
    import openai as _openai
except ImportError:  # pragma: no cover - openai is a litellm dependency
    _openai = None

try:
    import aiohttp as _aiohttp
except ImportError:  # pragma: no cover - aiohttp is a litellm dependency
    _aiohttp = None

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM provider returns an unusable response."""


# P3-11: backoff between the first LLM call and the single retry that
# kicks in when the provider returns an empty completion (typically
# transient rate limiting). Kept short because the call site is in
# the request hot path.
_LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC = 1.0

# Upper bound for the adaptive budget retry: when a completion comes back
# empty/truncated with finish_reason="length" (typical for reasoning models
# whose thinking tokens exhaust max_tokens), the retry runs with doubled
# max_tokens, capped here.
_LLM_ADAPTIVE_RETRY_MAX_TOKENS_CAP = 32768


async def _close_stream_response(response: Any) -> None:
    """Close a litellm streaming response so an aborted consumer stops the provider stream.

    Prefers ``aclose()`` (litellm ``CustomStreamWrapper``), falls back to
    ``close()``. Close failures are logged, never raised.
    """
    if response is None:
        return
    closer = getattr(response, "aclose", None)
    if not callable(closer):
        closer = getattr(response, "close", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Closing the LLM stream response failed", exc_info=True)


def _sanitize_tool_name(name: str, valid_names: set[str]) -> str | None:
    """Return the tool name to execute, or None when it is not an exact match.

    Only exact names are executed. The single repair is stripping a leaked
    chat-template control-token suffix (``web_search<|channel|>commentary``)
    and surrounding whitespace -- neither can be part of a tool name, so the
    remainder is the name the model emitted. Fuzzy or prefix matches are
    NOT repaired: remapping an unknown name to a *different* valid tool would
    execute something the model never asked for. Unknown names go down the
    invalid-name path, which returns an error string to the LLM.
    """
    if not name:
        return None
    if name in valid_names:
        return name
    candidate = name.split("<|", 1)[0].strip()
    if candidate and candidate in valid_names:
        return candidate
    return None


# Minimum remaining budget (seconds) required to start another LLM attempt
# (retry or tool round) when the caller passed a ``deadline``. Below this,
# the attempt would almost certainly be cut off by the dispatch timeout.
_MIN_ATTEMPT_BUDGET_SEC = 1.0

# Backoff before the single retry after a provider timeout.
_LLM_TIMEOUT_RETRY_DELAY_SEC = 2.0


class LLMDeadlineExceededError(LLMError):
    """Raised when the caller-supplied deadline leaves no budget for an LLM call."""


def _remaining_budget(deadline: float | None) -> float | None:
    """Seconds left until ``deadline`` (``time.monotonic()`` based); None = unbounded."""
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _bounded_timeout(config_timeout: float, deadline: float | None) -> float:
    """Clamp a per-call provider timeout to the remaining deadline budget."""
    remaining = _remaining_budget(deadline)
    if remaining is None:
        return config_timeout
    if remaining <= 0:
        raise LLMDeadlineExceededError("LLM deadline exceeded before the provider call")
    return min(float(config_timeout), remaining)


def _has_budget_for_attempt(deadline: float | None, delay: float = 0.0) -> bool:
    """True when another attempt (after ``delay``) still fits the deadline."""
    remaining = _remaining_budget(deadline)
    return remaining is None or remaining - delay >= _MIN_ATTEMPT_BUDGET_SEC


# Transient provider errors (rate limit, overload, 5xx, dropped connection)
# are retried this many times per provider call, independent of
# ``retry_on_timeout``: the failure happens before any response exists, so
# nothing has been parsed or executed yet.
_LLM_TRANSIENT_MAX_RETRIES = 1

# Backoff before a transient-error retry: base delay (doubled per further
# retry) plus uniform jitter, so concurrent callers do not retry in lockstep.
_LLM_TRANSIENT_RETRY_BASE_DELAY_SEC = 0.3
_LLM_TRANSIENT_RETRY_JITTER_SEC = 0.2

# A provider ``Retry-After`` hint up to this many seconds raises the backoff
# to the hint; a longer hint means no retry (request hot path).
_LLM_TRANSIENT_RETRY_AFTER_CAP_SEC = 2.0

# Without a caller deadline, a transient error is retried only when the
# failed attempt itself failed fast (below this many seconds); a slow
# failure retried without a budget could double the call time.
_LLM_TRANSIENT_FAST_FAIL_SEC = 1.0

# HTTP status codes that mark a provider error as transient. 529 is the
# "overloaded" status some providers use. 408 is excluded: timeouts follow
# the separate ``retry_on_timeout`` policy.
_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})

# Exception classes that are transient by type, and classes that are never
# retried even if a status code looks transient. Resolved lazily by name so
# a partially mocked ``litellm`` module (tests) cannot break classification.
# litellm's ``APIConnectionError`` is NOT listed: it is litellm's catch-all
# for unmapped exceptions (status_code hardcoded to 500) and is handled by
# inspecting its cause chain instead.
_TRANSIENT_LITELLM_ERROR_NAMES = (
    "RateLimitError",
    "ServiceUnavailableError",
    "InternalServerError",
    "BadGatewayError",
)
_PERMANENT_LITELLM_ERROR_NAMES = (
    "AuthenticationError",
    "PermissionDeniedError",
    "BadRequestError",
    "NotFoundError",
    "UnprocessableEntityError",
    "ContextWindowExceededError",
    "ContentPolicyViolationError",
    "BudgetExceededError",
)
# Unwrapped network-level failures (connection refused/reset, peer closed
# the connection mid-response). Timeouts are excluded by ``_is_timeout_error``.
_TRANSIENT_HTTPX_ERRORS: tuple[type[BaseException], ...] = (httpx.NetworkError, httpx.RemoteProtocolError)

# Maximum depth when walking an exception's ``__cause__``/``__context__`` chain.
_MAX_CAUSE_CHAIN_DEPTH = 8


def _exception_classes(module: Any, names: tuple[str, ...]) -> tuple[type[BaseException], ...]:
    """Return the exception classes ``module`` really defines for ``names``."""
    classes: list[type[BaseException]] = []
    for name in names:
        candidate = getattr(module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            classes.append(candidate)
    return tuple(classes)


def _error_status_code(exc: BaseException) -> int | None:
    """HTTP status code carried by a provider exception, if any."""
    for source in (exc, getattr(exc, "response", None)):
        status = getattr(source, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
        if isinstance(status, str) and status.isdigit():
            return int(status)
    return None


def _is_timeout_error(exc: BaseException) -> bool:
    if LiteLLMTimeout is not None and isinstance(exc, LiteLLMTimeout):
        return True
    if _openai is not None and isinstance(exc, _openai.APITimeoutError):
        return True
    return isinstance(exc, TimeoutError | httpx.TimeoutException)


def _litellm_connection_error_class() -> type[BaseException] | None:
    classes = _exception_classes(getattr(litellm, "exceptions", None), ("APIConnectionError",))
    return classes[0] if classes else None


def _is_network_failure(exc: BaseException) -> bool:
    """True for a transport-level failure (not a timeout, not litellm's catch-all)."""
    if _is_timeout_error(exc):
        return False
    if isinstance(exc, _TRANSIENT_HTTPX_ERRORS):
        return True
    if _aiohttp is not None and isinstance(exc, _aiohttp.ClientConnectionError):
        return True
    if _openai is not None and isinstance(exc, _openai.APIConnectionError):
        litellm_connection_error = _litellm_connection_error_class()
        return litellm_connection_error is None or not isinstance(exc, litellm_connection_error)
    return False


def _cause_chain_has_network_failure(exc: BaseException) -> bool:
    """True when the ``__cause__``/``__context__`` chain below ``exc`` holds a network failure."""
    seen: set[int] = {id(exc)}
    pending = [exc.__cause__, exc.__context__]
    depth = 0
    while pending and depth < _MAX_CAUSE_CHAIN_DEPTH:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        depth += 1
        if _is_network_failure(current):
            return True
        pending.extend((current.__cause__, current.__context__))
    return False


def _is_transient_provider_error(exc: BaseException) -> bool:
    """True when ``exc`` is a transient provider failure worth one quick retry.

    Classification uses the exception class, its HTTP status code and its
    cause chain only, never the provider's message text. Timeouts are not
    transient here: they follow the separate ``retry_on_timeout`` policy.
    """
    if not isinstance(exc, Exception) or isinstance(exc, LLMError):
        return False
    if _is_timeout_error(exc):
        return False
    litellm_exceptions = getattr(litellm, "exceptions", None)
    permanent = _exception_classes(litellm_exceptions, _PERMANENT_LITELLM_ERROR_NAMES)
    if permanent and isinstance(exc, permanent):
        return False
    litellm_connection_error = _litellm_connection_error_class()
    if litellm_connection_error is not None and isinstance(exc, litellm_connection_error):
        # Catch-all wrapper: transient only when a real network failure caused it.
        return _cause_chain_has_network_failure(exc)
    transient = _exception_classes(litellm_exceptions, _TRANSIENT_LITELLM_ERROR_NAMES)
    if _openai is not None:
        transient += _exception_classes(_openai, ("RateLimitError", "InternalServerError"))
    if transient and isinstance(exc, transient):
        return True
    if _is_network_failure(exc):
        return True
    status = _error_status_code(exc)
    return status is not None and status in _TRANSIENT_STATUS_CODES


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Parse a numeric ``Retry-After`` header from the provider error, if present."""
    for headers in (getattr(exc, "headers", None), getattr(getattr(exc, "response", None), "headers", None)):
        if headers is None or not hasattr(headers, "get"):
            continue
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            raw = None
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def _describe_retried_error(exc: BaseException) -> str:
    """Compact ``ClassName(status)`` label for logs and span metadata."""
    status = _error_status_code(exc)
    name = type(exc).__name__
    return f"{name}({status})" if status is not None else name


def _transient_retry_delay(
    exc: BaseException,
    retries_done: int,
    deadline: float | None,
    attempt_elapsed: float = 0.0,
) -> float | None:
    """Backoff before the next transient-error retry, or None when it must not retry.

    None means: not transient, retry budget used up, a ``Retry-After`` hint
    above the cap, no deadline and a slow (non fast-fail) attempt, or a
    deadline that leaves no room for another attempt after the backoff.
    ``attempt_elapsed`` is how long the failed attempt ran (seconds).
    """
    if retries_done >= _LLM_TRANSIENT_MAX_RETRIES or not _is_transient_provider_error(exc):
        return None
    label = _describe_retried_error(exc)
    if deadline is None and attempt_elapsed >= _LLM_TRANSIENT_FAST_FAIL_SEC:
        logger.warning(
            "Transient LLM provider error %s not retried: attempt took %.2fs without a deadline", label, attempt_elapsed
        )
        return None
    delay = _LLM_TRANSIENT_RETRY_BASE_DELAY_SEC * (2**retries_done) + random.uniform(
        0.0, _LLM_TRANSIENT_RETRY_JITTER_SEC
    )
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        if retry_after > _LLM_TRANSIENT_RETRY_AFTER_CAP_SEC:
            logger.warning("Transient LLM provider error %s not retried: Retry-After %.1fs", label, retry_after)
            return None
        delay = max(delay, retry_after)
    if not _has_budget_for_attempt(deadline, delay):
        logger.warning("Transient LLM provider error %s not retried: no deadline budget left", label)
        return None
    return delay


def _note_transient_retry(
    pspan: Any,
    exc: BaseException,
    *,
    agent_id: str,
    model: str,
    attempt: int,
    delay: float,
    streamed: bool = False,
) -> None:
    """Log one transient-error retry and record it on the provider-call span."""
    label = _describe_retried_error(exc)
    logger.warning(
        "Transient LLM provider error for agent=%s model=%s: %s status=%s; retry %d/%d in %.2fs%s",
        agent_id,
        model,
        type(exc).__name__,
        _error_status_code(exc),
        attempt,
        _LLM_TRANSIENT_MAX_RETRIES,
        delay,
        " (stream, before first token)" if streamed else "",
    )
    try:
        pspan["metadata"]["retry_count"] = attempt
        pspan["metadata"]["retried_error"] = label
    except Exception:
        logger.debug("Recording retry metadata on the provider span failed", exc_info=True)


async def _acompletion_with_transient_retry(
    call_kwargs: dict[str, Any],
    pspan: Any,
    *,
    agent_id: str,
    model: str,
    config_timeout: float,
    deadline: float | None,
) -> tuple[Any, float]:
    """``litellm.acompletion`` with bounded retries on transient provider errors.

    Returns ``(response, attempt_started)`` where ``attempt_started`` is the
    ``time.perf_counter()`` mark of the successful attempt, so latency
    metrics exclude failed attempts and backoff. Non-transient errors and
    timeouts propagate unchanged on the first failure. Before a retry the
    provider timeout is re-clamped to the remaining deadline budget.
    """
    retries = 0
    while True:
        attempt_started = time.perf_counter()
        try:
            return await litellm.acompletion(**call_kwargs), attempt_started
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = _transient_retry_delay(exc, retries, deadline, time.perf_counter() - attempt_started)
            if delay is None:
                raise
            retries += 1
            _note_transient_retry(pspan, exc, agent_id=agent_id, model=model, attempt=retries, delay=delay)
            await asyncio.sleep(delay)
            if deadline is not None:
                call_kwargs["timeout"] = _bounded_timeout(config_timeout, deadline)


def _resolve_max_tool_rounds(max_tool_rounds: int | None, config: AgentConfig) -> int:
    """Explicit argument wins; otherwise the agent's ``max_iterations`` config (min 1)."""
    if max_tool_rounds is not None:
        return max(1, int(max_tool_rounds))
    try:
        return max(1, int(config.max_iterations))
    except (TypeError, ValueError):
        return 3


# Tool result fed back to the LLM when a tool call outlives the deadline.
_TOOL_TIMEOUT_RESULT = "Tool error: the tool call did not finish within the remaining time budget."


async def _execute_tool_with_deadline(tool_executor: Callable, fn_name: str, fn_args: dict, deadline: float | None):
    """Run one tool call, bounded by the remaining deadline budget.

    ``_MIN_ATTEMPT_BUDGET_SEC`` of the budget is reserved for the final
    answer round, so a hanging tool cannot consume the whole deadline.
    """
    remaining = _remaining_budget(deadline)
    if remaining is None:
        return await tool_executor(fn_name, fn_args)
    remaining -= _MIN_ATTEMPT_BUDGET_SEC
    if remaining <= 0:
        return _TOOL_TIMEOUT_RESULT
    try:
        return await asyncio.wait_for(tool_executor(fn_name, fn_args), timeout=remaining)
    except TimeoutError:
        logger.warning("Tool '%s' exceeded the remaining deadline budget (%.1fs)", fn_name, remaining)
        return _TOOL_TIMEOUT_RESULT


async def _record_nonstream_call_metrics(
    pspan: Any,
    *,
    agent_id: str,
    model: str,
    t0: float,
    response: Any,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    """Record whole-call latency + token usage for a non-streaming completion.

    P2: the five non-streaming metric blocks were duplicates that
    mislabeled the whole-call latency as ``ttft_ms`` (and the resulting
    tokens/total-time ratio as ``tps``). The measurement is now recorded
    as ``latency_ms``; ``ttft_ms``/``tps`` are only emitted by the
    streaming paths, where first-chunk timing actually exists.
    """
    latency_ms = (time.perf_counter() - t0) * 1000
    pspan["metadata"]["model"] = model
    if extra_metadata:
        pspan["metadata"].update(extra_metadata)
    if hasattr(response, "usage") and response.usage:
        await track_token_usage(
            agent_id=agent_id,
            provider=model.split("/")[0] if "/" in model else "unknown",
            tokens_in=response.usage.prompt_tokens or 0,
            tokens_out=response.usage.completion_tokens or 0,
            latency_ms=round(latency_ms, 2),
        )
    pspan["metadata"]["latency_ms"] = round(latency_ms, 2)


async def complete(
    agent_id: str,
    messages: list[dict],
    **overrides: Any,
) -> str:
    """Non-streaming LLM completion with bounded retries.

    Optional overrides besides model/max_tokens/temperature/reasoning_effort:
        span_collector: trace collector for ``llm_provider_call`` spans.
        deadline: absolute ``time.monotonic()`` deadline for the whole call,
            retries included. Every provider timeout is clamped to the
            remaining budget, and a retry only starts when at least
            ``_MIN_ATTEMPT_BUDGET_SEC`` remain after its backoff.
        retry_on_timeout: when False, a provider timeout is raised at once
            instead of being retried (domain agents: the HA action still
            has to run inside the dispatch budget).
    """
    span_collector = overrides.pop("span_collector", None)
    deadline: float | None = overrides.pop("deadline", None)
    retry_on_timeout = bool(overrides.pop("retry_on_timeout", True))
    row = await AgentConfigRepository.get(agent_id)
    if row is None:
        raise ValueError(f"No config found for agent: {agent_id}")
    config = AgentConfig(**row)

    model = overrides.get("model") or config.model
    if model is None:
        raise ValueError(f"No model configured for agent: {agent_id}")
    max_tokens = overrides.get("max_tokens", config.max_tokens)
    temperature = overrides.get("temperature", config.temperature)
    reasoning_effort = overrides.get("reasoning_effort") or config.reasoning_effort

    provider_params = await resolve_provider_params(model)

    logger.debug("LLM call: agent=%s model=%s tokens=%s temp=%s", agent_id, model, max_tokens, temperature)

    call_kwargs: dict[str, Any] = {}
    try:
        call_kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=_bounded_timeout(config.timeout, deadline),
            **provider_params,
        )
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
            call_kwargs["drop_params"] = True
        async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
            response, t0 = await _acompletion_with_transient_retry(
                call_kwargs, pspan, agent_id=agent_id, model=model, config_timeout=config.timeout, deadline=deadline
            )
            await _record_nonstream_call_metrics(
                pspan,
                agent_id=agent_id,
                model=model,
                t0=t0,
                response=response,
                extra_metadata={"provider": model.split("/")[0] if "/" in model else "unknown"},
            )
        if not response.choices:
            raise LLMError("Empty choices from provider")
        if response.choices[0].finish_reason == "length":
            logger.warning(
                "LLM response truncated (finish_reason=length) for agent=%s model=%s max_tokens=%s",
                agent_id,
                model,
                max_tokens,
            )
        content = (response.choices[0].message.content or "").strip() if response.choices[0].message else ""

        # Single retry on empty response (e.g. rate limiting). When the
        # completion was cut off (finish_reason="length" — typical for
        # reasoning models whose thinking tokens exhaust max_tokens), the
        # retry runs with a doubled token budget; same-budget retries would
        # fail again deterministically.
        if not content and not _has_budget_for_attempt(deadline, _LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC):
            finish_reason = response.choices[0].finish_reason if response.choices else "unknown"
            logger.warning(
                "Empty LLM response for agent=%s model=%s; no deadline budget left for a retry",
                agent_id,
                model,
            )
            raise ValueError(
                f"Empty LLM response for agent={agent_id} without retry budget "
                f"(model={model} max_tokens={max_tokens} finish_reason={finish_reason})"
            )

        if not content:
            finish_reason_first = response.choices[0].finish_reason if response.choices else "unknown"
            retry_max_tokens = max_tokens
            if finish_reason_first == "length" and isinstance(max_tokens, int):
                retry_max_tokens = min(max_tokens * 2, _LLM_ADAPTIVE_RETRY_MAX_TOKENS_CAP)
            if retry_max_tokens != max_tokens:
                logger.warning(
                    "Empty LLM response for agent=%s model=%s finish_reason=length, "
                    "retrying once with doubled max_tokens=%s (was %s)",
                    agent_id,
                    model,
                    retry_max_tokens,
                    max_tokens,
                )
                call_kwargs["max_tokens"] = retry_max_tokens
                max_tokens = retry_max_tokens
            else:
                logger.warning(
                    "Empty LLM response for agent=%s model=%s finish_reason=%s, retrying once after 1s",
                    agent_id,
                    model,
                    finish_reason_first,
                )
            await asyncio.sleep(_LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC)
            call_kwargs["timeout"] = _bounded_timeout(config.timeout, deadline)
            async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
                response, t0 = await _acompletion_with_transient_retry(
                    call_kwargs, pspan, agent_id=agent_id, model=model, config_timeout=config.timeout, deadline=deadline
                )
                await _record_nonstream_call_metrics(
                    pspan,
                    agent_id=agent_id,
                    model=model,
                    t0=t0,
                    response=response,
                    extra_metadata={"retry": True},
                )
            if not response.choices:
                raise LLMError("Empty choices from provider on retry")
            if response.choices[0].finish_reason == "length":
                logger.warning(
                    "LLM response truncated (finish_reason=length) for agent=%s model=%s max_tokens=%s",
                    agent_id,
                    model,
                    max_tokens,
                )
            content = (response.choices[0].message.content or "").strip() if response.choices[0].message else ""

        if not content:
            finish_reason = response.choices[0].finish_reason if response.choices else "unknown"
            logger.warning(
                "LLM response completely empty after retry — "
                "agent=%s model=%s max_tokens=%s finish_reason=%s "
                "(prompt likely exceeds max_tokens or model returned no content)",
                agent_id,
                model,
                max_tokens,
                finish_reason,
            )
            raise ValueError(
                f"Empty LLM response for agent={agent_id} after retry "
                f"(model={model} max_tokens={max_tokens} finish_reason={finish_reason})"
            )
        return content
    except asyncio.CancelledError:
        raise
    except litellm.exceptions.AuthenticationError:
        logger.error("Authentication failed for agent=%s model=%s -- check API key", agent_id, model)
        raise
    except litellm.exceptions.APIError as e:
        status = getattr(e, "status_code", "?")
        logger.error("LLM API error agent=%s model=%s status=%s: %s", agent_id, model, status, str(e))
        raise
    except Exception as e:
        if LiteLLMTimeout is not None and isinstance(e, LiteLLMTimeout):
            if not retry_on_timeout:
                logger.warning("LLM timeout for agent=%s model=%s (timeout retry disabled)", agent_id, model)
                raise
            if not _has_budget_for_attempt(deadline, _LLM_TIMEOUT_RETRY_DELAY_SEC):
                logger.warning(
                    "LLM timeout for agent=%s model=%s; no deadline budget left for a retry",
                    agent_id,
                    model,
                )
                raise
            logger.warning("LLM timeout for agent=%s model=%s, retrying once after 2s", agent_id, model)
            await asyncio.sleep(_LLM_TIMEOUT_RETRY_DELAY_SEC)
            call_kwargs["timeout"] = _bounded_timeout(config.timeout, deadline)
            try:
                async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
                    response, t0 = await _acompletion_with_transient_retry(
                        call_kwargs,
                        pspan,
                        agent_id=agent_id,
                        model=model,
                        config_timeout=config.timeout,
                        deadline=deadline,
                    )
                    await _record_nonstream_call_metrics(
                        pspan,
                        agent_id=agent_id,
                        model=model,
                        t0=t0,
                        response=response,
                        extra_metadata={"retry": "timeout"},
                    )
            except asyncio.CancelledError:
                raise
            if not response.choices:
                raise LLMError("Empty choices from provider on timeout retry") from e
            content = (response.choices[0].message.content or "").strip() if response.choices[0].message else ""
            if not content:
                finish_reason = response.choices[0].finish_reason if response.choices else "unknown"
                logger.warning(
                    "LLM response completely empty after timeout retry — "
                    "agent=%s model=%s max_tokens=%s finish_reason=%s",
                    agent_id,
                    model,
                    max_tokens,
                    finish_reason,
                )
                raise ValueError(
                    f"Empty LLM response for agent={agent_id} after timeout retry "
                    f"(model={model} max_tokens={max_tokens} finish_reason={finish_reason})"
                ) from e
            return content
        raise


async def complete_stream(
    agent_id: str,
    messages: list[dict],
    **overrides: Any,
) -> AsyncGenerator[str, None]:
    """Stream LLM completion tokens via litellm.acompletion(stream=True).

    Yields individual content tokens (strings). The caller must
    reconstruct the full response if needed.

    Raises LLMError on empty choices or unrecoverable API errors.
    Does NOT retry on empty response (incompatible with streaming). A
    transient provider error (rate limit, overload, 5xx, dropped
    connection) is retried only while no token has been yielded yet.
    Optional ``deadline`` (absolute ``time.monotonic()``) clamps the
    provider timeout and gates that retry.
    """
    span_collector = overrides.pop("span_collector", None)
    deadline: float | None = overrides.pop("deadline", None)
    row = await AgentConfigRepository.get(agent_id)
    if row is None:
        raise ValueError(f"No config found for agent: {agent_id}")
    config = AgentConfig(**row)

    model = overrides.get("model") or config.model
    if model is None:
        raise ValueError(f"No model configured for agent: {agent_id}")
    max_tokens = overrides.get("max_tokens", config.max_tokens)
    temperature = overrides.get("temperature", config.temperature)
    reasoning_effort = overrides.get("reasoning_effort") or config.reasoning_effort

    provider_params = await resolve_provider_params(model)

    logger.debug(
        "LLM stream call: agent=%s model=%s tokens=%s temp=%s",
        agent_id,
        model,
        max_tokens,
        temperature,
    )

    call_kwargs = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=_bounded_timeout(config.timeout, deadline),
        stream=True,
        **provider_params,
    )
    if reasoning_effort:
        call_kwargs["reasoning_effort"] = reasoning_effort
        call_kwargs["drop_params"] = True

    # Request a final usage-only trailer chunk where supported.
    call_kwargs["stream_options"] = {"include_usage": True}

    response = None
    try:
        async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
            pspan["metadata"]["model"] = model
            pspan["metadata"]["provider"] = model.split("/")[0] if "/" in model else "unknown"
            pspan["metadata"]["streamed"] = True

            retries = 0
            while True:
                # Per-attempt state: a transient error before the first token
                # restarts the stream, so nothing from a failed attempt leaks.
                # ``t_call`` marks the attempt start: ttft/tps/latency describe
                # the successful attempt only.
                t_call = time.perf_counter()
                first_chunk_time = None
                last_chunk_time = None
                saw_choice = False
                usage = None
                yielded = False
                try:
                    response = await litellm.acompletion(**call_kwargs)
                    async for chunk in response:
                        chunk_usage = getattr(chunk, "usage", None)
                        if chunk_usage is not None:
                            usage = chunk_usage
                        if not chunk.choices:
                            # Usage-only trailer chunk (stream_options include_usage).
                            continue
                        saw_choice = True
                        delta = chunk.choices[0].delta
                        content = getattr(delta, "content", None)
                        if content:
                            yielded = True
                            yield content
                        if first_chunk_time is None:
                            first_chunk_time = time.perf_counter()
                        last_chunk_time = time.perf_counter()
                        if chunk.choices[0].finish_reason == "length":
                            logger.warning(
                                "LLM stream truncated (finish_reason=length) for agent=%s model=%s max_tokens=%s",
                                agent_id,
                                model,
                                max_tokens,
                            )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Never retry once output reached the caller.
                    delay = (
                        None
                        if yielded
                        else _transient_retry_delay(exc, retries, deadline, time.perf_counter() - t_call)
                    )
                    if delay is None:
                        raise
                    retries += 1
                    _note_transient_retry(
                        pspan, exc, agent_id=agent_id, model=model, attempt=retries, delay=delay, streamed=True
                    )
                    await _close_stream_response(response)
                    response = None
                    await asyncio.sleep(delay)
                    call_kwargs["timeout"] = _bounded_timeout(config.timeout, deadline)

            if not saw_choice:
                raise LLMError("Empty choices from provider during stream")

            ttft_ms = (first_chunk_time - t_call) * 1000 if first_chunk_time else None
            stream_ms = (last_chunk_time - first_chunk_time) * 1000 if first_chunk_time and last_chunk_time else None
            latency_ms = (time.perf_counter() - t_call) * 1000

            if usage is None:
                usage = getattr(response, "usage", None)
            if usage:
                tokens_out = usage.completion_tokens or 0
                tps = tokens_out / (stream_ms / 1000.0) if stream_ms and stream_ms > 0 else None
                await track_token_usage(
                    agent_id=agent_id,
                    provider=model.split("/")[0] if "/" in model else "unknown",
                    tokens_in=usage.prompt_tokens or 0,
                    tokens_out=tokens_out,
                    ttft_ms=round(ttft_ms, 2) if ttft_ms else None,
                    tps=round(tps, 2) if tps else None,
                    latency_ms=round(latency_ms, 2),
                )
                pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2) if ttft_ms else None
                pspan["metadata"]["tps"] = round(tps, 2) if tps else None
            pspan["metadata"]["latency_ms"] = round(latency_ms, 2)
    except asyncio.CancelledError:
        raise
    except litellm.exceptions.AuthenticationError:
        logger.error("Authentication failed for agent=%s model=%s -- check API key", agent_id, model)
        raise
    except litellm.exceptions.APIError as e:
        status = getattr(e, "status_code", "?")
        logger.error("LLM stream API error agent=%s model=%s status=%s: %s", agent_id, model, status, str(e))
        raise
    except Exception as e:
        if LiteLLMTimeout is not None and isinstance(e, LiteLLMTimeout):
            logger.warning("LLM stream timeout for agent=%s model=%s", agent_id, model)
            raise
        logger.error("LLM stream error agent=%s model=%s: %s", agent_id, model, str(e))
        raise
    finally:
        # A stalled / cancelled / closed consumer must stop the provider stream.
        await _close_stream_response(response)


async def complete_with_tools(
    agent_id: str,
    messages: list[dict],
    tools: list[dict],
    tool_executor: Callable,
    max_tool_rounds: int | None = None,
    **overrides: Any,
) -> str:
    """LLM completion with tool/function calling loop.

    Parameters:
        agent_id: Agent ID for config lookup.
        messages: Conversation messages (system + user).
        tools: OpenAI-format tool schemas.
        tool_executor: Async callable (tool_name, arguments) -> str.
            When the model returns multiple ``tool_calls`` in one assistant message,
            they are executed **in parallel** (``asyncio.gather``); tool messages are
            still appended in the same order as ``tool_calls`` for the next LLM turn.
        max_tool_rounds: Max LLM<->tool round-trips; ``None`` uses the agent's
            ``max_iterations`` config.
        **overrides: Model/temperature/max_tokens overrides, plus
            ``deadline`` (absolute ``time.monotonic()``): a whole-loop budget.
            Provider timeouts and tool calls are clamped to the remaining
            budget; when less than ``_MIN_ATTEMPT_BUDGET_SEC`` remains before
            a round, the loop stops and forces the final answer.

    Returns:
        Final text response from the LLM.
    """
    span_collector = overrides.pop("span_collector", None)
    deadline: float | None = overrides.pop("deadline", None)
    row = await AgentConfigRepository.get(agent_id)
    if row is None:
        raise ValueError(f"No config found for agent: {agent_id}")
    config = AgentConfig(**row)
    max_tool_rounds = _resolve_max_tool_rounds(max_tool_rounds, config)

    model = overrides.get("model") or config.model
    if model is None:
        raise ValueError(f"No model configured for agent: {agent_id}")
    max_tokens = overrides.get("max_tokens", config.max_tokens)
    temperature = overrides.get("temperature", config.temperature)
    reasoning_effort = overrides.get("reasoning_effort") or config.reasoning_effort

    provider_params = await resolve_provider_params(model)

    # Make a mutable copy of messages for the tool-call loop
    msgs = list(messages)

    rounds_run = 0
    for _round in range(max_tool_rounds):
        if _round > 0 and not _has_budget_for_attempt(deadline):
            logger.warning(
                "Tool-loop deadline reached after %d round(s) for agent=%s, forcing final response",
                _round,
                agent_id,
            )
            break
        rounds_run = _round + 1
        logger.debug(
            "LLM tool-call round %d: agent=%s model=%s",
            _round + 1,
            agent_id,
            model,
        )
        tool_call_kwargs = dict(
            model=model,
            messages=msgs,
            tools=tools,
            tool_choice="auto",
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=_bounded_timeout(config.timeout, deadline),
            **provider_params,
        )
        if reasoning_effort:
            tool_call_kwargs["reasoning_effort"] = reasoning_effort
            tool_call_kwargs["drop_params"] = True
        async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
            response, t0 = await _acompletion_with_transient_retry(
                tool_call_kwargs,
                pspan,
                agent_id=agent_id,
                model=model,
                config_timeout=config.timeout,
                deadline=deadline,
            )
            await _record_nonstream_call_metrics(
                pspan,
                agent_id=agent_id,
                model=model,
                t0=t0,
                response=response,
                extra_metadata={"round": _round + 1},
            )
        if not response.choices:
            raise LLMError("Empty choices from provider")
        msg = response.choices[0].message
        if msg is None:
            return ""
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            # No tool calls -- return the text content
            if response.choices[0].finish_reason == "length":
                logger.warning(
                    "LLM response truncated (finish_reason=length) for agent=%s model=%s max_tokens=%s",
                    agent_id,
                    model,
                    max_tokens,
                )
            content = (msg.content or "").strip()
            if not content:
                logger.warning(
                    "Empty LLM response in tool-call loop for agent=%s round=%d",
                    agent_id,
                    _round + 1,
                )
                return ""
            return content

        # Validate and sanitize tool call names before they enter conversation history
        valid_names = {t["function"]["name"] for t in tools}
        fixed_calls = []
        invalid_map: dict[str, str] = {}

        for tc in tool_calls:
            original_name = tc.function.name
            fixed = _sanitize_tool_name(original_name, valid_names)
            if fixed is not None:
                if fixed != original_name:
                    logger.warning(
                        "Sanitized tool name '%s' -> '%s' for agent=%s",
                        original_name,
                        fixed,
                        agent_id,
                    )
                tc.function.name = fixed
                fixed_calls.append(tc)
            else:
                logger.warning(
                    "Invalid tool name '%s' from agent=%s; closest valid tools: %s",
                    original_name,
                    agent_id,
                    ", ".join(sorted(valid_names)) if valid_names else "none",
                )
                # History placeholder only (the call is NOT executed): the
                # tool message carries the invalid-name error for the LLM.
                fallback_name = sorted(valid_names)[0] if valid_names else None
                if fallback_name is not None:
                    tc.function.name = fallback_name
                    invalid_map[tc.id] = original_name
                    fixed_calls.append(tc)

        # Edge case: every tool call was invalid and no fallback exists (empty tools list)
        if not fixed_calls:
            error_text = (
                f"The model generated invalid tool call names "
                f"({[tc.function.name for tc in tool_calls]}). "
                f"No valid tools are available."
            )
            msgs.append({"role": "assistant", "content": error_text})
            continue

        # Rebuild assistant message as a clean dict so invalid names never enter history
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content,
        }
        if getattr(msg, "refusal", None):
            assistant_msg["refusal"] = msg.refusal
        if fixed_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in fixed_calls
            ]
        msgs.append(assistant_msg)

        async def _run_one_tool(tc, _invalid_map=invalid_map, _valid_names=valid_names) -> tuple[str | None, str]:
            fn_name = tc.function.name
            if tc.id in _invalid_map:
                original = _invalid_map[tc.id]
                result_str = (
                    f"Error: invalid tool name '{original}'. "
                    f"Available tools: {', '.join(sorted(_valid_names))}. "
                    f"Please use one of the listed tool names."
                )
            else:
                try:
                    fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    fn_args = {}
                    logger.warning("Failed to parse tool arguments for '%s'", fn_name)

                logger.debug("Executing tool '%s' with args: %s", fn_name, redact_sensitive_values(fn_args))

                try:
                    result_str = await _execute_tool_with_deadline(tool_executor, fn_name, fn_args, deadline)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Tool executor '%s' raised: %s", fn_name, e)
                    result_str = f"Tool error: {e}"
            return tc.id, result_str

        # Parallel execution: all tool_calls for this round run concurrently.
        # ``asyncio.gather`` preserves completion order matching the input awaitables,
        # so tool messages stay aligned with ``tool_calls`` order for the API.
        tool_results = await asyncio.gather(*[_run_one_tool(tc) for tc in fixed_calls])

        for tool_call_id, result_str in tool_results:
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_str,
                }
            )

    # Max rounds exhausted (or deadline reached) -- force a final text response without tools
    logger.warning(
        "Tool loop ended after %d round(s) (max %d) for agent=%s, forcing final response",
        rounds_run,
        max_tool_rounds,
        agent_id,
    )
    final_kwargs = dict(
        model=model,
        messages=msgs,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=_bounded_timeout(config.timeout, deadline),
        **provider_params,
    )
    if reasoning_effort:
        final_kwargs["reasoning_effort"] = reasoning_effort
        final_kwargs["drop_params"] = True
    async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
        response, t0 = await _acompletion_with_transient_retry(
            final_kwargs, pspan, agent_id=agent_id, model=model, config_timeout=config.timeout, deadline=deadline
        )
        await _record_nonstream_call_metrics(
            pspan,
            agent_id=agent_id,
            model=model,
            t0=t0,
            response=response,
            extra_metadata={"round": rounds_run + 1, "forced_final": True},
        )
    if not response.choices:
        raise LLMError("Empty choices from provider")
    if response.choices[0].finish_reason == "length":
        logger.warning(
            "LLM response truncated (finish_reason=length) for agent=%s model=%s max_tokens=%s",
            agent_id,
            model,
            max_tokens,
        )
    content = (response.choices[0].message.content or "").strip() if response.choices[0].message else ""
    return content or ""


async def complete_with_tools_stream(
    agent_id: str,
    messages: list[dict],
    tools: list[dict],
    tool_executor: Callable,
    max_tool_rounds: int | None = None,
    **overrides: Any,
) -> AsyncGenerator[str, None]:
    """Streaming variant of :func:`complete_with_tools`.

    Every round runs with ``stream=True``; content tokens are yielded as they
    arrive so the caller can relay them downstream immediately. When a round
    finishes WITH tool calls, the tools execute (in parallel, same semantics
    as the non-streaming loop) and the next round starts; the round that
    finishes WITHOUT tool calls is the final answer.

    Note: providers may attach content to a tool-call round ("preamble").
    Such tokens are yielded like any other content and are also kept in the
    assistant history, mirroring the non-streaming loop which preserves
    ``msg.content`` alongside tool calls. In practice tool-call rounds
    carry no content, so only the final answer is streamed.

    Yields:
        Individual content tokens (strings). The caller must collect them
        to reconstruct the final response.
    """
    span_collector = overrides.pop("span_collector", None)
    deadline: float | None = overrides.pop("deadline", None)
    row = await AgentConfigRepository.get(agent_id)
    if row is None:
        raise ValueError(f"No config found for agent: {agent_id}")
    config = AgentConfig(**row)
    max_tool_rounds = _resolve_max_tool_rounds(max_tool_rounds, config)

    model = overrides.get("model") or config.model
    if model is None:
        raise ValueError(f"No model configured for agent: {agent_id}")
    max_tokens = overrides.get("max_tokens", config.max_tokens)
    temperature = overrides.get("temperature", config.temperature)
    reasoning_effort = overrides.get("reasoning_effort") or config.reasoning_effort

    provider_params = await resolve_provider_params(model)

    # Make a mutable copy of messages for the tool-call loop
    msgs = list(messages)
    valid_names = {t["function"]["name"] for t in tools}

    async def _stream_round(
        round_msgs: list[dict],
        round_no: int,
        *,
        with_tools: bool,
        result: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Stream one LLM round, yielding content tokens as they arrive.

        Populates ``result["content"]`` (full round text) and
        ``result["tool_calls"]`` (reconstructed tool call dicts).
        """
        call_kwargs = dict(
            model=model,
            messages=round_msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=_bounded_timeout(config.timeout, deadline),
            stream=True,
            **provider_params,
        )
        if with_tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = "auto"
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
            call_kwargs["drop_params"] = True
        # Request a final usage-only trailer chunk where supported.
        call_kwargs["stream_options"] = {"include_usage": True}

        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, str]] = {}
        finish_reason = None
        async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
            pspan["metadata"]["model"] = model
            pspan["metadata"]["provider"] = model.split("/")[0] if "/" in model else "unknown"
            pspan["metadata"]["streamed"] = True
            pspan["metadata"]["round"] = round_no
            retries = 0
            while True:
                # Per-attempt state: a transient error before the first token
                # restarts the round, so partial tool-call deltas are dropped.
                # ``t_call`` marks the attempt start (metrics: successful attempt).
                t_call = time.perf_counter()
                content_parts.clear()
                tool_calls_acc.clear()
                finish_reason = None
                first_chunk_time = None
                last_chunk_time = None
                usage = None
                yielded = False
                response = None
                try:
                    response = await litellm.acompletion(**call_kwargs)
                    async for chunk in response:
                        chunk_usage = getattr(chunk, "usage", None)
                        if chunk_usage is not None:
                            usage = chunk_usage
                        if not chunk.choices:
                            # Usage-only trailer chunk (stream_options include_usage).
                            continue
                        delta = chunk.choices[0].delta
                        content = getattr(delta, "content", None)
                        if content:
                            content_parts.append(content)
                            yielded = True
                            yield content
                        for tc_delta in getattr(delta, "tool_calls", None) or []:
                            idx = getattr(tc_delta, "index", 0) or 0
                            slot = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if getattr(tc_delta, "id", None):
                                slot["id"] = tc_delta.id
                            fn = getattr(tc_delta, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    slot["name"] += fn.name
                                if getattr(fn, "arguments", None):
                                    slot["arguments"] += fn.arguments
                        if first_chunk_time is None:
                            first_chunk_time = time.perf_counter()
                        last_chunk_time = time.perf_counter()
                        if chunk.choices[0].finish_reason:
                            finish_reason = chunk.choices[0].finish_reason
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Never retry once output reached the caller.
                    delay = (
                        None
                        if yielded
                        else _transient_retry_delay(exc, retries, deadline, time.perf_counter() - t_call)
                    )
                    if delay is None:
                        raise
                    retries += 1
                    _note_transient_retry(
                        pspan, exc, agent_id=agent_id, model=model, attempt=retries, delay=delay, streamed=True
                    )
                finally:
                    # A stalled / cancelled consumer must stop the provider stream;
                    # a failed attempt is closed here before the retry backoff.
                    await _close_stream_response(response)
                # Only reached on a transient-error retry (success breaks, other errors raise).
                await asyncio.sleep(delay)
                call_kwargs["timeout"] = _bounded_timeout(config.timeout, deadline)

            ttft_ms = (first_chunk_time - t_call) * 1000 if first_chunk_time else None
            stream_ms = (last_chunk_time - first_chunk_time) * 1000 if first_chunk_time and last_chunk_time else None
            latency_ms = (time.perf_counter() - t_call) * 1000
            if usage is None:
                usage = getattr(response, "usage", None)
            if usage:
                tokens_out = usage.completion_tokens or 0
                tps = tokens_out / (stream_ms / 1000.0) if stream_ms and stream_ms > 0 else None
                await track_token_usage(
                    agent_id=agent_id,
                    provider=model.split("/")[0] if "/" in model else "unknown",
                    tokens_in=usage.prompt_tokens or 0,
                    tokens_out=tokens_out,
                    ttft_ms=round(ttft_ms, 2) if ttft_ms else None,
                    tps=round(tps, 2) if tps else None,
                    latency_ms=round(latency_ms, 2),
                )
                pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2) if ttft_ms else None
                pspan["metadata"]["tps"] = round(tps, 2) if tps else None
            pspan["metadata"]["latency_ms"] = round(latency_ms, 2)

        if finish_reason == "length":
            logger.warning(
                "LLM stream truncated (finish_reason=length) for agent=%s model=%s max_tokens=%s",
                agent_id,
                model,
                max_tokens,
            )
        result["content"] = "".join(content_parts)
        result["tool_calls"] = [
            {
                "id": slot["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            }
            for idx, slot in sorted(tool_calls_acc.items())
        ]

    rounds_run = 0
    for _round in range(max_tool_rounds):
        if _round > 0 and not _has_budget_for_attempt(deadline):
            logger.warning(
                "Tool-loop deadline reached after %d round(s) for agent=%s, forcing final response",
                _round,
                agent_id,
            )
            break
        rounds_run = _round + 1
        logger.debug(
            "LLM tool-call stream round %d: agent=%s model=%s",
            _round + 1,
            agent_id,
            model,
        )
        round_result: dict[str, Any] = {}
        async for _token in _stream_round(msgs, _round + 1, with_tools=True, result=round_result):
            yield _token
        content = round_result["content"]
        tool_calls = round_result["tool_calls"]

        if not tool_calls:
            # Final no-tools round -- tokens already streamed to the caller.
            return

        # Validate and sanitize tool call names before they enter conversation history
        fixed_calls = []
        invalid_map: dict[str, str] = {}

        for tc in tool_calls:
            original_name = tc["function"]["name"]
            fixed = _sanitize_tool_name(original_name, valid_names)
            if fixed is not None:
                if fixed != original_name:
                    logger.warning(
                        "Sanitized tool name '%s' -> '%s' for agent=%s",
                        original_name,
                        fixed,
                        agent_id,
                    )
                tc["function"]["name"] = fixed
                fixed_calls.append(tc)
            else:
                logger.warning(
                    "Invalid tool name '%s' from agent=%s; closest valid tools: %s",
                    original_name,
                    agent_id,
                    ", ".join(sorted(valid_names)) if valid_names else "none",
                )
                # History placeholder only (the call is NOT executed): the
                # tool message carries the invalid-name error for the LLM.
                fallback_name = sorted(valid_names)[0] if valid_names else None
                if fallback_name is not None:
                    tc["function"]["name"] = fallback_name
                    invalid_map[tc["id"]] = original_name
                    fixed_calls.append(tc)

        # Edge case: every tool call was invalid and no fallback exists (empty tools list)
        if not fixed_calls:
            error_text = (
                f"The model generated invalid tool call names "
                f"({[tc['function']['name'] for tc in tool_calls]}). "
                f"No valid tools are available."
            )
            msgs.append({"role": "assistant", "content": error_text})
            continue

        # Rebuild assistant message as a clean dict so invalid names never enter history
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": fixed_calls,
        }
        msgs.append(assistant_msg)

        async def _run_one_tool(tc, _invalid_map=invalid_map, _valid_names=valid_names) -> tuple[str | None, str]:
            fn_name = tc["function"]["name"]
            if tc["id"] in _invalid_map:
                original = _invalid_map[tc["id"]]
                result_str = (
                    f"Error: invalid tool name '{original}'. "
                    f"Available tools: {', '.join(sorted(_valid_names))}. "
                    f"Please use one of the listed tool names."
                )
            else:
                try:
                    fn_args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                except (json.JSONDecodeError, TypeError):
                    fn_args = {}
                    logger.warning("Failed to parse tool arguments for '%s'", fn_name)

                logger.debug("Executing tool '%s' with args: %s", fn_name, redact_sensitive_values(fn_args))

                try:
                    result_str = await _execute_tool_with_deadline(tool_executor, fn_name, fn_args, deadline)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Tool executor '%s' raised: %s", fn_name, e)
                    result_str = f"Tool error: {e}"
            return tc["id"], result_str

        # Parallel execution: all tool_calls for this round run concurrently.
        # ``asyncio.gather`` preserves completion order matching the input awaitables,
        # so tool messages stay aligned with ``tool_calls`` order for the API.
        tool_results = await asyncio.gather(*[_run_one_tool(tc) for tc in fixed_calls])

        for tool_call_id, result_str in tool_results:
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_str,
                }
            )

    # Max rounds exhausted (or deadline reached) -- force a final text response without tools (streamed).
    logger.warning(
        "Tool loop ended after %d round(s) (max %d) for agent=%s, forcing final response",
        rounds_run,
        max_tool_rounds,
        agent_id,
    )
    final_result: dict[str, Any] = {}
    async for _token in _stream_round(msgs, rounds_run + 1, with_tools=False, result=final_result):
        yield _token
