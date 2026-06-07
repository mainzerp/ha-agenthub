import asyncio
import contextlib
import difflib
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

import litellm

from app.analytics.collector import track_token_usage
from app.analytics.tracer import _optional_span
from app.db.repository import AgentConfigRepository
from app.llm.providers import resolve_provider_params
from app.models.agent import AgentConfig

try:
    from litellm.exceptions import Timeout as LiteLLMTimeout
except ImportError:
    LiteLLMTimeout = None

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM provider returns an unusable response."""


# Enable litellm debug logging for provider troubleshooting.
os.environ["LITELLM_LOG"] = "DEBUG"

# P3-11: backoff between the first LLM call and the single retry that
# kicks in when the provider returns an empty completion (typically
# transient rate limiting). Kept short because the call site is in
# the request hot path.
_LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC = 1.0


def _sanitize_tool_name(name: str, valid_names: set[str]) -> str | None:
    """Return a valid tool name, or None if the name cannot be repaired."""
    if name in valid_names:
        return name
    match = re.match(r"^([a-zA-Z0-9_-]+)", name)
    if match:
        prefix = match.group(1)
        if prefix in valid_names:
            return prefix
    close = difflib.get_close_matches(name, valid_names, n=1, cutoff=0.5)
    if close:
        return close[0]
    return None


async def complete(
    agent_id: str,
    messages: list[dict],
    **overrides: Any,
) -> str:
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

    logger.debug("LLM call: agent=%s model=%s tokens=%s temp=%s", agent_id, model, max_tokens, temperature)

    try:
        call_kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=config.timeout,
            **provider_params,
        )
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
            call_kwargs["drop_params"] = True
        async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
            t0 = time.perf_counter()
            response = await litellm.acompletion(**call_kwargs)
            ttft_ms = (time.perf_counter() - t0) * 1000
            pspan["metadata"]["model"] = model
            pspan["metadata"]["provider"] = model.split("/")[0] if "/" in model else "unknown"
            tps = None
            if hasattr(response, "usage") and response.usage and response.usage.completion_tokens:
                tps = response.usage.completion_tokens / (ttft_ms / 1000.0) if ttft_ms > 0 else None
            if hasattr(response, "usage") and response.usage:
                await track_token_usage(
                    agent_id=agent_id,
                    provider=model.split("/")[0] if "/" in model else "unknown",
                    tokens_in=response.usage.prompt_tokens or 0,
                    tokens_out=response.usage.completion_tokens or 0,
                    ttft_ms=round(ttft_ms, 2),
                    tps=round(tps, 2) if tps else None,
                )
            pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2)
            pspan["metadata"]["tps"] = round(tps, 2) if tps else None
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

        # Single retry on empty response (e.g. rate limiting)
        if not content:
            logger.warning(
                "Empty LLM response for agent=%s model=%s finish_reason=%s, retrying once after 1s",
                agent_id,
                model,
                response.choices[0].finish_reason if response.choices else "unknown",
            )
            await asyncio.sleep(_LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC)
            async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
                t0 = time.perf_counter()
                response = await litellm.acompletion(**call_kwargs)
                ttft_ms = (time.perf_counter() - t0) * 1000
                pspan["metadata"]["model"] = model
                pspan["metadata"]["retry"] = True
                tps = None
                if hasattr(response, "usage") and response.usage and response.usage.completion_tokens:
                    tps = response.usage.completion_tokens / (ttft_ms / 1000.0) if ttft_ms > 0 else None
                if hasattr(response, "usage") and response.usage:
                    await track_token_usage(
                        agent_id=agent_id,
                        provider=model.split("/")[0] if "/" in model else "unknown",
                        tokens_in=response.usage.prompt_tokens or 0,
                        tokens_out=response.usage.completion_tokens or 0,
                        ttft_ms=round(ttft_ms, 2),
                        tps=round(tps, 2) if tps else None,
                    )
                pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2)
                pspan["metadata"]["tps"] = round(tps, 2) if tps else None
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
    except litellm.exceptions.AuthenticationError:
        logger.error("Authentication failed for agent=%s model=%s -- check API key", agent_id, model)
        raise
    except litellm.exceptions.APIError as e:
        status = getattr(e, "status_code", "?")
        logger.error("LLM API error agent=%s model=%s status=%s: %s", agent_id, model, status, str(e))
        raise
    except Exception as e:
        if LiteLLMTimeout is not None and isinstance(e, LiteLLMTimeout):
            logger.warning("LLM timeout for agent=%s model=%s, retrying once after 2s", agent_id, model)
            await asyncio.sleep(2)
            try:
                async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
                    t0 = time.perf_counter()
                    response = await litellm.acompletion(**call_kwargs)
                    ttft_ms = (time.perf_counter() - t0) * 1000
                    pspan["metadata"]["model"] = model
                    pspan["metadata"]["retry"] = "timeout"
                    tps = None
                    if hasattr(response, "usage") and response.usage and response.usage.completion_tokens:
                        tps = response.usage.completion_tokens / (ttft_ms / 1000.0) if ttft_ms > 0 else None
                    if hasattr(response, "usage") and response.usage:
                        await track_token_usage(
                            agent_id=agent_id,
                            provider=model.split("/")[0] if "/" in model else "unknown",
                            tokens_in=response.usage.prompt_tokens or 0,
                            tokens_out=response.usage.completion_tokens or 0,
                            ttft_ms=round(ttft_ms, 2),
                            tps=round(tps, 2) if tps else None,
                        )
                    pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2)
                    pspan["metadata"]["tps"] = round(tps, 2) if tps else None
            except Exception:
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

    # Attempt usage tracking via stream_options; skip if unsupported.
    with contextlib.suppress(TypeError, ValueError):
        call_kwargs["stream_options"] = {"include_usage": True}

    try:
        async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
            pspan["metadata"]["model"] = model
            pspan["metadata"]["provider"] = model.split("/")[0] if "/" in model else "unknown"
            pspan["metadata"]["streamed"] = True

            t_call = time.perf_counter()
            response = await litellm.acompletion(**call_kwargs)

            first_chunk_time = None
            last_chunk_time = None
            async for chunk in response:
                if not chunk.choices:
                    raise LLMError("Empty choices from provider during stream")
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

            ttft_ms = (first_chunk_time - t_call) * 1000 if first_chunk_time else None
            stream_ms = (last_chunk_time - first_chunk_time) * 1000 if first_chunk_time and last_chunk_time else None

            # Token usage may be delivered in a final chunk with empty choices.
            if hasattr(response, "usage") and response.usage:
                tokens_out = response.usage.completion_tokens or 0
                tps = tokens_out / (stream_ms / 1000.0) if stream_ms and stream_ms > 0 else None
                await track_token_usage(
                    agent_id=agent_id,
                    provider=model.split("/")[0] if "/" in model else "unknown",
                    tokens_in=response.usage.prompt_tokens or 0,
                    tokens_out=tokens_out,
                    ttft_ms=round(ttft_ms, 2) if ttft_ms else None,
                    tps=round(tps, 2) if tps else None,
                )
                pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2) if ttft_ms else None
                pspan["metadata"]["tps"] = round(tps, 2) if tps else None
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


async def complete_with_tools(
    agent_id: str,
    messages: list[dict],
    tools: list[dict],
    tool_executor: Callable,
    max_tool_rounds: int = 5,
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
        max_tool_rounds: Max LLM<->tool round-trips (default 5).
        **overrides: Model/temperature/max_tokens overrides.

    Returns:
        Final text response from the LLM.
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

    # Make a mutable copy of messages for the tool-call loop
    msgs = list(messages)

    for _round in range(max_tool_rounds):
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
            timeout=config.timeout,
            **provider_params,
        )
        if reasoning_effort:
            tool_call_kwargs["reasoning_effort"] = reasoning_effort
            tool_call_kwargs["drop_params"] = True
        async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
            t0 = time.perf_counter()
            response = await litellm.acompletion(**tool_call_kwargs)
            ttft_ms = (time.perf_counter() - t0) * 1000
            pspan["metadata"]["model"] = model
            pspan["metadata"]["round"] = _round + 1
            tps = None
            if hasattr(response, "usage") and response.usage and response.usage.completion_tokens:
                tps = response.usage.completion_tokens / (ttft_ms / 1000.0) if ttft_ms > 0 else None
            if hasattr(response, "usage") and response.usage:
                await track_token_usage(
                    agent_id=agent_id,
                    provider=model.split("/")[0] if "/" in model else "unknown",
                    tokens_in=response.usage.prompt_tokens or 0,
                    tokens_out=response.usage.completion_tokens or 0,
                    ttft_ms=round(ttft_ms, 2),
                    tps=round(tps, 2) if tps else None,
                )
            pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2)
            pspan["metadata"]["tps"] = round(tps, 2) if tps else None
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
                fallback = difflib.get_close_matches(original_name, valid_names, n=1, cutoff=0.1)
                fallback_name = fallback[0] if fallback else (next(iter(valid_names)) if valid_names else None)
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

                logger.debug("Executing tool '%s' with args: %s", fn_name, fn_args)

                try:
                    result_str = await tool_executor(fn_name, fn_args)
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

    # Max rounds exhausted -- force a final text response without tools
    logger.warning(
        "Max tool rounds (%d) exhausted for agent=%s, forcing final response",
        max_tool_rounds,
        agent_id,
    )
    final_kwargs = dict(
        model=model,
        messages=msgs,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=config.timeout,
        **provider_params,
    )
    if reasoning_effort:
        final_kwargs["reasoning_effort"] = reasoning_effort
        final_kwargs["drop_params"] = True
    async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
        t0 = time.perf_counter()
        response = await litellm.acompletion(**final_kwargs)
        ttft_ms = (time.perf_counter() - t0) * 1000
        pspan["metadata"]["model"] = model
        pspan["metadata"]["round"] = max_tool_rounds + 1
        pspan["metadata"]["forced_final"] = True
        tps = None
        if hasattr(response, "usage") and response.usage and response.usage.completion_tokens:
            tps = response.usage.completion_tokens / (ttft_ms / 1000.0) if ttft_ms > 0 else None
        if hasattr(response, "usage") and response.usage:
            await track_token_usage(
                agent_id=agent_id,
                provider=model.split("/")[0] if "/" in model else "unknown",
                tokens_in=response.usage.prompt_tokens or 0,
                tokens_out=response.usage.completion_tokens or 0,
                ttft_ms=round(ttft_ms, 2),
                tps=round(tps, 2) if tps else None,
            )
        pspan["metadata"]["ttft_ms"] = round(ttft_ms, 2)
        pspan["metadata"]["tps"] = round(tps, 2) if tps else None
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
