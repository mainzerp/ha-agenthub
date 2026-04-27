"""Bridge tests for filler completion gating.

Minimal hass test double needed here:
- hass.services.async_call: AsyncMock
- hass.async_create_task: returns asyncio.create_task(coro)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1].parent


class _FakeHass:
    def __init__(self, service_side_effect=None):
        self.services = SimpleNamespace(async_call=AsyncMock(side_effect=service_side_effect))

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


class _StateChangeTracker:
    def __init__(self):
        self.callback = None
        self.entity_ids = None
        self.unsubscribe = Mock()

    def track(self, hass, entity_ids, callback):
        self.entity_ids = list(entity_ids)
        self.callback = callback
        return self.unsubscribe

    def fire_state_change(self, old_state_str, new_state_str):
        if self.callback is None:
            raise AssertionError("state callback was not registered")
        event = SimpleNamespace(
            data={
                "old_state": None if old_state_str is None else SimpleNamespace(state=old_state_str),
                "new_state": None if new_state_str is None else SimpleNamespace(state=new_state_str),
            }
        )
        self.callback(event)


def _import_conversation_module():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from custom_components.ha_agenthub import conversation as conversation_module

    return conversation_module


def _ws_text(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(payload))


def _ws_closed() -> SimpleNamespace:
    return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED)


def _make_entity(conversation_module, *, service_side_effect=None, build_result=None):
    entity_cls = conversation_module.HaAgentHubConversationEntity
    entity = entity_cls.__new__(entity_cls)
    entity.hass = _FakeHass(service_side_effect)
    entity._ws = SimpleNamespace(send_json=AsyncMock(), receive=AsyncMock())
    entity._filler_gates = {}
    entity._resolve_origin_context = MagicMock(return_value={})
    entity._is_native_plain_timers_enabled = MagicMock(return_value=False)
    entity._ws_last_active = 0.0
    entity._build_result = MagicMock(return_value=build_result if build_result is not None else object())
    return entity


class TestHABridgeFillerWait:
    @pytest.fixture(autouse=True)
    def _mock_homeassistant(self):
        mocks = {}
        ha_modules = [
            "homeassistant",
            "homeassistant.components",
            "homeassistant.components.assist_pipeline",
            "homeassistant.components.conversation",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.area_registry",
            "homeassistant.helpers.device_registry",
            "homeassistant.helpers.entity_registry",
            "homeassistant.helpers.intent",
            "homeassistant.helpers.event",
            "homeassistant.helpers.entity_platform",
        ]
        for mod in ha_modules:
            if mod not in sys.modules:
                mocks[mod] = MagicMock()
                sys.modules[mod] = mocks[mod]

        sys.modules["homeassistant.const"].CONF_URL = "url"
        sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"
        sys.modules["homeassistant.const"].MATCH_ALL = "*"
        sys.modules["homeassistant.config_entries"].ConfigEntry = type("ConfigEntry", (), {})
        sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
        sys.modules["homeassistant.helpers.entity_platform"].AddConfigEntryEntitiesCallback = MagicMock()
        sys.modules["homeassistant.helpers.event"].async_track_state_change_event = MagicMock()
        conv_mod = sys.modules["homeassistant.components.conversation"]
        conv_mod.ConversationEntityFeature = MagicMock()
        conv_mod.ConversationEntity = type(
            "ConversationEntity",
            (),
            {"__init__": lambda self, *args, **kwargs: None},
        )
        conv_mod.ConversationResult = type("ConversationResult", (), {})
        sys.modules["homeassistant.components"].conversation = conv_mod
        sys.modules["homeassistant.components"].assist_pipeline = sys.modules[
            "homeassistant.components.assist_pipeline"
        ]

        yield

        for mod in mocks:
            sys.modules.pop(mod, None)
        for key in list(sys.modules):
            if key.startswith("custom_components"):
                del sys.modules[key]

    async def test_filler_then_final_waits_for_announce(self):
        conversation_module = _import_conversation_module()
        result_sentinel = object()
        announce_release = asyncio.Event()

        async def _service_call(domain, service, service_data, blocking):
            assert domain == "assist_satellite"
            assert service == "announce"
            assert blocking is True
            assert service_data == {
                "entity_id": "assist_satellite.kitchen",
                "message": "Working on it",
            }
            await announce_release.wait()

        entity = _make_entity(
            conversation_module,
            service_side_effect=_service_call,
            build_result=result_sentinel,
        )
        entity._resolve_satellite_entity = MagicMock(return_value="assist_satellite.kitchen")

        entity._ws.receive = AsyncMock(
            side_effect=[
                _ws_text({"token": "Working on it", "is_filler": True}),
                _ws_text({"token": "Done.", "done": True, "sanitized": True}),
            ]
        )

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id="device-a")

        task = asyncio.create_task(conversation_module.HaAgentHubConversationEntity._process_via_ws(entity, user_input))
        await asyncio.sleep(0.05)

        assert not task.done()
        entity.hass.services.async_call.assert_awaited_once()

        announce_release.set()
        result = await task

        assert result is result_sentinel
        assert entity._filler_gates == {}
        assert entity.hass.services.async_call.await_args.kwargs["blocking"] is True

    async def test_no_filler_no_wait(self):
        conversation_module = _import_conversation_module()
        result_sentinel = object()
        entity = _make_entity(conversation_module, build_result=result_sentinel)
        entity._ws.receive = AsyncMock(return_value=_ws_text({"token": "Done.", "done": True, "sanitized": True}))

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id=None)

        started = time.monotonic()
        result = await conversation_module.HaAgentHubConversationEntity._process_via_ws(entity, user_input)
        elapsed = time.monotonic() - started

        assert result is result_sentinel
        assert elapsed < 0.1
        assert entity._filler_gates == {}

    async def test_fallback_waits_for_media_player_state(self):
        conversation_module = _import_conversation_module()
        result_sentinel = object()
        tracker = _StateChangeTracker()
        entity = _make_entity(conversation_module, build_result=result_sentinel)
        entity._resolve_satellite_entity = MagicMock(return_value=None)
        entity._resolve_tts_entity = MagicMock(return_value="media_player.kitchen")
        entity._resolve_tts_engine_entity = MagicMock(return_value="tts.engine")
        entity._ws.receive = AsyncMock(
            side_effect=[
                _ws_text({"token": "Working on it", "is_filler": True}),
                _ws_text({"token": "Done.", "done": True, "sanitized": True}),
            ]
        )

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id="device-a")

        with patch.object(conversation_module, "async_track_state_change_event", tracker.track):
            task = asyncio.create_task(
                conversation_module.HaAgentHubConversationEntity._process_via_ws(entity, user_input)
            )
            await asyncio.sleep(0.05)

            assert not task.done()
            tracker.fire_state_change("idle", "playing")
            await asyncio.sleep(0.05)
            assert not task.done()

            tracker.fire_state_change("playing", "idle")
            result = await task

        assert result is result_sentinel
        assert tracker.entity_ids == ["media_player.kitchen"]
        tracker.unsubscribe.assert_called_once_with()
        assert entity._filler_gates == {}

    async def test_fallback_ignores_pre_play_idle(self):
        conversation_module = _import_conversation_module()
        tracker = _StateChangeTracker()
        entity = _make_entity(conversation_module)
        entity._resolve_satellite_entity = MagicMock(return_value=None)
        entity._resolve_tts_entity = MagicMock(return_value="media_player.kitchen")
        entity._resolve_tts_engine_entity = MagicMock(return_value="tts.engine")

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id="device-a")
        gate_key = conversation_module.HaAgentHubConversationEntity._filler_gate_key(entity, user_input)

        with patch.object(conversation_module, "async_track_state_change_event", tracker.track):
            await conversation_module.HaAgentHubConversationEntity._speak_filler(entity, "Working on it", user_input)
            gate = entity._filler_gates[gate_key]
            tracker.fire_state_change("unknown", "idle")
            assert gate.event.is_set() is False
            gate.event.set()
            await conversation_module.HaAgentHubConversationEntity._await_filler_gate(entity, gate_key)

        tracker.unsubscribe.assert_called_once_with()
        assert entity._filler_gates == {}

    async def test_hard_cap_releases_gate_on_stuck_signal(self, caplog):
        conversation_module = _import_conversation_module()
        result_sentinel = object()
        tracker = _StateChangeTracker()
        entity = _make_entity(conversation_module, build_result=result_sentinel)
        entity._resolve_satellite_entity = MagicMock(return_value=None)
        entity._resolve_tts_entity = MagicMock(return_value="media_player.kitchen")
        entity._resolve_tts_engine_entity = MagicMock(return_value="tts.engine")
        entity._ws.receive = AsyncMock(
            side_effect=[
                _ws_text({"token": "Working on it", "is_filler": True}),
                _ws_text({"token": "Done.", "done": True, "sanitized": True}),
            ]
        )

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id="device-a")

        caplog.set_level(logging.WARNING)
        with (
            patch.object(conversation_module, "MAX_FILLER_WAIT_SECONDS", 0.2),
            patch.object(conversation_module, "async_track_state_change_event", tracker.track),
        ):
            started = time.monotonic()
            result = await conversation_module.HaAgentHubConversationEntity._process_via_ws(entity, user_input)
            elapsed = time.monotonic() - started

        assert result is result_sentinel
        assert elapsed < 0.5
        assert "Filler completion signal not received within 0.2s cap; releasing gate" in caplog.text
        tracker.unsubscribe.assert_called_once_with()
        assert entity._filler_gates == {}

    async def test_two_satellites_do_not_block_each_other(self):
        conversation_module = _import_conversation_module()
        entity = _make_entity(conversation_module)

        gate_one = conversation_module.HaAgentHubConversationEntity._arm_filler_gate(
            entity,
            "device:satellite-one",
            mechanism="announce",
        )
        gate_two = conversation_module.HaAgentHubConversationEntity._arm_filler_gate(
            entity,
            "device:satellite-two",
            mechanism="announce",
        )

        wait_one = asyncio.create_task(
            conversation_module.HaAgentHubConversationEntity._await_filler_gate(entity, "device:satellite-one")
        )
        wait_two = asyncio.create_task(
            conversation_module.HaAgentHubConversationEntity._await_filler_gate(entity, "device:satellite-two")
        )
        await asyncio.sleep(0.05)

        gate_two.event.set()
        await wait_two

        assert not wait_one.done()
        assert "device:satellite-one" in entity._filler_gates

        gate_one.event.set()
        await wait_one

        assert entity._filler_gates == {}

    async def test_directive_short_circuit_also_waits_for_filler(self):
        conversation_module = _import_conversation_module()
        tracker = _StateChangeTracker()
        entity = _make_entity(conversation_module)
        entity._resolve_satellite_entity = MagicMock(return_value=None)
        entity._resolve_tts_entity = MagicMock(return_value="media_player.kitchen")
        entity._resolve_tts_engine_entity = MagicMock(return_value="tts.engine")
        entity._ws.receive = AsyncMock(
            side_effect=[
                _ws_text({"token": "Working on it", "is_filler": True}),
                _ws_text(
                    {
                        "token": "",
                        "done": True,
                        "directive": "delegate_native_plain_timer",
                        "reason": "native_timer",
                    }
                ),
            ]
        )

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id="device-a")

        with patch.object(conversation_module, "async_track_state_change_event", tracker.track):
            task = asyncio.create_task(
                conversation_module.HaAgentHubConversationEntity._process_via_ws(entity, user_input)
            )
            await asyncio.sleep(0.05)

            assert not task.done()
            tracker.fire_state_change("idle", "playing")
            await asyncio.sleep(0.05)
            assert not task.done()

            tracker.fire_state_change("playing", "idle")
            result = await task

        assert isinstance(result, conversation_module._BridgeDirective)
        assert result.directive == "delegate_native_plain_timer"
        tracker.unsubscribe.assert_called_once_with()
        assert entity._filler_gates == {}

    async def test_dropped_stream_clears_gate(self):
        conversation_module = _import_conversation_module()
        tracker = _StateChangeTracker()
        entity = _make_entity(conversation_module)
        entity._resolve_satellite_entity = MagicMock(return_value=None)
        entity._resolve_tts_entity = MagicMock(return_value="media_player.kitchen")
        entity._resolve_tts_engine_entity = MagicMock(return_value="tts.engine")
        entity._ws.receive = AsyncMock(
            side_effect=[
                _ws_text({"token": "Working on it", "is_filler": True}),
                _ws_closed(),
            ]
        )

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id="device-a")

        with patch.object(conversation_module, "async_track_state_change_event", tracker.track):
            with pytest.raises(conversation_module._WsDroppedAfterSendError):
                await conversation_module.HaAgentHubConversationEntity._process_via_ws(entity, user_input)

        tracker.unsubscribe.assert_called_once_with()
        assert entity._filler_gates == {}

        result_sentinel = object()
        entity._build_result = MagicMock(return_value=result_sentinel)
        entity._ws = SimpleNamespace(send_json=AsyncMock(), receive=AsyncMock(return_value=_ws_text({"token": "Done.", "done": True, "sanitized": True})))

        started = time.monotonic()
        result = await conversation_module.HaAgentHubConversationEntity._process_via_ws(entity, user_input)
        elapsed = time.monotonic() - started

        assert result is result_sentinel
        assert elapsed < 0.1

    async def test_speak_filler_early_return_arms_no_gate(self):
        conversation_module = _import_conversation_module()
        entity = _make_entity(conversation_module)
        entity._resolve_satellite_entity = MagicMock(return_value=None)

        user_input = MagicMock(text="hello", conversation_id="conv-1", language="en", device_id=None)

        await conversation_module.HaAgentHubConversationEntity._speak_filler(entity, "Working on it", user_input)

        entity.hass.services.async_call.assert_not_awaited()
        assert entity._filler_gates == {}