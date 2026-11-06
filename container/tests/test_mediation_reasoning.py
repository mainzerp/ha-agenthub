"""Per-purpose reasoning effort of the mediation LLM calls.

``mediation.reasoning_effort`` controls mediate_response, mediate_response_stream
and localize_message; ``mediation.merge_reasoning_effort`` controls merge_responses.
Empty means inherit the orchestrator agent config (no override is sent).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.agents.mediation import MediationService, normalize_reasoning_effort
from app.agents.orchestrator import OrchestratorAgent
from app.analytics.tracer import SpanCollector
from app.db.repository import SettingsRepository
from tests.conftest import build_integration_test_app

MEDIATION_KEY = "mediation.reasoning_effort"
MERGE_KEY = "mediation.merge_reasoning_effort"


def _make_orch(*, reasoning: str | None = None, merge_reasoning: str | None = None, llm_text: str = "ok"):
    """Bare orchestrator with the mediation collaborators mocked."""
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch._mediation_model = None
    orch._mediation_temperature = 0.3
    orch._mediation_max_tokens = 512
    if reasoning is not None:
        orch._mediation_reasoning_effort = reasoning
    if merge_reasoning is not None:
        orch._mediation_merge_reasoning_effort = merge_reasoning
    orch._get_personality_cached = AsyncMock(return_value="Be warm")
    orch._load_prompt_async = AsyncMock(return_value="{personality} {language} {organic_followup_hint} {language}")
    orch._wrap_user_input = lambda text: text
    orch._call_llm = AsyncMock(return_value=llm_text)

    async def _stream(messages, **overrides):
        orch._stream_overrides = overrides
        yield "tok"

    orch._call_llm_stream = _stream
    return orch


def _overrides(orch) -> dict:
    return orch._call_llm.call_args.kwargs


MERGE_INPUT = [("light-agent", "Light on.", True), ("music-agent", "Playing.", True)]


class TestOverrideSent:
    async def test_mediate_response_sends_override(self):
        orch = _make_orch(reasoning="high", merge_reasoning="low")
        speech, _ = await MediationService(orch).mediate_response("Done.", "turn on", "light-agent")
        assert speech == "ok"
        assert _overrides(orch)["reasoning_effort"] == "high"

    async def test_mediate_response_stream_sends_override(self):
        orch = _make_orch(reasoning="medium", merge_reasoning="low")
        tokens = [t async for t in MediationService(orch).mediate_response_stream("Done.", "turn on", "light-agent")]
        assert tokens == ["tok"]
        assert orch._stream_overrides["reasoning_effort"] == "medium"

    async def test_localize_message_sends_mediation_override(self):
        orch = _make_orch(reasoning="none", merge_reasoning="high")
        await MediationService(orch).localize_message("I couldn't process that request.", "de")
        assert _overrides(orch)["reasoning_effort"] == "none"

    async def test_merge_responses_sends_merge_override_not_mediation_one(self):
        orch = _make_orch(reasoning="high", merge_reasoning="low")
        await MediationService(orch).merge_responses(MERGE_INPUT, "do both")
        assert _overrides(orch)["reasoning_effort"] == "low"

    async def test_merge_inherits_when_only_mediation_is_set(self):
        orch = _make_orch(reasoning="high", merge_reasoning="")
        await MediationService(orch).merge_responses(MERGE_INPUT, "do both")
        assert "reasoning_effort" not in _overrides(orch)

    async def test_mediation_inherits_when_only_merge_is_set(self):
        orch = _make_orch(reasoning="", merge_reasoning="high")
        await MediationService(orch).mediate_response("Done.", "turn on", "light-agent")
        assert "reasoning_effort" not in _overrides(orch)
        await MediationService(orch).localize_message("I couldn't process that request.", "de")
        assert "reasoning_effort" not in _overrides(orch)


class TestEmptyMeansUnchangedCall:
    """Defaults: the LLM call carries exactly the pre-existing overrides."""

    @pytest.mark.parametrize("attrs", [{}, {"reasoning": "", "merge_reasoning": ""}])
    async def test_calls_have_only_legacy_overrides(self, attrs):
        orch = _make_orch(**attrs)
        service = MediationService(orch)

        await service.mediate_response("Done.", "turn on", "light-agent")
        assert set(_overrides(orch)) == {"span_collector", "temperature", "max_tokens"}

        await service.merge_responses(MERGE_INPUT, "do both")
        assert set(_overrides(orch)) == {"span_collector", "temperature", "max_tokens"}

        await service.localize_message("I couldn't process that request.", "de")
        assert set(_overrides(orch)) == {"span_collector", "temperature", "max_tokens"}

        async for _ in service.mediate_response_stream("Done.", "turn on", "light-agent"):
            pass
        assert set(orch._stream_overrides) == {"span_collector", "temperature", "max_tokens"}

    async def test_non_string_value_is_ignored(self):
        orch = _make_orch()
        orch._mediation_reasoning_effort = MagicMock()
        await MediationService(orch).mediate_response("Done.", "turn on", "light-agent")
        assert "reasoning_effort" not in _overrides(orch)


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, ""), ("", ""), ("  ", ""), ("none", "none"), ("LOW", "low"), (" medium ", "medium"), ("high", "high")],
    )
    def test_valid_values(self, raw, expected):
        assert normalize_reasoning_effort(raw, MEDIATION_KEY) == expected

    def test_invalid_value_inherits_with_one_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.agents.mediation"):
            assert normalize_reasoning_effort("turbo", MEDIATION_KEY) == ""
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert MEDIATION_KEY in warnings[0].getMessage()

    async def test_invalid_stored_value_falls_back_to_inherit_on_load(self, caplog):
        values = {MEDIATION_KEY: "turbo", MERGE_KEY: "minimal"}
        orch = _make_orch()
        with patch("app.agents.orchestrator.SettingsRepository") as repo:
            repo.get_value = AsyncMock(side_effect=lambda key, default=None: values.get(key, default))
            with caplog.at_level(logging.WARNING, logger="app.agents.mediation"):
                await orch._load_mediation_config()
        assert orch._mediation_reasoning_effort == ""
        assert orch._mediation_merge_reasoning_effort == ""
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
        await MediationService(orch).mediate_response("Done.", "turn on", "light-agent")
        assert "reasoning_effort" not in _overrides(orch)


class TestHotReload:
    async def test_settings_write_applies_to_next_call_without_restart(self, db_repository):
        listeners_before = list(SettingsRepository._write_listeners)
        try:
            orch = _make_orch()
            await orch._load_mediation_config()
            orch._register_mediation_reload_listener()
            orch._register_mediation_reload_listener()  # idempotent
            assert len(SettingsRepository._write_listeners) == len(listeners_before) + 1
            service = MediationService(orch)

            await service.mediate_response("Done.", "turn on", "light-agent")
            assert "reasoning_effort" not in _overrides(orch)

            await SettingsRepository.set(MEDIATION_KEY, "high", "string", "mediation")
            await service.mediate_response("Done.", "turn on", "light-agent")
            assert _overrides(orch)["reasoning_effort"] == "high"
            await service.merge_responses(MERGE_INPUT, "do both")
            assert "reasoning_effort" not in _overrides(orch)

            await SettingsRepository.set(MERGE_KEY, "low", "string", "mediation")
            await service.merge_responses(MERGE_INPUT, "do both")
            assert _overrides(orch)["reasoning_effort"] == "low"

            await SettingsRepository.set(MEDIATION_KEY, "", "string", "mediation")
            await service.mediate_response("Done.", "turn on", "light-agent")
            assert "reasoning_effort" not in _overrides(orch)

            # Existing mediation settings reload as well.
            await SettingsRepository.set("mediation.temperature", "0.9", "number", "mediation")
            await service.mediate_response("Done.", "turn on", "light-agent")
            assert _overrides(orch)["temperature"] == 0.9
        finally:
            SettingsRepository._write_listeners[:] = listeners_before

    async def test_unrelated_key_does_not_reload(self, db_repository):
        listeners_before = list(SettingsRepository._write_listeners)
        try:
            orch = _make_orch()
            orch._load_mediation_config = AsyncMock()
            orch._register_mediation_reload_listener()
            await SettingsRepository.set("language", "de", "string", "general")
            orch._load_mediation_config.assert_not_awaited()
            await SettingsRepository.set(MERGE_KEY, "low", "string", "mediation")
            orch._load_mediation_config.assert_awaited_once()
        finally:
            SettingsRepository._write_listeners[:] = listeners_before

    async def test_seeded_rows_are_empty_mediation_settings(self, db_repository):
        for key in (MEDIATION_KEY, MERGE_KEY):
            row = await SettingsRepository.get(key)
            assert row is not None
            assert row["value"] == ""
            assert row["category"] == "mediation"
            assert row["description"]


class TestProviderSpanMetadata:
    @staticmethod
    def _config(reasoning_effort):
        return {
            "agent_id": "orchestrator",
            "enabled": True,
            "model": "openrouter/openai/gpt-4o-mini",
            "timeout": 5,
            "max_iterations": 3,
            "temperature": 0.3,
            "max_tokens": 256,
            "description": "Orchestrator",
            "reasoning_effort": reasoning_effort,
        }

    @pytest.mark.parametrize(
        ("config_effort", "override", "expected"),
        [("low", None, "inherit:low"), ("low", "high", "high"), (None, "none", "none"), (None, None, None)],
    )
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_records_effective_effort(
        self, mock_repo, mock_params, mock_acompletion, config_effort, override, expected
    ):
        from app.llm.client import complete

        mock_repo.get = AsyncMock(return_value=self._config(config_effort))
        choice = MagicMock()
        choice.message.content = "Done!"
        mock_acompletion.return_value = MagicMock(choices=[choice], usage=None)
        collector = SpanCollector("trace-effort")
        overrides = {"reasoning_effort": override} if override else {}

        await complete("orchestrator", [{"role": "user", "content": "x"}], span_collector=collector, **overrides)

        span = next(s for s in collector._spans if s["span_name"] == "llm_provider_call")
        assert span["metadata"].get("reasoning_effort") == expected

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_stream_records_effective_effort(self, mock_repo, mock_params, mock_acompletion):
        from app.llm.client import complete_stream

        mock_repo.get = AsyncMock(return_value=self._config("low"))

        async def _chunks():
            chunk = MagicMock()
            chunk.usage = None
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = "hi"
            chunk.choices[0].finish_reason = None
            yield chunk

        mock_acompletion.return_value = _chunks()
        collector = SpanCollector("trace-effort-stream")
        async for _ in complete_stream(
            "orchestrator", [{"role": "user", "content": "x"}], span_collector=collector, reasoning_effort="medium"
        ):
            pass

        span = next(s for s in collector._spans if s["span_name"] == "llm_provider_call")
        assert span["metadata"]["reasoning_effort"] == "medium"
        assert mock_acompletion.call_args.kwargs["reasoning_effort"] == "medium"


def _build_app():
    return build_integration_test_app(
        setup_complete=True,
        override_api_key=True,
        override_admin_session=True,
    )


async def _client_for(app):
    with patch("app.db.repository.SetupStateRepository.is_complete", new_callable=AsyncMock, return_value=True):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
class TestDashboardApi:
    async def test_personality_config_round_trip(self, db_repository):
        app = _build_app()
        async for client in _client_for(app):
            resp = await client.get("/api/admin/personality/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["mediation_reasoning_effort"] == ""
            assert data["mediation_merge_reasoning_effort"] == ""
            assert "mediation_model_supports_reasoning" in data

            resp = await client.put(
                "/api/admin/personality/config",
                json={"mediation_reasoning_effort": "low", "mediation_merge_reasoning_effort": "HIGH"},
            )
            assert resp.status_code == 200

            data = (await client.get("/api/admin/personality/config")).json()
            assert data["mediation_reasoning_effort"] == "low"
            assert data["mediation_merge_reasoning_effort"] == "high"

            resp = await client.put("/api/admin/personality/config", json={"mediation_reasoning_effort": ""})
            assert resp.status_code == 200
            data = (await client.get("/api/admin/personality/config")).json()
            assert data["mediation_reasoning_effort"] == ""
            assert data["mediation_merge_reasoning_effort"] == "high"

    async def test_partial_update_keeps_prompt_and_reasoning(self, db_repository):
        app = _build_app()
        async for client in _client_for(app):
            await client.put(
                "/api/admin/personality/config",
                json={"prompt": "Be warm", "mediation_reasoning_effort": "medium"},
            )
            await client.put("/api/admin/personality/config", json={"mediation_temperature": 0.5})
            data = (await client.get("/api/admin/personality/config")).json()
        assert data["prompt"] == "Be warm"
        assert data["mediation_reasoning_effort"] == "medium"
        assert data["mediation_temperature"] == 0.5

    async def test_invalid_value_is_rejected_without_partial_write(self, db_repository):
        app = _build_app()
        async for client in _client_for(app):
            resp = await client.put(
                "/api/admin/personality/config",
                json={"prompt": "Changed", "mediation_merge_reasoning_effort": "turbo"},
            )
            assert resp.status_code == 400
            data = (await client.get("/api/admin/personality/config")).json()
        assert data["prompt"] == ""
        assert data["mediation_merge_reasoning_effort"] == ""

    async def test_reports_reasoning_support_of_effective_model(self, db_repository):
        app = _build_app()
        await SettingsRepository.set("mediation.model", "openai/gpt-5-mini", "string", "mediation")
        async for client in _client_for(app):
            with patch("litellm.supports_reasoning", return_value=False) as supports:
                data = (await client.get("/api/admin/personality/config")).json()
        supports.assert_called_once_with("openai/gpt-5-mini")
        assert data["mediation_model"] == "openai/gpt-5-mini"
        assert data["mediation_model_supports_reasoning"] is False

    async def test_empty_mediation_model_uses_orchestrator_model(self, db_repository):
        app = _build_app()
        async for client in _client_for(app):
            with patch("litellm.supports_reasoning", return_value=True):
                data = (await client.get("/api/admin/personality/config")).json()
        assert data["mediation_model"]
        assert data["mediation_model_supports_reasoning"] is True

    async def test_generic_settings_api_validates_values(self, db_repository):
        app = _build_app()
        async for client in _client_for(app):
            ok = await client.put(f"/api/admin/settings/{MEDIATION_KEY}", json={"value": "medium"})
            bad = await client.put(f"/api/admin/settings/{MERGE_KEY}", json={"value": "minimal"})
        assert ok.status_code == 200
        assert bad.status_code == 400
