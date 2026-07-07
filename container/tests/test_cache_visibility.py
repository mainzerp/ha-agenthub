"""Tests for cached-action visibility re-check, empty-result handling, and
sequential-send canned-string filtering (FLOW-CRIT-1, FLOW-CRIT-2, FLOW-CRIT-3)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# litellm is not installed in the test environment; provide a stub so
# importing the orchestrator module does not fail.
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


class _RateLimitError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
_litellm_mock.RateLimitError = _RateLimitError
sys.modules.setdefault("litellm", _litellm_mock)

from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.cache.cache_manager import ActionReplayOutcome  # noqa: E402
from tests.helpers import make_cached_action, make_entity_index_entry  # noqa: E402

pytestmark = pytest.mark.asyncio


def _make_orchestrator(entity_index=None):
    dispatcher = AsyncMock()
    cache_manager = MagicMock()
    cache_manager.apply_rewrite = AsyncMock()
    ha_client = AsyncMock()
    # ``call_service_with_verification`` calls ``expect_state(...)`` when present;
    # a bare AsyncMock returns an un-awaited coroutine. Use REST-only path in tests.
    ha_client.expect_state = None
    orch = OrchestratorAgent(
        dispatcher=dispatcher,
        cache_manager=cache_manager,
        ha_client=ha_client,
        entity_index=entity_index,
    )
    return orch, dispatcher, cache_manager, ha_client


# ---------------------------------------------------------------------------
# FLOW-CRIT-1: visibility re-check on cached-action replay
# ---------------------------------------------------------------------------


class TestCachedActionVisibility:
    async def test_cached_action_blocked_when_entity_revoked(self):
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator()

        rules = [{"rule_type": "domain_include", "rule_value": "switch"}]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is False

    async def test_cached_action_executes_when_entity_still_visible(self):
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator()

        rules = [{"rule_type": "domain_include", "rule_value": "light"}]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is True

    async def test_cached_action_no_rules_means_full_access(self):
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator()

        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[]),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is True

    async def test_visibility_lookup_failure_fails_closed(self):
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator()

        with patch(
            "app.agents.cache_orchestrator.entity_is_visible",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is False

    async def test_area_include_denies_cached_action(self):
        entity_index = MagicMock()
        entity_index.get_by_id.return_value = make_entity_index_entry(
            "light.kitchen",
            "Kitchen Light",
            area="kitchen",
        )
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator(entity_index=entity_index)

        rules = [{"rule_type": "area_include", "rule_value": "bedroom"}]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is False

    async def test_area_exclude_denies_cached_action(self):
        entity_index = MagicMock()
        entity_index.get_by_id.return_value = make_entity_index_entry(
            "light.kitchen",
            "Kitchen Light",
            area="kitchen",
        )
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator(entity_index=entity_index)

        rules = [{"rule_type": "area_exclude", "rule_value": "kitchen"}]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is False

    async def test_device_class_include_denies_cached_action(self):
        entity_index = MagicMock()
        entity_index.get_by_id.return_value = make_entity_index_entry(
            "sensor.power",
            "Power",
            device_class="power",
        )
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator(entity_index=entity_index)

        rules = [
            {"rule_type": "domain_include", "rule_value": "sensor"},
            {"rule_type": "device_class_include", "rule_value": "temperature"},
        ]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("climate-agent", "sensor.power")

        assert result is False

    async def test_device_class_exclude_denies_cached_action(self):
        entity_index = MagicMock()
        entity_index.get_by_id.return_value = make_entity_index_entry(
            "sensor.power",
            "Power",
            device_class="power",
        )
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator(entity_index=entity_index)

        rules = [
            {"rule_type": "domain_include", "rule_value": "sensor"},
            {"rule_type": "device_class_exclude", "rule_value": "power"},
        ]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("climate-agent", "sensor.power")

        assert result is False

    async def test_entity_include_allows_cached_action_despite_domain_and_area_filters(self):
        entity_index = MagicMock()
        entity_index.get_by_id.return_value = make_entity_index_entry(
            "light.kitchen",
            "Kitchen Light",
            area="kitchen",
        )
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator(entity_index=entity_index)

        rules = [
            {"rule_type": "domain_include", "rule_value": "switch"},
            {"rule_type": "area_include", "rule_value": "bedroom"},
            {"rule_type": "entity_include", "rule_value": "light.kitchen"},
        ]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is True

    async def test_missing_index_entry_for_scoped_rule_fails_closed(self):
        entity_index = MagicMock()
        entity_index.get_by_id.return_value = None
        orch, _dispatcher, _cache_manager, _ha_client = _make_orchestrator(entity_index=entity_index)

        rules = [{"rule_type": "area_include", "rule_value": "kitchen"}]
        with patch(
            "app.db.repository.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=rules),
        ):
            result = await orch._cached_action_is_still_visible("light-agent", "light.kitchen")

        assert result is False


# ---------------------------------------------------------------------------
# FLOW-CRIT-2: empty HA REST response treated as cache miss
# ---------------------------------------------------------------------------


class TestCachedActionEmptyResponse:
    """Simplified cached action path: direct REST call, no observer.
    Empty responses are treated as success (idempotent actions).
    """

    async def test_empty_list_response_returns_success(self):
        orch, _dispatcher, _cache_manager, ha_client = _make_orchestrator()
        ha_client.call_service.return_value = []
        cached = make_cached_action(service="light/turn_on", entity_id="light.kitchen")

        result = await orch._execute_cached_action(cached)

        assert result is not None
        assert result["success"] is True
        assert result["source"] == "cached_call"
        ha_client.call_service.assert_called_once()

    async def test_empty_dict_response_returns_success(self):
        orch, _dispatcher, _cache_manager, ha_client = _make_orchestrator()
        ha_client.call_service.return_value = {}
        cached = make_cached_action(service="light/turn_on", entity_id="light.kitchen")

        result = await orch._execute_cached_action(cached)

        assert result is not None
        assert result["success"] is True
        assert result["source"] == "cached_call"

    async def test_non_empty_list_response_returns_success(self):
        orch, _dispatcher, _cache_manager, ha_client = _make_orchestrator()
        payload = [{"entity_id": "light.kitchen", "state": "on"}]
        ha_client.call_service.return_value = payload
        cached = make_cached_action(service="light/turn_on", entity_id="light.kitchen")

        result = await orch._execute_cached_action(cached)

        assert result is not None
        assert result["success"] is True
        assert result["entity_id"] == "light.kitchen"
        assert result["action"] == "turn_on"
        assert result["source"] == "cached_call"


# ---------------------------------------------------------------------------
# FLOW-CRIT-3: sequential-send must not pipe canned content-failure text
# ---------------------------------------------------------------------------


class TestSequentialSendContentFailure:
    def _orchestrator_for_send(self):
        orch, dispatcher, _cache_manager, _ha_client = _make_orchestrator()
        # ha_client must be falsy in _dispatch_single's home-context branch
        # to avoid network / zoneinfo dependencies during this unit test.
        orch._ha_client = None
        return orch, dispatcher

    async def test_skips_send_when_content_dispatch_times_out(self):
        """Content agent timed out -> _dispatch_single returns canned text +
        result=None. Send-agent must NOT be invoked."""
        orch, _dispatcher = self._orchestrator_for_send()

        async def fake_dispatch_single(*args, **kwargs):
            # Mimic the timeout fallback shape from _dispatch_single.
            return ("general-agent", "I couldn't process that request in time.", None)

        with patch.object(orch, "_dispatch_single", side_effect=fake_dispatch_single) as mock_ds:
            classifications = [
                ("general-agent", "summarize today", 0.9, []),
                ("send-agent", "telegram", 0.9, []),
            ]
            routed_to, speech, result = await orch._handle_sequential_send(
                classifications,
                user_text="send today summary to telegram",
                conversation_id="conv-1",
                turns=[],
                span_collector=None,
                incoming_context=None,
            )

        # Only the content dispatch should have happened; no send dispatch.
        assert mock_ds.call_count == 1
        assert routed_to == "send-agent"
        assert speech == "I could not prepare the content to send."
        assert result and result.get("error", {}).get("code") == "content_unavailable"

    async def test_skips_send_when_content_returns_error(self):
        """Content agent returned a result dict with `error` -- skip send."""
        orch, _dispatcher = self._orchestrator_for_send()

        async def fake_dispatch_single(*args, **kwargs):
            return (
                "general-agent",
                "Sorry, that didn't work.",
                {
                    "speech": "Sorry, that didn't work.",
                    "error": {
                        "code": "llm_error",
                        "recoverable": True,
                    },
                },
            )

        with patch.object(orch, "_dispatch_single", side_effect=fake_dispatch_single) as mock_ds:
            classifications = [
                ("general-agent", "summarize today", 0.9, []),
                ("send-agent", "telegram", 0.9, []),
            ]
            _routed_to, speech, result = await orch._handle_sequential_send(
                classifications,
                user_text="send today summary to telegram",
                conversation_id="conv-1",
                turns=[],
                span_collector=None,
                incoming_context=None,
            )

        assert mock_ds.call_count == 1
        assert speech == "I could not prepare the content to send."
        assert result["error"]["code"] == "content_unavailable"

    async def test_skips_send_when_content_returns_partial_failure(self):
        orch, _dispatcher = self._orchestrator_for_send()

        async def fake_dispatch_single(*args, **kwargs):
            return (
                "general-agent",
                "Partial result text.",
                {"speech": "Partial result text.", "partial_failure": True},
            )

        with patch.object(orch, "_dispatch_single", side_effect=fake_dispatch_single) as mock_ds:
            classifications = [
                ("general-agent", "summarize", 0.9, []),
                ("send-agent", "telegram", 0.9, []),
            ]
            _routed, speech, result = await orch._handle_sequential_send(
                classifications,
                user_text="x",
                conversation_id="conv-1",
                turns=[],
                span_collector=None,
                incoming_context=None,
            )

        assert mock_ds.call_count == 1
        assert speech == "I could not prepare the content to send."
        assert result["error"]["code"] == "content_unavailable"

    async def test_send_proceeds_on_successful_content(self):
        """Content succeeded -> send-agent dispatch happens normally."""
        orch, _dispatcher = self._orchestrator_for_send()

        call_log: list[str] = []

        async def fake_dispatch_single(target_agent, *args, **kwargs):
            call_log.append(target_agent)
            if target_agent == "send-agent":
                return (
                    "send-agent",
                    "Sent.",
                    {"speech": "Sent."},
                )
            return (
                target_agent,
                "Today: light usage normal.",
                {"speech": "Today: light usage normal."},
            )

        with patch.object(orch, "_dispatch_single", side_effect=fake_dispatch_single):
            classifications = [
                ("general-agent", "summarize today", 0.9, []),
                ("send-agent", "telegram", 0.9, []),
            ]
            _routed, speech, result = await orch._handle_sequential_send(
                classifications,
                user_text="send today summary to telegram",
                conversation_id="conv-1",
                turns=[],
                span_collector=None,
                incoming_context=None,
            )

        assert call_log == ["general-agent", "send-agent"]
        assert speech == "Sent."
        assert (result or {}).get("error") is None


class TestActionCacheTraceDualWrite:
    async def test_orchestrator_writes_both_action_and_legacy_metadata_keys(self):
        orch, _dispatcher, cache_manager, _ha_client = _make_orchestrator()
        cached = make_cached_action(service="light/turn_on", entity_id="light.kitchen")
        hit = ActionReplayOutcome(
            kind="full_hit",
            entry_id="action-1",
            agent_id="light-agent",
            response_text="Done.",
            replay_result={"success": True},
            similarity=1.0,
            cached_action=cached,
        )

        from app.analytics.tracer import SpanCollector

        span_collector = SpanCollector("dual-write-test")
        orch._get_turns = AsyncMock(return_value=[])
        orch._store_turn = AsyncMock()
        cache_manager.apply_rewrite = AsyncMock(return_value="Done.")

        with patch("app.analytics.tracer.create_trace_summary", new=AsyncMock()):
            result = await orch._finalize_action_replay_hit(
                hit,
                "conv-1",
                "turn on the kitchen light",
                span_collector,
            )

        assert result is not None
        return_spans = [s for s in span_collector._spans if s.get("span_name") == "return"]
        assert return_spans, "orchestrator must emit a 'return' span on cache hit"
        meta = return_spans[-1].get("metadata") or {}
        assert meta.get("action_cache_hit") is True
        assert meta.get("response_cache_hit") is False

    async def test_action_hit_writes_trace_summary_row_with_provenance(self):
        """REGRESSION: an action-cache hit must reach create_trace_summary.

        Previously the ``_create_trace(...)`` call in
        ``finalize_action_replay_hit`` passed 10 positional args to a
        9-positional signature, raising ``TypeError`` that the swallowing
        ``except`` hid, so no ``trace_summary`` row was written. This pins
        the row-write and the routing provenance/cache_hit_type.
        """
        orch, _dispatcher, cache_manager, _ha_client = _make_orchestrator()
        cached = make_cached_action(service="light/turn_on", entity_id="light.kitchen")
        hit = ActionReplayOutcome(
            kind="full_hit",
            entry_id="action-2",
            agent_id="light-agent",
            response_text="Done.",
            replay_result={"success": True},
            similarity=1.0,
            cached_action=cached,
        )

        from app.analytics.tracer import SpanCollector

        span_collector = SpanCollector("action-hit-trace")
        # Mirror the cache_lookup span emitted by the real try_cache_replay on
        # an action hit, so _create_trace can derive cache_hit_type.
        span_collector._spans.append(
            {
                "span_name": "cache_lookup",
                "agent_id": "orchestrator",
                "metadata": {"hit_type": "action_hit"},
                "duration_ms": 1.0,
            }
        )
        orch._get_turns = AsyncMock(return_value=[])
        orch._store_turn = AsyncMock()
        cache_manager.apply_rewrite = AsyncMock(return_value="Done.")

        user_text = "turn on the kitchen light"
        with patch("app.analytics.tracer.create_trace_summary", new_callable=AsyncMock) as mock_summary:
            await orch._finalize_action_replay_hit(
                hit,
                "conv-1",
                user_text,
                span_collector,
            )

        mock_summary.assert_awaited_once()
        kwargs = mock_summary.await_args.kwargs
        assert kwargs["routing_agent"] == "light-agent"
        assert kwargs["routing_confidence"] == 1.0
        assert kwargs["condensed_task"] == user_text
        assert "light-agent" in kwargs["agents"]
        assert kwargs["cache_hit_type"] == "action_hit"
