"""Trace visibility: identity (HA user / voice speaker) and loaded session memories.

Covers the ``speaker_ha_user_id`` on the ``speaker_id`` span, the resolved
requester name on the trace summary row (migration 50), and the bounded
``memories`` list / ``scope`` / ``search_user_id`` on the ``memory_retrieval``
span -- including that the trace sanitizer keeps all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

sys.modules.setdefault("litellm", MagicMock())

from app.agents.orchestrator import (  # noqa: E402
    OrchestratorAgent,
    _attach_cached_requester_person,
    _resolve_memory_context,
    _search_memory_after_speaker,
    _trace_memory_retrieval,
)
from app.analytics.tracer import SpanCollector, create_trace_summary, sanitize_trace_metadata  # noqa: E402
from app.cache.cache_manager import ActionReplayOutcome  # noqa: E402
from app.db.schema import _create_indexes, _create_tables, _seed_defaults  # noqa: E402
from app.db.schema._migrations import _run_migrations  # noqa: E402
from app.ha_client.persons import PersonDirectory  # noqa: E402
from app.memory.service import MemoryMatch  # noqa: E402
from app.models.agent import AgentCard, IngressTask, SpeakerContext, TaskContext  # noqa: E402
from app.voice import service as voice_service  # noqa: E402
from app.voice.service import SpeakerIdService, VoiceConfig, attach_speaker_to_task, make_result  # noqa: E402
from tests.helpers import make_ingress_task, shutdown_aiosqlite  # noqa: E402

# A Home Assistant user id: 32 hex chars, which the generic long-token
# pattern would redact if the key were not kept verbatim.
_HA_USER_HEX = "0123456789abcdef0123456789abcdef"
_OTHER_HEX = "fedcba9876543210fedcba9876543210"


# ---------------------------------------------------------------------------
# speaker_id span: linked HA user id
# ---------------------------------------------------------------------------


@pytest.fixture()
def speaker_service():
    svc = SpeakerIdService(engine=MagicMock(model_id="m", status="ready"))
    voice_service.set_speaker_service(svc)
    yield svc
    voice_service.set_speaker_service(None)


@pytest.fixture()
def voice_enabled():
    config = VoiceConfig(
        enabled=True,
        threshold=0.55,
        margin=0.05,
        min_duration_s=1.5,
        min_enroll_duration_s=2.0,
        wait_ms=500,
        num_threads=2,
    )
    with patch.object(voice_service, "load_voice_config", AsyncMock(return_value=config)):
        yield


def _resolve_speaker(svc: SpeakerIdService, conversation_id: str, result: dict) -> None:
    token = svc.store.begin(conversation_id)
    svc.store.resolve(conversation_id, token, result)


class TestSpeakerSpanHaUser:
    async def test_identified_span_carries_linked_ha_user_and_survives_sanitization(
        self, speaker_service, voice_enabled
    ):
        result = make_result(
            "identified", duration_s=2.4, profile_id=4, name="Anna", ha_user_id=_HA_USER_HEX, score=0.81, margin=0.3
        )
        _resolve_speaker(speaker_service, "conv-1", result)
        task = make_ingress_task("hi", "conv-1", TaskContext())
        collector = SpanCollector("trace-1", source="ha")

        await attach_speaker_to_task(task, "conv-1", collector)

        span = next(s for s in collector.get_spans() if s["span_name"] == "speaker_id")
        assert span["metadata"]["speaker_ha_user_id"] == _HA_USER_HEX
        sanitized = sanitize_trace_metadata(span["metadata"])
        assert sanitized["speaker_ha_user_id"] == _HA_USER_HEX
        assert sanitized["speaker_name"] == "Anna"

    async def test_unknown_or_unlinked_speaker_has_no_ha_user_key(self, speaker_service, voice_enabled):
        _resolve_speaker(speaker_service, "conv-2", make_result("unknown", duration_s=2.0, score=0.4, margin=0.01))
        _resolve_speaker(
            speaker_service, "conv-3", make_result("identified", duration_s=2.0, profile_id=1, name="Ben", score=0.9)
        )
        for conversation_id in ("conv-2", "conv-3"):
            task = make_ingress_task("hi", conversation_id, TaskContext())
            collector = SpanCollector(f"trace-{conversation_id}", source="ha")
            await attach_speaker_to_task(task, conversation_id, collector)
            span = next(s for s in collector.get_spans() if s["span_name"] == "speaker_id")
            assert "speaker_ha_user_id" not in span["metadata"]


class TestSanitizerKeepsIdentityKeys:
    def test_verbatim_identity_ids(self):
        sanitized = sanitize_trace_metadata(
            {"user_id": _HA_USER_HEX, "speaker_ha_user_id": _HA_USER_HEX, "search_user_id": _OTHER_HEX}
        )
        assert sanitized == {
            "user_id": _HA_USER_HEX,
            "speaker_ha_user_id": _HA_USER_HEX,
            "search_user_id": _OTHER_HEX,
        }

    def test_verbatim_keys_still_redact_api_keys(self):
        sanitized = sanitize_trace_metadata({"search_user_id": "ghp_" + "a" * 30})
        assert sanitized["search_user_id"] == "[REDACTED_TOKEN]"

    def test_long_hex_under_other_keys_is_still_redacted(self):
        assert sanitize_trace_metadata({"profile_ref": _HA_USER_HEX})["profile_ref"] == "[REDACTED_TOKEN]"


# ---------------------------------------------------------------------------
# Trace summary: resolved requester name
# ---------------------------------------------------------------------------


async def _call_create_trace(task_context: TaskContext | None) -> dict:
    collector = SpanCollector("trace-name", source="ha")
    with patch("app.analytics.tracer.create_trace_summary", new_callable=AsyncMock) as create:
        await OrchestratorAgent._create_trace(
            MagicMock(),
            collector,
            "conv-1",
            "hello",
            "Hi.",
            "general-agent",
            0.9,
            "hello",
            [("general-agent", "hello", 0.9)],
            [],
            task_context=task_context,
        )
    create.assert_awaited_once()
    return create.await_args.kwargs


class TestTraceSummaryUserName:
    async def test_ha_person_name_wins(self):
        context = TaskContext(
            user_id=_HA_USER_HEX,
            requester_person="person.anna",
            requester_name="Anna",
            speaker=SpeakerContext(status="identified", profile_id=1, name="Ben", ha_user_id=_HA_USER_HEX),
        )
        kwargs = await _call_create_trace(context)
        assert kwargs["user_name"] == "Anna"
        assert kwargs["user_name_source"] == "ha"
        assert kwargs["identity_source"] == "ha"

    async def test_ha_user_without_person_named_by_voice(self):
        """HA user without a person entity + identified speaker: voice name, HA personalization."""
        context = TaskContext(
            user_id=_HA_USER_HEX,
            speaker=SpeakerContext(status="identified", profile_id=2, name="Ben", ha_user_id=None),
        )
        kwargs = await _call_create_trace(context)
        assert kwargs["user_name"] == "Ben"
        assert kwargs["user_name_source"] == "voice"
        assert kwargs["identity_source"] == "ha"

    async def test_voice_name_for_voice_only_turn(self):
        context = TaskContext(
            speaker=SpeakerContext(status="identified", profile_id=1, name="Ben", ha_user_id=_HA_USER_HEX)
        )
        kwargs = await _call_create_trace(context)
        assert kwargs["user_name"] == "Ben"
        assert kwargs["user_name_source"] == "voice"
        assert kwargs["identity_source"] == "voice"

    async def test_no_identity(self):
        kwargs = await _call_create_trace(TaskContext())
        assert kwargs["user_name"] is None
        assert kwargs["user_name_source"] is None
        assert kwargs["identity_source"] == "none"
        kwargs = await _call_create_trace(None)
        assert kwargs["user_name"] is None
        assert kwargs["identity_source"] is None

    async def test_row_round_trip_and_search(self, db_repository):
        from app.db.repository import TraceSummaryRepository

        common = {
            "user_input": "turn on the light",
            "final_response": "Done.",
            "routing_agent": "light-agent",
            "routing_confidence": 1.0,
            "routing_duration_ms": 5.0,
            "condensed_task": "turn on the light",
            "agents": ["orchestrator"],
            "source": "ha",
        }
        await create_trace_summary(
            "t-anna", "conv-a", user_name="Ben", user_name_source="voice", identity_source="ha", **common
        )
        await create_trace_summary("t-anon", "conv-b", identity_source="none", user_name_source="ha", **common)

        row = await TraceSummaryRepository.get("t-anna")
        assert row["user_name"] == "Ben" and row["user_name_source"] == "voice"
        assert row["identity_source"] == "ha"
        anon = await TraceSummaryRepository.get("t-anon")
        # No name: no name source either.
        assert anon["user_name"] is None and anon["user_name_source"] is None
        assert anon["identity_source"] == "none"

        found = await TraceSummaryRepository.list_filtered(search="ben")
        assert [r["trace_id"] for r in found] == ["t-anna"]


class TestMigration50:
    async def test_adds_identity_columns_on_existing_db(self, tmp_path: Path):
        path = tmp_path / "upgrade.db"
        db = await aiosqlite.connect(str(path))
        try:
            await _create_tables(db)
            await _create_indexes(db)
            await _seed_defaults(db)
            await _run_migrations(db)
            # Simulate an install at schema 49: no identity columns yet, one old row.
            await db.execute("ALTER TABLE trace_summary DROP COLUMN user_name")
            await db.execute("ALTER TABLE trace_summary DROP COLUMN identity_source")
            await db.execute("ALTER TABLE trace_summary DROP COLUMN user_name_source")
            await db.execute("INSERT INTO trace_summary (trace_id, user_input) VALUES ('old-trace', 'hello')")
            await db.execute("DELETE FROM schema_version WHERE version >= 50")
            await db.commit()

            await _run_migrations(db)
            await db.commit()
            # Re-running is a no-op (column guard).
            await db.execute("DELETE FROM schema_version WHERE version >= 50")
            await _run_migrations(db)
            await db.commit()

            cursor = await db.execute("PRAGMA table_info(trace_summary)")
            columns = {row[1] for row in await cursor.fetchall()}
            assert {"user_name", "identity_source", "user_name_source"} <= columns
            cursor = await db.execute("SELECT user_input, user_name FROM trace_summary WHERE trace_id = 'old-trace'")
            assert await cursor.fetchone() == ("hello", None)
            cursor = await db.execute("SELECT MAX(version) FROM schema_version")
            assert (await cursor.fetchone())[0] >= 50
        finally:
            await shutdown_aiosqlite(db)


# ---------------------------------------------------------------------------
# memory_retrieval span: loaded memories, scope, search user
# ---------------------------------------------------------------------------


class _FakeSpanCollector:
    def __init__(self):
        self.spans: dict[str, dict] = {}

    def start_span(self, name, **kwargs):
        span: dict = {"metadata": {}}
        self.spans[name] = span

        @contextlib.asynccontextmanager
        async def _cm():
            yield span

        return _cm()


def _match(conversation_id: str, similarity: float, text: str, *, turn_id: int, continuation: int = 0) -> MemoryMatch:
    return MemoryMatch(
        conversation_id=conversation_id,
        session_id=7,
        similarity=similarity,
        matched_text=text,
        snippet_turns=[{"user_text": text, "response_text": "ok"}],
        continuation_turns=[{"user_text": "u", "response_text": "r"}] * continuation,
        last_turn_at=1754400000,
        user_id=_HA_USER_HEX,
        turn_id=turn_id,
    )


def _memory_service(wait_mode: str = "blocking", wait_timeout_ms: int = 800) -> MagicMock:
    service = MagicMock()
    service.wait_config = AsyncMock(return_value=(wait_mode, wait_timeout_ms))
    return service


def _span_state(search_user_id: str | None = _HA_USER_HEX, scope: str | None = "user") -> dict:
    return {
        "resolved": asyncio.Event(),
        "metadata": {"identity_source": "ha", "search_user_id": search_user_id},
        "search_info": {"scope": scope} if scope else {},
    }


class TestMemoryRetrievalSpan:
    async def test_attached_memories_listed_with_bounded_snippets(self):
        long_text = "remember the\nboiler   settings " + "word " * 80
        matches = [
            _match("conv-old", 0.912345, long_text, turn_id=41, continuation=3),
            _match("conv-older", 0.87, "short one", turn_id=12),
        ]

        async def _search():
            return matches

        memory_task = asyncio.create_task(_search())
        state = _span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))
        task = IngressTask(description="q", conversation_id="conv-new", context=TaskContext())

        await _resolve_memory_context(task, memory_task, _memory_service(), span_state=state)
        await span_task

        meta = collector.spans["memory_retrieval"]["metadata"]
        assert meta["scope"] == "user"
        assert meta["search_user_id"] == _HA_USER_HEX
        memories = meta["memories"]
        assert len(memories) == 2
        first = memories[0]
        assert set(first) == {
            "conversation_id",
            "session_id",
            "turn_id",
            "similarity",
            "last_turn_at",
            "snippet",
            "continuation_count",
            "attached",
        }
        assert first["conversation_id"] == "conv-old"
        assert first["turn_id"] == 41 and first["session_id"] == 7
        assert first["similarity"] == 0.9123
        assert first["continuation_count"] == 3
        assert first["attached"] is True
        assert len(first["snippet"]) == 150 and first["snippet"].endswith("...")
        assert "\n" not in first["snippet"] and "  " not in first["snippet"]
        assert memories[1]["snippet"] == "short one"
        # The task context got the same matches, now carrying turn_id.
        assert task.context.memory_context[0]["turn_id"] == 41

    async def test_timed_out_memories_are_not_attached(self):
        gate = asyncio.Event()

        async def _slow_search():
            await gate.wait()
            return [_match("conv-old", 0.9, "late match", turn_id=3)]

        memory_task = asyncio.create_task(_slow_search())
        state = _span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))
        task = IngressTask(description="q", conversation_id="conv-new", context=TaskContext())

        await _resolve_memory_context(task, memory_task, _memory_service(wait_timeout_ms=10), span_state=state)
        assert task.context.memory_context is None
        gate.set()
        await span_task

        meta = collector.spans["memory_retrieval"]["metadata"]
        assert meta["abandoned"] is True
        assert [m["attached"] for m in meta["memories"]] == [False]

    async def test_abandoned_best_effort_memories_are_not_attached(self):
        gate = asyncio.Event()

        async def _slow_search():
            await gate.wait()
            return [_match("conv-old", 0.9, "late match", turn_id=3)]

        memory_task = asyncio.create_task(_slow_search())
        state = _span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))
        task = IngressTask(description="q", conversation_id="conv-new", context=TaskContext())

        await _resolve_memory_context(task, memory_task, _memory_service(wait_mode="best_effort"), span_state=state)
        gate.set()
        await span_task

        assert [m["attached"] for m in collector.spans["memory_retrieval"]["metadata"]["memories"]] == [False]

    async def test_no_matches_records_no_memories(self):
        async def _search():
            return []

        memory_task = asyncio.create_task(_search())
        state = _span_state(search_user_id=None)
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))
        task = IngressTask(description="q", conversation_id="conv-new", context=TaskContext())

        await _resolve_memory_context(task, memory_task, _memory_service(), span_state=state)
        await span_task

        meta = collector.spans["memory_retrieval"]["metadata"]
        assert "memories" not in meta
        assert meta["search_user_id"] is None
        assert meta["scope"] == "user"

    async def test_entries_are_capped(self):
        matches = [_match(f"conv-{i}", 0.9, f"text {i}", turn_id=i) for i in range(15)]

        async def _search():
            return matches

        memory_task = asyncio.create_task(_search())
        state = _span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))
        task = IngressTask(description="q", conversation_id="conv-new", context=TaskContext())
        await _resolve_memory_context(task, memory_task, _memory_service(), span_state=state)
        await span_task

        assert len(collector.spans["memory_retrieval"]["metadata"]["memories"]) == 10

    async def test_voice_search_records_voice_search_user(self):
        lookup = MagicMock()
        lookup.wait_ha_user_id = AsyncMock(return_value=_OTHER_HEX)
        service = MagicMock()
        service.search = AsyncMock(return_value=[])
        state = _span_state(search_user_id=None, scope=None)

        await _search_memory_after_speaker(service, "q", "conv-new", lookup, state)

        assert state["metadata"]["search_user_id"] == _OTHER_HEX
        assert state["metadata"]["identity_source"] == "voice"
        assert service.search.await_args.kwargs["search_info"] is state["search_info"]

    def test_sanitizer_keeps_memories_structure_and_redacts_secrets_in_snippets(self):
        metadata = {
            "match_count": 1,
            "scope": "global",
            "search_user_id": _HA_USER_HEX,
            "memories": [
                {
                    "conversation_id": "01JABCDEFGHJKMNPQRSTVWXYZ",
                    "session_id": 7,
                    "turn_id": 41,
                    "similarity": 0.91,
                    "last_turn_at": 1754400000,
                    "snippet": "my password: hunter22 and the garage light",
                    "continuation_count": 2,
                    "attached": True,
                }
            ],
        }
        sanitized = sanitize_trace_metadata(metadata)
        assert sanitized["search_user_id"] == _HA_USER_HEX
        assert sanitized["scope"] == "global"
        entry = sanitized["memories"][0]
        assert entry["conversation_id"] == "01JABCDEFGHJKMNPQRSTVWXYZ"
        assert entry["turn_id"] == 41 and entry["session_id"] == 7
        assert entry["similarity"] == 0.91 and entry["last_turn_at"] == 1754400000
        assert entry["continuation_count"] == 2 and entry["attached"] is True
        assert "hunter22" not in entry["snippet"]
        assert "garage light" in entry["snippet"]


# ---------------------------------------------------------------------------
# Dashboard templates
# ---------------------------------------------------------------------------

_TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


class TestSnippetRedactionBeforeTruncation:
    def test_long_token_cut_by_truncation_is_still_redacted(self):
        from app.agents.orchestrator import _memory_trace_entries

        token = "a1b2c3d4e5" * 4  # 40 hex chars: redacted only while >= 32 chars long
        text = "x " * 65 + token + " tail"  # the token straddles the 150-char cut
        entry = _memory_trace_entries([_match("conv-1", 0.9, text, turn_id=1)], True)[0]
        assert token[:12] not in entry["snippet"]
        assert "[REDACTED_TOKEN]" in entry["snippet"]
        assert len(entry["snippet"]) <= 150


class TestVerbatimConversationIds:
    def test_conversation_ids_kept_verbatim(self):
        owui_id = "owui-" + _HA_USER_HEX
        sanitized = sanitize_trace_metadata(
            {
                "conversation_id": owui_id,
                "matched_conversation_id": owui_id,
                "memories": [{"conversation_id": owui_id, "snippet": "hi"}],
            }
        )
        assert sanitized["conversation_id"] == owui_id
        assert sanitized["matched_conversation_id"] == owui_id
        assert sanitized["memories"][0]["conversation_id"] == owui_id

    def test_secret_shaped_conversation_ids_still_redacted(self):
        sanitized = sanitize_trace_metadata(
            {"conversation_id": "ghp_" + "b" * 30, "matched_conversation_id": "sk-" + "c" * 20}
        )
        assert sanitized["conversation_id"] == "[REDACTED_TOKEN]"
        assert sanitized["matched_conversation_id"] == "[REDACTED_TOKEN]"


class TestAttachedRequiresTaskContext:
    async def test_matches_without_task_context_are_not_attached(self):
        async def _search():
            return [_match("conv-old", 0.9, "text", turn_id=1)]

        memory_task = asyncio.create_task(_search())
        state = _span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))
        task = IngressTask(description="q", conversation_id="conv-new", context=None)

        await _resolve_memory_context(task, memory_task, _memory_service(), span_state=state)
        await span_task

        meta = collector.spans["memory_retrieval"]["metadata"]
        assert meta["matches_attached"] is False
        assert [m["attached"] for m in meta["memories"]] == [False]


# ---------------------------------------------------------------------------
# Action-cache replay: requester name from the warm person map only
# ---------------------------------------------------------------------------

_PERSON_STATES = [{"entity_id": "person.anna", "attributes": {"friendly_name": "Anna", "user_id": _HA_USER_HEX}}]


def _replay_orchestrator(ha_client) -> OrchestratorAgent:
    registry = AsyncMock()
    registry.list_agents = AsyncMock(
        return_value=[AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"])]
    )
    orch = OrchestratorAgent(dispatcher=AsyncMock(), registry=registry, cache_manager=MagicMock())
    orch._ha_client = ha_client
    orch._is_background_turn = MagicMock(return_value=False)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(side_effect=lambda _key, default: default)
    orch._cache_orchestrator.try_cache_replay = AsyncMock(
        return_value=(
            ActionReplayOutcome(
                kind="full_hit",
                entry_id="action-1",
                agent_id="light-agent",
                response_text="Done.",
                replay_result={"success": True},
                similarity=1.0,
            ),
            None,
        )
    )
    orch._finalize_action_replay_hit = AsyncMock(
        return_value={"speech": "Done.", "routed_to": "light-agent", "action_executed": None}
    )
    return orch


def _replay_task(user_id: str | None) -> IngressTask:
    task = IngressTask(description="turn on the light", conversation_id="conv-r", context=TaskContext(user_id=user_id))
    task.span_collector = SpanCollector("trace-replay", source="ha")
    return task


class TestReplayRequesterName:
    async def test_warm_map_names_the_replay(self):
        client = AsyncMock()
        client.get_states = AsyncMock(return_value=_PERSON_STATES)
        directory = PersonDirectory()
        await directory.lookup(client, _HA_USER_HEX)  # warm the map
        client.get_states.reset_mock()
        orch = _replay_orchestrator(client)
        task = _replay_task(_HA_USER_HEX)

        with patch("app.agents.orchestrator.get_person_directory", return_value=directory):
            prelude = await orch._run_pipeline_prelude(task)

        assert prelude.early_exit["_exit_type"] == "cache_replay"
        assert task.context.requester_name == "Anna"
        assert task.span_collector.request_attributes["requester_name"] == "Anna"
        client.get_states.assert_not_called()
        kwargs = await _call_create_trace(task.context)
        assert kwargs["user_name"] == "Anna" and kwargs["user_name_source"] == "ha"
        await directory.aclose()

    async def test_cold_map_does_not_wait_and_has_no_name(self):
        gate = asyncio.Event()
        client = AsyncMock()

        async def _slow_states():
            await gate.wait()
            return _PERSON_STATES

        client.get_states = AsyncMock(side_effect=_slow_states)
        directory = PersonDirectory()
        orch = _replay_orchestrator(client)
        task = _replay_task(_HA_USER_HEX)

        with patch("app.agents.orchestrator.get_person_directory", return_value=directory):
            # The HA fetch never completes during the replay: it must not block.
            prelude = await asyncio.wait_for(orch._run_pipeline_prelude(task), timeout=2)

        assert prelude.early_exit["_exit_type"] == "cache_replay"
        assert task.context.requester_name is None
        assert "requester_name" not in task.span_collector.request_attributes
        # A background refresh was started; it warms the map for later turns.
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert directory.lookup_cached(client, _HA_USER_HEX).name == "Anna"
        await directory.aclose()

    def test_anonymous_or_clientless_replay_is_a_noop(self):
        task = _replay_task(None)
        _attach_cached_requester_person(task, AsyncMock(), task.span_collector)
        assert task.context.requester_name is None
        task = _replay_task(_HA_USER_HEX)
        _attach_cached_requester_person(task, None, task.span_collector)
        assert task.context.requester_name is None


class TestTraceTemplates:
    def test_trace_detail_renders_identity_and_memories(self):
        html = (_TEMPLATES / "trace_detail.html").read_text(encoding="utf-8")
        assert 'x-show="hasIdentity()"' in html
        # The name badge follows the name's own source; personalization is separate.
        assert "sourceBadge(nameSource())" in html
        assert "sourceLabel(traceDetail.identity_source)" in html
        assert "user_name_source" in html
        assert "speaker_ha_user_id" in html
        assert "Loaded memories" in html
        assert "/dashboard/traces?search=' + encodeURIComponent(mem.conversation_id)" in html
        # The live session history toggle is labelled as conversation history.
        assert ">Conversation history<" in html
        assert 'fw-600">Memory<' not in html

    def test_trace_list_shows_user_name(self):
        html = (_TEMPLATES / "traces.html").read_text(encoding="utf-8")
        assert "t.user_name" in html
