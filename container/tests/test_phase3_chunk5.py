"""Phase 3 chunk 5 regression tests (P3-1 / P3-3 / P3-4 / P3-7 / P3-8).

Each block is intentionally small and isolated so a future drift in
unrelated areas of the orchestrator / cache stack does not mask a
real regression of the targeted behaviour.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.action_cache import ActionCache
from app.cache.vector_store import VectorStore
from app.models.cache import ActionCacheEntry, CachedAction
from app.models.conversation import ConversationResponse, StreamToken

# ---------------------------------------------------------------------------
# P3-1: backend signals sanitized=True so HA can skip its strip pass.
# ---------------------------------------------------------------------------


class TestSanitizedFlagDefault:
    def test_conversation_response_defaults_to_sanitized(self):
        resp = ConversationResponse(speech="**hi**", conversation_id="c1")
        # Backend is the source of truth: default True.
        assert resp.sanitized is True

    def test_stream_token_defaults_to_sanitized(self):
        tok = StreamToken(token="hello", done=True)
        assert tok.sanitized is True

    def test_sanitized_can_be_disabled_for_legacy_clients(self):
        resp = ConversationResponse(speech="**hi**", sanitized=False)
        assert resp.sanitized is False


# ---------------------------------------------------------------------------
# P3-3: VectorStore._reinitialize_sync must be atomic across threads.
# ---------------------------------------------------------------------------


class TestVectorStoreReinitLock:
    def test_concurrent_reinit_only_opens_one_connection(self):
        store = VectorStore()
        store._conn = None
        opens: list[int] = []
        sentinel = object()

        def _fake_open(self):
            opens.append(1)
            self._conn = sentinel

        with (
            patch.object(VectorStore, "_open_connection", _fake_open),
            patch.object(VectorStore, "_ensure_collection"),
        ):
            barrier = threading.Barrier(5)

            def _reinit() -> None:
                barrier.wait()
                store._reinitialize_sync()

            threads = [threading.Thread(target=_reinit) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Exactly one connection was opened across 5 threads; the lock +
        # double-check guard collapsed the rest.
        assert len(opens) == 1
        assert store._conn is sentinel

    def test_reinit_lock_skips_when_connection_already_open(self):
        """If the connection is already open, reinit becomes a no-op."""
        store = VectorStore()
        existing = MagicMock()
        store._conn = existing

        with patch.object(VectorStore, "_open_connection") as mock_open:
            store._reinitialize_sync()

        mock_open.assert_not_called()
        assert store._conn is existing


# ---------------------------------------------------------------------------
# P3-4: ActionCache.prepare_for_flush mirrors RoutingCache behaviour.
# ---------------------------------------------------------------------------


class TestActionCachePrepareForFlush:
    def _make_cache(self) -> tuple[ActionCache, MagicMock]:
        store = MagicMock(spec=VectorStore)
        cache = ActionCache(store)
        cache._max_entries = 100
        return cache, store

    def test_prepare_for_flush_clears_pending_and_bumps_generation(self):
        cache, _store = self._make_cache()
        cache._state.record_pending_update("id-1", "q", {"hit_count": "3"}, flush_interval=1_000_000)
        gen0 = cache._state.current_generation()

        cache.prepare_for_flush()

        assert cache._state.current_generation() == gen0 + 1
        assert not cache._state.has_pending()
        assert cache._state.hit_count() == 0

    def test_store_skips_upsert_when_invalidated_mid_flight(self):
        """Admin flush during an in-flight async store must not resurrect entries."""
        cache, store = self._make_cache()
        store.count.return_value = 0
        original_flush_pending = cache._flush_pending_updates

        def flush_pending_then_invalidate() -> None:
            original_flush_pending()
            cache.prepare_for_flush()

        cache._flush_pending_updates = flush_pending_then_invalidate
        entry = ActionCacheEntry(
            query_text="turn on light",
            response_text="done",
            agent_id="light-agent",
            cached_action=CachedAction(service="light/turn_on", entity_id="light.kitchen"),
            confidence=0.99,
            entity_ids=["light.kitchen"],
            language="en",
        )
        cache.store(entry)
        store.upsert.assert_not_called()

    def test_cache_manager_flush_calls_action_prepare(self):
        """flush('response') must call the response tier's prepare_for_flush."""
        from app.cache.cache_manager import CacheManager

        manager = CacheManager.__new__(CacheManager)
        manager._cache_store = MagicMock()
        manager._cache_store.count.return_value = 0
        manager._routing_cache = MagicMock()
        manager._action_cache = MagicMock()

        manager.flush(tier="action")
        manager._action_cache.prepare_for_flush.assert_called_once()

    def test_cache_manager_flush_all_calls_both_prepares(self):
        from app.cache.cache_manager import CacheManager

        manager = CacheManager.__new__(CacheManager)
        manager._cache_store = MagicMock()
        manager._cache_store.count.return_value = 0
        manager._routing_cache = MagicMock()
        manager._action_cache = MagicMock()

        manager.flush()  # tier=None -> both
        manager._routing_cache.prepare_for_flush.assert_called_once()
        manager._action_cache.prepare_for_flush.assert_called_once()


