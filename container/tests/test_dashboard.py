"""Integration tests for dashboard routes.

Tests login-required behavior, page accessibility with session, and basic
template rendering.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL
from tests.conftest import build_integration_test_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_dashboard_app(*, override_session: bool = True):
    """Build a FastAPI app for dashboard integration tests."""
    registry = MagicMock()
    registry.list_agents = AsyncMock(return_value=[])
    return build_integration_test_app(
        setup_complete=True,
        override_api_key=override_session,
        override_admin_session=override_session,
        registry=registry,
    )


@pytest_asyncio.fixture()
async def dashboard_client(db_repository):
    """Client with admin session authentication overridden."""
    app = _build_dashboard_app(override_session=True)
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


@pytest_asyncio.fixture()
async def no_session_client(db_repository):
    """Client WITHOUT session auth overrides (for login-required tests)."""
    app = _build_dashboard_app(override_session=False)
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
# Login required (redirect)
# ===================================================================


@pytest.mark.integration
class TestDashboardLoginRequired:
    async def test_dashboard_index_requires_auth(self, no_session_client: httpx.AsyncClient):
        resp = await no_session_client.get("/dashboard/")
        # The require_admin_session_redirect raises HTTPException with 303
        # which gets handled by the exception handler as a JSON response
        # with Location header
        assert resp.status_code == 303
        assert "/dashboard/login" in resp.headers.get("location", "")

    async def test_agents_page_requires_auth(self, no_session_client: httpx.AsyncClient):
        resp = await no_session_client.get("/dashboard/agents")
        assert resp.status_code == 303

    async def test_logs_page_requires_auth(self, no_session_client: httpx.AsyncClient):
        resp = await no_session_client.get("/dashboard/logs")
        assert resp.status_code == 303

    async def test_login_page_accessible_without_session(self, no_session_client: httpx.AsyncClient):
        resp = await no_session_client.get("/dashboard/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_admin_api_returns_session_expired_without_session(self, no_session_client: httpx.AsyncClient):
        resp = await no_session_client.get("/api/admin/health/extended")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Session expired"


# ===================================================================
# Page accessibility with session
# ===================================================================


@pytest.mark.integration
class TestDashboardPageAccessibility:
    async def test_dashboard_index(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_agents_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/agents")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_system_health_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/system-health")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_mcp_servers_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/mcp-servers")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_entity_visibility_redirects(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/entity-visibility", follow_redirects=False)
        assert resp.status_code == 301
        assert "/dashboard/entity-index" in resp.headers.get("location", "")

    async def test_entity_visibility_redirect_preserves_agent(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/entity-visibility?agent=light-agent", follow_redirects=False)
        assert resp.status_code == 301
        assert "agent=light-agent" in resp.headers.get("location", "")

    async def test_analytics_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/analytics")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_plugins_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/plugins")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_settings_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/settings")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert DEFAULT_LOCAL_EMBEDDING_MODEL in resp.text

    async def test_timers_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/timers")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_logs_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/logs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# ===================================================================
# Template rendering content checks
# ===================================================================


@pytest.mark.integration
class TestDashboardTemplateRendering:
    async def test_login_page_contains_form(self, no_session_client: httpx.AsyncClient):
        resp = await no_session_client.get("/dashboard/login")
        html = resp.text
        assert "<form" in html.lower() or "form" in html.lower()

    async def test_logout_clears_session(self, dashboard_client: httpx.AsyncClient):
        page = await dashboard_client.get("/dashboard/agents")
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert match
        token = match.group(1)
        assert token == dashboard_client.cookies.get("agent_assist_csrf")

        resp = await dashboard_client.post("/dashboard/logout", data={"csrf_token": token})
        assert resp.status_code == 303
        assert "/dashboard/login" in resp.headers.get("location", "")

    async def test_logout_get_method_not_allowed(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/logout")
        assert resp.status_code == 405

    async def test_logout_post_without_csrf_rejected(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.post("/dashboard/logout")
        assert resp.status_code == 401

    async def test_dashboard_base_alpine_fallback_asset_exists(self, dashboard_client: httpx.AsyncClient):
        alpine_path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "dashboard"
            / "static"
            / "vendor"
            / "alpine"
            / "alpine-3.14.1.min.js"
        )
        content = alpine_path.read_text(encoding="utf-8")
        assert alpine_path.is_file()
        assert alpine_path.stat().st_size > 0
        assert re.search(r"Alpine\.js v3\.\d+\.\d+", content)

    async def test_dashboard_base_renders_alpine_missing_banner_handler(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/agents")
        html = resp.text
        assert "alpine-3.14.1.min.js" in html

    async def test_system_health_page_includes_dashboard_helper(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/system-health")
        html = resp.text
        assert "window.dashboardApi" in html
        assert "dashboardApi.safeJson('/api/admin/health/extended')" in html

    async def test_agents_page_includes_dashboard_helper(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/agents")
        html = resp.text
        assert "window.dashboardApi" in html
        assert "dashboardApi.json('/api/admin/agents')" in html
        assert "agent._actionError" in html
        assert "agent._promptSaved" in html

    async def test_dashboard_sidebar_toggle_has_accessibility_attributes(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/agents")
        html = resp.text
        assert 'aria-controls="dashboard-sidebar"' in html
        assert "x-bind:aria-expanded=\"sidebarOpen ? 'true' : 'false'\"" in html
        assert 'id="dashboard-sidebar"' in html

    async def test_sidebar_inert_binding(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/agents")
        html = resp.text
        assert ':inert="sidebarInert ? true : null"' in html
        assert ":aria-hidden=\"sidebarInert ? 'true' : 'false'\"" in html

    async def test_send_devices_page_has_labels_and_live_region(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/send-devices")
        html = resp.text
        assert 'for="send-device-display-name"' in html
        assert 'id="send-device-display-name"' in html
        assert 'for="send-device-type"' in html
        assert 'id="send-device-type"' in html
        assert 'for="send-device-target-select"' in html
        assert 'id="send-device-target-manual"' in html
        assert '@submit.prevent="createMapping()"' in html
        assert "window.toast" in html

    async def test_send_devices_page_uses_dashboard_api_helper(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/send-devices")
        html = resp.text
        assert "window.dashboardApi.safeJson('/api/admin/send-devices')" in html
        assert "await fetch('/api/admin/send-devices'" not in html

    async def test_mcp_servers_page_only_offers_supported_transports(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/mcp-servers")
        html = resp.text
        assert '<option value="stdio">stdio</option>' in html
        assert '<option value="sse">sse</option>' in html
        assert '<option value="http">' not in html

    @pytest.mark.integration
    async def test_dashboard_respects_root_path(self, db_repository):
        app = _build_dashboard_app(override_session=True)
        with patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = httpx.ASGITransport(app=app, root_path="/proxy")
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=False,
            ) as client:
                resp = await client.get("/dashboard/agents")
                assert resp.status_code == 200
                html = resp.text
                assert 'href="/proxy/dashboard/static/favicon.svg"' in html
                assert 'href="/proxy/dashboard/chat"' in html
                assert 'action="/proxy/dashboard/logout"' in html
                assert "rootPath: '/proxy'" in html
                assert "loginUrl: '/proxy/dashboard/login'" in html

                login_resp = await client.get("/dashboard/login")
                assert login_resp.status_code == 200
                assert 'action="/proxy/dashboard/login"' in login_resp.text

    @pytest.mark.integration
    async def test_login_redirect_respects_root_path(self, db_repository):
        app = _build_dashboard_app(override_session=False)
        with patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = httpx.ASGITransport(app=app, root_path="/proxy")
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=False,
            ) as client:
                resp = await client.get("/dashboard/agents")
                assert resp.status_code == 303
                assert resp.headers.get("location") == "/proxy/dashboard/login"

    async def test_entity_index_diagnostics_in_english(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/entity-index")
        html = resp.text
        assert "No matches." in html
        assert "Allowed domains: " in html
        assert "In index: " in html
        assert "Likely cause: no entities of the allowed domains exist in the index." in html
        assert (
            "Likely cause: entities exist but were filtered out by visibility, recall, or confidence threshold." in html
        )
        assert "Keine Treffer." not in html
        assert "Erlaubte Domains: " not in html

    def test_orphaned_templates_removed(self):
        template_dir = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
        assert not (template_dir / "conversations.html").exists()
        assert not (template_dir / "rewrite_config.html").exists()

    def test_no_raw_fetch_in_dashboard_templates(self):
        template_dir = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
        for template_path in sorted(template_dir.glob("*.html")):
            if template_path.name == "dashboard_base.html":
                continue
            html = template_path.read_text(encoding="utf-8")
            assert "fetch(" not in html, f"raw fetch found in {template_path.name}"

    async def test_settings_page_has_live_regions(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/settings")
        html = resp.text
        assert "window.toast" in html
        assert "dashToasts" in html

    async def test_timers_page_uses_scheduler_contract_copy(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/timers")
        html = resp.text
        assert "Scheduler Timers" in html
        assert "remaining_seconds" in html
        assert "logical_name" in html
        assert "Timer Pool" not in html
        assert "Pending Delayed Tasks" not in html

    async def test_logs_page_contains_alpine_component(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/logs")
        html = resp.text
        assert "Remote Logs" in html
        assert 'x-data="logsPage()"' in html

    async def test_logs_page_uses_dashboard_api_helper(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/logs")
        html = resp.text
        assert "window.dashboardApi.safeJson('/api/admin/logs?'" in html
        assert "window.dashboardApi.request('/api/admin/logs/levels'" in html
        assert "await fetch('/api/admin/logs'" not in html

    async def test_logs_page_has_refresh_interval_cleanup(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/logs")
        html = resp.text
        assert "clearInterval(this.refreshInterval)" in html

    async def test_timers_page_origin_display_prefers_origin_label_then_fallbacks(
        self, dashboard_client: httpx.AsyncClient
    ):
        resp = await dashboard_client.get("/dashboard/timers")
        html = resp.text
        label_idx = html.find("if (timer.origin_label) return String(timer.origin_label);")
        device_idx = html.find("if (timer.origin_device_id) return `device:${timer.origin_device_id}`;")
        area_idx = html.find("if (timer.origin_area) return `area:${timer.origin_area}`;")
        assert label_idx != -1
        assert device_idx != -1
        assert area_idx != -1
        assert label_idx < device_idx
        assert device_idx < area_idx

    async def test_timers_modal_has_dialog_semantics(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/timers")
        html = resp.text
        assert 'role="dialog"' in html
        assert 'aria-modal="true"' in html
        assert 'aria-labelledby="timer-edit-modal-title"' in html
        assert '@keydown.escape.window="showEditModal=false"' in html

    def test_dashboard_x_cloak_global_rule(self):
        layout_css_path = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static" / "css" / "layout.css"
        css = layout_css_path.read_text(encoding="utf-8")
        assert "[x-cloak]" in css

    def test_dashboard_polling_templates_clear_refresh_interval(self):
        template_dir = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
        for template_name in ("overview.html", "system_health.html", "timers.html"):
            html = (template_dir / template_name).read_text(encoding="utf-8")
            assert "clearInterval(this.refreshInterval)" in html

    def test_no_btn_xs_class_in_templates(self):
        template_dir = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
        for template_path in sorted(template_dir.glob("*.html")):
            html = template_path.read_text(encoding="utf-8")
            assert "btn-xs" not in html, f"btn-xs found in {template_path.name}"


@pytest.mark.integration
class TestTimerDashboardApiContract:
    async def test_admin_timers_returns_scheduler_fields(self, dashboard_client: httpx.AsyncClient):
        app = dashboard_client._transport.app
        app.state.timer_scheduler = MagicMock()
        app.state.timer_scheduler.list = AsyncMock(
            return_value=[
                {
                    "id": "timer-1",
                    "logical_name": "kitchen timer",
                    "kind": "plain",
                    "fires_at": 9999999999,
                    "duration_seconds": 300,
                    "origin_area": "kitchen",
                    "origin_device_id": "device-1",
                    "state": "pending",
                }
            ]
        )
        app.state.ha_client.get_states = AsyncMock(return_value=[])

        resp = await dashboard_client.get("/api/admin/timers")
        assert resp.status_code == 200
        data = resp.json()
        assert "timers" in data
        assert "alarms" in data
        assert len(data["timers"]) == 1
        row = data["timers"][0]
        assert row["id"] == "timer-1"
        assert row["logical_name"] == "kitchen timer"
        assert row["kind"] == "plain"
        assert "remaining_seconds" in row
        assert row["duration_seconds"] == 300
        assert row["state"] == "pending"
        assert row["origin_area"] == "kitchen"
        assert row["origin_device_id"] == "device-1"
        assert row["origin_label"] == "device-1"
        assert "entity_id" not in row
        assert "name" not in row

    async def test_admin_timers_origin_label_uses_device_name_when_resolved(self, dashboard_client: httpx.AsyncClient):
        app = dashboard_client._transport.app
        app.state.timer_scheduler = MagicMock()
        app.state.timer_scheduler.list = AsyncMock(
            return_value=[
                {
                    "id": "timer-1",
                    "logical_name": "tea timer",
                    "kind": "plain",
                    "fires_at": 9999999999,
                    "duration_seconds": 120,
                    "origin_area": "kitchen",
                    "origin_device_id": "device-1",
                    "state": "pending",
                }
            ]
        )
        app.state.ha_client.get_states = AsyncMock(return_value=[])
        app.state.ha_client.get_area_registry = AsyncMock(return_value={"kitchen": "Kitchen"})
        app.state.ha_client.render_template = AsyncMock(return_value="Kitchen Satellite")

        resp = await dashboard_client.get("/api/admin/timers")
        assert resp.status_code == 200
        row = resp.json()["timers"][0]
        assert row["origin_label"] == "Kitchen Satellite"

    async def test_admin_timers_origin_label_falls_back_to_area_name_then_raw_id(
        self, dashboard_client: httpx.AsyncClient
    ):
        app = dashboard_client._transport.app
        app.state.timer_scheduler = MagicMock()
        app.state.timer_scheduler.list = AsyncMock(
            return_value=[
                {
                    "id": "timer-1",
                    "logical_name": "tea timer",
                    "kind": "plain",
                    "fires_at": 9999999999,
                    "duration_seconds": 120,
                    "origin_area": "kitchen",
                    "origin_device_id": None,
                    "state": "pending",
                },
                {
                    "id": "timer-2",
                    "logical_name": "pasta timer",
                    "kind": "plain",
                    "fires_at": 9999999999,
                    "duration_seconds": 240,
                    "origin_area": "attic",
                    "origin_device_id": None,
                    "state": "pending",
                },
            ]
        )
        app.state.ha_client.get_states = AsyncMock(return_value=[])
        app.state.ha_client.get_area_registry = AsyncMock(return_value={"kitchen": "Kitchen"})

        resp = await dashboard_client.get("/api/admin/timers")
        assert resp.status_code == 200
        rows = resp.json()["timers"]
        assert rows[0]["origin_label"] == "Kitchen"
        assert rows[1]["origin_label"] == "attic"

    async def test_admin_timers_alarm_sources_include_internal_and_ha_legacy(self, dashboard_client: httpx.AsyncClient):
        app = dashboard_client._transport.app
        app.state.timer_scheduler = MagicMock()
        app.state.timer_scheduler.list = AsyncMock(
            return_value=[
                {
                    "id": "alarm-1",
                    "logical_name": "Morning Alarm",
                    "kind": "alarm",
                    "fires_at": 9999999999,
                    "duration_seconds": 300,
                    "origin_area": "bedroom",
                    "origin_device_id": "device-1",
                    "state": "pending",
                }
            ]
        )
        app.state.ha_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "input_datetime.legacy_alarm",
                    "state": "07:00:00",
                    "attributes": {"friendly_name": "Legacy Alarm", "has_date": False, "has_time": True},
                }
            ]
        )

        resp = await dashboard_client.get("/api/admin/timers")
        assert resp.status_code == 200
        alarms = resp.json()["alarms"]
        assert any(a.get("source") == "internal" and a.get("name") == "Morning Alarm" for a in alarms)
        assert any(
            a.get("source") == "ha_legacy" and a.get("entity_id") == "input_datetime.legacy_alarm" for a in alarms
        )


# ===================================================================
# Personality page
# ===================================================================


@pytest.mark.integration
class TestPersonalityPage:
    async def test_personality_page_accessible(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/personality")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_personality_page_requires_auth(self, no_session_client: httpx.AsyncClient):
        resp = await no_session_client.get("/dashboard/personality")
        assert resp.status_code == 303

    async def test_get_personality_config(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/api/admin/personality/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "prompt" in data

    async def test_put_personality_config(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.put(
            "/api/admin/personality/config",
            json={"prompt": "You are Lucia, a friendly assistant."},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # Verify it persisted
        resp2 = await dashboard_client.get("/api/admin/personality/config")
        assert resp2.json()["prompt"] == "You are Lucia, a friendly assistant."

    async def test_put_personality_config_empty(self, dashboard_client: httpx.AsyncClient):
        # Set then clear
        await dashboard_client.put(
            "/api/admin/personality/config",
            json={"prompt": "Something"},
        )
        resp = await dashboard_client.put(
            "/api/admin/personality/config",
            json={"prompt": ""},
        )
        assert resp.status_code == 200
        resp2 = await dashboard_client.get("/api/admin/personality/config")
        assert resp2.json()["prompt"] == ""


@pytest.mark.integration
class TestAgentEditorFailures:
    async def test_update_agent_config_returns_json_error_on_repository_failure(
        self, dashboard_client: httpx.AsyncClient
    ):
        with patch(
            "app.api.routes.dashboard_api.AgentConfigRepository.upsert",
            new_callable=AsyncMock,
        ) as mock_upsert:
            mock_upsert.side_effect = RuntimeError("save failed")
            resp = await dashboard_client.put(
                "/api/admin/agents/light-agent",
                json={"description": "Updated description"},
            )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Failed to update agent"

    async def test_update_agent_prompt_rejects_invalid_agent_id(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.put(
            "/api/admin/agents/bad$id/prompt",
            json={"content": "Prompt text"},
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid agent ID"


# ===================================================================
# Overview extended endpoint
# ===================================================================


@pytest.mark.integration
class TestOverviewExtended:
    async def test_overview_extended_returns_all_fields(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/api/admin/overview/extended")
        assert resp.status_code == 200
        data = resp.json()

        expected_keys = {
            "recent_requests",
            "cache_hit_rate",
            "agent_count",
            "entity_count",
            "mcp_server_count",
            "avg_latency_ms",
            "total_conversations",
            "agent_distribution",
            "cache_tier",
            "request_trend",
            "recent_traces",
            "warnings",
        }
        assert expected_keys.issubset(data.keys())

        assert isinstance(data["agent_distribution"], list)
        assert isinstance(data["cache_tier"], dict)
        assert "routing_hits" in data["cache_tier"]
        assert "action_hits" in data["cache_tier"]
        assert "misses" in data["cache_tier"]
        assert isinstance(data["request_trend"], dict)
        assert "labels" in data["request_trend"]
        assert "data" in data["request_trend"]
        assert isinstance(data["recent_traces"], list)
        assert isinstance(data["warnings"], dict)
        assert "agent_timeouts" in data["warnings"]
        assert "rewrite_failures" in data["warnings"]

    async def test_overview_extended_numeric_types(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/api/admin/overview/extended")
        assert resp.status_code == 200
        data = resp.json()

        assert isinstance(data["recent_requests"], int)
        assert isinstance(data["cache_hit_rate"], (int, float))
        assert isinstance(data["agent_count"], int)
        assert isinstance(data["entity_count"], int)
        assert isinstance(data["avg_latency_ms"], (int, float))
        assert isinstance(data["total_conversations"], int)

    async def test_overview_extended_attempts_runtime_bootstrap(self, dashboard_client: httpx.AsyncClient):
        with patch(
            "app.api.routes.dashboard_api.ensure_setup_runtime_initialized",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_init:
            resp = await dashboard_client.get("/api/admin/overview/extended")
            assert resp.status_code == 200
            mock_init.assert_awaited_once()

    async def test_extended_health_reports_entity_index_building_as_warning(self, dashboard_client: httpx.AsyncClient):
        app = dashboard_client._transport.app
        app.state.ha_client.get_states = AsyncMock(return_value=[])
        entity_index = MagicMock()
        entity_index.get_stats.return_value = {
            "count": 0,
            "embedding_status": {
                "state": "building",
                "progress": 25,
                "processed": 500,
                "total": 2000,
                "error": None,
            },
        }
        app.state.entity_index = entity_index
        app.state.cache_manager = MagicMock()
        app.state.cache_manager.get_stats.return_value = {"routing": {}, "action": {}}

        resp = await dashboard_client.get("/api/admin/health/extended")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_index"]["status"] == "warning"
        assert data["entity_index"]["progress"] == 25


# ===================================================================
# Send devices API
# ===================================================================


@pytest.mark.integration
class TestSendDevicesAPI:
    async def test_list_empty(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/api/admin/send-devices")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_and_list(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.post(
            "/api/admin/send-devices",
            json={
                "display_name": "Laura Handy",
                "device_type": "notify",
                "ha_service_target": "mobile_app_lauras_iphone",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data

        resp = await dashboard_client.get("/api/admin/send-devices")
        assert resp.status_code == 200
        mappings = resp.json()
        assert len(mappings) == 1
        assert mappings[0]["display_name"] == "Laura Handy"

    async def test_create_duplicate_rejected(self, dashboard_client: httpx.AsyncClient):
        await dashboard_client.post(
            "/api/admin/send-devices",
            json={
                "display_name": "Laura Handy",
                "device_type": "notify",
                "ha_service_target": "svc_a",
            },
        )
        resp = await dashboard_client.post(
            "/api/admin/send-devices",
            json={
                "display_name": "Laura Handy",
                "device_type": "notify",
                "ha_service_target": "svc_b",
            },
        )
        assert resp.status_code == 409

    async def test_create_invalid_type(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.post(
            "/api/admin/send-devices",
            json={
                "display_name": "Test",
                "device_type": "invalid",
                "ha_service_target": "svc",
            },
        )
        assert resp.status_code == 400

    async def test_delete(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.post(
            "/api/admin/send-devices",
            json={
                "display_name": "To Delete",
                "device_type": "notify",
                "ha_service_target": "svc_del",
            },
        )
        mapping_id = resp.json()["id"]
        resp = await dashboard_client.delete(f"/api/admin/send-devices/{mapping_id}")
        assert resp.status_code == 200

        resp = await dashboard_client.get("/api/admin/send-devices")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    async def test_send_devices_page(self, dashboard_client: httpx.AsyncClient):
        resp = await dashboard_client.get("/dashboard/send-devices")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
