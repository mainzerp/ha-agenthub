"""Tests for app.agents.background_actions — G20 parity and event handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agents import background_actions as ba
from app.models.agent import BackgroundEvent, TaskContext

# ---------------------------------------------------------------------------
# G20: Parity between notification_dispatcher and background_actions
# ---------------------------------------------------------------------------


class TestNotificationDispatcherBackgroundActionsParity:
    @pytest.mark.asyncio
    async def test_both_modules_have_handle_background_event(self):
        """G20: background_actions should expose handle_background_event."""
        assert hasattr(ba, "handle_background_event")
        assert callable(ba.handle_background_event)

    @pytest.mark.asyncio
    async def test_both_modules_handle_alarm_notification(self):
        """G20: background_actions should handle alarm_notification events."""
        event = BackgroundEvent(event_type="alarm_notification", payload={"alarm_name": "Morning"})
        with patch.object(ba, "dispatch_alarm_notification", new=AsyncMock(return_value=None)) as mock_alarm:
            await ba.handle_background_event(
                event,
                context=TaskContext(),
                ha_client=AsyncMock(),
            )
            mock_alarm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_both_modules_handle_timer_notification(self):
        """G20: background_actions should handle timer_notification events."""
        event = BackgroundEvent(
            event_type="timer_notification",
            payload={"timer_name": "Pasta", "duration": "10 min"},
        )
        with patch.object(ba, "dispatch_timer_notification", new=AsyncMock(return_value=None)) as mock_timer:
            await ba.handle_background_event(
                event,
                context=TaskContext(),
                ha_client=AsyncMock(),
            )
            mock_timer.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_background_event coverage for other event types
# ---------------------------------------------------------------------------


class TestHandleBackgroundEvent:
    @pytest.mark.asyncio
    async def test_delayed_action_success(self):
        """delayed_action with valid payload should call HA service."""
        ha_client = AsyncMock()
        event = BackgroundEvent(
            event_type="delayed_action",
            payload={"target_entity": "light.bedroom", "target_action": "light/turn_on"},
        )
        result = await ba.handle_background_event(event, ha_client=ha_client)
        assert result["speech"] == ""
        assert result["action_executed"]["success"] is True
        ha_client.call_service.assert_awaited_once_with("light", "turn_on", "light.bedroom")

    @pytest.mark.asyncio
    async def test_delayed_action_missing_ha_client(self):
        """delayed_action without ha_client should return error."""
        event = BackgroundEvent(
            event_type="delayed_action",
            payload={"target_entity": "light.bedroom", "target_action": "light/turn_on"},
        )
        result = await ba.handle_background_event(event, ha_client=None)
        assert result["error"]["code"] == "ha_unavailable"

    @pytest.mark.asyncio
    async def test_delayed_action_incomplete_payload(self):
        """delayed_action with incomplete payload should return parse_error."""
        event = BackgroundEvent(
            event_type="delayed_action",
            payload={"target_entity": ""},
        )
        result = await ba.handle_background_event(event, ha_client=AsyncMock())
        assert result["error"]["code"] == "parse_error"

    @pytest.mark.asyncio
    async def test_sleep_media_stop_success(self):
        """sleep_media_stop should call media_player/media_stop."""
        ha_client = AsyncMock()
        event = BackgroundEvent(
            event_type="sleep_media_stop",
            payload={"media_player": "media_player.bedroom"},
        )
        result = await ba.handle_background_event(event, ha_client=ha_client)
        assert result["action_executed"]["action"] == "media_stop"
        ha_client.call_service.assert_awaited_once_with("media_player", "media_stop", "media_player.bedroom")

    @pytest.mark.asyncio
    async def test_sleep_media_stop_missing_ha_client(self):
        """sleep_media_stop without ha_client should return error."""
        event = BackgroundEvent(
            event_type="sleep_media_stop",
            payload={"media_player": "media_player.bedroom"},
        )
        result = await ba.handle_background_event(event, ha_client=None)
        assert result["error"]["code"] == "ha_unavailable"

    @pytest.mark.asyncio
    async def test_voice_followup_spawns_task(self):
        """voice_followup should spawn a background task."""
        ha_client = AsyncMock()
        with patch.object(ba, "spawn_voice_followup_after_conversation") as mock_spawn:
            event = BackgroundEvent(
                event_type="voice_followup",
                payload={"area_id": "living_room"},
            )
            result = await ba.handle_background_event(event, ha_client=ha_client)
            assert result["speech"] == ""
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_timer_notification_returns_empty_speech(self):
        """timer_notification should return empty speech after dispatch."""
        event = BackgroundEvent(
            event_type="timer_notification",
            payload={"timer_name": "Tea", "entity_id": "timer.tea"},
        )
        with patch.object(ba, "dispatch_timer_notification", new=AsyncMock(return_value=None)) as mock_timer:
            result = await ba.handle_background_event(
                event,
                context=TaskContext(),
                ha_client=AsyncMock(),
            )
            assert result["speech"] == ""
            mock_timer.assert_awaited_once()


# ---------------------------------------------------------------------------
# Notification metadata helpers
# ---------------------------------------------------------------------------


class TestNotificationMetadata:
    def test_normalize_area_for_match(self):
        assert ba._normalize_area_for_match("Living Room") == "living room"
        assert ba._normalize_area_for_match(None) is None
        assert ba._normalize_area_for_match("") is None
        assert ba._normalize_area_for_match("   ") is None

    def test_error_result_structure(self):
        result = ba._error_result("something went wrong", code="test_code", recoverable=False)
        assert result["speech"] == ""
        assert result["error"]["code"] == "test_code"
        assert result["error"]["message"] == "something went wrong"
        assert result["error"]["recoverable"] is False


# ---------------------------------------------------------------------------
# Timer name heuristic
# ---------------------------------------------------------------------------


class TestHasMeaningfulTimerName:
    def test_meaningful_name(self):
        assert ba._has_meaningful_timer_name("Tea", "timer.pasta") is True

    def test_generic_timer_name(self):
        assert ba._has_meaningful_timer_name("timer", "timer.kitchen") is False
        assert ba._has_meaningful_timer_name("timer 1", "timer.kitchen") is False

    def test_name_matches_entity_id(self):
        assert ba._has_meaningful_timer_name("kitchen", "timer.kitchen") is False

    def test_empty_name(self):
        assert ba._has_meaningful_timer_name("", "timer.kitchen") is False
