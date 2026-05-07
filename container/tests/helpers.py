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
    original_response_text: str | None = None,
    rewrite_applied: bool = False,
    rewrite_latency_ms: float | None = None,
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
        original_response_text=original_response_text,
        rewrite_applied=rewrite_applied,
        rewrite_latency_ms=rewrite_latency_ms,
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


# ---------------------------------------------------------------------------
# Bridge Action Audit helpers
# ---------------------------------------------------------------------------


class BridgeActionAudit:
    """Black-box assertion helpers for HA-bridge action-audit tests."""

    @staticmethod
    def assert_routing(response: dict, expected_agent: str, scenario_id: str = "") -> None:
        """Assert that ``response["routed_agent"]`` matches ``expected_agent``."""
        actual = response.get("routed_agent")
        prefix = f"[{scenario_id}] " if scenario_id else ""
        if actual != expected_agent:
            raise AssertionError(f"{prefix}routing mismatch: expected {expected_agent!r} got {actual!r}")

    @staticmethod
    def assert_action_executed(
        response: dict,
        expected_service: str,
        expected_entity: str | None = None,
        expected_data_keys: list[str] | None = None,
        scenario_id: str = "",
    ) -> None:
        """Assert that ``response["action_executed"]`` matches expectations."""
        action = response.get("action_executed") or {}
        prefix = f"[{scenario_id}] " if scenario_id else ""
        if not action:
            raise AssertionError(f"{prefix}expected action_executed, got None")
        actual_service = action.get("service")
        if actual_service != expected_service:
            raise AssertionError(
                f"{prefix}action_executed.service mismatch: expected {expected_service!r} got {actual_service!r}"
            )
        if expected_entity is not None:
            actual_entity = action.get("entity_id")
            if actual_entity != expected_entity:
                raise AssertionError(
                    f"{prefix}action_executed.entity_id mismatch: expected {expected_entity!r} got {actual_entity!r}"
                )
        if expected_data_keys:
            service_data = action.get("service_data") or {}
            for key in expected_data_keys:
                if key not in service_data:
                    raise AssertionError(
                        f"{prefix}action_executed.service_data missing key {key!r}; got {service_data!r}"
                    )

    @staticmethod
    def assert_full_contract(response: dict, expected: dict, scenario_id: str = "") -> None:
        """Dataclass-driven assertion over the full response/action contract.

        ``expected`` keys:
        - ``routed_agent``: str
        - ``action_executed.service``: str
        - ``action_executed.entity_id``: str
        - ``action_executed.service_data_keys``: list[str]
        - ``speech_contains``: list[str]
        """
        prefix = f"[{scenario_id}] " if scenario_id else ""
        if "routed_agent" in expected:
            BridgeActionAudit.assert_routing(response, expected["routed_agent"], scenario_id)
        if "action_executed" in expected:
            ae_expected = expected["action_executed"]
            BridgeActionAudit.assert_action_executed(
                response,
                expected_service=ae_expected.get("service", ""),
                expected_entity=ae_expected.get("entity_id"),
                expected_data_keys=ae_expected.get("service_data_keys"),
                scenario_id=scenario_id,
            )
        speech = response.get("speech") or response.get("mediated_speech") or response.get("token", "")
        for needle in expected.get("speech_contains", []):
            if needle.lower() not in speech.lower():
                raise AssertionError(f"{prefix}expected speech to contain {needle!r}; got {speech!r}")


# ---------------------------------------------------------------------------
# HA Mimic Client -- mimics the HA integration's bridge behavior
# ---------------------------------------------------------------------------


class HAMimicClient:
    """Async test helper that mimics the HA integration conversation client.

    Uses ``fastapi.testclient.TestClient`` (sync) for both HTTP and WebSocket
    because ``httpx`` does not support WebSocket. Sync TestClient calls are
    wrapped with :func:`asyncio.to_thread` so the helper can be used inside
    ``pytest-asyncio`` tests without blocking the event loop.
    """

    def __init__(self, app, api_key: str = "test-api-key") -> None:
        self.app = app
        self.api_key = api_key
        self._client = None
        self._ws = None

    async def __aenter__(self):
        import asyncio

        from fastapi.testclient import TestClient

        def _enter():
            c = TestClient(self.app)
            c.__enter__()
            return c

        self._client = await asyncio.to_thread(_enter)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def connect_ws(self):
        """Open a WebSocket to ``/ws/conversation`` with Bearer auth."""
        import asyncio

        if self._client is None:
            raise RuntimeError("HAMimicClient must be entered via async with before connect_ws")

        def _connect():
            ws = self._client.websocket_connect(
                "/ws/conversation",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            ws.__enter__()
            return ws

        self._ws = await asyncio.to_thread(_connect)

    async def send_turn(
        self,
        text: str,
        conversation_id: str | None = None,
        language: str = "en",
        device_id: str | None = None,
    ) -> list[dict]:
        """Send a conversation turn over WS and accumulate StreamToken dicts."""
        import asyncio

        if self._ws is None:
            raise RuntimeError("WebSocket not connected; call connect_ws() first")
        payload: dict[str, object] = {"text": text, "language": language}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if device_id is not None:
            payload["device_id"] = device_id
        await asyncio.to_thread(self._ws.send_json, payload)
        tokens: list[dict] = []
        while True:
            msg = await asyncio.to_thread(self._ws.receive_json)
            tokens.append(msg)
            if msg.get("done"):
                break
        return tokens

    async def rest_turn(
        self,
        text: str,
        conversation_id: str | None = None,
        language: str = "en",
        device_id: str | None = None,
    ) -> dict:
        """POST to ``/api/conversation`` and return the JSON response."""
        import asyncio

        if self._client is None:
            raise RuntimeError("HAMimicClient must be entered via async with before rest_turn")
        payload: dict[str, object] = {"text": text, "language": language}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if device_id is not None:
            payload["device_id"] = device_id
        resp = await asyncio.to_thread(
            self._client.post,
            "/api/conversation",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def sse_turn(
        self,
        text: str,
        conversation_id: str | None = None,
        language: str = "en",
        device_id: str | None = None,
    ) -> list[dict]:
        """POST to ``/api/conversation/stream``, parse SSE, return StreamToken dicts."""
        import asyncio
        import json

        if self._client is None:
            raise RuntimeError("HAMimicClient must be entered via async with before sse_turn")
        payload: dict[str, object] = {"text": text, "language": language}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if device_id is not None:
            payload["device_id"] = device_id
        resp = await asyncio.to_thread(
            self._client.post,
            "/api/conversation/stream",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        tokens: list[dict] = []
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
                tokens.append(data)
                if data.get("done"):
                    break
        return tokens

    async def close(self):
        """Close WS and the underlying TestClient."""
        import asyncio

        if self._ws is not None:

            def _exit_ws():
                self._ws.__exit__(None, None, None)

            await asyncio.to_thread(_exit_ws)
            self._ws = None
        if self._client is not None:

            def _exit():
                self._client.__exit__(None, None, None)

            await asyncio.to_thread(_exit)
            self._client = None


# ---------------------------------------------------------------------------
# aiosqlite shutdown helper
# ---------------------------------------------------------------------------


async def shutdown_aiosqlite(conn) -> None:
    """Close an aiosqlite connection and block until its worker thread exits.

    aiosqlite's background thread may still be draining its queue after
    ``await conn.close()`` returns.  Joining prevents pytest-asyncio from
    closing the event loop while the thread is alive.
    """
    await conn.close()
    try:
        thread = conn._thread
        if thread.is_alive():
            thread.join(timeout=1.0)
    except Exception:
        pass
