"""Tests for timer-agent-owned native plain-timer delegation (0.25.2).

The route no longer runs a standalone classifier or short-circuits the
dispatcher. Instead it forwards the integration's eligibility signal into
task context, the timer-agent may emit a native delegation directive, and
the orchestrator/route pass that directive back to the integration.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. Container route + eligibility propagation tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRouteEligibilityPropagation:
    async def _client(self):
        from contextlib import asynccontextmanager

        import httpx
        import pytest_asyncio  # noqa: F401  (ensure asyncio mode)

        from app.api.routes import conversation as conv_routes
        from tests.conftest import build_integration_test_app

        dispatcher = MagicMock()
        dispatcher.dispatch = AsyncMock(return_value=MagicMock(error=None, result={"speech": "fallback orchestrator"}))
        conv_routes.set_dispatcher(dispatcher)

        app = build_integration_test_app(setup_complete=True, override_api_key=True, dispatcher=dispatcher)

        @asynccontextmanager
        async def _client_ctx():
            with patch(
                "app.db.repository.SetupStateRepository.is_complete",
                new_callable=AsyncMock,
                return_value=True,
            ):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                    yield c, dispatcher

        return _client_ctx()

    async def test_eligible_request_dispatches_and_sets_context_flag(self, db_repository):
        ctx = await self._client()
        captured: dict = {}

        async with ctx as (client, dispatcher):

            async def _dispatch(request):
                captured["task"] = request.params["task"]
                response = MagicMock()
                response.error = None
                response.result = {"speech": "fallback orchestrator"}
                return response

            dispatcher.dispatch = AsyncMock(side_effect=_dispatch)

            resp = await client.post(
                "/api/conversation",
                json={"text": "Set a timer for 5 minutes", "native_plain_timer_eligible": True},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["speech"] == "fallback orchestrator"
            dispatcher.dispatch.assert_awaited_once()
            assert captured["task"]["context"]["native_plain_timer_eligible"] is True

    async def test_header_only_eligibility_sets_same_context_flag(self, db_repository):
        ctx = await self._client()
        captured: dict = {}

        async with ctx as (client, dispatcher):

            async def _dispatch(request):
                captured["task"] = request.params["task"]
                response = MagicMock()
                response.error = None
                response.result = {"speech": "fallback orchestrator"}
                return response

            dispatcher.dispatch = AsyncMock(side_effect=_dispatch)

            resp = await client.post(
                "/api/conversation",
                json={"text": "Cancel my timer"},
                headers={"X-HA-AgentHub-Native-Plain-Timer-Eligible": "1"},
            )
            assert resp.status_code == 200
            dispatcher.dispatch.assert_awaited_once()
            assert captured["task"]["context"]["native_plain_timer_eligible"] is True

    async def test_directive_response_round_trips_through_dispatcher(self, db_repository):
        ctx = await self._client()
        async with ctx as (client, dispatcher):
            dispatcher.dispatch = AsyncMock(
                return_value=MagicMock(
                    error=None,
                    result={
                        "speech": "",
                        "directive": "delegate_native_plain_timer",
                        "reason": "native_cancel",
                    },
                )
            )

            resp = await client.post(
                "/api/conversation",
                json={"text": "Cancel my timer", "native_plain_timer_eligible": True},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["directive"] == "delegate_native_plain_timer"
            assert body["reason"] == "native_cancel"
            assert body["speech"] == ""
            dispatcher.dispatch.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Integration directive-flow tests
# ---------------------------------------------------------------------------


@pytest.fixture
def _ha_stubs():
    import sys

    mocks: dict[str, MagicMock] = {}
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
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.selector",
    ]
    for mod in ha_modules:
        if mod not in sys.modules:
            mocks[mod] = MagicMock()
            sys.modules[mod] = mocks[mod]

    sys.modules["homeassistant.const"].CONF_URL = "url"
    sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"
    sys.modules["homeassistant.const"].MATCH_ALL = "*"
    conv_mod = sys.modules["homeassistant.components.conversation"]
    conv_mod.ConversationEntityFeature = MagicMock()
    conv_mod.ConversationEntity = type(
        "ConversationEntity",
        (),
        {"__init__": lambda self, *a, **kw: None},
    )
    conv_mod.async_converse = AsyncMock(name="async_converse_stub")
    sys.modules["homeassistant.components"].conversation = conv_mod
    sys.modules["homeassistant.components"].assist_pipeline = sys.modules["homeassistant.components.assist_pipeline"]

    sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

    yield conv_mod

    for mod in mocks:
        sys.modules.pop(mod, None)
    for key in list(sys.modules):
        if key.startswith("custom_components"):
            del sys.modules[key]


def _build_entity(_ha_stubs, *, native_enabled: bool, native_delegate=None):
    """Construct a HaAgentHubConversationEntity wired for delegation tests
    without going through ``__init__``."""
    from custom_components.ha_agenthub.const import CONF_NATIVE_PLAIN_TIMERS
    from custom_components.ha_agenthub.conversation import HaAgentHubConversationEntity

    entity = HaAgentHubConversationEntity.__new__(HaAgentHubConversationEntity)
    entity._coalesce_lock = asyncio.Lock()
    entity._inflight_bridge = {}
    entity._coalesce_window_sec = 1.0

    entry = MagicMock()
    entry.data = {CONF_NATIVE_PLAIN_TIMERS: native_enabled}
    entity._entry = entry

    if native_delegate is not None:
        _ha_stubs.async_converse = native_delegate

    class _FakeHass:
        def async_create_task(self, coro):
            return asyncio.create_task(coro)

    entity.hass = _FakeHass()

    bridge = AsyncMock(name="bridge", return_value="bridge-result")
    entity._async_bridge_to_container = bridge
    return entity, bridge


def _user_input(text: str, conversation_id: str = "c-1"):
    ui = MagicMock()
    ui.text = text
    ui.conversation_id = conversation_id
    ui.language = "en"
    ui.context = None
    ui.device_id = None
    return ui


def _directive(reason: str = "native_start"):
    from custom_components.ha_agenthub.conversation import _BridgeDirective

    return _BridgeDirective(directive="delegate_native_plain_timer", reason=reason)


class TestIntegrationDirectiveFlow:
    async def test_scenario_01_native_start_directive_delegates(self, _ha_stubs):
        native = AsyncMock(return_value="native-result")
        entity, bridge = _build_entity(_ha_stubs, native_enabled=True, native_delegate=native)
        bridge.return_value = _directive("native_start")

        result = await entity._async_handle_message(_user_input("Set a timer for 5 minutes"), MagicMock())

        assert result == "native-result"
        bridge.assert_awaited_once()
        native.assert_awaited_once()
        kwargs = native.call_args.kwargs
        assert kwargs["agent_id"] == "conversation.home_assistant"

    async def test_scenario_02_native_cancel_directive_delegates(self, _ha_stubs):
        native = AsyncMock(return_value="native-cancel-result")
        entity, bridge = _build_entity(_ha_stubs, native_enabled=True, native_delegate=native)
        bridge.return_value = _directive("native_cancel")

        result = await entity._async_handle_message(_user_input("Cancel my timer"), MagicMock())

        assert result == "native-cancel-result"
        native.assert_awaited_once()

    @pytest.mark.parametrize(
        "reason",
        [
            "advanced_reminder",
            "advanced_alarm",
            "advanced_sleep_or_media",
            "advanced_delayed_action",
            "advanced_notification",
            "advanced_device_target",
            "advanced_compound",
            "advanced_absolute_time",
            "advanced_helper_or_entity",
            "ambiguous",
            "not_timer",
            "unsupported_timer_verb",
        ],
    )
    async def test_scenario_03_04_false_decisions_use_normal_bridge(self, _ha_stubs, reason):
        # When the container returns a normal bridge result (no directive),
        # the integration stays on the AgentHub path.
        native = AsyncMock(return_value="native-result")
        entity, bridge = _build_entity(_ha_stubs, native_enabled=True, native_delegate=native)
        bridge.return_value = "bridge-result"

        result = await entity._async_handle_message(_user_input(f"utterance for {reason}"), MagicMock())

        assert result == "bridge-result"
        bridge.assert_awaited_once()
        native.assert_not_awaited()

    async def test_scenario_05_opt_in_off_skips_eligibility(self, _ha_stubs):
        from custom_components.ha_agenthub.const import (
            NATIVE_PLAIN_TIMER_ELIGIBLE_FIELD,
            NATIVE_PLAIN_TIMER_ELIGIBLE_HEADER,
        )

        entity, _bridge = _build_entity(_ha_stubs, native_enabled=False)

        # Directly exercise the REST sender to inspect the outgoing payload.
        captured: dict = {}

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"speech": "ok", "conversation_id": "c-1"}

        class _Session:
            def post(self, url, json=None, headers=None, timeout=None):
                captured["json"] = json
                captured["headers"] = headers
                return _Resp()

        entity._session = _Session()
        entity._url = "http://x"
        entity._api_key = "k"
        entity._resolve_origin_context = lambda ui: {}

        await entity._process_via_rest(_user_input("hello"))

        assert NATIVE_PLAIN_TIMER_ELIGIBLE_FIELD not in captured["json"]
        assert NATIVE_PLAIN_TIMER_ELIGIBLE_HEADER not in captured["headers"]

    async def test_eligibility_emitted_when_opt_in_enabled(self, _ha_stubs):
        from custom_components.ha_agenthub.const import (
            NATIVE_PLAIN_TIMER_ELIGIBLE_FIELD,
            NATIVE_PLAIN_TIMER_ELIGIBLE_HEADER,
        )

        entity, _bridge = _build_entity(_ha_stubs, native_enabled=True)

        captured: dict = {}

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"speech": "ok", "conversation_id": "c-1"}

        class _Session:
            def post(self, url, json=None, headers=None, timeout=None):
                captured["json"] = json
                captured["headers"] = headers
                return _Resp()

        entity._session = _Session()
        entity._url = "http://x"
        entity._api_key = "k"
        entity._resolve_origin_context = lambda ui: {}

        await entity._process_via_rest(_user_input("Set a timer for 5 minutes"))

        assert captured["json"][NATIVE_PLAIN_TIMER_ELIGIBLE_FIELD] is True
        assert captured["headers"][NATIVE_PLAIN_TIMER_ELIGIBLE_HEADER] == "1"

    async def test_scenario_08_duplicates_collapse_to_one_native_call(self, _ha_stubs):
        gate = asyncio.Event()
        native_calls = 0

        async def _delegate(*args, **kwargs):
            nonlocal native_calls
            native_calls += 1
            await gate.wait()
            return "native-result"

        native = AsyncMock(side_effect=_delegate)
        entity, bridge = _build_entity(_ha_stubs, native_enabled=True, native_delegate=native)
        bridge.return_value = _directive("native_start")

        ui = _user_input("Set a timer for 5 minutes")
        t1 = asyncio.create_task(entity._async_handle_message(ui, MagicMock()))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(entity._async_handle_message(ui, MagicMock()))
        await asyncio.sleep(0)
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)

        assert native_calls == 1
        assert r1 == r2 == "native-result"
        bridge.assert_awaited_once()

    async def test_scenario_09_directive_without_native_seam_retries_bridge_once(self, _ha_stubs):
        # No async_converse seam available: integration retries the bridge
        # exactly once with eligibility suppressed, never recursing.
        _ha_stubs.async_converse = None
        entity, bridge = _build_entity(_ha_stubs, native_enabled=True)

        # First call: bridge returns directive. Second call: bridge returns
        # a normal result (because eligibility is suppressed).
        bridge.side_effect = [_directive("native_start"), "fallback-result"]

        result = await entity._async_handle_message(_user_input("Set a timer for 5 minutes"), MagicMock())

        assert result == "fallback-result"
        assert bridge.await_count == 2

    async def test_scenario_10_native_raises_falls_back_via_existing_seam(self, _ha_stubs):
        # ``_async_delegate_to_native`` already has its own single-fallback
        # bridge call when the native callable raises before its handler
        # runs. Verify it still works and that the second bridge call sees
        # eligibility suppressed.
        from custom_components.ha_agenthub import conversation as conv_mod

        seen_suppressed: list[bool] = []

        async def _native(*args, **kwargs):
            raise RuntimeError("pre-handler boom")

        entity, _bridge = _build_entity(_ha_stubs, native_enabled=True, native_delegate=_native)

        async def _bridge(_ui):
            seen_suppressed.append(conv_mod._suppress_native_plain_timer_eligibility.get())
            if len(seen_suppressed) == 1:
                return _directive("native_start")
            return "fallback-after-native-error"

        entity._async_bridge_to_container = AsyncMock(side_effect=_bridge)

        result = await entity._async_handle_message(_user_input("Set a timer for 5 minutes"), MagicMock())

        assert result == "fallback-after-native-error"
        # First call: from inside _async_bridge_with_cleanup (not suppressed).
        # Second call: triggered inside _async_delegate_to_native fallback,
        # which runs while the suppression context is active.
        assert seen_suppressed == [False, True]

    async def test_scenario_11_logging_emits_stable_reason_codes(self, _ha_stubs, caplog):
        caplog.set_level(logging.DEBUG, logger="custom_components.ha_agenthub.conversation")

        native = AsyncMock(return_value="native-result")
        entity, bridge = _build_entity(_ha_stubs, native_enabled=True, native_delegate=native)
        bridge.return_value = _directive("native_start")

        await entity._async_handle_message(_user_input("Set a timer for 5 minutes"), MagicMock())

        log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "path=native" in log_text
        assert "reason=native_start" in log_text

    async def test_scenario_12_unknown_directive_does_not_loop(self, _ha_stubs):
        from custom_components.ha_agenthub.conversation import _BridgeDirective

        entity, bridge = _build_entity(_ha_stubs, native_enabled=True)
        bridge.side_effect = [
            _BridgeDirective(directive="something_unknown", reason=None),
            "fallback-result",
        ]

        result = await entity._async_handle_message(_user_input("Set a timer for 5 minutes"), MagicMock())

        assert result == "fallback-result"
        assert bridge.await_count == 2


# ---------------------------------------------------------------------------
# 3. Model round-trip tests for the public wire fields
# ---------------------------------------------------------------------------


class TestConversationModelDirectiveFields:
    def test_request_eligibility_round_trip(self):
        from app.models.conversation import ConversationRequest

        req = ConversationRequest(text="hi", native_plain_timer_eligible=True)
        data = req.model_dump_json()
        restored = ConversationRequest.model_validate_json(data)
        assert restored.native_plain_timer_eligible is True

    def test_response_directive_round_trip(self):
        from app.models.conversation import ConversationResponse

        resp = ConversationResponse(speech="", directive="delegate_native_plain_timer", reason="native_start")
        data = resp.model_dump_json()
        restored = ConversationResponse.model_validate_json(data)
        assert restored.directive == "delegate_native_plain_timer"
        assert restored.reason == "native_start"

    def test_stream_token_directive_round_trip(self):
        from app.models.conversation import StreamToken

        tok = StreamToken(token="", done=True, directive="delegate_native_plain_timer", reason="native_cancel")
        data = tok.model_dump_json()
        restored = StreamToken.model_validate_json(data)
        assert restored.done is True
        assert restored.directive == "delegate_native_plain_timer"
        assert restored.reason == "native_cancel"


# ---------------------------------------------------------------------------
# 4. LLM-only delegation contract (0.26.0)
# ---------------------------------------------------------------------------


class TestLLMDelegationContract:
    """The LLM is the sole gate for ``delegate_native_plain_timer``.

    The container injects an eligibility hint as the LAST line of the
    user-content message. There is no deterministic pre-LLM heuristic;
    these tests prove the LLM is always called and that the hint is
    delivered verbatim.
    """

    def _agent(self):
        from app.agents.timer import TimerAgent

        agent = TimerAgent(ha_client=MagicMock(), entity_index=MagicMock(), entity_matcher=MagicMock())
        agent._call_llm = AsyncMock(
            return_value='```json\n{"action": "list_timers", "entity": "", "parameters": {}}\n```'
        )
        return agent

    def _task(self, text: str, *, eligible: bool):
        from app.models.agent import TaskContext
        from tests.helpers import make_agent_task

        ctx = TaskContext(native_plain_timer_eligible=eligible, language="de")
        return make_agent_task(description=text, user_text=text, context=ctx)

    async def test_eligibility_line_present_when_true(self):
        agent = self._agent()
        task = self._task("Stelle einen Timer auf 3 Minuten.", eligible=True)
        await agent.handle_task(task)
        agent._call_llm.assert_awaited_once()
        messages = agent._call_llm.await_args.args[0]
        last_user = [m for m in messages if m["role"] == "user"][-1]
        assert last_user["content"].rstrip().endswith("(Execution context: native_plain_timer_eligible=true)")

    async def test_eligibility_line_present_when_false(self):
        agent = self._agent()
        task = self._task("Stelle einen Timer auf 3 Minuten.", eligible=False)
        await agent.handle_task(task)
        messages = agent._call_llm.await_args.args[0]
        last_user = [m for m in messages if m["role"] == "user"][-1]
        assert last_user["content"].rstrip().endswith("(Execution context: native_plain_timer_eligible=false)")

    async def test_system_prompt_contains_eligibility_aware_few_shots(self):
        agent = self._agent()
        await agent.handle_task(self._task("Set a timer for 5 minutes.", eligible=True))
        messages = agent._call_llm.await_args.args[0]
        system = messages[0]["content"]
        assert messages[0]["role"] == "system"
        for needle in (
            "Set a timer for 3 minutes.",
            "Set a timer for 5 minutes.",
            "delegate_native_plain_timer",
            "kitchen timer",
        ):
            assert needle in system

    async def test_system_prompt_does_not_contain_helper_pool_framing(self):
        agent = self._agent()
        await agent.handle_task(self._task("Set a timer for 5 minutes.", eligible=True))
        system = agent._call_llm.await_args.args[0][0]["content"]
        for forbidden in (
            "helper pool",
            "idle timer",
            "no specific entity matches",
            "available idle timer",
        ):
            assert forbidden not in system

    @pytest.mark.parametrize(
        "text",
        [
            "Stelle einen Timer auf 3 Minuten.",
            "Set a timer for 5 minutes.",
            "Stoppe den Timer.",
        ],
    )
    async def test_no_pre_llm_short_circuit(self, text):
        agent = self._agent()
        await agent.handle_task(self._task(text, eligible=True))
        agent._call_llm.assert_awaited_once()

    def test_timer_agent_has_no_handle_task_override(self):
        from app.agents.actionable import ActionableAgent
        from app.agents.timer import TimerAgent

        # handle_task must come from ActionableAgent, not be overridden.
        assert "handle_task" not in TimerAgent.__dict__
        assert TimerAgent.handle_task is ActionableAgent.handle_task

        import app.agents.timer as timer_mod

        for forbidden in (
            "_detect_plain_timer_directive",
            "_PLAIN_TIMER_PLACEHOLDERS",
            "_PLAIN_TIMER_NEGATIVE_KEYWORDS",
            "_PLAIN_START_PATTERNS",
            "_PLAIN_CANCEL_PATTERNS",
            "_build_native_delegation_result",
        ):
            assert getattr(timer_mod, forbidden, None) is None


# ---------------------------------------------------------------------------
# 5. Removal-assertions (0.26.0)
# ---------------------------------------------------------------------------


class TestHelperPoolRemoval:
    def test_idle_pool_symbols_removed(self):
        import app.agents.timer_executor as te

        assert getattr(te, "_TimerPool", None) is None
        assert getattr(te, "_timer_pool", None) is None
        assert getattr(te, "_find_idle_timer", None) is None
        assert getattr(te, "on_timer_finished", None) is None
        assert getattr(te, "TimerMetadata", None) is None
        assert getattr(te, "ExpiredTimer", None) is None

    def test_delayed_tasks_module_removed(self):
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("app.agents.delayed_tasks")

    def test_idle_pool_failure_speech_unreachable(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "app" / "agents" / "timer_executor.py"
        text = src.read_text(encoding="utf-8")
        assert "no idle timer entities are available for pool allocation" not in text
