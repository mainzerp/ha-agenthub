import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

import litellm

from app.analytics.collector import track_token_usage
from app.analytics.tracer import _optional_span
from app.db.repository import AgentConfigRepository
from app.llm.providers import resolve_provider_params
from app.models.agent import AgentConfig

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
            response = await litellm.acompletion(**call_kwargs)
            pspan["metadata"]["model"] = model
            pspan["metadata"]["provider"] = model.split("/")[0] if "/" in model else "unknown"
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
                response = await litellm.acompletion(**call_kwargs)
                pspan["metadata"]["model"] = model
                pspan["metadata"]["retry"] = True
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
        if hasattr(response, "usage") and response.usage:
            await track_token_usage(
                agent_id=agent_id,
                provider=model.split("/")[0] if "/" in model else "unknown",
                tokens_in=response.usage.prompt_tokens or 0,
                tokens_out=response.usage.completion_tokens or 0,
            )
        return content
    except litellm.exceptions.AuthenticationError:
        logger.error("Authentication failed for agent=%s model=%s -- check API key", agent_id, model)
        raise
    except litellm.Timeout as e:
        logger.warning(
            "LLM timeout for agent=%s model=%s, retrying once after 2s", agent_id, model
        )
        await asyncio.sleep(2)
        try:
            async with _optional_span(span_collector, "llm_provider_call", agent_id=agent_id) as pspan:
                response = await litellm.acompletion(**call_kwargs)
                pspan["metadata"]["model"] = model
                pspan["metadata"]["retry"] = "timeout"
        except Exception:
            raise
        if not response.choices:
            raise LLMError("Empty choices from provider on timeout retry") from e
        content = (response.choices[0].message.content or "").strip() if response.choices[0].message else ""
    except litellm.exceptions.APIError as e:
        status = getattr(e, "status_code", "?")
        logger.error("LLM API error agent=%s model=%s status=%s: %s", agent_id, model, status, str(e))
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
            response = await litellm.acompletion(**tool_call_kwargs)
            pspan["metadata"]["model"] = model
            pspan["metadata"]["round"] = _round + 1
        if hasattr(response, "usage") and response.usage:
            await track_token_usage(
                agent_id=agent_id,
                provider=model.split("/")[0] if "/" in model else "unknown",
                tokens_in=response.usage.prompt_tokens or 0,
                tokens_out=response.usage.completion_tokens or 0,
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

        # Append the assistant message with tool_calls to the conversation
        msgs.append(msg)

        async def _run_one_tool(tc) -> tuple[str | None, str]:
            fn_name = tc.function.name
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
        tool_results = await asyncio.gather(*[_run_one_tool(tc) for tc in tool_calls])

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
        response = await litellm.acompletion(**final_kwargs)
        pspan["metadata"]["model"] = model
        pspan["metadata"]["round"] = max_tool_rounds + 1
        pspan["metadata"]["forced_final"] = True
    if hasattr(response, "usage") and response.usage:
        await track_token_usage(
            agent_id=agent_id,
            provider=model.split("/")[0] if "/" in model else "unknown",
            tokens_in=response.usage.prompt_tokens or 0,
            tokens_out=response.usage.completion_tokens or 0,
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
