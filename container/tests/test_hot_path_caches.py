"""Tests for P2 hot-path memoization: secrets, agent configs, provider params.

Verifies memoized reads, invalidate-on-write semantics, and that
decryption failures fail loudly and are never cached.
"""

from __future__ import annotations

import pytest

from app.db.repository import AgentConfigRepository, SecretsRepository, SettingsRepository
from app.llm.providers import invalidate_provider_params, resolve_provider_params
from app.security.encryption import (
    delete_secret,
    invalidate_secret_cache,
    retrieve_secret,
    store_secret,
)

pytestmark = pytest.mark.asyncio


class TestSecretMemoization:
    async def test_retrieve_secret_is_memoized(self, db_repository, monkeypatch):
        await store_secret("memo_key", "topsecret")
        calls = 0
        original_get = SecretsRepository.get

        async def _counting_get(key):
            nonlocal calls
            calls += 1
            return await original_get(key)

        monkeypatch.setattr(SecretsRepository, "get", staticmethod(_counting_get))
        assert await retrieve_secret("memo_key") == "topsecret"
        assert await retrieve_secret("memo_key") == "topsecret"
        assert calls == 1

    async def test_store_secret_invalidates(self, db_repository):
        await store_secret("inv_key", "old")
        assert await retrieve_secret("inv_key") == "old"
        await store_secret("inv_key", "new")
        assert await retrieve_secret("inv_key") == "new"

    async def test_delete_secret_invalidates(self, db_repository):
        await store_secret("del_key", "value")
        assert await retrieve_secret("del_key") == "value"
        await delete_secret("del_key")
        assert await retrieve_secret("del_key") is None

    async def test_decryption_failure_fails_loudly_and_is_not_cached(self, db_repository, monkeypatch):
        await SecretsRepository.set("bad_key", b"not-a-fernet-token")
        calls = 0
        original_get = SecretsRepository.get

        async def _counting_get(key):
            nonlocal calls
            calls += 1
            return await original_get(key)

        monkeypatch.setattr(SecretsRepository, "get", staticmethod(_counting_get))
        with pytest.raises(RuntimeError, match="Failed to decrypt secret"):
            await retrieve_secret("bad_key")
        with pytest.raises(RuntimeError, match="Failed to decrypt secret"):
            await retrieve_secret("bad_key")
        # Failures must not be memoized -- every read retries and raises.
        assert calls == 2

    async def test_secret_write_invalidates_provider_params(self, db_repository):
        await store_secret("openrouter_api_key", "sk-one")
        first = await resolve_provider_params("openrouter/openai/gpt-4o")
        assert first["api_key"] == "sk-one"
        await store_secret("openrouter_api_key", "sk-two")
        second = await resolve_provider_params("openrouter/openai/gpt-4o")
        assert second["api_key"] == "sk-two"


class TestAgentConfigMemoization:
    async def test_get_is_memoized(self, db_repository, monkeypatch):
        await AgentConfigRepository.upsert("memo-agent", model="openrouter/test", temperature=0.5)
        calls = 0
        from app.db.repositories import agent_config as agent_config_module

        original_cache_get = agent_config_module._config_cache.get

        async def _counting_cache_get(key):
            nonlocal calls
            calls += 1
            return await original_cache_get(key)

        monkeypatch.setattr(agent_config_module._config_cache, "get", _counting_cache_get)
        first = await AgentConfigRepository.get("memo-agent")
        second = await AgentConfigRepository.get("memo-agent")
        assert first == second
        assert first["model"] == "openrouter/test"
        assert calls == 2  # both reads consulted the memo cache

    async def test_upsert_invalidates(self, db_repository):
        await AgentConfigRepository.upsert("cfg-agent", temperature=0.4)
        assert (await AgentConfigRepository.get("cfg-agent"))["temperature"] == 0.4
        await AgentConfigRepository.upsert("cfg-agent", temperature=0.9)
        assert (await AgentConfigRepository.get("cfg-agent"))["temperature"] == 0.9

    async def test_delete_invalidates(self, db_repository):
        await AgentConfigRepository.upsert("gone-agent", temperature=0.4)
        assert await AgentConfigRepository.get("gone-agent") is not None
        await AgentConfigRepository.delete("gone-agent")
        assert await AgentConfigRepository.get("gone-agent") is None

    async def test_custom_agent_runtime_write_invalidates(self, db_repository):
        from app.db.repository import CustomAgentRepository

        await CustomAgentRepository.create_with_runtime("memo-custom", "You are helpful.")
        first = await AgentConfigRepository.get("custom-memo-custom")
        assert first is not None
        await CustomAgentRepository.delete_with_runtime("memo-custom")
        assert await AgentConfigRepository.get("custom-memo-custom") is None


class TestProviderParamsMemoization:
    async def test_resolve_provider_params_is_memoized(self, db_repository, monkeypatch):
        await store_secret("groq_api_key", "gsk-test")
        calls = 0
        from app.llm import providers as providers_module

        original_retrieve = providers_module.retrieve_secret

        async def _counting_retrieve(key):
            nonlocal calls
            calls += 1
            return await original_retrieve(key)

        await invalidate_secret_cache()
        monkeypatch.setattr(providers_module, "retrieve_secret", _counting_retrieve)
        first = await resolve_provider_params("groq/llama3-70b")
        second = await resolve_provider_params("groq/llama3-70b")
        assert first == second
        assert first["api_key"] == "gsk-test"
        # retrieve_secret itself is memoized too, but the provider-params
        # cache must short-circuit before reaching it on the second call.
        assert calls == 1

    async def test_settings_write_invalidates_provider_params(self, db_repository):
        await invalidate_provider_params()
        first = await resolve_provider_params("ollama/llama3")
        assert first["api_base"] == "http://localhost:11434"
        await SettingsRepository.set("ollama_base_url", "http://ollama.internal:11434")
        second = await resolve_provider_params("ollama/llama3")
        assert second["api_base"] == "http://ollama.internal:11434"

    async def test_cached_params_are_defensive_copies(self, db_repository):
        await invalidate_provider_params()
        first = await resolve_provider_params("ollama/llama3")
        first["api_base"] = "http://mutated:1"
        second = await resolve_provider_params("ollama/llama3")
        assert second["api_base"] == "http://localhost:11434"