# ---------------------------------------------------------------------------
# P3-7: _get_known_agents memoises within TTL.
# ---------------------------------------------------------------------------


class TestKnownAgentsMemoization:
    def _make_registry(self) -> tuple[object, MagicMock]:
        from app.agents.agent_registry import CachedAgentRegistry

        registry = MagicMock()
        card_a = MagicMock(agent_id="light-agent")
        card_b = MagicMock(agent_id="music-agent")
        registry.list_agents = AsyncMock(return_value=[card_a, card_b])
        agent_reg = CachedAgentRegistry(registry)
        return agent_reg, registry

    @pytest.mark.asyncio
    async def test_repeat_calls_within_ttl_hit_cache(self):
        agent_reg, registry = self._make_registry()
        agent_reg._known_agents_ttl = 60.0

        first = await agent_reg.get_known_agents()
        second = await agent_reg.get_known_agents()

        assert first == second
        registry.list_agents.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_zero_ttl_disables_cache(self):
        agent_reg, registry = self._make_registry()
        agent_reg._known_agents_ttl = 0.0  # always expired

        await agent_reg.get_known_agents()
        await agent_reg.get_known_agents()

        assert registry.list_agents.await_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_caches_clears_memo(self):
        agent_reg, registry = self._make_registry()
        agent_reg._known_agents_ttl = 60.0
        await agent_reg.get_known_agents()
        assert registry.list_agents.await_count == 1

        agent_reg.invalidate_caches()

        await agent_reg.get_known_agents()
        assert registry.list_agents.await_count == 2

    @pytest.mark.asyncio
    async def test_load_reliability_config_clears_known_agents_memo(self):
        """The real _load_reliability_config must invalidate the CachedAgentRegistry memo."""
        from app.agents.orchestrator import OrchestratorAgent

        registry = MagicMock()
        card_a = MagicMock(agent_id="light-agent")
        card_b = MagicMock(agent_id="music-agent")
        registry.list_agents = AsyncMock(return_value=[card_a, card_b])
        orch = OrchestratorAgent(
            dispatcher=MagicMock(),
            registry=registry,
        )
        orch._agent_registry._known_agents_ttl = 60.0
        await orch._get_known_agents()
        assert orch._agent_registry._known_agents_cache is not None

        async def fake_get_value(key: str, default: object | None = None) -> object:
            return default

        with patch("app.agents.orchestrator.SettingsRepository.get_value", new=AsyncMock(side_effect=fake_get_value)):
            await orch._load_reliability_config()

        assert orch._agent_registry._known_agents_cache is None
        # Next call now refetches.
        await orch._get_known_agents()
        assert registry.list_agents.await_count == 2


# ---------------------------------------------------------------------------
# P3-8: TTS LLM call must not borrow the orchestrator agent_id.
# ---------------------------------------------------------------------------


class TestNotificationDispatcherAgentId:
    @pytest.mark.asyncio
    async def test_generate_tts_message_uses_dispatcher_id(self):
        from app.agents import notification_dispatcher as nd
        from app.security.sanitization import USER_INPUT_END, USER_INPUT_START

        with patch("app.llm.client.complete", new=AsyncMock(return_value="Timer ist fertig.")) as fake_complete:
            result = await nd._generate_tts_message(
                timer_name="Pasta",
                duration="10 Minuten",
                area="Kueche",
                language="de",
                has_meaningful_name=True,
            )

        assert result == "Timer ist fertig."
        # P3-8: token tracking must attribute usage to the notification
        # dispatcher, not the orchestrator agent.
        kwargs = fake_complete.await_args.kwargs
        assert kwargs["agent_id"] == "notification-dispatcher"
        user_prompt = kwargs["messages"][1]["content"]
        assert user_prompt.count(USER_INPUT_START) == 3
        assert user_prompt.count(USER_INPUT_END) == 3
        assert f"{USER_INPUT_START}\nPasta\n{USER_INPUT_END}" in user_prompt
        assert f"{USER_INPUT_START}\n10 Minuten\n{USER_INPUT_END}" in user_prompt
        assert f"{USER_INPUT_START}\nKueche\n{USER_INPUT_END}" in user_prompt
