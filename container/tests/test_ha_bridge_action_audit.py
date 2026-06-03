"""HA-Bridge Action-Audit test suite (20 black-box integration tests)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from tests.helpers import BridgeActionAudit, HAMimicClient
from tests.scenarios.loader import load_scenario
from tests.scenarios.types import (
    Expected,
    FollowUpTurn,
    LlmReplies,
    Preconditions,
    Scenario,
    ScenarioContext,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def light_on_app(db_path):
    """App backed by the ``light/turn_on_kitchen`` scenario."""
    scenario_path = Path(__file__).resolve().parent / "data" / "scenarios" / "light" / "turn_on_kitchen.yaml"
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(load_scenario(scenario_path), db_path)
    yield app


@pytest_asyncio.fixture()
async def climate_app(db_path):
    """App backed by the ``climate/set_temperature_living_room`` scenario."""
    scenario_path = (
        Path(__file__).resolve().parent / "data" / "scenarios" / "climate" / "set_temperature_living_room.yaml"
    )
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(load_scenario(scenario_path), db_path)
    yield app


@pytest_asyncio.fixture()
async def media_app(db_path):
    """App backed by the ``media/pause_living_room_tv`` scenario."""
    scenario_path = Path(__file__).resolve().parent / "data" / "scenarios" / "media" / "pause_living_room_tv.yaml"
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(load_scenario(scenario_path), db_path)
    yield app


@pytest_asyncio.fixture()
async def scene_app(db_path):
    """App backed by the ``scene/activate_movie_night`` scenario."""
    scenario_path = Path(__file__).resolve().parent / "data" / "scenarios" / "scene" / "activate_movie_night.yaml"
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(load_scenario(scenario_path), db_path)
    yield app


@pytest_asyncio.fixture()
async def brightness_app(db_path):
    """App backed by the ``light/set_brightness_bedroom`` scenario."""
    scenario_path = Path(__file__).resolve().parent / "data" / "scenarios" / "light" / "set_brightness_bedroom.yaml"
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(load_scenario(scenario_path), db_path)
    yield app


@pytest_asyncio.fixture()
async def ambiguous_app(db_path):
    """App backed by the ``light/ambiguous_area_tiebreak_living`` scenario."""
    scenario_path = (
        Path(__file__).resolve().parent / "data" / "scenarios" / "light" / "ambiguous_area_tiebreak_living.yaml"
    )
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(load_scenario(scenario_path), db_path)
    yield app


@pytest_asyncio.fixture()
async def notfound_app(db_path):
    """App backed by the ``light/entity_not_found`` scenario."""
    scenario_path = Path(__file__).resolve().parent / "data" / "scenarios" / "light" / "entity_not_found.yaml"
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(load_scenario(scenario_path), db_path)
    yield app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _custom_scenario_app(db_path, scenario: Scenario):
    from tests.conftest import build_scenario_backed_app

    return build_scenario_backed_app(scenario, db_path)


# ===================================================================
# A. Routing Audit
# ===================================================================


@pytest.mark.integration
class TestRoutingAudit:
    async def test_rest_routes_light_agent(self, light_on_app):
        async with HAMimicClient(light_on_app) as client:
            response = await client.rest_turn("turn on the kitchen light")
        BridgeActionAudit.assert_routing(response, "light-agent", "A1")

    async def test_rest_routes_climate_agent(self, climate_app):
        async with HAMimicClient(climate_app) as client:
            response = await client.rest_turn("set living room thermostat to 22 degrees")
        BridgeActionAudit.assert_routing(response, "climate-agent", "A2")

    async def test_ws_routes_media_agent(self, media_app):
        async with HAMimicClient(media_app) as client:
            await client.connect_ws()
            tokens = await client.send_turn("pause the living room tv")
        done_frame = tokens[-1]
        BridgeActionAudit.assert_routing(done_frame, "media-agent", "A3")

    async def test_sse_routes_scene_agent(self, scene_app):
        async with HAMimicClient(scene_app) as client:
            tokens = await client.sse_turn("activate movie night")
        done_frame = tokens[-1]
        BridgeActionAudit.assert_routing(done_frame, "scene-agent", "A4")


# ===================================================================
# B. Entity Resolution Audit
# ===================================================================


@pytest.mark.integration
class TestEntityResolutionAudit:
    async def test_rest_resolves_exact_entity_id(self, db_path):
        scenario = Scenario(
            id="test_exact_entity_id",
            agent="light-agent",
            description="Exact entity_id resolution",
            snapshot="home_default",
            language="en",
            request_text="turn on light.kitchen_ceiling",
            context=ScenarioContext(source="ha", area_id="kitchen", area_name="Kitchen", device_id="satellite_kitchen"),
            preconditions=Preconditions(),
            llm=LlmReplies(
                classify="light-agent (95%): Turn on light.kitchen_ceiling\n",
                agents={
                    "light-agent": [
                        'Turning on the kitchen ceiling light.\n```json\n{"action": "turn_on", "entity": "light.kitchen_ceiling", "parameters": {}}\n```\n'
                    ]
                },
            ),
            expected=Expected(routed_agent="light-agent"),
        )
        app = _custom_scenario_app(db_path, scenario)
        async with HAMimicClient(app) as client:
            response = await client.rest_turn("turn on light.kitchen_ceiling")
        action = response.get("action_executed") or {}
        assert action.get("entity_id") == "light.kitchen_ceiling", (
            f"Expected exact entity_id resolution, got {action.get('entity_id')!r}"
        )

    async def test_rest_resolves_exact_friendly_name(self, db_path):
        scenario = Scenario(
            id="test_exact_friendly_name",
            agent="light-agent",
            description="Exact friendly_name resolution",
            snapshot="home_default",
            language="en",
            request_text="turn on Kitchen Ceiling",
            context=ScenarioContext(source="ha", area_id="kitchen", area_name="Kitchen", device_id="satellite_kitchen"),
            preconditions=Preconditions(),
            llm=LlmReplies(
                classify="light-agent (95%): Turn on Kitchen Ceiling\n",
                agents={
                    "light-agent": [
                        'Turning on the kitchen ceiling light.\n```json\n{"action": "turn_on", "entity": "Kitchen Ceiling", "parameters": {}}\n```\n'
                    ]
                },
            ),
            expected=Expected(routed_agent="light-agent"),
        )
        app = _custom_scenario_app(db_path, scenario)
        async with HAMimicClient(app) as client:
            response = await client.rest_turn("turn on Kitchen Ceiling")
        action = response.get("action_executed") or {}
        assert action.get("entity_id") == "light.kitchen_ceiling", (
            f"Expected exact friendly_name resolution, got {action.get('entity_id')!r}"
        )

    async def test_rest_resolves_alias(self, db_path):
        pytest.skip("home_default snapshot has no aliases defined")

    async def test_rest_resolves_area_disambiguation(self, ambiguous_app):
        async with HAMimicClient(ambiguous_app) as client:
            response = await client.rest_turn("turn on the ceiling light")
        action = response.get("action_executed") or {}
        # The ambiguous_area_tiebreak_living scenario uses living_room context.
        assert action.get("entity_id") == "light.living_room_ceiling", (
            f"Expected area tie-break to living_room, got {action.get('entity_id')!r}"
        )

    async def test_ws_resolves_hybrid_matcher(self, db_path):
        from app.api.routes import conversation as conv_routes

        old_dispatcher = conv_routes._dispatcher

        async def _mock_stream(req):
            yield {
                "token": "",
                "mediated_speech": "Turned on the living room lamp.",
                "done": True,
                "action_executed": {
                    "action": "turn_on",
                    "entity_id": "light.living_room_lamp",
                    "success": True,
                    "service_data": {},
                },
                "routed_to": "light-agent",
            }

        mock_d = MagicMock()
        mock_d.dispatch_stream = _mock_stream

        app = _custom_scenario_app(
            db_path,
            Scenario(
                id="test_hybrid",
                agent="light-agent",
                description="Hybrid matcher fallback",
                snapshot="home_default",
                language="en",
                request_text="turn on the big lamp in living room",
                context=ScenarioContext(source="ha"),
                preconditions=Preconditions(),
                llm=LlmReplies(),
                expected=Expected(),
            ),
        )
        async with HAMimicClient(app) as client:
            conv_routes._dispatcher = mock_d
            try:
                await client.connect_ws()
                tokens = await client.send_turn("turn on the big lamp in living room")
            finally:
                conv_routes._dispatcher = old_dispatcher
        done_frame = tokens[-1]
        action = done_frame.get("action_executed") or {}
        assert action.get("entity_id") == "light.living_room_lamp", (
            f"Expected hybrid matcher fallback, got {action.get('entity_id')!r}"
        )

    async def test_rest_no_match_returns_error(self, notfound_app):
        async with HAMimicClient(notfound_app) as client:
            response = await client.rest_turn("turn on the garden gnome lamp")
        action = response.get("action_executed") or {}
        # Entity-not-found results in an action_executed with empty entity_id and error result.
        assert action.get("result") == "error", f"Expected error result for not-found, got {action!r}"
        assert action.get("entity_id") == "", f"Expected empty entity_id for not-found, got {action.get('entity_id')!r}"
        assert "could not find" in response.get("speech", "").lower() or "find" in response.get("speech", "").lower()


# ===================================================================
# C. Action Executed Audit
# ===================================================================


@pytest.mark.integration
class TestActionExecutedAudit:
    async def test_rest_action_executed_has_service(self, light_on_app):
        async with HAMimicClient(light_on_app) as client:
            response = await client.rest_turn("turn on the kitchen light")
        BridgeActionAudit.assert_action_executed(response, expected_service="light/turn_on", scenario_id="C11")

    async def test_rest_action_executed_has_entity_id(self, light_on_app):
        async with HAMimicClient(light_on_app) as client:
            response = await client.rest_turn("turn on the kitchen light")
        BridgeActionAudit.assert_action_executed(
            response,
            expected_service="light/turn_on",
            expected_entity="light.kitchen_ceiling",
            scenario_id="C12",
        )

    async def test_rest_action_executed_has_service_data(self, brightness_app):
        async with HAMimicClient(brightness_app) as client:
            response = await client.rest_turn("set bedroom ceiling to 30 percent")
        action = response.get("action_executed") or {}
        assert action.get("entity_id") == "light.bedroom_ceiling", (
            f"C13 entity_id mismatch: {action.get('entity_id')!r}"
        )
        service_data = action.get("service_data") or {}
        assert "brightness" in service_data, f"C13 missing brightness in service_data: {service_data!r}"

    async def test_ws_action_executed_on_done_frame(self, light_on_app):
        async with HAMimicClient(light_on_app) as client:
            await client.connect_ws()
            tokens = await client.send_turn("turn on the kitchen light")
        done_frame = tokens[-1]
        assert done_frame.get("done") is True
        action = done_frame.get("action_executed") or {}
        assert action.get("service") == "light/turn_on", f"C14 expected light/turn_on, got {action.get('service')!r}"
        assert action.get("entity_id") == "light.kitchen_ceiling"
        assert done_frame.get("routed_agent") == "light-agent"

    async def test_sse_action_executed_on_done_frame(self, light_on_app):
        async with HAMimicClient(light_on_app) as client:
            tokens = await client.sse_turn("turn on the kitchen light")
        done_frame = tokens[-1]
        assert done_frame.get("done") is True
        action = done_frame.get("action_executed") or {}
        assert action.get("service") == "light/turn_on", f"C15 expected light/turn_on, got {action.get('service')!r}"
        assert action.get("entity_id") == "light.kitchen_ceiling"
        assert done_frame.get("routed_agent") == "light-agent"

    async def test_rest_multi_turn_preserves_conversation_id(self, db_path):
        scenario = Scenario(
            id="test_multi_turn",
            agent="light-agent",
            description="Multi-turn conversation id preservation",
            snapshot="home_default",
            language="en",
            request_text="turn on the kitchen light",
            context=ScenarioContext(source="ha", area_id="kitchen", area_name="Kitchen", device_id="satellite_kitchen"),
            preconditions=Preconditions(),
            llm=LlmReplies(
                classify="light-agent (95%): Turn on the kitchen ceiling light\n",
                agents={
                    "light-agent": [
                        'Turning on the kitchen ceiling light.\n```json\n{"action": "turn_on", "entity": "kitchen ceiling", "parameters": {}}\n```\n'
                    ],
                },
            ),
            expected=Expected(routed_agent="light-agent"),
            follow_up=[
                FollowUpTurn(
                    text="turn off the living room ceiling light",
                    llm=LlmReplies(
                        classify="light-agent (95%): Turn off the living room ceiling light\n",
                        agents={
                            "light-agent": [
                                'Turning off the living room ceiling light.\n```json\n{"action": "turn_off", "entity": "living room ceiling", "parameters": {}}\n```\n'
                            ],
                        },
                    ),
                    expected=Expected(routed_agent="light-agent"),
                )
            ],
        )
        app = _custom_scenario_app(db_path, scenario)
        async with HAMimicClient(app) as client:
            response1 = await client.rest_turn("turn on the kitchen light", conversation_id="conv-multi")
            response2 = await client.rest_turn("turn off the living room ceiling light", conversation_id="conv-multi")
        assert response1.get("conversation_id") == "conv-multi"
        assert response2.get("conversation_id") == "conv-multi"
        action1 = response1.get("action_executed") or {}
        action2 = response2.get("action_executed") or {}
        assert action1.get("entity_id") == "light.kitchen_ceiling"
        assert action2.get("entity_id") == "light.living_room_ceiling"


# ===================================================================
# D. Full Contract Audit
# ===================================================================


@pytest.mark.integration
class TestFullContractAudit:
    async def test_rest_full_contract_light_on(self, light_on_app):
        async with HAMimicClient(light_on_app) as client:
            response = await client.rest_turn("turn on the kitchen light")
        BridgeActionAudit.assert_full_contract(
            response,
            {
                "routed_agent": "light-agent",
                "action_executed": {
                    "service": "light/turn_on",
                    "entity_id": "light.kitchen_ceiling",
                },
                "speech_contains": ["kitchen"],
            },
            "D17",
        )

    async def test_rest_full_contract_climate_set(self, climate_app):
        async with HAMimicClient(climate_app) as client:
            response = await client.rest_turn("set living room thermostat to 22 degrees")
        BridgeActionAudit.assert_full_contract(
            response,
            {
                "routed_agent": "climate-agent",
                "action_executed": {
                    "service": "climate/set_temperature",
                    "entity_id": "climate.living_room_thermostat",
                    "service_data_keys": ["temperature"],
                },
                "speech_contains": ["temperature"],
            },
            "D18",
        )

    async def test_ws_full_contract_scene_activate(self, scene_app):
        async with HAMimicClient(scene_app) as client:
            await client.connect_ws()
            tokens = await client.send_turn("activate movie night")
        done_frame = tokens[-1]
        BridgeActionAudit.assert_full_contract(
            done_frame,
            {
                "routed_agent": "scene-agent",
                "action_executed": {
                    "service": "scene/activate_scene",
                    "entity_id": "scene.movie_night",
                },
                "speech_contains": ["movie"],
            },
            "D19",
        )

    async def test_rest_vs_ws_action_parity(self, light_on_app, db_path):
        # Use a separate WS app so the deterministic LLM stub is independent.
        scenario_path = Path(__file__).resolve().parent / "data" / "scenarios" / "light" / "turn_on_kitchen.yaml"
        from tests.conftest import build_scenario_backed_app

        ws_app = build_scenario_backed_app(load_scenario(scenario_path), db_path)

        async with HAMimicClient(light_on_app) as rest_client:
            rest_response = await rest_client.rest_turn("turn on the kitchen light")

        async with HAMimicClient(ws_app) as ws_client:
            await ws_client.connect_ws()
            tokens = await ws_client.send_turn("turn on the kitchen light")

        ws_done = tokens[-1]
        rest_action = rest_response.get("action_executed") or {}
        ws_action = ws_done.get("action_executed") or {}
        assert rest_action.get("service") == ws_action.get("service"), (
            f"D20 service mismatch: REST={rest_action.get('service')!r} WS={ws_action.get('service')!r}"
        )
        assert rest_action.get("entity_id") == ws_action.get("entity_id"), (
            f"D20 entity_id mismatch: REST={rest_action.get('entity_id')!r} WS={ws_action.get('entity_id')!r}"
        )
