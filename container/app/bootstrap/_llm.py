"""Bootstrap: LLM client wrapper for components that need LLM without agent DB config."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class _LLMClientWrapper:
    """Thin wrapper that calls litellm directly without requiring an agent DB config."""

    async def complete(self, agent_id: str, messages: list, **kwargs):
        import litellm

        from app.llm.providers import resolve_provider_params

        model = kwargs.get("model")
        if not model:
            raise ValueError("model is required")

        provider_params = await resolve_provider_params(model)

        call_kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=kwargs.get("max_tokens", 256),
            temperature=kwargs.get("temperature", 0.2),
            timeout=kwargs.get("timeout", 60),
            **provider_params,
        )
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
            call_kwargs["drop_params"] = True

        response = await litellm.acompletion(**call_kwargs)
        if not response.choices:
            raise RuntimeError("Empty choices from provider")
        content = (response.choices[0].message.content or "").strip() if response.choices[0].message else ""
        return content


async def setup_llm_client(app: FastAPI, source: str):
    """Create and store the LLM client wrapper on ``app.state.llm_client``.

    Returns the wrapper instance for use by later steps.
    """
    if getattr(app.state, "llm_client", None) is None:
        app.state.llm_client = _LLMClientWrapper()
    return app.state.llm_client
