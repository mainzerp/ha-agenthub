"""Tests for app.api.routes.traces_api."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import aiosqlite
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
class TestTraceFilterParity:
    """List/export parity and validation through the real routes and database."""

    async def test_list_export_parity_and_422_parity(self, db_repository):
        from app.db.repository import TraceSummaryRepository

        rows = [
            ("same-day", "open kitchen", "conv-day", "light-agent", "review", "2026-09-17 08:00:00"),
            ("conv-only", "other request", "conv-day", "media-agent", "other", "2026-09-17 09:00:00"),
            ("next-day", "open kitchen", "conv-other", "light-agent", "review", "2026-09-18 08:00:00"),
        ]
        for trace_id, user_input, conversation, agent, label, _stamp in rows:
            await TraceSummaryRepository.create(
                {
                    "trace_id": trace_id,
                    "user_input": user_input,
                    "conversation_id": conversation,
                    "routing_agent": agent,
                    "label": label,
                }
            )
        async with aiosqlite.connect(str(db_repository)) as db:
            await db.executemany(
                "UPDATE trace_summary SET created_at = ? WHERE trace_id = ?",
                [(row[5], row[0]) for row in rows],
            )
            await db.commit()

        async for client in _client_for(_build_app()):
            filters = {
                "search": "conv-day",
                "agent": "light-agent",
                "label": "review",
                "from": "2026-09-17",
                "to": "2026-09-17",
            }
            resp = await client.get("/api/admin/traces", params=filters)
            assert resp.status_code == 200
            body = resp.json()
            assert [t["trace_id"] for t in body["traces"]] == ["same-day"]
            assert body["total"] == 1 and body["pages"] == 1
            exported = await client.get("/api/admin/traces/export", params=filters)
            assert exported.status_code == 200
            trace_ids = [row["Trace ID"] for row in csv.DictReader(io.StringIO(exported.text))]
            assert trace_ids == ["same-day"]
            for endpoint in ("/api/admin/traces", "/api/admin/traces/export"):
                for value in ("2026-02-30", "9999-12-31"):
                    resp = await client.get(endpoint, params={"to": value})
                    assert resp.status_code == 422, (endpoint, value, resp.text)
                for value in ("2026-09-17 12:00:00", "2026-09-17T12:00:00+02:00", "2026-2-30", "arbitrary"):
                    resp = await client.get(endpoint, params={"from": value, "to": value})
                    assert resp.status_code == 200, (endpoint, value, resp.text)
            assert (await client.get("/api/admin/traces")).status_code == 200
            assert (
                await client.get("/api/admin/traces", params={"from": "2026-09-18", "to": "2026-09-17"})
            ).status_code == 200

    async def test_list_timestamp_client_boundary_semantics(self, db_repository):
        from app.db.repository import TraceSummaryRepository

        await TraceSummaryRepository.create({"trace_id": "boundary", "user_input": "boundary"})
        async with aiosqlite.connect(str(db_repository)) as db:
            await db.execute("UPDATE trace_summary SET created_at = '2026-09-17 12:00:00'")
            await db.commit()
        async for client in _client_for(_build_app()):
            assert (await client.get("/api/admin/traces", params={"to": "2026-09-17 12:00:00"})).json()["total"] == 1
            assert (await client.get("/api/admin/traces", params={"to": "2026-09-17 12:00:01"})).json()["total"] == 1
            assert (await client.get("/api/admin/traces", params={"to": "2026-09-18"})).json()["total"] == 1
            assert (await client.get("/api/admin/traces", params={"to": "2026-09-17"})).json()["total"] == 1


@pytest.mark.asyncio
class TestBuildResponse:
    async def test_build_response_all_branches(self):
        """_build_response covers every span_name branch."""
        # Mapped keys
        assert _build_response("return", {"final_response": "hello"}) == "hello"
        assert _build_response("rewrite", {"rewritten_text": "hi"}) == "hi"
        assert (
            _build_response("rewrite", {"success": False, "fallback": "cached_response"})
            == "Rewrite failed; cached response returned"
        )
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

        # memory_retrieval
        assert (
            _build_response("memory_retrieval", {"match_count": 2, "top_similarity": 0.91})
            == "2 session match(es), top sim 0.91"
        )
        assert _build_response("memory_retrieval", {"match_count": 0}) == "no session match"
        assert _build_response("memory_retrieval", {"match_count": 1, "abandoned": True}) == (
            "1 session match(es) (abandoned)"
        )
        assert _build_response("memory_retrieval", {"match_count": 0, "timed_out": True}) == (
            "no session match (timed out)"
        )

        # entity_resolution
        assert _build_response("entity_resolution", {"resolved_count": 3, "resolve_ms": 12}) == (
            "3 entities resolved (12ms)"
        )
        assert _build_response("entity_resolution", {"resolved_count": 3}) == "3 entities resolved"
        assert _build_response("entity_resolution", {}) == ""
        assert (
            _build_response("entity_resolution", {"resolved_count": 0, "resolve_ms": 5, "recall_count": 4})
            == "0 entities resolved (5ms), 4 candidate(s) recalled"
        )
        assert _build_response("entity_resolution", {"recall_count": 0}) == "0 candidate(s) recalled"

        # entity_validate
        assert (
            _build_response("entity_validate", {"entity_id": "light.couch", "resolution_path": "llm_entity_id"})
            == "accepted: light.couch"
        )
        assert (
            _build_response(
                "entity_validate", {"entity_id": "light.gibts_nicht", "resolution_path": "rejected_entity_id"}
            )
            == "rejected: light.gibts_nicht"
        )
        assert _build_response("entity_validate", {}) == ""

        # entity_match
        assert _build_response("entity_match", {"entity_id": "light.couch"}) == "light.couch"
        assert _build_response("entity_match", {"match_count": 2, "resolution_path": "cache"}) == (
            "2 match(es) via cache"
        )
        assert _build_response("entity_match", {}) == ""

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


@pytest.mark.asyncio
class TestCacheHitVisibility:
    """The traces API must surface cache-hit status in both list and detail."""

    @staticmethod
    async def _seed(trace_id: str, cache_hit_type: str | None) -> None:
        from app.db.repository import TraceSpanRepository, TraceSummaryRepository

        await TraceSummaryRepository.create(
            {
                "trace_id": trace_id,
                "conversation_id": f"conv-{trace_id}",
                "user_input": "turn on the kitchen light",
                "final_response": "Done.",
                "agents": ["light-agent"],
                "source": "ws",
                "routing_agent": "light-agent",
                "routing_confidence": 1.0,
                "cache_hit_type": cache_hit_type,
            }
        )
        now = datetime.now(UTC).isoformat()
        if cache_hit_type == "action_hit":
            # Action hit: return span carries action_cache_hit; no classify span.
            await TraceSpanRepository.insert(
                trace_id, "cache_lookup", now, 1.0, agent_id="orchestrator", metadata={"hit_type": "action_hit"}
            )
            await TraceSpanRepository.insert(
                trace_id,
                "return",
                now,
                2.0,
                agent_id="orchestrator",
                metadata={"action_cache_hit": True, "response_cache_hit": False, "from_agent": "light-agent"},
            )
        else:
            # Routing hit: classify span carries routing_cached=True.
            await TraceSpanRepository.insert(
                trace_id, "cache_lookup", now, 1.0, agent_id="orchestrator", metadata={"hit_type": "routing_hit"}
            )
            await TraceSpanRepository.insert(
                trace_id,
                "classify",
                now,
                3.0,
                agent_id="orchestrator",
                metadata={"routing_cached": True, "target_agent": "light-agent"},
            )

    async def test_list_surfaces_cache_hit_type(self, db_repository):
        await self._seed("trace-action", "action_hit")
        await self._seed("trace-routing", "routing_hit")

        app = _build_app()
        async for client in _client_for(app):
            resp = await client.get("/api/admin/traces")
            assert resp.status_code == 200
            rows = {r["trace_id"]: r for r in resp.json()["traces"]}
            assert rows["trace-action"]["cache_hit_type"] == "action_hit"
            assert rows["trace-routing"]["cache_hit_type"] == "routing_hit"

    async def test_detail_surfaces_action_cache_hit(self, db_repository):
        await self._seed("trace-action", "action_hit")

        app = _build_app()
        async for client in _client_for(app):
            resp = await client.get("/api/admin/traces/trace-action")
            assert resp.status_code == 200
            body = resp.json()
            assert body["cache_hit_type"] == "action_hit"
            assert body["routing"]["action_cache_hit"] is True

    async def test_detail_surfaces_routing_cached(self, db_repository):
        await self._seed("trace-routing", "routing_hit")

        app = _build_app()
        async for client in _client_for(app):
            resp = await client.get("/api/admin/traces/trace-routing")
            assert resp.status_code == 200
            body = resp.json()
            assert body["cache_hit_type"] == "routing_hit"
            assert body["routing_cached"] is True


@pytest.mark.asyncio
class TestAgentExecutionsPreludeSpans:
    """Prelude/lookup spans with an agent_id must appear in agent_executions."""

    async def test_detail_includes_prelude_spans_in_agent_executions(self, db_repository):
        from app.db.repository import TraceSpanRepository, TraceSummaryRepository

        await TraceSummaryRepository.create(
            {
                "trace_id": "trace-prelude",
                "conversation_id": "conv-trace-prelude",
                "user_input": "turn on the kitchen light",
                "final_response": "Done.",
                "agents": ["light-agent"],
                "source": "ws",
                "routing_agent": "light-agent",
                "routing_confidence": 1.0,
            }
        )
        now = datetime.now(UTC).isoformat()
        await TraceSpanRepository.insert(
            "trace-prelude", "memory_retrieval", now, 5.0, agent_id="orchestrator", metadata={"match_count": 1}
        )
        await TraceSpanRepository.insert(
            "trace-prelude",
            "entity_resolution",
            now,
            4.0,
            agent_id="light-agent",
            metadata={"resolved_count": 2, "resolve_ms": 4},
        )

        app = _build_app()
        async for client in _client_for(app):
            resp = await client.get("/api/admin/traces/trace-prelude")
            assert resp.status_code == 200
            executions = resp.json()["agent_executions"]
            names = {e["span_name"] for e in executions}
            assert {"memory_retrieval", "entity_resolution"} <= names


@pytest.mark.asyncio
class TestTraceIdentity:
    """Trace detail exposes identity_source and the speaker outcome; list carries user_name."""

    _HA_USER = "0123456789abcdef0123456789abcdef"
    _VOICE_USER = "fedcba9876543210fedcba9876543210"

    @staticmethod
    async def _seed(trace_id: str, root_metadata: dict, speaker_metadata: dict | None, **summary) -> None:
        """Record spans through a SpanCollector so the flush sanitizer runs."""
        from app.analytics.tracer import SpanCollector, create_trace_summary

        await create_trace_summary(
            trace_id,
            f"conv-{trace_id}",
            "hello",
            "Hi.",
            "general-agent",
            0.9,
            5.0,
            "hello",
            ["orchestrator"],
            "ha",
            **summary,
        )
        collector = SpanCollector(trace_id, source="ha")
        for key, value in root_metadata.items():
            collector.set_request_attribute(key, value)
        now = datetime.now(UTC).isoformat()
        collector.record_root_span(
            {
                "span_id": "root0000000a",
                "trace_id": trace_id,
                "span_name": "conversation",
                "agent_id": None,
                "parent_span": None,
                "start_time": now,
                "end_time": now,
                "duration_ms": 10.0,
                "status": "ok",
                "metadata": {},
            }
        )
        if speaker_metadata is not None:
            async with collector.start_span("speaker_id", agent_id="orchestrator", parent_span="root0000000a") as span:
                span["metadata"].update(speaker_metadata)
        await collector.flush()

    async def test_voice_only_turn(self, db_repository):
        await self._seed(
            "t-voice",
            {"user_id": None, "identity_source": "voice"},
            {
                "status": "identified",
                "profile_id": 3,
                "score": 0.82,
                "margin": 0.2,
                "duration_s": 2.1,
                "waited_ms": 4.0,
                "speaker_name": "Ben",
                "speaker_ha_user_id": self._VOICE_USER,
            },
            user_name="Ben",
            user_name_source="voice",
            identity_source="voice",
        )
        async for client in _client_for(_build_app()):
            body = (await client.get("/api/admin/traces/t-voice")).json()
            assert body["identity_source"] == "voice"
            assert body["user_id"] is None
            assert body["user_name"] == "Ben"
            assert body["user_name_source"] == "voice"
            assert body["speaker"] == {
                "status": "identified",
                "speaker_name": "Ben",
                "profile_id": 3,
                "score": 0.82,
                "margin": 0.2,
                "duration_s": 2.1,
                "speaker_ha_user_id": self._VOICE_USER,
            }
            rows = (await client.get("/api/admin/traces")).json()["traces"]
            row = next(r for r in rows if r["trace_id"] == "t-voice")
            assert row["user_name"] == "Ben" and row["identity_source"] == "voice"
            assert row["user_name_source"] == "voice"

    async def test_csv_export_user_column(self, db_repository):
        await self._seed("t-csv-ha", {"identity_source": "ha"}, None, user_name="Anna", user_name_source="ha")
        await self._seed("t-csv-none", {"identity_source": "none"}, None)
        async for client in _client_for(_build_app()):
            resp = await client.get("/api/admin/traces/export")
            assert resp.status_code == 200
            rows = list(csv.reader(io.StringIO(resp.text)))
            header = rows[0]
            assert header[-1] == "User"
            by_trace = {row[header.index("Trace ID")]: row for row in rows[1:]}
            assert by_trace["t-csv-ha"][-1] == "Anna"
            assert by_trace["t-csv-none"][-1] == ""

    async def test_ha_only_turn(self, db_repository):
        await self._seed(
            "t-ha",
            {
                "user_id": self._HA_USER,
                "identity_source": "ha",
                "requester_person": "person.anna",
                "requester_name": "Anna",
            },
            None,
            user_name="Anna",
            identity_source="ha",
        )
        async for client in _client_for(_build_app()):
            body = (await client.get("/api/admin/traces/t-ha")).json()
            assert body["identity_source"] == "ha"
            assert body["user_id"] == self._HA_USER
            assert body["requester_name"] == "Anna"
            assert body["requester_person"] == "person.anna"
            assert body["speaker"] is None
            resp = await client.get("/api/admin/traces", params={"search": "Anna"})
            assert [r["trace_id"] for r in resp.json()["traces"]] == ["t-ha"]

    async def test_no_identity(self, db_repository):
        await self._seed(
            "t-none",
            {"user_id": None, "identity_source": "none"},
            {"status": "unknown", "profile_id": None, "score": 0.3, "margin": 0.01, "duration_s": 1.9},
            identity_source="none",
        )
        async for client in _client_for(_build_app()):
            body = (await client.get("/api/admin/traces/t-none")).json()
            assert body["identity_source"] == "none"
            assert body["user_id"] is None and body["user_name"] is None
            assert body["speaker"]["status"] == "unknown"
            assert body["speaker"]["speaker_name"] is None
            assert body["speaker"]["speaker_ha_user_id"] is None

    async def test_memory_span_payload_exposes_memories(self, db_repository):
        from app.analytics.tracer import SpanCollector

        await self._seed("t-mem", {"identity_source": "ha"}, None, identity_source="ha")
        collector = SpanCollector("t-mem", source="ha")
        async with collector.start_span("memory_retrieval", agent_id="orchestrator") as span:
            span["metadata"].update(
                {
                    "match_count": 1,
                    "top_similarity": 0.91,
                    "matches_attached": True,
                    "scope": "user",
                    "search_user_id": self._HA_USER,
                    "memories": [
                        {
                            "conversation_id": "conv-old",
                            "session_id": 7,
                            "turn_id": 41,
                            "similarity": 0.91,
                            "last_turn_at": 1754400000,
                            "snippet": "what about the timers",
                            "continuation_count": 2,
                            "attached": True,
                        }
                    ],
                }
            )
        await collector.flush()
        async for client in _client_for(_build_app()):
            body = (await client.get("/api/admin/traces/t-mem")).json()
            span = next(s for s in body["spans"] if s["span_name"] == "memory_retrieval")
            assert span["metadata"]["search_user_id"] == self._HA_USER
            assert span["metadata"]["memories"][0]["turn_id"] == 41
            assert span["metadata"]["memories"][0]["snippet"] == "what about the timers"
            execution = next(e for e in body["agent_executions"] if e["span_name"] == "memory_retrieval")
            assert execution["response"] == "1 session match(es), top sim 0.91, attached [scope user]"
