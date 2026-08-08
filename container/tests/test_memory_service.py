"""Tests for app.memory.service -- MemoryService search/index/backfill.

Uses a real SessionMemoryVectorStore on a tmp path with hand-crafted
384-dim vectors and a fake embedding engine; the relational layer
(MemoryRepository) and settings are mocked. Covers the empty-store
short-circuit, threshold filtering, the scope=user/global visibility matrix
(incl. the anonymous bucket), best-per-session grouping, continuation copy,
index_turn rollup, and failure containment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.repository import SettingsRepository
from app.memory.service import MemoryService
from app.memory.vector_store import SessionMemoryVectorStore

_DIM = 384


def _basis_vec(index: int) -> list[float]:
    vec = [0.0] * _DIM
    vec[index % _DIM] = 1.0
    return vec


def _mix_vec(a: int, b: int, weight: float) -> list[float]:
    import math

    vec = [0.0] * _DIM
    vec[a % _DIM] = weight
    vec[b % _DIM] = math.sqrt(1.0 - weight * weight)
    return vec


class _FakeEngine:
    def __init__(self, vector: list[float] | None = None, *, fail: bool = False) -> None:
        self._vector = vector if vector is not None else _basis_vec(0)
        self._fail = fail
        self.embed_calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        if self._fail:
            raise RuntimeError("embedding backend down")
        return self._vector

    def get_info(self) -> dict:
        return {"provider": "local", "model": "fake-model", "dimensions": _DIM, "is_multilingual": False}


@pytest.fixture()
def store(tmp_path):
    s = SessionMemoryVectorStore(str(tmp_path / "session_memory.db"))
    yield s
    s.close()


@pytest.fixture()
def service(store):
    return MemoryService(vector_store=store)


def _patch_settings(monkeypatch, overrides: dict[str, str] | None = None) -> None:
    values = dict(overrides or {})

    async def _get_value(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(SettingsRepository, "get_value", _get_value)


def _patch_engine(monkeypatch, engine: _FakeEngine) -> None:
    monkeypatch.setattr("app.memory.service.get_embedding_engine", AsyncMock(return_value=engine))


def _patch_repo(monkeypatch, **overrides) -> MagicMock:
    repo = MagicMock()
    repo.count_turns = AsyncMock(return_value=0)
    repo.get_turns_by_ids = AsyncMock(return_value=[])
    repo.get_session_turns = AsyncMock(return_value=[])
    repo.upsert_session = AsyncMock(return_value=10)
    repo.insert_turn_ref = AsyncMock(return_value=20)
    repo.set_turn_vec = AsyncMock()
    repo.list_unembedded_turns = AsyncMock(return_value=[])
    repo.reset_vec_refs = AsyncMock(return_value=0)
    for name, value in overrides.items():
        setattr(repo, name, value)
    monkeypatch.setattr("app.memory.service.MemoryRepository", repo)
    return repo


def _turn_row(turn_id: int, session_id: int, conversation_id: str, user_id: str | None, **extra) -> dict:
    row = {
        "turn_id": turn_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "created_at": 1000 + turn_id,
        "conversation_row_id": 500 + turn_id,
        "last_turn_at": 2000 + session_id,
        "user_text": f"user text {turn_id}",
        "response_text": f"response text {turn_id}",
    }
    row.update(extra)
    return row


class TestSearch:
    async def test_empty_store_short_circuits_before_embed(self, monkeypatch, service):
        _patch_settings(monkeypatch)
        engine = _FakeEngine()
        _patch_engine(monkeypatch, engine)
        _patch_repo(monkeypatch, count_turns=AsyncMock(return_value=0))

        assert await service.search("anything", "user-1") == []
        assert engine.embed_calls == []

    async def test_disabled_short_circuits(self, monkeypatch, service):
        _patch_settings(monkeypatch, {"memory.enabled": "false"})
        engine = _FakeEngine()
        _patch_engine(monkeypatch, engine)
        repo = _patch_repo(monkeypatch, count_turns=AsyncMock(return_value=5))

        assert await service.search("anything", "user-1") == []
        assert engine.embed_calls == []
        repo.count_turns.assert_not_awaited()

    async def test_threshold_filters_low_similarity(self, monkeypatch, service, store):
        _patch_settings(monkeypatch)
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))  # similarity 1.0
        store.store_embedding(2, _basis_vec(1))  # similarity 0.0
        _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=2),
            get_turns_by_ids=AsyncMock(
                return_value=[
                    _turn_row(1, 10, "conv-a", "user-1"),
                    _turn_row(2, 11, "conv-b", "user-1"),
                ]
            ),
        )

        matches = await service.search("query", "user-1")
        assert [m.conversation_id for m in matches] == ["conv-a"]
        assert matches[0].similarity == pytest.approx(1.0, abs=1e-3)

    async def test_scope_user_excludes_other_users_and_anonymous(self, monkeypatch, service, store):
        _patch_settings(monkeypatch)
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))
        store.store_embedding(2, _basis_vec(0))
        store.store_embedding(3, _basis_vec(0))
        _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=3),
            get_turns_by_ids=AsyncMock(
                return_value=[
                    _turn_row(1, 10, "conv-own", "user-1"),
                    _turn_row(2, 11, "conv-other", "user-2"),
                    _turn_row(3, 12, "conv-anon", None),
                ]
            ),
        )

        # Identified user: sees only their own rows, never the anonymous bucket.
        matches = await service.search("query", "user-1")
        assert [m.conversation_id for m in matches] == ["conv-own"]

    async def test_scope_user_anonymous_request_sees_only_anonymous_bucket(self, monkeypatch, service, store):
        _patch_settings(monkeypatch)
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))
        store.store_embedding(2, _basis_vec(0))
        _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=2),
            get_turns_by_ids=AsyncMock(
                return_value=[
                    _turn_row(1, 10, "conv-anon", None),
                    _turn_row(2, 11, "conv-identified", "user-1"),
                ]
            ),
        )

        matches = await service.search("query", None)
        assert [m.conversation_id for m in matches] == ["conv-anon"]

    async def test_scope_global_includes_everything(self, monkeypatch, service, store):
        _patch_settings(monkeypatch, {"memory.scope": "global"})
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))
        store.store_embedding(2, _mix_vec(0, 1, 0.95))
        store.store_embedding(3, _basis_vec(0))
        _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=3),
            get_turns_by_ids=AsyncMock(
                return_value=[
                    _turn_row(1, 10, "conv-a", "user-1"),
                    _turn_row(2, 11, "conv-b", "user-2"),
                    _turn_row(3, 12, "conv-c", None),
                ]
            ),
        )

        matches = await service.search("query", "user-1")
        assert {m.conversation_id for m in matches} == {"conv-a", "conv-b", "conv-c"}

    async def test_best_match_per_session_grouping(self, monkeypatch, service, store):
        _patch_settings(monkeypatch)
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))  # session 10, similarity 1.0
        store.store_embedding(2, _mix_vec(0, 1, 0.9))  # session 10, lower similarity
        _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=2),
            get_turns_by_ids=AsyncMock(
                return_value=[
                    _turn_row(1, 10, "conv-a", "user-1"),
                    _turn_row(2, 10, "conv-a", "user-1"),
                ]
            ),
        )

        matches = await service.search("query", "user-1")
        assert len(matches) == 1
        assert matches[0].similarity == pytest.approx(1.0, abs=1e-3)
        assert matches[0].matched_text == "user text 1"

    async def test_snippet_truncated_to_max_snippet_chars(self, monkeypatch, service, store):
        _patch_settings(monkeypatch, {"memory.max_snippet_chars": "10"})
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))
        _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=1),
            get_turns_by_ids=AsyncMock(return_value=[_turn_row(1, 10, "conv-a", "user-1", user_text="x" * 500)]),
        )

        matches = await service.search("query", "user-1")
        assert matches[0].snippet_turns[0]["user_text"] == "x" * 10

    async def test_continuation_attached_only_for_top_cross_session_match(self, monkeypatch, service, store):
        _patch_settings(monkeypatch, {"memory.max_continuation_turns": "2"})
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))  # top match: old session
        store.store_embedding(2, _mix_vec(0, 1, 0.9))  # second match
        repo = _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=2),
            get_turns_by_ids=AsyncMock(
                return_value=[
                    _turn_row(1, 10, "conv-old", "user-1"),
                    _turn_row(2, 11, "conv-current", "user-1"),
                ]
            ),
            get_session_turns=AsyncMock(
                return_value=[
                    {"user_text": "u1", "response_text": "a1"},
                    {"user_text": "u2", "response_text": "a2"},
                    {"user_text": "u3", "response_text": "a3"},
                ]
            ),
        )

        matches = await service.search("query", "user-1", current_conversation_id="conv-current")
        assert [m.conversation_id for m in matches] == ["conv-old"]
        # Top cross-session match: continuation capped at max_continuation_turns (last 2).
        assert matches[0].continuation_turns == [
            {"user_text": "u2", "response_text": "a2"},
            {"user_text": "u3", "response_text": "a3"},
        ]
        repo.get_session_turns.assert_awaited_once_with("conv-old")

    async def test_current_session_excluded_from_matches(self, monkeypatch, service, store):
        # The current session's own turns are already in the live conversation
        # context; they must never appear as memory matches (self-match).
        _patch_settings(monkeypatch)
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))
        repo = _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=1),
            get_turns_by_ids=AsyncMock(return_value=[_turn_row(1, 10, "conv-current", "user-1")]),
        )

        matches = await service.search("query", "user-1", current_conversation_id="conv-current")
        assert matches == []
        repo.get_session_turns.assert_not_awaited()

    async def test_duplicate_matched_text_deduped(self, monkeypatch, service, store):
        # Sessions whose best-matched text is identical to a higher-ranked
        # session (repeated recall phrasings) are dropped from the top-k so
        # distinct sessions keep their slots.
        _patch_settings(monkeypatch)
        engine = _FakeEngine(_basis_vec(0))
        _patch_engine(monkeypatch, engine)
        store.store_embedding(1, _basis_vec(0))  # sim 1.0, session 10
        store.store_embedding(2, _mix_vec(0, 1, 0.95))  # sim ~0.95, session 11, same text
        store.store_embedding(3, _mix_vec(0, 1, 0.9))  # sim ~0.9, session 12, distinct text
        _patch_repo(
            monkeypatch,
            count_turns=AsyncMock(return_value=3),
            get_turns_by_ids=AsyncMock(
                return_value=[
                    _turn_row(1, 10, "conv-a", "user-1", user_text="same question?"),
                    _turn_row(2, 11, "conv-b", "user-1", user_text="Same   question?"),
                    _turn_row(3, 12, "conv-c", "user-1", user_text="different content"),
                ]
            ),
        )

        matches = await service.search("query", "user-1")
        assert [m.conversation_id for m in matches] == ["conv-a", "conv-c"]

    async def test_search_failure_is_contained(self, monkeypatch, service):
        _patch_settings(monkeypatch)
        _patch_engine(monkeypatch, _FakeEngine(fail=True))
        _patch_repo(monkeypatch, count_turns=AsyncMock(return_value=5))

        assert await service.search("query", "user-1") == []


class TestIndexTurn:
    async def test_index_turn_rolls_up_session_and_vector(self, monkeypatch, service, store):
        _patch_settings(monkeypatch)
        engine = _FakeEngine()
        _patch_engine(monkeypatch, engine)
        repo = _patch_repo(monkeypatch)

        await service.index_turn(
            conversation_id="conv-1",
            conversation_row_id=99,
            user_id="user-1",
            user_text="u" * 500,
            response_text="response",
            language="en",
            source="ha",
        )

        # Session digest = first user message truncated to 200 chars.
        upsert_kwargs = repo.upsert_session.await_args.kwargs
        assert upsert_kwargs["conversation_id"] == "conv-1"
        assert upsert_kwargs["user_id"] == "user-1"
        assert upsert_kwargs["summary_text"] == "u" * 200
        repo.insert_turn_ref.assert_awaited_once()
        set_vec_args = repo.set_turn_vec.await_args.args
        assert set_vec_args[0] == 20  # turn id from insert_turn_ref
        assert set_vec_args[2] == "fake-model"
        assert set_vec_args[3] == _DIM
        assert store.has_embedding(20)
        # Embed input is the turn pair capped at 1000 chars.
        assert engine.embed_calls == [("u" * 500 + "\nresponse")[:1000]]

    async def test_index_turn_disabled_does_nothing(self, monkeypatch, service, store):
        _patch_settings(monkeypatch, {"memory.enabled": "false"})
        engine = _FakeEngine()
        _patch_engine(monkeypatch, engine)
        repo = _patch_repo(monkeypatch)

        await service.index_turn("conv-1", 99, None, "u", "r", None, None)

        assert engine.embed_calls == []
        repo.upsert_session.assert_not_awaited()
        assert not store.has_embedding(20)

    async def test_index_turn_failure_is_swallowed(self, monkeypatch, service):
        _patch_settings(monkeypatch)
        _patch_engine(monkeypatch, _FakeEngine(fail=True))
        _patch_repo(monkeypatch)

        # Must not raise even when the embedding backend fails.
        await service.index_turn("conv-1", 99, None, "u", "r", None, None)


class TestBackfill:
    async def test_backfill_embeds_unembedded_turns(self, monkeypatch, service, store):
        _patch_settings(monkeypatch)
        engine = _FakeEngine()
        _patch_engine(monkeypatch, engine)
        repo = _patch_repo(
            monkeypatch,
            list_unembedded_turns=AsyncMock(
                side_effect=[
                    [
                        {"turn_id": 5, "conversation_row_id": 50, "user_text": "hello", "response_text": "hi"},
                        {"turn_id": 6, "conversation_row_id": 51, "user_text": "bye", "response_text": "see you"},
                    ],
                    [],
                ]
            ),
        )

        await service.backfill()

        assert len(engine.embed_calls) == 2
        assert store.has_embedding(5)
        assert store.has_embedding(6)
        assert repo.set_turn_vec.await_count == 2

    async def test_backfill_noop_when_nothing_unembedded(self, monkeypatch, service, store):
        _patch_settings(monkeypatch)
        engine = _FakeEngine()
        _patch_engine(monkeypatch, engine)
        _patch_repo(monkeypatch)

        await service.backfill()
        assert engine.embed_calls == []
