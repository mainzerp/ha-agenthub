"""Tests for the token posting map in EntityIndex (token-based preselection).

Covers build/update/clear of the in-memory posting map and the
``find_by_tokens`` retrieval semantics: document-frequency cap,
IDF-weighted ranking (rare, distinctive tokens outrank common area
tokens), and the max_candidates cap. The VectorStore is mocked; only
the primary-store/posting-map layer is exercised.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.entity.index import EntityIndex
from tests.helpers import make_entity_index_entry


def _make_index() -> tuple[EntityIndex, MagicMock]:
    store = MagicMock()
    return EntityIndex(store), store


class TestTokenPostingMap:
    def test_populate_builds_postings(self):
        index, _store = _make_index()
        index.populate(
            [
                make_entity_index_entry("light.kitchen", "Kitchen Light", area="kitchen"),
                make_entity_index_entry("light.bedroom", "Bedroom Light", area="bedroom"),
            ]
        )
        hits = index.find_by_tokens({"kitchen"})
        assert [e.entity_id for e in hits] == ["light.kitchen"]

    def test_find_by_tokens_empty_index(self):
        index, _store = _make_index()
        assert index.find_by_tokens({"kitchen"}) == []
        assert index.find_by_tokens(set()) == []

    def test_df_cap_excludes_common_token(self):
        index, _store = _make_index()
        index.populate(
            [
                make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"),
                make_entity_index_entry("light.decke", "Decke", area="wohnzimmer"),
            ]
        )
        # "wohnzimmer" appears in 2/2 entities -> df ratio 1.0 > 0.5 -> ignored.
        # "couch" appears in 1/2 -> 0.5 is not > 0.5 -> survives.
        hits = index.find_by_tokens({"wohnzimmer", "couch"}, max_df_ratio=0.5)
        assert [e.entity_id for e in hits] == ["light.couch"]

    def test_ranking_prefers_rare_token_over_common_token(self):
        index, _store = _make_index()
        index.populate(
            [
                # Rare token "couch" (df=1). Named light.z so a count-based
                # tie-break (entity_id asc) would rank it LAST, proving the
                # order comes from IDF weight, not the tie-break.
                make_entity_index_entry("light.z", "Couch", area="bad"),
                # Common token "wohnzimmer" (df=3 of N=4, under the cap).
                make_entity_index_entry("light.a", "Decke", area="wohnzimmer"),
                make_entity_index_entry("light.b", "Regal", area="wohnzimmer"),
                make_entity_index_entry("light.c", "Tisch", area="wohnzimmer"),
            ]
        )
        hits = index.find_by_tokens({"couch", "wohnzimmer"}, max_df_ratio=1.0)
        # idf(couch) = log(4/1) ~ 1.39 outranks idf(wohnzimmer) = log(4/3) ~ 0.29.
        assert [e.entity_id for e in hits] == ["light.z", "light.a", "light.b", "light.c"]

    def test_rare_token_entity_survives_candidate_cap(self):
        """Regression: the live "Licht bei Couch im Wohnzimmer" failure.

        light.couch carries the rare token "couch" (df=1) plus the common
        area token "wohnzimmer" (df large but under the ratio cap). Under
        count-based ranking the wohnzimmer-only devices tied light.couch
        and pushed it past max_candidates; IDF ranking must keep it at
        rank 1 even with a small cap.
        """
        index, _store = _make_index()
        entries = [make_entity_index_entry("light.couch", "Couch", area="wohnzimmer")]
        entries += [make_entity_index_entry(f"light.wz{i}", f"Geraet {i}", area="wohnzimmer") for i in range(21)]
        entries += [make_entity_index_entry(f"light.f{i}", f"Filler {i}", area=f"area{i}") for i in range(28)]
        index.populate(entries)
        # N=50; df(wohnzimmer)=22 -> ratio 0.44 < 0.5 (survives the cap);
        # df(couch)=1 -> idf log(50) ~ 3.91 vs idf(wohnzimmer) log(50/22) ~ 0.82.
        hits = index.find_by_tokens({"couch", "wohnzimmer"}, max_df_ratio=0.5, max_candidates=5)
        assert hits[0].entity_id == "light.couch"
        assert "light.couch" in [e.entity_id for e in hits]
        assert len(hits) == 5

    def test_max_candidates_cap(self):
        index, _store = _make_index()
        index.populate(
            [
                make_entity_index_entry("light.a", "Couch A", area="wohnzimmer"),
                make_entity_index_entry("light.b", "Couch B", area="wohnzimmer"),
            ]
        )
        hits = index.find_by_tokens({"couch"}, max_df_ratio=1.0, max_candidates=1)
        assert len(hits) == 1

    def test_add_updates_postings(self):
        index, _store = _make_index()
        index.add(make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"))
        # Single-entity index: df/N is 1.0, so the default 0.5 df cap would
        # exclude every token; max_df_ratio=1.0 disables the cap here.
        assert [e.entity_id for e in index.find_by_tokens({"couch"}, max_df_ratio=1.0)] == ["light.couch"]

    def test_add_replaces_stale_tokens_on_update(self):
        index, store = _make_index()
        store.get.return_value = {"ids": [], "metadatas": []}
        index.add(make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"))
        index.add(make_entity_index_entry("light.couch", "Sofa", area="wohnzimmer"))
        assert index.find_by_tokens({"couch"}, max_df_ratio=1.0) == []
        assert [e.entity_id for e in index.find_by_tokens({"sofa"}, max_df_ratio=1.0)] == ["light.couch"]

    def test_batch_add_updates_postings(self):
        index, _store = _make_index()
        index.batch_add(
            [
                make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"),
                make_entity_index_entry("light.decke", "Decke", area="wohnzimmer"),
            ]
        )
        hits = index.find_by_tokens({"decke"})
        assert [e.entity_id for e in hits] == ["light.decke"]

    def test_remove_updates_postings(self):
        index, _store = _make_index()
        index.add(make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"))
        index.remove("light.couch")
        assert index.find_by_tokens({"couch"}) == []

    def test_clear_empties_postings(self):
        index, store = _make_index()
        store.count.return_value = 0
        store.get.return_value = {"ids": []}
        index.populate([make_entity_index_entry("light.couch", "Couch", area="wohnzimmer")])
        index.clear()
        assert index.find_by_tokens({"couch"}) == []

    def test_sync_rebuilds_postings(self):
        index, store = _make_index()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        index.sync(
            [
                make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"),
                make_entity_index_entry("light.decke", "Decke", area="wohnzimmer"),
            ]
        )
        hits = index.find_by_tokens({"couch"})
        assert [e.entity_id for e in hits] == ["light.couch"]

    async def test_find_by_tokens_async_direct_call(self):
        index, _store = _make_index()
        index.add(make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"))
        hits = await index.find_by_tokens_async({"couch"}, max_df_ratio=1.0)
        assert [e.entity_id for e in hits] == ["light.couch"]


class TestTokenIdf:
    """Tests for EntityIndex.token_idf (IDF weights for span coverage)."""

    def test_returns_log_n_over_df(self):
        from math import log

        index, _store = _make_index()
        index.populate(
            [
                make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"),
                make_entity_index_entry("light.decke", "Decke", area="wohnzimmer"),
            ]
        )
        weights = index.token_idf({"couch", "wohnzimmer"})
        # N=2: df(couch)=1 -> log(2/1); df(wohnzimmer)=2 -> log(2/2) = 0.0.
        assert weights["couch"] == pytest.approx(log(2.0))
        assert weights["wohnzimmer"] == pytest.approx(0.0)

    def test_unseen_tokens_are_skipped(self):
        """Defined unseen-token behavior: no postings -> token absent from the
        result; no df is invented for it."""
        index, _store = _make_index()
        index.populate([make_entity_index_entry("light.couch", "Couch", area="wohnzimmer")])
        weights = index.token_idf({"couch", "zzz_unseen"})
        assert set(weights.keys()) == {"couch"}

    def test_empty_index_or_empty_tokens_returns_empty(self):
        index, _store = _make_index()
        assert index.token_idf({"couch"}) == {}
        index.populate([make_entity_index_entry("light.couch", "Couch", area="wohnzimmer")])
        assert index.token_idf(set()) == {}

    async def test_token_idf_async_direct_call(self):
        index, _store = _make_index()
        index.add(make_entity_index_entry("light.couch", "Couch", area="wohnzimmer"))
        weights = await index.token_idf_async({"couch"})
        assert set(weights.keys()) == {"couch"}
        assert weights["couch"] >= 0.0
