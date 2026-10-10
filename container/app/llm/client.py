import asyncio
import inspect
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

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
            t0 = time.perf_counter()
            response = await litellm.acompletion(**call_kwargs)
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
                t0 = time.perf_counter()
                response = await litellm.acompletion(**call_kwargs)
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
                    t0 = time.perf_counter()
                    response = await litellm.acompletion(**call_kwargs)
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
    Does NOT retry on empty response (incompatible with streaming).
    """
    span_collector = overrides.pop("span_collector", None)
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
        timeout=config.timeout,
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

            t_call = time.perf_counter()
            response = await litellm.acompletion(**call_kwargs)

            first_chunk_time = None
            last_chunk_time = None
            saw_choice = False
            usage = None
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
            t0 = time.perf_counter()
            response = await litellm.acompletion(**tool_call_kwargs)
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
        t0 = time.perf_counter()
        response = await litellm.acompletion(**final_kwargs)
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
            t_call = time.perf_counter()
            response = await litellm.acompletion(**call_kwargs)
            try:
                first_chunk_time = None
                last_chunk_time = None
                usage = None
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
            finally:
                # A stalled / cancelled consumer must stop the provider stream.
                await _close_stream_response(response)

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
