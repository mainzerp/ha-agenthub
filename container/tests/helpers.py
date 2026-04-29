"""Test factory functions and utilities for agent-assist tests."""

from __future__ import annotations

import random
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

from app.models.agent import AgentCard, AgentConfig, AgentTask, TaskContext
from app.models.cache import ActionCacheEntry, CachedAction, RoutingCacheEntry
from app.models.conversation import ActionResult, ConversationRequest, ConversationResponse, StreamToken
from app.models.entity_index import EntityIndexEntry

# ---------------------------------------------------------------------------
# Conversation factories
# ---------------------------------------------------------------------------


def make_conversation_request(
    text: str = "turn on the kitchen light",
    conversation_id: str | None = None,
    language: str = "en",
) -> ConversationRequest:
    """Build a ConversationRequest with sensible defaults."""
    return ConversationRequest(
        text=text,
        conversation_id=conversation_id,
        language=language,
    )


def make_conversation_response(
    speech: str = "Done, kitchen light is on.",
    conversation_id: str | None = None,
    action_executed: ActionResult | None = None,
) -> ConversationResponse:
    """Build a ConversationResponse with sensible defaults."""
    return ConversationResponse(
        speech=speech,
        conversation_id=conversation_id,
        action_executed=action_executed,
    )


def make_action_result(
    service: str = "light/turn_on",
    entity_id: str = "light.kitchen_ceiling",
    result: str = "success",
    service_data: dict | None = None,
) -> ActionResult:
    """Build an ActionResult with sensible defaults."""
    return ActionResult(
        service=service,
        entity_id=entity_id,
        result=result,
        service_data=service_data,
    )


def make_stream_token(
    token: str = "Hello",
    done: bool = False,
    conversation_id: str | None = None,
) -> StreamToken:
    """Build a StreamToken."""
    return StreamToken(token=token, done=done, conversation_id=conversation_id)


# ---------------------------------------------------------------------------
# Agent factories
# ---------------------------------------------------------------------------


def make_agent_card(
    agent_id: str = "light-agent",
    name: str = "Light Agent",
    description: str = "Controls lighting devices",
    skills: list[str] | None = None,
    endpoint: str = "local://light-agent",
) -> AgentCard:
    """Build an AgentCard."""
    return AgentCard(
        agent_id=agent_id,
        name=name,
        description=description,
        skills=skills or ["light_control"],
        endpoint=endpoint,
    )


def make_agent_config(
    agent_id: str = "light-agent",
    enabled: bool = True,
    model: str | None = "openrouter/openai/gpt-4o-mini",
    timeout: int = 5,
    max_iterations: int = 3,
    temperature: float = 0.7,
    max_tokens: int = 256,
    description: str | None = "Lighting control",
) -> AgentConfig:
    """Build an AgentConfig."""
    return AgentConfig(
        agent_id=agent_id,
        enabled=enabled,
        model=model,
        timeout=timeout,
        max_iterations=max_iterations,
        temperature=temperature,
        max_tokens=max_tokens,
        description=description,
    )


def make_agent_task(
    description: str = "Turn on the kitchen light",
    user_text: str = "turn on the kitchen light",
    conversation_id: str | None = None,
    context: TaskContext | None = None,
    verbatim_terms: list[str] | None = None,
) -> AgentTask:
    """Build an AgentTask."""
    return AgentTask(
        description=description,
        user_text=user_text,
        conversation_id=conversation_id,
        context=context,
        verbatim_terms=verbatim_terms or [],
    )


# ---------------------------------------------------------------------------
# Entity factories
# ---------------------------------------------------------------------------


