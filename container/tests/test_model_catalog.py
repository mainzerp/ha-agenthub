"""Tests for the per-provider model catalog (app.llm.model_catalog). No network: HTTP is mocked with respx."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.llm import model_catalog


@pytest.fixture(autouse=True)
async def _clear_catalog_cache():
    await model_catalog.invalidate_model_catalog()
    yield
    await model_catalog.invalidate_model_catalog()


@contextmanager
def _stored(key: str | None = "stored-key", base_url: str | None = None, headers: dict | None = None):
    """Patch the credential lookups the catalog uses."""
    with (
        patch.object(model_catalog, "get_api_key", new=AsyncMock(return_value=key)),
        patch.object(model_catalog, "get_base_url", new=AsyncMock(return_value=base_url)),
        patch.object(model_catalog, "get_custom_openai_headers", new=AsyncMock(return_value=headers or {})),
    ):
        yield


# ---------------------------------------------------------------------------
# Per-provider parsing
# ---------------------------------------------------------------------------


@respx.mock
async def test_anthropic_paginates_and_keeps_api_order():
    route = respx.get("https://api.anthropic.com/v1/models")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-new", "display_name": "Claude New", "type": "model", "created_at": "x"},
                    {"id": "claude-mid", "display_name": "Claude Mid", "type": "model", "created_at": "x"},
                ],
                "has_more": True,
                "last_id": "claude-mid",
            },
        ),
        httpx.Response(
            200,
            json={
                "data": [{"id": "claude-old", "display_name": "", "type": "model", "created_at": "x"}],
                "has_more": False,
                "last_id": "claude-old",
            },
        ),
    ]
    with _stored(key="sk-ant-stored"):
        result = await model_catalog.list_provider_models("anthropic")

    assert result["status"] == "ok"
    assert result["cached"] is False
    assert result["fetched_at"]
    assert result["models"] == [
        {"id": "claude-new", "name": "Claude New", "model": "anthropic/claude-new"},
        {"id": "claude-mid", "name": "Claude Mid", "model": "anthropic/claude-mid"},
        {"id": "claude-old", "name": "claude-old", "model": "anthropic/claude-old"},
    ]
    first, second = route.calls
    assert first.request.headers["x-api-key"] == "sk-ant-stored"
    assert first.request.headers["anthropic-version"] == "2023-06-01"
    assert first.request.url.params["limit"] == "1000"
    assert "after_id" not in first.request.url.params
    assert second.request.url.params["after_id"] == "claude-mid"


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("groq", "https://api.groq.com/openai/v1/models"),
        ("cerebras", "https://api.cerebras.ai/v1/models"),
    ],
)
@respx.mock
async def test_bearer_providers_sorted_by_id(provider, url):
    route = respx.get(url).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "zeta"}, {"id": "alpha"}, {"bogus": 1}]})
    )
    with _stored():
        result = await model_catalog.list_provider_models(provider, api_key="explicit-key")

    assert result["status"] == "ok"
    assert [m["id"] for m in result["models"]] == ["alpha", "zeta"]
    assert result["models"][0] == {"id": "alpha", "name": "alpha", "model": f"{provider}/alpha"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer explicit-key"


@respx.mock
async def test_openrouter_validates_key_then_lists():
    key_route = respx.get("https://openrouter.ai/api/v1/key").mock(return_value=httpx.Response(200, json={"data": {}}))
    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "openai/gpt-4o-mini", "name": "OpenAI: GPT-4o-mini"},
                    {"id": "anthropic/claude-x", "name": "Anthropic: Claude X"},
                ]
            },
        )
    )
    with _stored():
        result = await model_catalog.list_provider_models("openrouter")

    assert result["status"] == "ok"
    assert result["models"] == [
        {"id": "anthropic/claude-x", "name": "Anthropic: Claude X", "model": "openrouter/anthropic/claude-x"},
        {"id": "openai/gpt-4o-mini", "name": "OpenAI: GPT-4o-mini", "model": "openrouter/openai/gpt-4o-mini"},
    ]
    assert key_route.calls.last.request.headers["Authorization"] == "Bearer stored-key"


@respx.mock
async def test_openrouter_key_endpoint_404_falls_back_to_auth_key():
    respx.get("https://openrouter.ai/api/v1/key").mock(return_value=httpx.Response(404))
    fallback = respx.get("https://openrouter.ai/api/v1/auth/key").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://openrouter.ai/api/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
    with _stored():
        result = await model_catalog.list_provider_models("openrouter")

    assert result["status"] == "ok"
    assert fallback.called


@respx.mock
async def test_openrouter_invalid_key_is_auth_error_without_listing():
    respx.get("https://openrouter.ai/api/v1/key").mock(return_value=httpx.Response(401))
    models = respx.get("https://openrouter.ai/api/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
    with _stored():
        result = await model_catalog.list_provider_models("openrouter", api_key="sk-or-bad")

    assert result == {
        "provider": "openrouter",
        "status": "error",
        "error_kind": "auth",
        "detail": result["detail"],
        "models": [],
    }
    assert "401" in result["detail"]
    assert "sk-or-bad" not in result["detail"]
    assert not models.called


@respx.mock
async def test_ollama_tags():
    respx.get("http://ollama.local:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3:latest"}]})
    )
    with _stored(key=None, base_url="http://ollama.local:11434/"):
        result = await model_catalog.list_provider_models("ollama")

    assert result["status"] == "ok"
    assert result["models"] == [
        {"id": "llama3:latest", "name": "llama3:latest", "model": "ollama/llama3:latest"},
        {"id": "qwen3:8b", "name": "qwen3:8b", "model": "ollama/qwen3:8b"},
    ]


@respx.mock
async def test_custom_openai_lists_with_headers_and_trailing_slash():
    route = respx.get("https://llm.example.invalid/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "my-model"}]})
    )
    with _stored(key="custom-key", base_url="https://llm.example.invalid/v1/", headers={"X-Tenant": "t1"}):
        result = await model_catalog.list_provider_models("custom_openai")

    assert result["status"] == "ok"
    assert result["models"] == [{"id": "my-model", "name": "my-model", "model": "custom_openai/my-model"}]
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer custom-key"
    assert request.headers["X-Tenant"] == "t1"


@respx.mock
async def test_custom_openai_base_url_override_skips_stored_credentials_and_cache():
    route = respx.get("https://other.example.invalid/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "wizard-model"}]})
    )
    with _stored(key="stored-key", base_url="https://llm.example.invalid/v1", headers={"X-Tenant": "t1"}):
        result = await model_catalog.list_provider_models("custom_openai", base_url="https://other.example.invalid/v1")
        assert result["status"] == "ok"
        request = route.calls.last.request
        assert "Authorization" not in request.headers
        assert "X-Tenant" not in request.headers
        hit, _ = await model_catalog._catalog_cache.get("custom_openai")
        assert hit is False


@respx.mock
async def test_ollama_base_url_override():
    route = respx.get("http://wizard-ollama.invalid:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
    )
    with _stored(key=None, base_url="http://stored.invalid:11434"):
        result = await model_catalog.list_provider_models("ollama", base_url="http://wizard-ollama.invalid:11434")
    assert result["status"] == "ok"
    assert result["models"][0]["model"] == "ollama/llama3:latest"
    assert route.called


@pytest.mark.parametrize("status", [404, 405])
@respx.mock
async def test_custom_openai_without_models_endpoint_is_unsupported(status):
    respx.get("https://llm.example.invalid/v1/models").mock(return_value=httpx.Response(status))
    with _stored(key=None, base_url="https://llm.example.invalid/v1"):
        result = await model_catalog.list_provider_models("custom_openai")

    assert result["status"] == "error"
    assert result["error_kind"] == "unsupported"
    assert str(status) in result["detail"]


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


async def test_missing_key_is_not_configured_without_network():
    with _stored(key=None), respx.mock(assert_all_called=False) as router:
        result = await model_catalog.list_provider_models("groq")
    assert result["error_kind"] == "not_configured"
    assert "No API key" in result["detail"]
    assert not router.calls


async def test_custom_openai_missing_base_url_is_not_configured():
    with _stored(base_url=None):
        result = await model_catalog.list_provider_models("custom_openai")
    assert result["error_kind"] == "not_configured"


@respx.mock
async def test_http_401_is_auth():
    respx.get("https://api.groq.com/openai/v1/models").mock(return_value=httpx.Response(401))
    with _stored():
        result = await model_catalog.list_provider_models("groq")
    assert result["error_kind"] == "auth"
    assert "stored-key" not in result["detail"]


@respx.mock
async def test_http_500_is_provider_error():
    respx.get("https://api.cerebras.ai/v1/models").mock(return_value=httpx.Response(503))
    with _stored():
        result = await model_catalog.list_provider_models("cerebras")
    assert result["error_kind"] == "provider_error"
    assert "503" in result["detail"]


@respx.mock
async def test_malformed_json_is_provider_error():
    respx.get("https://api.cerebras.ai/v1/models").mock(return_value=httpx.Response(200, text="<html>"))
    with _stored():
        result = await model_catalog.list_provider_models("cerebras")
    assert result["error_kind"] == "provider_error"


@respx.mock
async def test_connect_error_is_unreachable():
    respx.get("http://ollama.local:11434/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    with _stored(key=None, base_url="http://ollama.local:11434"):
        result = await model_catalog.list_provider_models("ollama")
    assert result["status"] == "error"
    assert result["error_kind"] == "unreachable"
    assert result["models"] == []


@respx.mock
async def test_timeout_is_unreachable():
    respx.get("https://api.groq.com/openai/v1/models").mock(side_effect=httpx.ReadTimeout("slow"))
    with _stored():
        result = await model_catalog.list_provider_models("groq")
    assert result["error_kind"] == "unreachable"


async def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        await model_catalog.list_provider_models("nope")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@respx.mock
async def test_cache_hit_and_refresh():
    route = respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m1"}]})
    )
    with _stored():
        first = await model_catalog.list_provider_models("groq")
        second = await model_catalog.list_provider_models("groq")
        assert route.call_count == 1
        assert first["cached"] is False
        assert second["cached"] is True
        assert second["fetched_at"] == first["fetched_at"]
        assert second["models"] == first["models"]

        refreshed = await model_catalog.list_provider_models("groq", use_cache=False)
        assert route.call_count == 2
        assert refreshed["cached"] is False


@respx.mock
async def test_explicit_key_is_never_cached():
    route = respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m1"}]})
    )
    with _stored():
        await model_catalog.list_provider_models("groq", api_key="explicit")
        result = await model_catalog.list_provider_models("groq")
    assert route.call_count == 2
    assert result["cached"] is False


@respx.mock
async def test_errors_are_not_cached():
    route = respx.get("https://api.groq.com/openai/v1/models")
    route.side_effect = [httpx.Response(500), httpx.Response(200, json={"data": [{"id": "m1"}]})]
    with _stored():
        failed = await model_catalog.list_provider_models("groq")
        ok = await model_catalog.list_provider_models("groq")
    assert failed["status"] == "error"
    assert ok["status"] == "ok"
    assert ok["cached"] is False


@respx.mock
async def test_store_secret_invalidates_provider_cache(db_repository):
    from app.security.encryption import delete_secret, store_secret

    groq = respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m1"}]})
    )
    cerebras = respx.get("https://api.cerebras.ai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "c1"}]})
    )
    await store_secret("groq_api_key", "gsk-one")
    await store_secret("cerebras_api_key", "csk-one")
    await model_catalog.list_provider_models("groq")
    await model_catalog.list_provider_models("cerebras")

    await store_secret("groq_api_key", "gsk-two")
    after_store = await model_catalog.list_provider_models("groq")
    assert after_store["cached"] is False
    assert groq.calls.last.request.headers["Authorization"] == "Bearer gsk-two"
    # Other providers keep their cache entry.
    assert (await model_catalog.list_provider_models("cerebras"))["cached"] is True
    assert cerebras.call_count == 1

    await delete_secret("groq_api_key")
    after_delete = await model_catalog.list_provider_models("groq")
    assert after_delete["error_kind"] == "not_configured"


async def test_settings_writes_invalidate_ollama_and_custom():
    with patch.object(model_catalog, "invalidate_model_catalog", new=AsyncMock()) as invalidate:
        await model_catalog._on_settings_write("ollama_base_url")
        await model_catalog._on_settings_write("custom_openai_provider.headers")
        await model_catalog._on_settings_write("unrelated.key")
    assert [c.args for c in invalidate.await_args_list] == [("ollama",), ("custom_openai",)]
