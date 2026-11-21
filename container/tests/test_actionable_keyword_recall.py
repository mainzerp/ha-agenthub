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


def _entry(
    entity_id: str,
    friendly_name: str,
    *,
    state: str | None = None,
    area: str | None = None,
    aliases: list[str] | None = None,
    entity_category: str | None = None,
):
    return SimpleNamespace(
        entity_id=entity_id,
        friendly_name=friendly_name,
        aliases=list(aliases or []),
        area=area,
        area_name=None,
        device_name=None,
        id_tokens=entity_id.split(".", 1)[1].split("_"),
        state=state,
        domain=entity_id.split(".", 1)[0],
        entity_category=entity_category,
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
async def test_previous_user_turn_ignored_without_pending_question():
    """Issue 238: a new request does not inherit the previous request's devices."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(20)]
    entries.append(_entry("light.ambiente_wohnen", "Ambiente Wohnen"))
    entries.append(_entry("light.esstisch", "Esstisch"))
    agent = _make_agent(entries)
    task = _task("Esstisch einschalten", turns=[{"role": "user", "content": "Ambiente wohnen einschalten"}])

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(task)

    recalled_ids = [entry.entity_id for entry, _hits in scored]
    assert recalled_ids == ["light.esstisch"]


@pytest.mark.asyncio
async def test_previous_user_turn_included_while_question_pending():
    """The turn answering a clarifying question still recalls the original request's devices."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(20)]
    entries.append(_entry("light.kitchen", "Kitchen Light"))
    agent = _make_agent(entries)
    context = TaskContext(
        conversation_turns=[{"role": "user", "content": "turn on the kitchen light"}],
        is_followup=True,
        pending_question="Which light do you mean?",
    )
    task = DispatchTask(description="the ceiling one", context=context)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(task)

    assert "light.kitchen" in {entry.entity_id for entry, _hits in scored}


@pytest.mark.asyncio
async def test_anaphora_recalls_last_entities_without_previous_turn():
    """ "turn it off" names no device: the recently controlled entity joins the recall."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(20)]
    entries.append(_entry("light.kitchen", "Kitchen Light"))
    agent = _make_agent(entries)
    context = TaskContext(
        conversation_turns=[{"role": "user", "content": "turn on the kitchen light"}],
        last_entities=[
            LastEntity(entity_id="light.kitchen", friendly_name="Kitchen Light"),
            LastEntity(entity_id="light.not_visible", friendly_name="Gone"),
        ],
    )
    task = DispatchTask(description="turn it off", context=context)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(task)

    assert [(entry.entity_id, hits) for entry, hits in scored] == [("light.kitchen", (0, 0, 0, 0))]
    assert "No matching devices" not in block
    assert "light.kitchen" in block


@pytest.mark.asyncio
async def test_last_entities_not_appended_when_a_device_is_named():
    """An explicitly named device wins: recency hints stay out of the recall."""
    entries = [_entry(f"light.l{i}", f"Lamp {i}") for i in range(20)]
    entries.append(_entry("light.kitchen", "Kitchen Light"))
    entries.append(_entry("light.hallway", "Hallway"))
    agent = _make_agent(entries)
    context = TaskContext(last_entities=[LastEntity(entity_id="light.kitchen", friendly_name="Kitchen Light")])
    task = DispatchTask(description="turn off the hallway", context=context)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(task)

    assert [entry.entity_id for entry, _hits in scored] == ["light.hallway"]


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
async def test_ambiguity_annotation_inverted_on_followup_turn():
    """Composition contract: tied recall on a follow-up turn (answering a
    pending clarifying question) inverts the annotation to choose-and-act
    instead of re-asking."""
    entries = [
        _entry("light.kitchen_main", "Kitchen"),
        _entry("light.kitchen_spots", "Kitchen"),
    ]
    agent = _make_agent(entries)

    context = TaskContext(
        conversation_turns=[],
        is_followup=True,
        pending_question="Did you mean Kitchen Main or Kitchen Spots?",
    )
    task = DispatchTask(description="turn on kitchen", context=context)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(task)

    assert len(scored) == 2
    assert "answering your clarifying question" in block
    assert "Did you mean Kitchen Main or Kitchen Spots?" in block
    assert "Do not guess" not in block
    assert "ambiguous" not in block


@pytest.mark.asyncio
async def test_handle_parse_miss_followup_question_heuristic():
    """FOLLOW_UP_QUESTION: a parse-miss speech ending in "?" requests a
    voice follow-up; plain prose does not."""
    agent = _make_agent([])
    question = agent._handle_parse_miss(_task("x"), "Did you mean A or B?")
    prose = agent._handle_parse_miss(_task("x"), "I am not sure which light you mean.")
    assert question.voice_followup is True
    assert prose.voice_followup is False


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


# ---------------------------------------------------------------------------
# Issue 238: recall quality (fuzzy names, filters, number tokens, exact hint)
# ---------------------------------------------------------------------------

_FILLER_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Foxtrot", "Golf", "Hotel", "India", "Juliet", "Kilo"]


def _fillers(count: int = 20) -> list:
    """Large-domain filler entities without digits or shared tokens."""
    return [_entry(f"light.filler_{chr(97 + i)}", f"{_FILLER_NAMES[i % 10]} Lamp{chr(97 + i)}") for i in range(count)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spoken", "entity_id", "friendly_name"),
    [
        ("Estisch einschalten", "light.esstisch", "Esstisch"),
        ("Wohnzimer einschalten", "light.wohnzimmer", "Wohnzimmer"),
        ("turn on ambience", "light.ambiente", "Ambiente"),
    ],
)
async def test_fuzzy_name_hit_recalls_stt_misspelling(spoken, entity_id, friendly_name):
    entries = _fillers()
    entries.append(_entry(entity_id, friendly_name))
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task(spoken))

    assert [(entry.entity_id, hits) for entry, hits in scored] == [(entity_id, (0, 1, 0, 0))]


@pytest.mark.asyncio
async def test_fuzzy_name_hit_ranks_below_exact_name_hit():
    entries = _fillers()
    entries.append(_entry("light.esstisch", "Esstisch"))
    entries.append(_entry("light.estisch", "Estisch"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task("Estisch einschalten"))

    ranked = [(entry.entity_id, hits) for entry, hits in scored]
    assert ranked[0] == ("light.estisch", (2, 0, 1, 0))
    assert ("light.esstisch", (0, 1, 0, 0)) in ranked


@pytest.mark.asyncio
async def test_fuzzy_needs_six_chars_and_same_first_letter():
    entries = _fillers()
    entries.append(_entry("light.lampen", "Lampen"))
    entries.append(_entry("light.spielt", "Spielt"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        # "rampen" differs in its first letter; "spiele" vs "spielt" (ratio 0.83)
        # stays below the similarity threshold.
        _block, scored = await agent._build_query_candidate_context(_task("rampen spiele an"))

    assert scored == []


@pytest.mark.asyncio
async def test_fuzzy_similarity_only_computed_for_prefiltered_pairs():
    """The similarity runs once per prefiltered (entity token, query token) pair, not per entity."""
    import app.entity.tokens as tokens_module

    entries = _fillers(40)
    entries.extend(_entry(f"light.esstisch_{chr(97 + i)}", "Esstisch") for i in range(10))
    entries.append(_entry("light.eckleuchte", "Eckleuchte"))
    agent = _make_agent(entries)
    calls: list[tuple[str, str]] = []
    original = tokens_module.token_similarity

    def _counting(a: str, b: str) -> float:
        calls.append((a, b))
        return original(a, b)

    with _visible_passthrough(), patch.object(tokens_module, "token_similarity", new=_counting):
        _block, scored = await agent._build_query_candidate_context(_task("Estisch einschalten"))

    # "esstisch" vs "estisch" (shared by ten entities: memoized) and
    # "eckleuchte" vs "einschalten" are the only pairs passing the
    # length / first-letter prefilter.
    assert sorted(calls) == [("eckleuchte", "einschalten"), ("esstisch", "estisch")]
    assert {entry.entity_id for entry, _hits in scored} == {f"light.esstisch_{chr(97 + i)}" for i in range(10)}


@pytest.mark.asyncio
async def test_config_and_diagnostic_entities_skipped_from_recall():
    entries = _fillers()
    entries.append(_entry("light.kuche", "Küche", state="on"))
    entries.append(_entry("light.kuche_led_ring", "Küche LED Ring", state="on", entity_category="config"))
    entries.append(_entry("light.kuche_status", "Küche Status", state="on", entity_category="diagnostic"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task("Küche ausschalten"))

    assert [entry.entity_id for entry, _hits in scored] == ["light.kuche"]


@pytest.mark.asyncio
async def test_config_entity_named_in_full_stays_recallable():
    entries = _fillers()
    entries.append(_entry("light.kuche", "Küche", state="on"))
    entries.append(_entry("light.kuche_led_ring", "Küche LED Ring", state="on", entity_category="config"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task("Küche LED Ring ausschalten"))

    assert scored[0][0].entity_id == "light.kuche_led_ring"


@pytest.mark.asyncio
async def test_unavailable_entity_dropped_when_available_named_candidate_exists():
    """Trace 5572e8: an unreachable satellite LED ring must not join a room request."""
    entries = _fillers()
    entries.append(_entry("light.wohnzimmer", "Wohnzimmer", state="on"))
    entries.append(_entry("light.kuche", "Küche", state="on"))
    entries.append(
        _entry("light.kuche_satellite_kuche_led_ring", "Küche Satellite Küche LED Ring", state="unavailable")
    )
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task("Wohnzimmer und Küche ausschalten"))

    assert {entry.entity_id for entry, _hits in scored} == {"light.wohnzimmer", "light.kuche"}


@pytest.mark.asyncio
async def test_unavailable_entity_named_explicitly_stays_recallable():
    entries = _fillers()
    entries.append(_entry("light.kuche", "Küche", state="on"))
    entries.append(
        _entry("light.kuche_satellite_kuche_led_ring", "Küche Satellite Küche LED Ring", state="unavailable")
    )
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task("Küche Satellite LED Ring ausschalten"))

    assert scored[0][0].entity_id == "light.kuche_satellite_kuche_led_ring"


@pytest.mark.asyncio
async def test_unavailable_entity_kept_without_available_name_match():
    entries = _fillers()
    entries.append(_entry("light.keller", "Keller", state="unavailable"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("Keller einschalten"))

    assert [entry.entity_id for entry, _hits in scored] == ["light.keller"]
    assert "(unavailable)" in block


@pytest.mark.asyncio
async def test_single_digit_entity_id_token_gives_no_identity_hit():
    """Trace 8f3195: "auf 4" must not recall every entity_id ending in _4."""
    entries = _fillers()
    entries.append(_entry("light.spot_4", "Spot"))
    entries.append(_entry("light.lampe_4", "Lampe"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        _block, scored = await agent._build_query_candidate_context(_task("Lautstärke auf 4 setzen"))

    assert scored == []


def test_entry_field_tokens_keep_multi_digit_and_name_digits():
    from app.entity.tokens import entry_field_tokens

    entry = _entry("light.steckdose_1_ch_12", "Steckdose 1")
    name_tokens, identity_tokens, _area = entry_field_tokens(entry)
    assert "1" in name_tokens
    assert "1" not in identity_tokens
    assert {"steckdose", "ch", "12"} <= identity_tokens


@pytest.mark.asyncio
async def test_exact_name_hint_names_the_exact_candidate():
    """Trace ec1281: "Ambiente" spoken, the entity named exactly "Ambiente" is the hinted pick."""
    entries = _fillers()
    entries.append(_entry("light.ambiente", "Ambiente"))
    entries.append(_entry("light.ambiente_wohnen", "Ambiente Wohnen"))
    entries.append(_entry("light.ambiente_essen", "Ambiente Essen"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("Ambiente ausschalten"))

    assert scored[0][0].entity_id == "light.ambiente"
    assert "candidate 1 (light.ambiente) carries exactly the name the user said" in block
    assert "ambiguous" not in block


@pytest.mark.asyncio
async def test_exact_name_hint_replaces_ambiguity_annotation_on_tie():
    entries = [
        _entry("light.a", "Ceiling"),
        _entry("light.b", "Ceiling Lamp Spot"),
    ]
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("ceiling spot off"))

    # Both score two name hits: a tie that used to trigger the ambiguity note.
    assert [hits for _entry, hits in scored] == [(2, 0, 0, 0), (2, 0, 0, 0)]
    assert "(light.a) carries exactly the name the user said" in block
    assert "ambiguous" not in block


@pytest.mark.asyncio
async def test_exact_name_hint_prefers_the_longest_nested_name():
    entries = _fillers()
    entries.append(_entry("switch.kuche", "Küche"))
    entries.append(_entry("switch.kuche_steckdose_1", "Küche Steckdose 1"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, _scored = await agent._build_query_candidate_context(_task("Küche Steckdose 1 ausschalten"))

    assert "(switch.kuche_steckdose_1) carries exactly the name" in block
    assert "(switch.kuche) carries" not in block


@pytest.mark.asyncio
async def test_exact_name_hint_uses_aliases():
    entries = _fillers()
    entries.append(_entry("light.girlande", "Lichterkette", aliases=["Gelande Schlafzimmer"]))
    entries.append(_entry("light.schlafzimmer", "Schlafzimmer Decke"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(_task("Gelande Schlafzimmer an"))

    assert scored[0][0].entity_id == "light.girlande"
    assert "(light.girlande) carries exactly the name" in block


@pytest.mark.asyncio
async def test_no_exact_name_hint_for_two_distinct_full_names():
    entries = _fillers()
    entries.append(_entry("light.wohnzimmer", "Wohnzimmer"))
    entries.append(_entry("light.kuche", "Küche"))
    agent = _make_agent(entries)

    with _visible_passthrough():
        block, _scored = await agent._build_query_candidate_context(_task("Wohnzimmer und Küche ausschalten"))

    assert "carries exactly the name" not in block
