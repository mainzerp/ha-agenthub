"""Tests for the area-tie-breaker logic added in 0.18.6 (FLOW-CTX-1).

The originating satellite's area should break ties between otherwise
equally good entity matches, but must NOT override a decisive top
match just because a same-area candidate exists further down.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.entity.deterministic_resolver import (
    _select_deterministic_candidate,
    rerank_matches_by_area,
)


@dataclass
class _Entry:
    entity_id: str
    friendly_name: str
    domain: str
    area: str | None = None


@dataclass
class _Match:
    entity_id: str
    friendly_name: str
    score: float
    area: str | None = None
    signal_scores: dict = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# rerank_matches_by_area
# ---------------------------------------------------------------------------


class TestRerankMatchesByArea:
    def test_no_preferred_area_returns_unchanged(self):
        matches = [
            _Match("light.a", "A", 0.9, area="living"),
            _Match("light.b", "B", 0.8, area="kitchen"),
        ]
        assert rerank_matches_by_area(matches, None) is matches

    def test_single_match_returns_unchanged(self):
        matches = [_Match("light.a", "A", 0.9, area="living")]
        assert rerank_matches_by_area(matches, "kitchen") is matches

    def test_top_already_in_area_returns_unchanged(self):
        matches = [
            _Match("light.k", "K", 0.9, area="kitchen"),
            _Match("light.l", "L", 0.85, area="living"),
        ]
        assert rerank_matches_by_area(matches, "kitchen") is matches

    def test_near_tie_same_area_wins(self):
        """Same-area candidate scoring within 0.05 of the top wins."""
        matches = [
            _Match("light.living", "Living", 0.90, area="living"),
            _Match("light.kitchen", "Kitchen", 0.88, area="kitchen"),
            _Match("light.bed", "Bed", 0.70, area="bedroom"),
        ]
        reranked = rerank_matches_by_area(matches, "kitchen")
        assert reranked[0].entity_id == "light.kitchen"

    def test_far_behind_same_area_loses(self):
        """A same-area candidate scoring >0.05 below top keeps losing."""
        matches = [
            _Match("light.living", "Living", 0.95, area="living"),
            _Match("light.kitchen", "Kitchen", 0.70, area="kitchen"),
        ]
        reranked = rerank_matches_by_area(matches, "kitchen")
        assert reranked[0].entity_id == "light.living"

    def test_original_list_not_mutated(self):
        matches = [
            _Match("light.living", "Living", 0.90, area="living"),
            _Match("light.kitchen", "Kitchen", 0.88, area="kitchen"),
        ]
        rerank_matches_by_area(matches, "kitchen")
        assert matches[0].entity_id == "light.living"

    def test_no_same_area_candidate_returns_unchanged(self):
        matches = [
            _Match("light.living", "Living", 0.90, area="living"),
            _Match("light.bed", "Bed", 0.88, area="bedroom"),
        ]
        reranked = rerank_matches_by_area(matches, "kitchen")
        assert reranked[0].entity_id == "light.living"


# ---------------------------------------------------------------------------
# _select_deterministic_candidate with preferred_area_id
# ---------------------------------------------------------------------------


class TestSelectDeterministicCandidateWithArea:
    def test_single_entry_returns_it(self):
        entry = _Entry("light.a", "A", "light", area="living")
        candidate, ambig = _select_deterministic_candidate(
            [entry],
            "A",
            preferred_area_id="kitchen",
        )
        assert candidate is entry
        assert ambig is None

    def test_area_tie_breaker_picks_same_area(self):
        entries = [
            _Entry("light.living", "Licht", "light", area="living"),
            _Entry("light.kitchen", "Licht", "light", area="kitchen"),
        ]
        candidate, ambig = _select_deterministic_candidate(
            entries,
            "Licht",
            preferred_area_id="kitchen",
        )
        assert candidate is not None
        assert candidate.entity_id == "light.kitchen"
        assert ambig is None

    def test_no_preferred_area_stays_ambiguous(self):
        entries = [
            _Entry("light.living", "Licht", "light", area="living"),
            _Entry("light.kitchen", "Licht", "light", area="kitchen"),
        ]
        candidate, ambig = _select_deterministic_candidate(
            entries,
            "Licht",
            preferred_area_id=None,
        )
        assert candidate is None
        assert ambig is not None

    def test_area_does_not_narrow_still_ambiguous(self):
        entries = [
            _Entry("light.kitchen1", "Licht", "light", area="kitchen"),
            _Entry("light.kitchen2", "Licht", "light", area="kitchen"),
        ]
        candidate, ambig = _select_deterministic_candidate(
            entries,
            "Licht",
            preferred_area_id="kitchen",
        )
        assert candidate is None
        assert ambig is not None


# ---------------------------------------------------------------------------
# TaskContext + ConversationRequest: new fields round-trip
# ---------------------------------------------------------------------------


class TestTaskContextFields:
    def test_defaults(self):
        from app.models.agent import TaskContext

        ctx = TaskContext()
        assert ctx.source == "api"
        assert ctx.device_name is None
        assert ctx.area_name is None

    def test_source_literal_accepts_ha_chat_api(self):
        from app.models.agent import TaskContext

        for src in ("ha", "chat", "api"):
            assert TaskContext(source=src).source == src

    def test_names_round_trip(self):
        from app.models.agent import TaskContext

        ctx = TaskContext(
            device_id="dev123",
            area_id="area456",
            device_name="Kitchen Satellite",
            area_name="Kitchen",
            source="ha",
        )
        assert ctx.device_name == "Kitchen Satellite"
        assert ctx.area_name == "Kitchen"

    def test_conversation_request_accepts_names(self):
        from app.models.conversation import ConversationRequest

        req = ConversationRequest(
            text="schalte licht ein",
            device_id="dev123",
            area_id="kitchen",
            device_name="Kitchen Satellite",
            area_name="Kitchen",
        )
        assert req.device_name == "Kitchen Satellite"
        assert req.area_name == "Kitchen"
