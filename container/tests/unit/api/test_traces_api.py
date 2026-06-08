"""Tests for app.api.routes.traces_api."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from tests.conftest import build_integration_test_app

from app.api.routes.traces_api import _build_response


def _build_app(**kwargs):
    return build_integration_test_app(
        setup_complete=True,
        override_api_key=True,
        override_admin_session=True,
        **kwargs,
    )


async def _client_for(app):
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
class TestBuildResponse:
    async def test_build_response_all_branches(self):
        """_build_response covers every span_name branch."""
        # Mapped keys
        assert _build_response("return", {"final_response": "hello"}) == "hello"
        assert _build_response("rewrite", {"rewritten_text": "hi"}) == "hi"
        assert _build_response("ha_action", {"result_speech": "done"}) == "done"
        assert _build_response("filler_generate", {"filler_text": "hold on"}) == "hold on"
        assert _build_response("filler_send", {"filler_text": "hold on"}) == "hold on"

        # mediation with all fields
        assert (
            _build_response("mediation", {"language": "de", "original_length": 10, "mediated_length": 5})
            == "Personality rewrite (de), 10 -> 5 chars"
        )
        assert _build_response("mediation", {"language": "de"}) == "Personality rewrite (de)"
        assert _build_response("mediation", {}) == "Personality rewrite"

        # mcp_tool_call
        assert _build_response("mcp_tool_call", {"tool_name": "t", "result": "x" * 200}) == f"t: {'x' * 120}..."
        assert _build_response("mcp_tool_call", {"tool_name": "t"}) == "t"
        assert _build_response("mcp_tool_call", {}) == ""

        # ha_call
        assert _build_response("ha_call", {"service": "s", "target": "e"}) == "s -> e"
        assert _build_response("ha_call", {"service": "s"}) == "s"
        assert _build_response("ha_call", {"target": "e"}) == "e"
        assert _build_response("ha_call", {}) == ""

        # fallback
        assert _build_response("dispatch", {"agent_response": " dispatched "}) == " dispatched "
        assert _build_response("unknown", {}) == ""


@pytest.mark.asyncio
class TestExportTracesAndUpdateLabel:
    async def test_export_traces_and_update_label(self, db_repository):
        """export_traces returns CSV; update_trace_label returns 200/404."""
        app = _build_app()

        with (
            patch(
                "app.api.routes.traces_api.TraceSummaryRepository.export_filtered",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "created_at": "2026-06-01T12:00:00",
                        "trace_id": "t1",
                        "conversation_id": "c1",
                        "user_input": "hi",
                        "final_response": "hello",
                        "routing_agent": "light-agent",
                        "routing_confidence": 0.95,
                        "total_duration_ms": 120,
                        "label": "test",
                        "source": "ws",
                        "agents": ["light-agent"],
                        "device_name": "Kitchen",
                        "area_name": "kitchen",
                        "voice_followup": True,
                        "conversation_turns": ["turn1"],
                    }
                ],
            ),
            patch(
                "app.api.routes.traces_api.TraceSummaryRepository.get",
                new_callable=AsyncMock,
                side_effect=lambda tid: {"trace_id": tid} if tid == "t1" else None,
            ),
            patch(
                "app.api.routes.traces_api.TraceSummaryRepository.update_label",
                new_callable=AsyncMock,
            ),
        ):
            async for client in _client_for(app):
                # Export
                resp = await client.get("/api/admin/traces/export")
                assert resp.status_code == 200
                assert resp.headers["content-type"] == "text/csv; charset=utf-8"
                body = resp.text
                assert "Timestamp" in body
                assert "t1" in body

                # Update label (exists)
                resp = await client.put("/api/admin/traces/t1/label", json={"label": "reviewed"})
                assert resp.status_code == 200
                assert resp.json()["label"] == "reviewed"

                # Update label (missing)
                resp = await client.put("/api/admin/traces/missing/label", json={"label": "reviewed"})
                assert resp.status_code == 404
