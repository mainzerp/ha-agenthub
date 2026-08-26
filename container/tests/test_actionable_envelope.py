"""Tests for the dispatch-envelope fast path in ActionableAgent.

The envelope fast path (``_resolve_relevant_entities``) reuses the ingress
top-K candidates carried on the DispatchTask when the top candidate is
unambiguous and still present in a fresh visible snapshot -- skipping the
deterministic-first matcher loop. Ambiguous or stale envelopes fall back
to the matcher path unchanged.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.actionable import LightAgent
from app.models.agent import DispatchTask, EntityCandidate


def _make_agent(visible_ids: list[str]) -> tuple[LightAgent, MagicMock]:
    agent = LightAgent()
    entries = [SimpleNamespace(entity_id=eid) for eid in visible_ids]
    index = MagicMock()
    index.list_entries_async = AsyncMock(return_value=entries)
    agent._entity_index = index
    agent._entity_matcher = MagicMock()
    return agent, index


def _make_task(candidates: list[tuple[str, str, float]]) -> DispatchTask:
    return DispatchTask(
        description="turn on couch light",
        candidates=[EntityCandidate(entity_id=eid, friendly_name=name, score=score) for eid, name, score in candidates],
    )


@pytest.mark.asyncio
async def test_unambiguous_envelope_resolves_without_matcher():
    """Score gap >= _AMBIGUITY_SCORE_GAP and top id in the visible snapshot:
    resolves [(top_id, friendly_name)] without calling the matcher."""
    agent, _index = _make_agent(["light.couch", "light.kitchen"])
    task = _make_task([("light.couch", "Couch", 0.90), ("light.kitchen", "Kitchen", 0.80)])

    with (
        patch(
            "app.agents.actionable.filter_visible_results",
            new=AsyncMock(side_effect=lambda _agent_id, entries, _index: entries),
        ),
        patch("app.agents.actionable.set_request_visible_entries") as mock_publish,
        patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new=AsyncMock(side_effect=AssertionError("matcher must not run on the envelope fast path")),
        ),
    ):
        resolved = await agent._resolve_relevant_entities(task)

    assert resolved == [("light.couch", "Couch")]
    mock_publish.assert_called_once()


@pytest.mark.asyncio
async def test_ambiguous_envelope_falls_back_to_matcher():
    """Score gap < _AMBIGUITY_SCORE_GAP: the envelope is not trusted and the
    deterministic matcher loop runs instead."""
    agent, _index = _make_agent(["light.couch", "light.kitchen"])
    task = _make_task([("light.couch", "Couch", 0.90), ("light.kitchen", "Kitchen", 0.87)])

    with (
        patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.couch", "friendly_name": "Couch"}),
        ) as mock_resolve,
    ):
        resolved = await agent._resolve_relevant_entities(task)

    mock_resolve.assert_awaited_once()
    assert resolved == [("light.couch", "Couch")]


@pytest.mark.asyncio
async def test_top_candidate_missing_from_visible_snapshot_falls_back(caplog):
    """A cached top candidate that is no longer in the visible snapshot must
    not be selected: warn and fall back to the matcher loop."""
    agent, _index = _make_agent(["light.kitchen"])
    task = _make_task([("light.couch", "Couch", 0.90), ("light.kitchen", "Kitchen", 0.80)])

    with (
        patch(
            "app.agents.actionable.filter_visible_results",
            new=AsyncMock(side_effect=lambda _agent_id, entries, _index: entries),
        ),
        patch("app.agents.actionable.set_request_visible_entries"),
        patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen"}),
        ) as mock_resolve,
        caplog.at_level(logging.WARNING, logger="app.agents.actionable"),
    ):
        resolved = await agent._resolve_relevant_entities(task)

    mock_resolve.assert_awaited_once()
    assert resolved == [("light.kitchen", "Kitchen")]
    assert "missing from visible snapshot" in caplog.text
