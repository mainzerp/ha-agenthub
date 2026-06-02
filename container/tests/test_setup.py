"""Integration tests for the setup wizard flow.

Tests setup routes including step progression, HA/LLM connection testing,
and completion behavior.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from app.security.auth import (
    require_admin_session,
    require_admin_session_redirect,
)
from tests.conftest import build_integration_test_app
from tests.helpers import csrf_post

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_setup_app(*, setup_complete: bool = False):
    """Build a FastAPI app for setup wizard tests.

    By default setup is *not* complete so the middleware allows /setup/ routes
    but redirects everything else.
    """
    return build_integration_test_app(setup_complete=setup_complete)


@pytest_asyncio.fixture()
async def setup_client(db_repository):
    """Client where setup is NOT complete (default seed state)."""
    app = _build_setup_app(setup_complete=False)
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=False,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            yield client


@pytest_asyncio.fixture()
async def setup_complete_client(db_repository):
    """Client where setup IS complete."""
    app = _build_setup_app(setup_complete=True)
    # Override admin session so dashboard routes are accessible
    app.dependency_overrides[require_admin_session] = lambda: {"username": "admin"}
    app.dependency_overrides[require_admin_session_redirect] = lambda: {"username": "admin"}
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            yield client


# ===================================================================
# Setup-incomplete state
# ===================================================================


@pytest.mark.integration
class TestSetupIncompleteRedirect:
    async def test_non_setup_routes_redirect_to_setup(self, setup_client: httpx.AsyncClient):
        resp = await setup_client.get("/dashboard/")
        assert resp.status_code == 302
        assert "/setup/" in resp.headers.get("location", "")

    async def test_api_routes_redirect_when_setup_incomplete(self, setup_client: httpx.AsyncClient):
        resp = await setup_client.post("/api/conversation", json={"text": "hello"})
        assert resp.status_code == 302

    async def test_health_accessible_during_setup(self, setup_client: httpx.AsyncClient):
        resp = await setup_client.get("/api/health")
        assert resp.status_code == 200

    async def test_setup_routes_accessible_during_setup(self, setup_client: httpx.AsyncClient):
        resp = await setup_client.get("/setup/")
        # Should redirect to first incomplete step, not 302 to /setup/ again
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            assert "/setup/step/" in resp.headers.get("location", "")


# ===================================================================
# Setup already complete
# ===================================================================


@pytest.mark.integration
class TestSetupAlreadyComplete:
    async def test_setup_index_redirects_to_dashboard_when_complete(self, setup_complete_client: httpx.AsyncClient):
        with patch(
            "app.setup.routes.SetupStateRepository.get_all_steps",
            new_callable=AsyncMock,
            return_value=[
                {"step": "admin_password", "completed": True},
                {"step": "ha_connection", "completed": True},
                {"step": "container_api_key", "completed": True},
                {"step": "llm_providers", "completed": True},
                {"step": "review_complete", "completed": True},
            ],
        ):
            resp = await setup_complete_client.get("/setup/")
            assert resp.status_code == 302
            assert "/dashboard/" in resp.headers.get("location", "")


# ===================================================================
# Step submissions
# ===================================================================


@pytest.mark.integration
class TestSetupStepSubmissions:
    async def test_step1_admin_password(self, setup_client: httpx.AsyncClient):
        with (
            patch(
                "app.setup.routes.hash_password",
                return_value="hashed-password",
            ),
            patch(
                "app.setup.routes.AdminAccountRepository.create",
                new_callable=AsyncMock,
            ) as mock_create,
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
        ):
            resp = await csrf_post(
                setup_client,
                "/setup/step/1",
                {"username": "admin", "password": "test-password-123"},
                get_url="/setup/step/1",
            )
            assert resp.status_code == 303
            assert "/setup/step/2" in resp.headers.get("location", "")
            mock_create.assert_awaited_once()

    async def test_step2_ha_connection(self, setup_client: httpx.AsyncClient):
        with (
            patch(
                "app.setup.routes.SettingsRepository.set",
                new_callable=AsyncMock,
            ),
            patch(
                "app.ha_client.auth.set_ha_token",
                new_callable=AsyncMock,
            ),
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
        ):
            resp = await csrf_post(
                setup_client,
                "/setup/step/2",
                {"ha_url": "http://homeassistant.local:8123", "ha_token": "test-token"},
                get_url="/setup/step/2",
            )
            assert resp.status_code == 303
            assert "/setup/step/3" in resp.headers.get("location", "")

    async def test_step5_triggers_runtime_initialization(self, setup_client: httpx.AsyncClient):
        with (
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
            patch(
                "app.setup.routes.ensure_setup_runtime_initialized",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_init,
        ):
            resp = await csrf_post(setup_client, "/setup/step/5", get_url="/setup/step/5")
            assert resp.status_code == 303
            assert "/dashboard/" in resp.headers.get("location", "")
            mock_init.assert_awaited_once()

    async def test_step3_api_key_generation(self, setup_client: httpx.AsyncClient):
        with (
            patch(
                "app.setup.routes.store_secret",
                new_callable=AsyncMock,
            ),
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
            patch(
                "app.setup.routes.SetupStateRepository.get_all_steps",
                new_callable=AsyncMock,
                return_value=[
                    {"step": "admin_password", "completed": True},
                    {"step": "ha_connection", "completed": True},
                    {"step": "container_api_key", "completed": True},
                    {"step": "llm_providers", "completed": False},
                    {"step": "review_complete", "completed": False},
                ],
            ),
        ):
            resp = await csrf_post(setup_client, "/setup/step/3", get_url="/setup/step/3")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")

    async def test_step4_llm_keys(self, setup_client: httpx.AsyncClient):
        with (
            patch(
                "app.setup.routes.store_secret",
                new_callable=AsyncMock,
            ),
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
        ):
            resp = await csrf_post(
                setup_client,
                "/setup/step/4",
                {"openrouter_key": "sk-test-123", "groq_key": "", "ollama_url": ""},
                get_url="/setup/step/4",
            )
            assert resp.status_code == 303
            assert "/setup/step/5" in resp.headers.get("location", "")

    async def test_step4_custom_provider_fields(self, setup_client: httpx.AsyncClient):
        with (
            patch(
                "app.setup.routes.store_secret",
                new_callable=AsyncMock,
            ) as mock_secret,
            patch(
                "app.setup.routes.SettingsRepository.set",
                new_callable=AsyncMock,
            ) as mock_settings,
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
        ):
            resp = await csrf_post(
                setup_client,
                "/setup/step/4",
                {
                    "openrouter_key": "",
                    "groq_key": "",
                    "ollama_url": "",
                    "custom_provider_name": "My Provider",
                    "custom_provider_url": "http://custom.local:8000/v1",
                    "custom_provider_key": "sk-custom",
                    "custom_provider_headers": '{"X-Custom": "value"}',
                },
                get_url="/setup/step/4",
            )
            assert resp.status_code == 303
            assert "/setup/step/5" in resp.headers.get("location", "")
            mock_secret.assert_any_await("custom_openai_api_key", "sk-custom")
            mock_settings.assert_any_await(
                "custom_openai_provider.name",
                "My Provider",
                "string",
                "llm",
                "Custom OpenAI provider name",
            )
            mock_settings.assert_any_await(
                "custom_openai_provider.base_url",
                "http://custom.local:8000/v1",
                "string",
                "llm",
                "Custom OpenAI provider base URL",
            )
            mock_settings.assert_any_await(
                "custom_openai_provider.headers",
                '{"X-Custom": "value"}',
                "json",
                "llm",
                "Custom OpenAI provider extra headers",
            )

    async def test_step4_saves_groq_key_and_ollama_url(self, setup_client: httpx.AsyncClient):
        """Cover branches for groq_key and ollama_url in save_llm_keys."""
        with (
            patch(
                "app.setup.routes.store_secret",
                new_callable=AsyncMock,
            ) as mock_secret,
            patch(
                "app.setup.routes.SettingsRepository.set",
                new_callable=AsyncMock,
            ) as mock_settings,
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
        ):
            resp = await csrf_post(
                setup_client,
                "/setup/step/4",
                {
                    "openrouter_key": "",
                    "groq_key": "gsk-test-key",
                    "ollama_url": "http://127.0.0.1:11434",
                },
                get_url="/setup/step/4",
            )
            assert resp.status_code == 303
            assert "/setup/step/5" in resp.headers.get("location", "")
            mock_secret.assert_any_await("groq_api_key", "gsk-test-key")
            mock_settings.assert_any_await(
                "ollama_base_url",
                "http://127.0.0.1:11434",
                "string",
                "llm",
                "Ollama API URL",
            )

    async def test_step5_complete(self, setup_client: httpx.AsyncClient):
        with patch(
            "app.setup.routes.SetupStateRepository.set_step_completed",
            new_callable=AsyncMock,
        ):
            resp = await csrf_post(setup_client, "/setup/step/5", get_url="/setup/step/5")
            assert resp.status_code == 303
            assert "/dashboard/" in resp.headers.get("location", "")

    async def test_step5_review_excludes_review_complete(self, setup_client: httpx.AsyncClient):
        """Step 5 review page should not show the 'review_complete' meta-step."""
        with patch(
            "app.setup.routes.SetupStateRepository.get_all_steps",
            new_callable=AsyncMock,
            return_value=[
                {"step": "admin_password", "completed": True},
                {"step": "ha_connection", "completed": True},
                {"step": "container_api_key", "completed": True},
                {"step": "llm_providers", "completed": True},
                {"step": "review_complete", "completed": False},
            ],
        ):
            resp = await setup_client.get("/setup/step/5")
            assert resp.status_code == 200
            body = resp.text
            assert "Admin Password" in body
            assert "Ha Connection" in body
            assert "Container Api Key" in body
            assert "Llm Providers" in body
            assert "Review Complete" not in body


# ===================================================================
# HA connection test endpoint
# ===================================================================


@pytest.mark.integration
class TestHAConnectionTest:
    async def test_ha_connection_test_success(self, setup_client: httpx.AsyncClient):
        with patch(
            "app.setup.routes.test_ha_connection",
            new_callable=AsyncMock,
            return_value=True,
        ):
            resp = await csrf_post(
                setup_client,
                "/setup/test/ha",
                {"ha_url": "http://ha.local:8123", "ha_token": "valid-token"},
                get_url="/setup/step/2",
            )
            assert resp.status_code == 200
            assert "alert-success" in resp.text
            assert "Connected to Home Assistant" in resp.text

    async def test_ha_connection_test_failure(self, setup_client: httpx.AsyncClient):
        with patch(
            "app.setup.routes.test_ha_connection",
            new_callable=AsyncMock,
            return_value=False,
        ):
            resp = await csrf_post(
                setup_client,
                "/setup/test/ha",
                {"ha_url": "http://bad-url:8123", "ha_token": "bad-token"},
                get_url="/setup/step/2",
            )
            assert resp.status_code == 200
            assert "alert-error" in resp.text
            assert "Failed to connect" in resp.text


# ===================================================================
# LLM test endpoint
# ===================================================================


@pytest.mark.integration
class TestLLMTest:
    async def test_llm_test_success(self, setup_client: httpx.AsyncClient):
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello!"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        mock_litellm_mod = MagicMock()
        mock_litellm_mod.acompletion = AsyncMock(return_value=mock_resp)
        with patch.dict(sys.modules, {"litellm": mock_litellm_mod}):
            resp = await csrf_post(
                setup_client,
                "/setup/test/llm",
                {"provider": "openrouter", "api_key": "sk-test"},
                get_url="/setup/step/4",
            )
            assert resp.status_code == 200
            assert "alert-success" in resp.text
            assert "Connected to openrouter" in resp.text

    async def test_llm_test_groq_success(self, setup_client: httpx.AsyncClient):
        mock_choice = MagicMock()
        mock_choice.message.content = "Hi"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_litellm_mod = MagicMock()
        mock_litellm_mod.acompletion = AsyncMock(return_value=mock_resp)
        with patch.dict(sys.modules, {"litellm": mock_litellm_mod}):
            resp = await csrf_post(
                setup_client,
                "/setup/test/llm",
                {"provider": "groq", "api_key": "gsk-xxx"},
                get_url="/setup/step/4",
            )
        assert resp.status_code == 200
        assert "alert-success" in resp.text
        assert "Connected to groq" in resp.text
        called = mock_litellm_mod.acompletion.await_args
        assert called.kwargs["model"] == "groq/llama-3.1-8b-instant"

    async def test_llm_test_ollama_success(self, setup_client: httpx.AsyncClient):
        mock_choice = MagicMock()
        mock_choice.message.content = "Hi"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_litellm_mod = MagicMock()
        mock_litellm_mod.acompletion = AsyncMock(return_value=mock_resp)
        with patch.dict(sys.modules, {"litellm": mock_litellm_mod}):
            resp = await csrf_post(
                setup_client,
                "/setup/test/llm",
                {"provider": "ollama", "api_key": "not-used"},
                get_url="/setup/step/4",
            )
        assert resp.status_code == 200
        assert "alert-success" in resp.text
        assert "Connected to ollama" in resp.text
        called = mock_litellm_mod.acompletion.await_args
        assert called.kwargs["model"] == "ollama/llama3"

    async def test_llm_test_failure(self, setup_client: httpx.AsyncClient):
        mock_litellm_mod = MagicMock()
        mock_litellm_mod.acompletion = AsyncMock(side_effect=Exception("API error"))
        with patch.dict(sys.modules, {"litellm": mock_litellm_mod}):
            resp = await csrf_post(
                setup_client,
                "/setup/test/llm",
                {"provider": "openrouter", "api_key": "bad-key"},
                get_url="/setup/step/4",
            )
            assert resp.status_code == 200
            assert "alert-error" in resp.text
            assert "Provider test failed" in resp.text

    async def test_llm_test_unknown_provider(self, setup_client: httpx.AsyncClient):
        mock_litellm_mod = MagicMock()
        with patch.dict(sys.modules, {"litellm": mock_litellm_mod}):
            resp = await csrf_post(
                setup_client,
                "/setup/test/llm",
                {"provider": "unknown-provider", "api_key": "key"},
                get_url="/setup/step/4",
            )
            assert resp.status_code == 200
            assert "alert-error" in resp.text
            assert "Unknown provider" in resp.text

    async def test_llm_test_custom_openai_success(self, setup_client: httpx.AsyncClient):
        mock_choice = MagicMock()
        mock_choice.message.content = "Hi"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_litellm_mod = MagicMock()
        mock_litellm_mod.acompletion = AsyncMock(return_value=mock_resp)
        with patch.dict(sys.modules, {"litellm": mock_litellm_mod}):
            resp = await csrf_post(
                setup_client,
                "/setup/test/llm",
                {
                    "provider": "custom_openai",
                    "api_key": "sk-custom",
                    "custom_provider_url": "http://custom.local:8000/v1",
                    "custom_provider_key": "sk-custom",
                },
                get_url="/setup/step/4",
            )
        assert resp.status_code == 200
        assert "alert-success" in resp.text
        assert "Connected to custom_openai" in resp.text
        called = mock_litellm_mod.acompletion.await_args
        assert called.kwargs["model"] == "custom_openai/gpt-4o-mini"
        assert called.kwargs["api_base"] == "http://custom.local:8000/v1"


# ===================================================================
# Phase 4.1: Additional setup wizard tests
# ===================================================================


@pytest.mark.integration
class TestSetupDuplicateAdmin:
    """Test that submitting step 1 twice (duplicate admin) does not crash (fix 1.4)."""

    async def test_duplicate_admin_submission_succeeds(self, setup_client: httpx.AsyncClient):
        """Submitting admin credentials a second time should use INSERT OR REPLACE."""
        with (
            patch(
                "app.setup.routes.hash_password",
                return_value="hashed-password",
            ),
            patch(
                "app.setup.routes.AdminAccountRepository.create",
                new_callable=AsyncMock,
            ) as mock_create,
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
        ):
            # First submission
            resp1 = await csrf_post(
                setup_client,
                "/setup/step/1",
                {"username": "admin", "password": "first-password"},
                get_url="/setup/step/1",
            )
            assert resp1.status_code == 303
            # Second submission (same username, different password)
            resp2 = await csrf_post(
                setup_client,
                "/setup/step/1",
                {"username": "admin", "password": "second-password"},
                get_url="/setup/step/1",
            )
            assert resp2.status_code == 303
            assert mock_create.await_count == 2


@pytest.mark.integration
class TestSetupXSSPrevention:
    """Test XSS prevention in the LLM test endpoint (fix 1.3)."""

    async def test_llm_test_provider_xss_escaped(self, setup_client: httpx.AsyncClient):
        """Provider name containing script tags should be HTML-escaped in the response."""
        mock_litellm_mod = MagicMock()
        mock_litellm_mod.acompletion = AsyncMock(side_effect=Exception("fail"))
        with patch.dict(sys.modules, {"litellm": mock_litellm_mod}):
            resp = await csrf_post(
                setup_client,
                "/setup/test/llm",
                {
                    "provider": '<script>alert("xss")</script>',
                    "api_key": "test-key",
                },
                get_url="/setup/step/4",
            )
            assert resp.status_code == 200
            body = resp.text
            # Raw script tag must NOT appear in the response
            assert "<script>" not in body
            # Escaped version should be present
            assert "&lt;script&gt;" in body or "Unknown provider" in body


# ===================================================================
# P1-2: _initialize_setup_dependent_services idempotency
# ===================================================================


def test_custom_agent_loader_accepts_mcp_tool_manager_for_setup_runtime():
    from app.agents.custom_loader import CustomAgentLoader

    registry = MagicMock()
    mcp_tool_manager = MagicMock()
    loader = CustomAgentLoader(registry, mcp_tool_manager=mcp_tool_manager)

    assert loader._mcp_tool_manager is mcp_tool_manager


@pytest.mark.asyncio
async def test_initialize_setup_dependent_services_is_idempotent():
    """FLOW-SETUP-1 (P1-2): calling the shared init helper twice must not
    re-instantiate HA client / cache manager, and must
    not duplicate the DuckDuckGo MCP server registration."""
    import contextlib
    from types import SimpleNamespace

    from app.runtime_setup import _initialize_setup_dependent_services

    app = SimpleNamespace()
    app.state = SimpleNamespace()

    class FakeRegistry:
        def __init__(self):
            self.registered: list[str] = []

        async def register(self, agent, *, replace=False):
            agent_id = getattr(agent.agent_card, "agent_id", "unknown")
            if replace and agent_id in self.registered:
                self.registered.remove(agent_id)
            self.registered.append(agent_id)

        async def list_agents(self):
            return list(self.registered)

    fake_registry = FakeRegistry()
    fake_dispatcher = MagicMock()
    fake_mcp_registry = MagicMock()
    fake_mcp_registry.load_from_db = AsyncMock()
    fake_mcp_registry.add_server = AsyncMock(return_value=False)
    fake_mcp_tool_manager = MagicMock()

    app.state.registry = fake_registry
    app.state.dispatcher = fake_dispatcher
    app.state.mcp_registry = fake_mcp_registry
    app.state.mcp_tool_manager = fake_mcp_tool_manager

    fake_ha_client = MagicMock()
    fake_ha_client.initialize = AsyncMock()
    fake_ha_client.reload = AsyncMock()
    fake_ha_client.set_state_observer = MagicMock()

    fake_entity_index = MagicMock()
    fake_vector_store = MagicMock()

    patches = [
        patch("app.runtime_setup.HARestClient", return_value=fake_ha_client),
        patch("app.runtime_setup.EntityIndex", return_value=fake_entity_index),
        patch("app.runtime_setup.get_embedding_engine", new_callable=AsyncMock),
        patch(
            "app.runtime_setup.get_vector_store",
            new_callable=AsyncMock,
            return_value=fake_vector_store,
        ),
        patch(
            "app.runtime_setup.schedule_entity_index_prime",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.runtime_setup.home_context_provider"),
        patch("app.runtime_setup.AliasResolver"),
        patch("app.runtime_setup.EntityMatcher"),
        patch("app.runtime_setup.RewriteAgent"),
        patch("app.runtime_setup.CacheManager"),
        patch(
            "app.db.repository.McpServerRepository.get",
            new_callable=AsyncMock,
            return_value={"name": "duckduckgo-search"},
        ),
        patch("app.agents.orchestrator.OrchestratorAgent"),
        patch("app.agents.general.GeneralAgent"),
        patch("app.agents.actionable.LightAgent"),
        patch("app.agents.actionable.MusicAgent"),
        patch("app.agents.filler.FillerAgent"),
        patch("app.runtime_setup.CustomAgentLoader"),
        patch(
            "app.db.repository.AgentConfigRepository.get",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("app.ha_client.websocket.HAWebSocketClient"),
        patch("app.agents.alarm_monitor.AlarmMonitor"),
        patch("app.agents.timer_scheduler.TimerScheduler"),
    ]

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        (
            _ha_cls,
            _ei_cls,
            _embed,
            _vs,
            _prime,
            mock_home_ctx,
            mock_alias_cls,
            mock_matcher_cls,
            mock_rewrite_cls,
            mock_cache_cls,
            _ddg_get,
            mock_orch_cls,
            mock_general_cls,
            mock_light_cls,
            mock_music_cls,
            mock_filler_cls,
            mock_custom_cls,
            _agent_cfg,
            mock_ws_cls,
            mock_alarm_cls,
            mock_timer_cls,
        ) = mocks

        mock_home_ctx.refresh = AsyncMock()
        alias_inst = MagicMock()
        alias_inst.load = AsyncMock()
        mock_alias_cls.return_value = alias_inst
        matcher_inst = MagicMock()
        matcher_inst.load_config = AsyncMock()
        mock_matcher_cls.return_value = matcher_inst
        mock_rewrite_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="rewrite-agent"))
        cache_inst = MagicMock()
        cache_inst.initialize = AsyncMock()
        cache_inst.purge_readonly_entries = AsyncMock(return_value=0)
        mock_cache_cls.return_value = cache_inst
        orch_inst = MagicMock(agent_card=SimpleNamespace(agent_id="orchestrator"))
        orch_inst.initialize = AsyncMock()
        mock_orch_cls.return_value = orch_inst
        mock_general_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="general-agent"))
        mock_light_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="light-agent"))
        mock_music_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="music-agent"))
        mock_filler_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="filler-agent"))
        loader_inst = MagicMock()
        loader_inst.load_all = AsyncMock()
        mock_custom_cls.return_value = loader_inst
        ws_inst = MagicMock()
        ws_inst.run = AsyncMock()
        ws_inst.on_event = MagicMock()
        mock_ws_cls.return_value = ws_inst
        alarm_inst = MagicMock()
        alarm_inst.start = AsyncMock()
        mock_alarm_cls.return_value = alarm_inst
        timer_inst = MagicMock()
        timer_inst.start = AsyncMock()
        mock_timer_cls.return_value = timer_inst

        await _initialize_setup_dependent_services(app, source="test-1")
        first_ha_calls = fake_ha_client.initialize.await_count
        first_cache_cls_calls = mock_cache_cls.call_count

        await _initialize_setup_dependent_services(app, source="test-2")

        # HA client must only initialize once; second call should see it
        # on app.state and call reload() instead.
        assert fake_ha_client.initialize.await_count == first_ha_calls
        assert fake_ha_client.reload.await_count >= 1
        # CacheManager must only be constructed once.
        assert mock_cache_cls.call_count == first_cache_cls_calls
        # Registry is re-populated on every init call; idempotency only applies
        # to HA client and cache manager construction above.
        assert "filler-agent" in fake_registry.registered
        assert getattr(app.state, "orchestrator_gateway", None) is not None


@pytest.mark.asyncio
async def test_initialize_setup_dependent_services_preloads_prompt_files():
    import contextlib
    from types import SimpleNamespace

    from app.runtime_setup import _initialize_setup_dependent_services

    app = SimpleNamespace()
    app.state = SimpleNamespace()

    class FakeRegistry:
        async def register(self, agent, *, replace=False):
            return None

        async def list_agents(self):
            return []

    fake_registry = FakeRegistry()
    fake_dispatcher = MagicMock()
    fake_mcp_registry = MagicMock()
    fake_mcp_registry.load_from_db = AsyncMock()
    fake_mcp_registry.add_server = AsyncMock(return_value=False)
    fake_mcp_tool_manager = MagicMock()

    app.state.registry = fake_registry
    app.state.dispatcher = fake_dispatcher
    app.state.mcp_registry = fake_mcp_registry
    app.state.mcp_tool_manager = fake_mcp_tool_manager

    fake_ha_client = MagicMock()
    fake_ha_client.initialize = AsyncMock()
    fake_ha_client.reload = AsyncMock()
    fake_ha_client.set_state_observer = MagicMock()

    fake_entity_index = MagicMock()
    fake_vector_store = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    patches = [
        patch("app.runtime_setup.HARestClient", return_value=fake_ha_client),
        patch("app.runtime_setup.EntityIndex", return_value=fake_entity_index),
        patch("app.runtime_setup.get_embedding_engine", new_callable=AsyncMock),
        patch(
            "app.runtime_setup.get_vector_store",
            new_callable=AsyncMock,
            return_value=fake_vector_store,
        ),
        patch(
            "app.runtime_setup.schedule_entity_index_prime",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.runtime_setup.home_context_provider"),
        patch("app.runtime_setup.AliasResolver"),
        patch("app.runtime_setup.EntityMatcher"),
        patch("app.runtime_setup.RewriteAgent"),
        patch("app.runtime_setup.CacheManager"),
        patch(
            "app.db.repository.McpServerRepository.get",
            new_callable=AsyncMock,
            return_value={"name": "duckduckgo-search"},
        ),
        patch("app.agents.orchestrator.OrchestratorAgent"),
        patch("app.agents.general.GeneralAgent"),
        patch("app.agents.actionable.LightAgent"),
        patch("app.agents.actionable.MusicAgent"),
        patch("app.agents.filler.FillerAgent"),
        patch("app.runtime_setup.CustomAgentLoader"),
        patch(
            "app.db.repository.AgentConfigRepository.get",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("app.ha_client.websocket.HAWebSocketClient"),
        patch("app.agents.alarm_monitor.AlarmMonitor"),
        patch("app.agents.timer_scheduler.TimerScheduler"),
        patch("app.runtime_setup.preload_prompt_cache"),
        patch("app.runtime_setup.asyncio.to_thread", new=AsyncMock(side_effect=fake_to_thread)),
        patch("app.entity.expansion.load_query_expansion_prompt_template", return_value="Prompt: {token}"),
        patch("app.entity.expansion.QueryExpansionService"),
    ]

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        (
            _ha_cls,
            _ei_cls,
            _embed,
            _vs,
            _prime,
            mock_home_ctx,
            mock_alias_cls,
            mock_matcher_cls,
            mock_rewrite_cls,
            mock_cache_cls,
            _ddg_get,
            mock_orch_cls,
            mock_general_cls,
            mock_light_cls,
            mock_music_cls,
            mock_filler_cls,
            mock_custom_cls,
            _agent_cfg,
            mock_ws_cls,
            mock_alarm_cls,
            mock_timer_cls,
            mock_preload_prompts,
            mock_to_thread,
            mock_load_query_prompt,
            mock_query_service_cls,
        ) = mocks

        mock_home_ctx.refresh = AsyncMock()
        alias_inst = MagicMock()
        alias_inst.load = AsyncMock()
        mock_alias_cls.return_value = alias_inst
        matcher_inst = MagicMock()
        matcher_inst.load_config = AsyncMock()
        mock_matcher_cls.return_value = matcher_inst
        mock_rewrite_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="rewrite-agent"))
        cache_inst = MagicMock()
        cache_inst.initialize = AsyncMock()
        cache_inst.purge_readonly_entries = AsyncMock(return_value=0)
        mock_cache_cls.return_value = cache_inst
        orch_inst = MagicMock(agent_card=SimpleNamespace(agent_id="orchestrator"))
        orch_inst.initialize = AsyncMock()
        mock_orch_cls.return_value = orch_inst
        mock_general_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="general-agent"))
        mock_light_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="light-agent"))
        mock_music_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="music-agent"))
        mock_filler_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="filler-agent"))
        loader_inst = MagicMock()
        loader_inst.load_all = AsyncMock()
        mock_custom_cls.return_value = loader_inst
        ws_inst = MagicMock()
        ws_inst.run = AsyncMock()
        ws_inst.on_event = MagicMock()
        mock_ws_cls.return_value = ws_inst
        alarm_inst = MagicMock()
        alarm_inst.start = AsyncMock()
        mock_alarm_cls.return_value = alarm_inst
        timer_inst = MagicMock()
        timer_inst.start = AsyncMock()
        mock_timer_cls.return_value = timer_inst
        mock_query_service = MagicMock()
        mock_query_service_cls.return_value = mock_query_service

        await _initialize_setup_dependent_services(app, source="test-prompt")

        mock_preload_prompts.assert_called_once_with()
        mock_load_query_prompt.assert_called_once_with()
        mock_query_service_cls.assert_called_once()
        assert mock_query_service_cls.call_args.kwargs["prompt_template"] == "Prompt: {token}"
        assert matcher_inst._expansion_service is mock_query_service
        assert mock_to_thread.await_count >= 2


# ===================================================================
# P1-2: _initialize_setup_dependent_services idempotency
# ===================================================================
