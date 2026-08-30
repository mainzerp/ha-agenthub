"""Tests for the agent-side keyword recall in ActionableAgent.

ENTITY_RESOLUTION_REWORK: the orchestrator no longer pre-resolves entity
candidates; each agent recalls its own candidates by token/keyword overlap
over its visible entities (small domains inject the whole visible list),
renders them as a closed-contract candidate block, and the executor
validates any LLM-picked entity_id against the recalled set fail-closed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.actionable import LightAgent
from app.models.agent import DispatchTask, LastEntity, TaskContext


def _entry(entity_id: str, friendly_name: str, *, state: str | None = None, area: str | None = None):
    return SimpleNamespace(
        entity_id=entity_id,
        friendly_name=friendly_name,
        aliases=[],
        area=area,
        area_name=None,
        device_name=None,
        id_tokens=entity_id.split(".", 1)[1].split("_"),
        state=state,
        domain=entity_id.split(".", 1)[0],
    )


def _make_agent(entries: list) -> LightAgent:
    agent = LightAgent()
    index = MagicMock()
    index.list_entries_async = AsyncMock(return_value=entries)
    agent._entity_index = index
    agent._entity_matcher = MagicMock()
    return agent


def _visible_passthrough():
    return patch(
        "app.agents.actionable.filter_visible_results",
        new=AsyncMock(side_effect=lambda _agent_id, entries, _index: entries),
    )


def _task(description: str, *, turns: list[dict] | None = None) -> DispatchTask:
    context = TaskContext(conversation_turns=turns or [])
    return DispatchTask(description=description, context=context)


@pytest.mark.asyncio
async def test_small_domain_injects_whole_visible_list():
    """<= 15 visible entities: no filtering, every entry is a candidate."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(3)]
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("turn on lamp 0"))

    assert len(scored) == 3
    for entry in entries:
        assert entry.entity_id in block


@pytest.mark.asyncio
async def test_large_domain_token_filter_picks_top_n():
    """> 15 visible entities: only token-matching entries are recalled."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(20)]
    entries.append(_entry("light.kitchen", "Kitchen Ceiling"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("turn on the kitchen ceiling"))

    recalled_ids = {entry.entity_id for entry, _hits in scored}
    assert "light.kitchen" in recalled_ids
    assert len(scored) <= 12
    assert "light.l3" not in recalled_ids
    assert "light.kitchen" in block


@pytest.mark.asyncio
async def test_compound_query_recalls_spaced_name():
    """ "Innenhofüberdachung" hits the tokens of "Innenhof Überdachung"."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(20)]
    entries.append(_entry("light.innenhof_uberdachung", "Innenhof Überdachung"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task("Innenhofüberdachung ausschalten"))

    recalled_ids = {entry.entity_id for entry, _hits in scored}
    assert "light.innenhof_uberdachung" in recalled_ids


@pytest.mark.asyncio
async def test_last_user_turn_terms_are_included():
    """Follow-up turns: the last user message feeds the recall terms."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(20)]
    entries.append(_entry("light.kitchen", "Kitchen Light"))
    agent = _make_agent(entries)
    task = _task(
        "turn it off",
        turns=[{"role": "user", "content": "turn on the kitchen light"}],
    )

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(task)

    recalled_ids = {entry.entity_id for entry, _hits in scored}
    assert "light.kitchen" in recalled_ids


@pytest.mark.asyncio
async def test_empty_recall_injects_clarifying_block():
    """No visible entities: the block instructs a clarifying question."""
    agent = _make_agent([])

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("turn on something"))

    assert scored == []
    assert "No matching devices" in block
    assert "Do NOT output a JSON action block" in block


@pytest.mark.asyncio
async def test_candidate_block_closed_contract_rendering():
    """Candidates render as 'entity_id — friendly_name (state)' with the
    closed entity_id contract."""
    entries = [_entry("light.kitchen", "Kitchen Light", state="on")]
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, _scored = await agent._build_query_candidate_context(_task("kitchen"))

    assert "light.kitchen — Kitchen Light (on)" in block
    assert "You MUST emit the 'entity_id' field verbatim" in block


@pytest.mark.asyncio
async def test_ambiguity_annotation_on_tied_recall():
    """Two candidates tying on the top hit count get the ambiguity note."""
    entries = [
        _entry("light.kitchen_main", "Kitchen"),
        _entry("light.kitchen_spots", "Kitchen"),
    ]
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("turn on kitchen"))

    assert len(scored) == 2
    assert "ambiguous" in block


@pytest.mark.asyncio
async def test_handle_parse_miss_followup_uses_recall_stash():
    """The voice-followup gate reads the per-request recall stash."""
    from app.agents.actionable import _recalled_candidates_var

    agent = _make_agent([])
    scored = [(_entry("light.a", "A"), 2), (_entry("light.b", "B"), 2)]
    token = _recalled_candidates_var.set(scored)
    try:
        result = agent._handle_parse_miss(_task("x"), "Did you mean A or B?")
    finally:
        _recalled_candidates_var.reset(token)
    assert result.voice_followup is True


@pytest.mark.asyncio
async def test_candidate_ids_gate_published_includes_last_entities(monkeypatch):
    """_handle_task_inner publishes recalled ids + last_entities ids to the
    executor's closed-contract gate."""
    from app.agents.action_executor import _request_candidate_ids

    entries = [_entry("light.kitchen", "Kitchen Light", state="off")]
    agent = _make_agent(entries)
    agent._ha_client = None  # no execution path; LLM response is informational
    agent._call_llm = AsyncMock(return_value="Done.")
    agent._load_prompt_async = AsyncMock(return_value="prompt")

    context = TaskContext(last_entities=[LastEntity(entity_id="light.hallway", friendly_name="Hallway")])
    task = DispatchTask(description="kitchen off", context=context)

    published: list = []
    original_set = None

    import app.agents.actionable as actionable_module

    original_set = actionable_module.set_request_candidate_ids

    def _spy(ids):
        published.append(frozenset(ids) if ids is not None else None)
        return original_set(ids)

    monkeypatch.setattr(actionable_module, "set_request_candidate_ids", _spy)

    with _visible_passthrough():
        await agent.handle_task(task)

    assert published, "candidate-id gate was never published"
    gate = published[-1]
    assert "light.kitchen" in gate
    assert "light.hallway" in gate
    # The ContextVar is cleared after the request.
    assert _request_candidate_ids.get() is None
