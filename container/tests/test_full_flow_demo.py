"""Full-flow demo: one voice request through the ENTITY_RESOLUTION_REWORK pipeline.

Drives the REAL agent + executor code paths with fakes only for the LLM
provider and the Home Assistant client:

- real ``ActionableAgent._recall_keyword_candidates`` (keyword recall,
  compound containment) and the closed-contract candidate block;
- real candidate-id gate publication (``set_request_candidate_ids``);
- real ``resolve_and_validate_entity`` fail-closed validation in
  ``execute_light_action``;
- mocked: agent LLM (``_call_llm``), HA client (``call_service``/
  ``get_state``), visibility rules (none).

The orchestrator routing stage is SIMULATED with a routing-LLM stub:
instantiating the full ``OrchestratorAgent`` requires the app bootstrap
(DB, cache managers, dispatcher). Everything after the routing decision
runs on production code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.actionable import LightAgent
from app.entity.index import EntityIndex
from app.models.agent import DispatchTask, TaskContext
from tests.helpers import make_entity_index_entry

# --- Scenario fixture data ---------------------------------------------------

LIGHTS = [
    ("light.innenhof_uberdachung", "Innenhof Überdachung", "on"),
    ("light.innenhof", "Innenhof", "on"),
    ("light.innenhof_boden", "Innenhof Boden", "off"),
    ("light.innenhof_lichtband", "Innenhof Lichtband", "on"),
]
COVERS = [
    ("cover.jalousie_links", "Jalousie Links", "open"),
    ("cover.jalousie_rechts", "Jalousie Rechts", "closed"),
]

USER_TEXT = "Innenhofüberdachung ausschalten."


def _build_entries():
    entries = []
    for entity_id, name, state in LIGHTS + COVERS:
        entry = make_entity_index_entry(
            entity_id,
            name,
            area="innenhof",
            id_tokens=entity_id.split(".", 1)[1].split("_"),
        )
        entry.state = state
        entries.append(entry)
    return entries


def _make_index(entries):
    def _by_domains(domains=None):
        if not domains:
            return list(entries)
        return [e for e in entries if e.domain in domains]

    index = MagicMock(spec=EntityIndex)
    index.list_entries_async = AsyncMock(side_effect=_by_domains)
    index.list_entries = MagicMock(side_effect=_by_domains)
    index.get_by_id = MagicMock(side_effect=lambda eid: next((e for e in entries if e.entity_id == eid), None))
    index.get_by_id_async = AsyncMock(side_effect=lambda eid: next((e for e in entries if e.entity_id == eid), None))
    return index


def _make_ha_client():
    client = AsyncMock()
    client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
    client.call_service = AsyncMock(
        return_value=[{"entity_id": "light.innenhof_uberdachung", "state": "off", "attributes": {}}]
    )
    # Non-CM return: the executor falls back to the plain REST call path.
    client.expect_state = MagicMock(return_value=None)
    return client


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


def _routing_llm_stub(text: str) -> tuple[str, float]:
    """Simulated routing decision (orchestrator stage, LLM mocked)."""
    assert "innenhof" in text.lower()
    return "light-agent", 0.97


def _make_agent(entries, ha_client, llm_responses: list[str]) -> tuple[LightAgent, list, list]:
    agent = LightAgent()
    agent._entity_index = _make_index(entries)
    agent._entity_matcher = None  # deterministic resolver only; no matcher re-run expected
    agent._ha_client = ha_client
    agent._load_prompt_async = AsyncMock(return_value="LIGHT AGENT PROMPT")

    captured_messages: list = []
    responses = list(llm_responses)

    async def _fake_call_llm(messages, **kwargs):
        captured_messages.append(messages)
        return responses.pop(0)

    agent._call_llm = _fake_call_llm

    recall_spy: list = []
    original_recall = agent._recall_keyword_candidates

    async def _spy_recall(task):
        scored = await original_recall(task)
        recall_spy.append(scored)
        return scored

    agent._recall_keyword_candidates = _spy_recall
    return agent, captured_messages, recall_spy


@pytest.mark.asyncio
async def test_full_flow_innenhof_ueberdachung_ausschalten(monkeypatch):
    """Positive path: closed-contract entity_id passes validation, HA call executes."""
    entries = _build_entries()
    ha_client = _make_ha_client()

    # Spy on the real executor validation to capture the resolution metadata
    # (the executor's SUCCESS result carries no metadata field).
    import app.agents.light_executor as light_executor_module

    real_resolve = light_executor_module.resolve_and_validate_entity
    validation_spy: list = []

    async def _spy_resolve(*args, **kwargs):
        resolved = await real_resolve(*args, **kwargs)
        validation_spy.append(resolved)
        return resolved

    monkeypatch.setattr(light_executor_module, "resolve_and_validate_entity", _spy_resolve)

    # Stage 1 (simulated orchestrator): routing LLM selects the light agent.
    target_agent, _confidence = _routing_llm_stub(USER_TEXT)
    assert target_agent == "light-agent"

    llm_response = (
        "Ich schalte die Innenhof Überdachung aus.\n"
        '```json\n{"action": "turn_off", "entity": "Innenhof Überdachung", '
        '"entity_id": "light.innenhof_uberdachung"}\n```'
    )
    agent, captured_messages, recall_spy = _make_agent(entries, ha_client, [llm_response])

    task = DispatchTask(description=USER_TEXT, context=TaskContext(conversation_turns=[]))
    result = await agent.handle_task(task)

    # Stage 2: keyword recall.
    assert recall_spy, "keyword recall never ran"
    scored = recall_spy[0]
    recalled_ids = [entry.entity_id for entry, _hits in scored]
    assert "light.innenhof_uberdachung" in recalled_ids
    # Covers are outside the light agent's domains and never recalled.
    assert not any(eid.startswith("cover.") for eid in recalled_ids)
    # Compound logic: "Innenhofüberdachung" hits "Innenhof Überdachung".
    hits_by_id = {entry.entity_id: hits for entry, hits in scored}
    assert hits_by_id["light.innenhof_uberdachung"] >= 2  # innenhof + uberdachung

    # Stage 3: injected candidate block (closed contract).
    system_prompt = captured_messages[0][0]["content"]
    assert "Candidate entities (choose from this list only):" in system_prompt
    assert "light.innenhof_uberdachung — Innenhof Überdachung (on)" in system_prompt
    assert "You MUST emit the 'entity_id' field verbatim" in system_prompt

    # Stage 4: executor validation accepted the LLM-picked id (no matcher re-run).
    assert result.action_executed is not None
    assert result.action_executed.success is True
    assert result.action_executed.entity_id == "light.innenhof_uberdachung"
    assert validation_spy, "resolve_and_validate_entity never ran"
    resolution_path = (validation_spy[0]["resolution"]["metadata"] or {}).get("resolution_path")
    assert resolution_path == "llm_entity_id"

    # Stage 5: the HA service call that was executed.
    ha_client.call_service.assert_awaited_once()
    call_args = ha_client.call_service.await_args
    assert call_args.args[0] == "light"
    assert call_args.args[1] == "turn_off"
    assert call_args.args[2] == "light.innenhof_uberdachung"


@pytest.mark.asyncio
async def test_full_flow_hallucinated_entity_id_rejected():
    """Negative path: hallucinated entity_id is rejected fail-closed, no HA call."""
    entries = _build_entries()
    ha_client = _make_ha_client()

    target_agent, _confidence = _routing_llm_stub(USER_TEXT)
    assert target_agent == "light-agent"

    hallucinated = (
        "Ich schalte das Gerät aus.\n"
        '```json\n{"action": "turn_off", "entity": "gibts nicht", '
        '"entity_id": "light.gibts_nicht"}\n```'
    )
    clarify = "Welches Gerät im Innenhof meinst du?"
    agent, _captured, recall_spy = _make_agent(entries, ha_client, [hallucinated, clarify])

    task = DispatchTask(description=USER_TEXT, context=TaskContext(conversation_turns=[]))
    result = await agent.handle_task(task)

    scored = recall_spy[0]
    assert "light.innenhof_uberdachung" in [entry.entity_id for entry, _hits in scored]

    # Fail-closed: the hallucinated id is NOT in the recalled candidate set.
    resolution_path = (result.metadata or {}).get("resolution_path")
    assert resolution_path == "rejected_entity_id"
    assert (result.metadata or {}).get("rejected_entity_id") == "light.gibts_nicht"

    ha_client.call_service.assert_not_awaited()
    assert result.speech == clarify
    assert result.voice_followup is True
    assert result.action_executed is not None
    assert result.action_executed.success is False
