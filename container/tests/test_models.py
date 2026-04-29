"""Tests for app.models -- Pydantic model validation, serialization, error cases."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.agent import AgentCard, AgentConfig, AgentTask, BackgroundEvent, TaskContext
from app.models.cache import ActionCacheEntry, CachedAction, RoutingCacheEntry
from app.models.conversation import ActionResult, ConversationRequest, ConversationResponse, StreamToken
from app.models.entity_index import EntityIndexEntry
from tests.helpers import (
    make_action_cache_entry,
    make_action_result,
    make_agent_card,
    make_agent_config,
    make_agent_task,
    make_cached_action,
    make_conversation_request,
    make_conversation_response,
    make_entity_index_entry,
    make_routing_cache_entry,
    make_stream_token,
)

# ---- Conversation models ----


class TestConversationRequest:
    def test_valid_request_required_fields(self):
        req = ConversationRequest(text="turn on the light")
        assert req.text == "turn on the light"
        assert req.language == "en"
        assert req.conversation_id is None

    def test_valid_request_all_fields(self):
        req = make_conversation_request(text="hello", conversation_id="conv-1", language="de")
        assert req.text == "hello"
        assert req.conversation_id == "conv-1"
        assert req.language == "de"

    def test_missing_text_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ConversationRequest()

    def test_json_round_trip(self):
        req = make_conversation_request()
        data = req.model_dump_json()
        restored = ConversationRequest.model_validate_json(data)
        assert restored.text == req.text


class TestConversationResponse:
    def test_valid_response_minimal(self):
        resp = ConversationResponse(speech="Done.")
        assert resp.speech == "Done."
        assert resp.action_executed is None

    def test_response_with_action(self):
        action = make_action_result()
        resp = make_conversation_response(action_executed=action)
        assert resp.action_executed is not None
        assert resp.action_executed.service == "light/turn_on"

    def test_missing_speech_raises(self):
        with pytest.raises(ValidationError):
            ConversationResponse()


class TestActionResult:
    def test_defaults(self):
        ar = ActionResult(service="light/turn_on", entity_id="light.kitchen")
        assert ar.result == "success"
        assert ar.service_data is None

    def test_json_round_trip(self):
        ar = make_action_result(service_data={"brightness": 200})
        data = ar.model_dump_json()
        restored = ActionResult.model_validate_json(data)
        assert restored.service_data == {"brightness": 200}


class TestStreamToken:
    def test_defaults(self):
        st = StreamToken(token="Hi")
        assert st.done is False
        assert st.conversation_id is None
        assert st.is_filler is False

    def test_done_token(self):
        st = make_stream_token(token="", done=True)
        assert st.done is True

    def test_is_filler_defaults_false(self):
        st = StreamToken(token="hello")
        assert st.is_filler is False

    def test_is_filler_serialization(self):
        st = StreamToken(token="One moment...", is_filler=True)
        data = st.model_dump()
        assert data["is_filler"] is True
        assert data["sanitized"] is False
        restored = StreamToken.model_validate(data)
        assert restored.is_filler is True
        assert restored.sanitized is False


# ---- Agent models ----


class TestAgentCard:
    def test_valid_card(self):
        card = make_agent_card()
        assert card.agent_id == "light-agent"
        assert "light_control" in card.skills

    def test_default_io_types(self):
        card = AgentCard(agent_id="a", name="A", description="desc")
        assert "text/plain" in card.input_types
        assert "application/json" in card.output_types

    def test_expected_latency_defaults_low(self):
        card = AgentCard(agent_id="a", name="A", description="desc")
        assert card.expected_latency == "low"

    def test_expected_latency_high(self):
        card = AgentCard(agent_id="a", name="A", description="desc", expected_latency="high")
        assert card.expected_latency == "high"

    def test_missing_required_raises(self):
        with pytest.raises(ValidationError):
            AgentCard(agent_id="a")

    def test_json_round_trip(self):
        card = make_agent_card()
        data = card.model_dump_json()
        restored = AgentCard.model_validate_json(data)
        assert restored.agent_id == card.agent_id


class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig(agent_id="test")
        assert cfg.enabled is True
        assert cfg.timeout == 5
        assert cfg.temperature == 0.2

    def test_factory(self):
        cfg = make_agent_config(enabled=False, temperature=0.3)
        assert cfg.enabled is False
        assert cfg.temperature == 0.3


class TestAgentTask:
    def test_valid_task(self):
        task = make_agent_task()
        assert task.description == "Turn on the kitchen light"
        assert task.context is None

    def test_task_with_context(self):
        ctx = TaskContext(area_id="kitchen")
        task = make_agent_task(context=ctx)
        assert task.context.area_id == "kitchen"

    def test_task_context_language_default(self):
        ctx = TaskContext()
        assert ctx.language == "en"

    def test_task_context_language_custom(self):
        ctx = TaskContext(language="de")
        assert ctx.language == "de"

    def test_task_context_background_event_round_trip(self):
        ctx = TaskContext(
            source="background",
            background_event=BackgroundEvent(
                event_type="timer_notification",
                payload={"timer_name": "Tea"},
            ),
        )
        restored = TaskContext.model_validate(ctx.model_dump())
        assert restored.source == "background"
        assert restored.background_event is not None
        assert restored.background_event.event_type == "timer_notification"
        assert restored.background_event.payload["timer_name"] == "Tea"

    def test_missing_description_raises(self):
        with pytest.raises(ValidationError):
            AgentTask(user_text="hello")


# ---- Cache models ----


class TestRoutingCacheEntry:
    def test_valid_entry(self):
        entry = make_routing_cache_entry()
        assert entry.agent_id == "light-agent"
        assert entry.hit_count == 1
        assert entry.language == "en"
        assert entry.schema_version == 4

    def test_json_round_trip(self):
        entry = make_routing_cache_entry()
        data = entry.model_dump_json()
        restored = RoutingCacheEntry.model_validate_json(data)
        assert restored.query_text == entry.query_text
        assert restored.schema_version == 4


class TestActionCacheEntry:
    def test_valid_entry(self):
        entry = make_action_cache_entry()
        assert entry.agent_id == "light-agent"
        assert "light.kitchen_ceiling" in entry.entity_ids
        assert entry.language == "en"
        assert entry.schema_version == 4

    def test_with_cached_action(self):
        action = make_cached_action()
        entry = make_action_cache_entry(cached_action=action)
        assert entry.cached_action.service == "light/turn_on"

    def test_json_round_trip(self):
        entry = make_action_cache_entry()
        data = entry.model_dump_json()
        restored = ActionCacheEntry.model_validate_json(data)
        assert restored.response_text == entry.response_text
        assert restored.cached_action.entity_id == entry.cached_action.entity_id


class TestCachedAction:
    def test_defaults(self):
        ca = CachedAction(service="light/turn_on", entity_id="light.kitchen")
        assert ca.service_data == {}

    def test_with_service_data(self):
        ca = make_cached_action(service_data={"brightness": 100})
        assert ca.service_data["brightness"] == 100


# ---- Entity index models ----


class TestEntityIndexEntry:
    def test_valid_entry(self):
        entry = make_entity_index_entry()
        assert entry.entity_id == "light.kitchen_ceiling"
        assert entry.domain == "light"

    def test_auto_domain(self):
        entry = EntityIndexEntry(entity_id="climate.thermostat", friendly_name="Thermostat", domain="climate")
        assert entry.domain == "climate"

    def test_embedding_text_basic(self):
        entry = make_entity_index_entry(friendly_name="Kitchen Light", area="kitchen", domain="light")
        text = entry.embedding_text
        assert "Kitchen Light" in text
        assert "kitchen" in text
        assert "light" in text

    def test_embedding_text_no_area(self):
        entry = EntityIndexEntry(entity_id="light.x", friendly_name="X", domain="light", area=None)
        text = entry.embedding_text
        assert "X" in text
        # area should not appear
        assert text == "X light"

    def test_missing_entity_id_raises(self):
        with pytest.raises(ValidationError):
            EntityIndexEntry(friendly_name="Test")