def make_entity_state(
    entity_id: str = "light.kitchen_ceiling",
    friendly_name: str = "Kitchen Ceiling",
    domain: str | None = None,
    state: str = "off",
    area: str | None = "kitchen",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an HA entity state dict as returned by GET /api/states."""
    if domain is None:
        domain = entity_id.split(".")[0] if "." in entity_id else ""
    attrs: dict[str, Any] = {
        "friendly_name": friendly_name,
    }
    if area is not None:
        attrs["area_id"] = area
    if attributes:
        attrs.update(attributes)
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attrs,
    }


def make_entity_index_entry(
    entity_id: str = "light.kitchen_ceiling",
    friendly_name: str = "Kitchen Ceiling",
    domain: str | None = None,
    area: str | None = "kitchen",
    device_class: str | None = None,
    aliases: list[str] | None = None,
    area_name: str | None = None,
    device_name: str | None = None,
    id_tokens: list[str] | None = None,
) -> EntityIndexEntry:
    """Build an EntityIndexEntry."""
    if domain is None:
        domain = entity_id.split(".")[0] if "." in entity_id else ""
    return EntityIndexEntry(
        entity_id=entity_id,
        friendly_name=friendly_name,
        domain=domain,
        area=area,
        area_name=area_name,
        device_class=device_class,
        aliases=aliases or [],
        device_name=device_name,
        id_tokens=id_tokens or [],
    )


# ---------------------------------------------------------------------------
# Cache factories
# ---------------------------------------------------------------------------


def make_routing_cache_entry(
    query_text: str = "turn on kitchen lights",
    agent_id: str = "light-agent",
    confidence: float = 0.95,
    hit_count: int = 1,
    language: str = "en",
    condensed_task: str | None = None,
    entity_ids: list[str] | None = None,
) -> RoutingCacheEntry:
    """Build a RoutingCacheEntry."""
    return RoutingCacheEntry(
        query_text=query_text,
        language=language,
        agent_id=agent_id,
        condensed_task=condensed_task,
        confidence=confidence,
        entity_ids=entity_ids or [],
        hit_count=hit_count,
    )


def make_action_cache_entry(
    query_text: str = "turn on kitchen lights",
    response_text: str = "Done, kitchen light is on.",
    agent_id: str = "light-agent",
    confidence: float = 0.97,
    cached_action: CachedAction | None = None,
    entity_ids: list[str] | None = None,
    language: str = "en",
    condensed_task: str | None = None,
) -> ActionCacheEntry:
    """Build an ActionCacheEntry."""
    action = cached_action or make_cached_action()
    return ActionCacheEntry(
        query_text=query_text,
        language=language,
        response_text=response_text,
        agent_id=agent_id,
        condensed_task=condensed_task,
        confidence=confidence,
        cached_action=action,
        entity_ids=entity_ids or [action.entity_id],
    )


def make_response_cache_entry(*args, **kwargs) -> ActionCacheEntry:
    """Legacy test helper alias; returns an ActionCacheEntry."""
    return make_action_cache_entry(*args, **kwargs)


def make_cached_action(
    service: str = "light/turn_on",
    entity_id: str = "light.kitchen_ceiling",
    service_data: dict | None = None,
) -> CachedAction:
    """Build a CachedAction."""
    return CachedAction(
        service=service,
        entity_id=entity_id,
        service_data=service_data or {},
    )


# ---------------------------------------------------------------------------
# A2A protocol factories
# ---------------------------------------------------------------------------


def make_a2a_request(
    method: str = "message/send",
    params: dict[str, Any] | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    """Build an A2A JSON-RPC request dict."""
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": id or str(uuid.uuid4()),
    }


def make_a2a_response(
    result: Any = None,
    id: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an A2A JSON-RPC response dict."""
    resp: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": id or str(uuid.uuid4()),
    }
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result or {"status": "ok"}
    return resp


# ---------------------------------------------------------------------------
# LLM mock factories
# ---------------------------------------------------------------------------


def make_mock_llm_response(
    content: str = "I turned on the kitchen light for you.",
    role: str = "assistant",
) -> MagicMock:
    """Build a mock litellm ChatCompletion response object.

    Mimics the structure returned by litellm.acompletion().
    """
    choice = MagicMock()
    choice.message.content = content
    choice.message.role = role
    choice.finish_reason = "stop"

    response = MagicMock()
    response.choices = [choice]
    response.model = "openrouter/openai/gpt-4o-mini"
    response.usage.prompt_tokens = 50
    response.usage.completion_tokens = 20
    response.usage.total_tokens = 70
    return response


def make_mock_embedding(dim: int = 384) -> list[float]:
    """Return a random embedding vector of the given dimension."""
    return [random.uniform(-1.0, 1.0) for _ in range(dim)]


# ---------------------------------------------------------------------------
# HA client helpers
# ---------------------------------------------------------------------------


def attach_expect_state_shim(client: Any) -> Any:
    """Install an ``expect_state`` async context manager on a mocked client.

    FLOW-VERIFY-SHARED (0.18.5): all domain executors now go through
    :func:`app.agents.action_executor.call_service_with_verification`,
    which opens ``ha_client.expect_state`` as an async context manager.
    Plain ``AsyncMock()`` instances don't satisfy the context-manager
    protocol, so tests that mock the HA client need this shim.

    The shim mimics the "no WS observer" fallback: it yields a mutable
    dict to the ``with`` body and, on exit, fills ``new_state`` from a
    single call to ``client.get_state`` (or leaves it ``None`` if
    ``get_state`` raises). That keeps tests deterministic without
    pulling the real REST client into the unit test.

    Returns the same ``client`` for call-chaining.
    """

    @asynccontextmanager
    async def _expect_state(
        entity_id,
        *,
        expected=None,
        timeout=0.05,
        poll_interval=0.01,
        poll_max=0.05,
    ):
        result = {"new_state": None}
        yield result
        try:
            state_resp = await client.get_state(entity_id)
        except Exception:
            return
        if isinstance(state_resp, dict):
            result["new_state"] = state_resp.get("state")

    client.expect_state = _expect_state
    client.set_state_observer = MagicMock()
    return client


# ---------------------------------------------------------------------------
# CSRF helper
# ---------------------------------------------------------------------------


async def csrf_post(
    client,
    url: str,
    data: dict | None = None,
    *,
    get_url: str | None = None,
):
    """POST a form with a valid CSRF cookie+token pair.

    Performs a GET against ``get_url`` (or ``url`` if not provided) so the
    server sets the ``agent_assist_csrf`` cookie; reads the cookie back from
    the test client and adds the matching ``csrf_token`` form field on the
    POST.
    """
    fetch_url = get_url or url
    await client.get(fetch_url)
    token = client.cookies.get("agent_assist_csrf")
    payload = dict(data or {})
    if token is not None:
        payload["csrf_token"] = token
    return await client.post(url, data=payload)
