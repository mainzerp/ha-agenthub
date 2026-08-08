"""Tests for app.memory.vector_store -- dedicated session-memory sqlite-vec store.

Covers store/search round-trips with hand-crafted 384-dim vectors (no real
embedding model), k-NN ordering, has_embedding, and the dim/model-mismatch
reset path. Mirrors the test_action_cache_sidecar.py style.
"""

from __future__ import annotations

import math

import pytest

from app.memory.vector_store import SessionMemoryVectorStore

_DIM = 384


def _basis_vec(index: int) -> list[float]:
    """Unit vector along basis axis ``index`` (cosine distance 1.0 to other axes)."""
    vec = [0.0] * _DIM
    vec[index % _DIM] = 1.0
    return vec


def _mix_vec(a: int, b: int, weight: float) -> list[float]:
    """Normalized mix of two basis vectors (controls the cosine distance)."""
    vec = [0.0] * _DIM
    vec[a % _DIM] = weight
    vec[b % _DIM] = math.sqrt(1.0 - weight * weight)
    return vec


@pytest.fixture()
def store(tmp_path):
    s = SessionMemoryVectorStore(str(tmp_path / "session_memory.db"))
    yield s
    s.close()


class TestSessionMemoryVectorStore:
    def test_vec_extension_available(self, store):
        assert store.vec_available

    def test_store_and_search_round_trip(self, store):
        vec_rowid = store.store_embedding(1, _basis_vec(0))
        assert vec_rowid is not None and vec_rowid > 0

        hits = store.search(_basis_vec(0), k=5)
        assert len(hits) == 1
        turn_id, distance = hits[0]
        assert turn_id == 1
        assert distance == pytest.approx(0.0, abs=1e-5)

    def test_search_orders_by_ascending_distance(self, store):
        store.store_embedding(1, _basis_vec(0))  # identical to query
        store.store_embedding(2, _mix_vec(0, 1, 0.8))  # close
        store.store_embedding(3, _basis_vec(1))  # orthogonal

        hits = store.search(_basis_vec(0), k=3)
        assert [turn_id for turn_id, _ in hits] == [1, 2, 3]
        distances = [distance for _, distance in hits]
        assert distances == sorted(distances)
        assert distances[1] < 0.2
        assert distances[2] == pytest.approx(1.0, abs=1e-4)

    def test_search_respects_k(self, store):
        for turn_id in range(1, 6):
            store.store_embedding(turn_id, _basis_vec(turn_id))
        assert len(store.search(_basis_vec(0), k=2)) == 2

    def test_search_empty_store_returns_empty(self, store):
        assert store.search(_basis_vec(0), k=5) == []

    def test_has_embedding(self, store):
        assert not store.has_embedding(1)
        store.store_embedding(1, _basis_vec(0))
        assert store.has_embedding(1)

    def test_store_replaces_existing_vector(self, store):
        store.store_embedding(1, _basis_vec(0))
        store.store_embedding(1, _basis_vec(1))
        hits = store.search(_basis_vec(1), k=5)
        assert len(hits) == 1
        assert hits[0][0] == 1
        assert hits[0][1] == pytest.approx(0.0, abs=1e-5)

    def test_ensure_active_model_no_reset_when_unchanged(self, store):
        assert store.ensure_active_model("model-a", _DIM) is False
        store.store_embedding(1, _basis_vec(0))
        assert store.ensure_active_model("model-a", _DIM) is False
        assert store.has_embedding(1)

    def test_model_change_resets_embeddings(self, store):
        store.store_embedding(1, _basis_vec(0))
        assert store.ensure_active_model("model-a", _DIM) is False

        assert store.ensure_active_model("model-b", _DIM) is True
        assert not store.has_embedding(1)
        assert store.search(_basis_vec(0), k=5) == []

        # Store still works after the reset.
        store.store_embedding(2, _basis_vec(0))
        assert store.has_embedding(2)

    def test_dim_change_on_store_resets_embeddings(self, store):
        store.store_embedding(1, _basis_vec(0))
        assert store.has_embedding(1)

        # A write with a different dimension triggers the lazy reset.
        store.store_embedding(2, [0.0] * 512)
        assert not store.has_embedding(1)
        assert store.has_embedding(2)

    def test_reset_embeddings_clears_everything(self, store):
        store.store_embedding(1, _basis_vec(0))
        store.reset_embeddings()
        assert not store.has_embedding(1)
        assert store.search(_basis_vec(0), k=5) == []

    def test_persists_across_reopen(self, tmp_path):
        db_path = str(tmp_path / "session_memory.db")
        first = SessionMemoryVectorStore(db_path)
        first.store_embedding(1, _basis_vec(0))
        first.close()

        second = SessionMemoryVectorStore(db_path)
        try:
            assert second.has_embedding(1)
            assert second.search(_basis_vec(0), k=1)[0][0] == 1
        finally:
            second.close()
