"""Tests for the read-only presence agent: app.agents.presence_executor and PresenceAgent."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

sys.modules.setdefault("litellm", MagicMock())

import app.llm.client  # noqa: E402,F401 -- force module load for patch targets
from app.agents.actionable import PresenceAgent  # noqa: E402
from app.agents.presence_executor import (  # noqa: E402
    _ALLOWED_DOMAINS,
    classify_presence,
    execute_presence_action,
)
from app.db.schema._migrations import _migrate_to_54  # noqa: E402
from app.entity.index import EntityIndex  # noqa: E402
from app.entity.ingest import parse_ha_states  # noqa: E402
from tests.helpers import make_dispatch_task, shutdown_aiosqlite  # noqa: E402
from tests.scenarios.runner import StubVectorStore  # noqa: E402

AGENT_ID = "presence-agent"

# Default seed rules for presence-agent (app.defaults.PRESENCE_AGENT_VISIBILITY_RULES).
_DEFAULT_RULES = [
    {"rule_type": "domain_include", "rule_value": "person"},
    {"rule_type": "domain_include", "rule_value": "device_tracker"},
]


def _person(entity_id: str, name: str, state: str, **attrs: Any) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": name, **attrs},
        "last_changed": "2026-11-01T08:00:00+00:00",
        "last_updated": "2026-11-01T08:00:00+00:00",
    }


def _ha_client(states: list[dict[str, Any]]) -> AsyncMock:
    by_id = {s["entity_id"]: s for s in states}
    client = AsyncMock()
    client.get_states = AsyncMock(return_value=states)
    client.get_state = AsyncMock(side_effect=lambda entity_id: by_id.get(entity_id))
    client.call_service = AsyncMock(side_effect=AssertionError("presence is read-only"))
    return client


def _index(states: list[dict[str, Any]]) -> EntityIndex:
    index = EntityIndex(StubVectorStore())
    index.populate(parse_ha_states(states))
    return index


def _rules(rules_by_agent: dict[str, list[dict[str, str]]]):
    return patch(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(side_effect=lambda agent_id: rules_by_agent.get(agent_id, [])),
    )


LAURA = _person("person.laura", "Laura", "home", user_id="u1")
MAX = _person("person.max", "Max", "not_home")
NORA = _person("person.nora", "Nora", "Work", latitude=50.0012, longitude=8.2711, gps_accuracy=12)
PHONE = _person("device_tracker.laura_phone", "Laura Phone", "home", latitude=50.1, longitude=8.3)
LIGHT = _person("light.kitchen", "Kitchen", "on")


async def _list(states: list[dict[str, Any]]) -> dict:
    with _rules({AGENT_ID: _DEFAULT_RULES}):
        return await execute_presence_action(
            {"action": "list_presence", "entity": ""}, _ha_client(states), _index(states), None, agent_id=AGENT_ID
        )


async def _query(
    states: list[dict[str, Any]], entity: str, rules: list[dict[str, str]] | None = None, entity_id: str | None = None
) -> dict:
    action: dict[str, Any] = {"action": "query_presence", "entity": entity}
    if entity_id:
        action["entity_id"] = entity_id
    with _rules({AGENT_ID: _DEFAULT_RULES if rules is None else rules}):
        return await execute_presence_action(action, _ha_client(states), _index(states), None, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("home", ("home", None)),
        ("not_home", ("away", None)),
        ("Work", ("zone", "Work")),
        ("unknown", ("unknown", None)),
        ("unavailable", ("unknown", None)),
        (None, ("unknown", None)),
    ],
)
def test_classify_presence(state, expected):
    assert classify_presence(state) == expected


# ---------------------------------------------------------------------------
# list_presence: who is home, how many
# ---------------------------------------------------------------------------


async def test_all_home():
    states = [_person("person.laura", "Laura", "home"), _person("person.max", "Max", "home"), LIGHT]
    result = await _list(states)
    assert result["success"] is True
    assert result["cacheable"] is False
    assert result["speech"] == "Everyone is at home, 2 of 2 people: Laura and Max."
    assert result["metadata"] == {"presence_home_count": 2, "presence_person_count": 2}


async def test_none_home():
    states = [_person("person.laura", "Laura", "not_home"), _person("person.max", "Max", "not_home")]
    result = await _list(states)
    assert result["success"] is True
    assert result["speech"] == "Nobody is at home, 0 of 2 people. Not at home: Laura and Max."
    assert result["metadata"]["presence_home_count"] == 0


async def test_count_with_mixed_presence_and_zone():
    states = [MAX, NORA, LAURA, _person("person.otto", "Otto", "unavailable"), PHONE, LIGHT]
    result = await _list(states)
    # Trackers and other domains are never counted; names are sorted.
    assert result["speech"] == (
        "1 of 4 people is at home: Laura. Not at home: Max and Nora (zone Work). Location unknown: Otto."
    )
    assert result["metadata"] == {"presence_home_count": 1, "presence_person_count": 4}


async def test_count_two_of_three():
    states = [LAURA, _person("person.max", "Max", "home"), NORA]
    result = await _list(states)
    assert result["speech"].startswith("2 of 3 people are at home: Laura and Max.")


async def test_single_person_household():
    result = await _list([LAURA])
    assert result["speech"] == "Laura is at home."


async def test_list_never_speaks_coordinates():
    result = await _list([NORA, PHONE])
    assert "50.0" not in result["speech"]
    assert "8.27" not in result["speech"]


async def test_list_without_visible_persons():
    states = [LAURA, MAX]
    with _rules({AGENT_ID: [{"rule_type": "domain_include", "rule_value": "device_tracker"}]}):
        result = await execute_presence_action(
            {"action": "list_presence", "entity": ""}, _ha_client(states), _index(states), None, agent_id=AGENT_ID
        )
    assert result["success"] is True
    assert result["speech"] == "No person entities are available."
    assert "Laura" not in result["speech"]


async def test_list_hides_visibility_filtered_person():
    states = [LAURA, MAX, NORA]
    with patch(
        "app.agents.presence_executor.entity_is_visible",
        AsyncMock(side_effect=lambda agent_id, entity_id, entity_index: entity_id != "person.max"),
    ):
        result = await execute_presence_action(
            {"action": "list_presence", "entity": ""}, _ha_client(states), None, None, agent_id=AGENT_ID
        )
    assert "Max" not in result["speech"]
    assert result["metadata"] == {"presence_home_count": 1, "presence_person_count": 2}


async def test_list_ha_failure_is_reported():
    client = AsyncMock()
    client.get_states = AsyncMock(side_effect=RuntimeError("boom"))
    result = await execute_presence_action({"action": "list_presence", "entity": ""}, client, None, None)
    assert result["success"] is False
    assert "boom" not in result["speech"]
    assert result["cacheable"] is False


# ---------------------------------------------------------------------------
# query_presence: one named person or tracker
# ---------------------------------------------------------------------------


async def test_named_person_home():
    result = await _query([LAURA, MAX, PHONE], "Laura")
    assert result["success"] is True
    assert result["entity_id"] == "person.laura"
    assert result["new_state"] == "home"
    assert result["speech"] == "Laura is at home."
    assert result["cacheable"] is False


async def test_named_person_away():
    result = await _query([LAURA, MAX], "Max")
    assert result["entity_id"] == "person.max"
    assert result["speech"] == "Max is not at home."


async def test_named_person_in_zone():
    result = await _query([LAURA, NORA], "Nora")
    assert result["speech"] == "Nora is not at home, but in the zone Work."
    assert "50.0" not in result["speech"]


async def test_named_person_unknown_state():
    result = await _query([_person("person.otto", "Otto", "unknown")], "Otto")
    assert result["success"] is True
    assert result["speech"] == "The location of Otto is unknown."


async def test_device_tracker_query():
    result = await _query([LAURA, PHONE], "Laura Phone")
    assert result["entity_id"] == "device_tracker.laura_phone"
    assert result["speech"] == "Laura Phone is at home."


async def test_unknown_person_name():
    result = await _query([LAURA, MAX], "Bartholomew")
    assert result["success"] is False
    assert result["entity_id"] is None
    assert result["speech"] == "I could not find a person named 'Bartholomew'."
    assert result["cacheable"] is False


async def test_visibility_filtered_person_answers_like_unknown():
    hidden_rules = [{"rule_type": "domain_include", "rule_value": "device_tracker"}]
    result = await _query([LAURA, MAX], "Laura", rules=hidden_rules)
    assert result["success"] is False
    assert result["entity_id"] is None
    assert result["speech"] == "I could not find a person named 'Laura'."


async def test_hidden_llm_entity_id_is_rejected():
    hidden_rules = [{"rule_type": "domain_include", "rule_value": "device_tracker"}]
    result = await _query([LAURA], "Laura", rules=hidden_rules, entity_id="person.laura")
    assert result["entity_id"] is None
    assert "home" not in result["speech"]


async def test_query_rejects_other_domains():
    result = await _query([LAURA, LIGHT], "Kitchen", rules=[])
    assert result["entity_id"] is None
    assert result["speech"] == "I could not find a person named 'Kitchen'."


async def test_unknown_action():
    result = await execute_presence_action({"action": "turn_on", "entity": "Laura"}, AsyncMock(), None, None)
    assert result["success"] is False
    assert result["speech"].startswith("Unknown action")


def test_allowed_domains_are_read_only_presence_domains():
    assert frozenset({"person", "device_tracker"}) == _ALLOWED_DOMAINS


# ---------------------------------------------------------------------------
# Seed and migration 54
# ---------------------------------------------------------------------------


async def _presence_rows(db) -> tuple[Any, set[tuple[str, str]]]:
    cursor = await db.execute("SELECT enabled, description FROM agent_configs WHERE agent_id = ?", (AGENT_ID,))
    config = await cursor.fetchone()
    cursor = await db.execute(
        "SELECT rule_type, rule_value FROM entity_visibility_rules WHERE agent_id = ?", (AGENT_ID,)
    )
    rules = {(row[0], row[1]) for row in await cursor.fetchall()}
    return config, rules


async def test_fresh_seed_has_disabled_presence_agent(db_repository):
    db = await aiosqlite.connect(str(db_repository))
    try:
        config, rules = await _presence_rows(db)
    finally:
        await shutdown_aiosqlite(db)
    assert config is not None and config[0] == 0
    assert rules == {("domain_include", "person"), ("domain_include", "device_tracker")}


async def test_migration_54_adds_presence_agent_and_keeps_admin_edits(db_repository):
    db = await aiosqlite.connect(str(db_repository))
    try:
        await db.execute("DELETE FROM agent_configs WHERE agent_id = ?", (AGENT_ID,))
        await db.execute("DELETE FROM entity_visibility_rules WHERE agent_id = ?", (AGENT_ID,))
        await db.commit()
        await _migrate_to_54(db)
        await db.commit()
        config, rules = await _presence_rows(db)
        assert config is not None and config[0] == 0
        assert rules == {("domain_include", "person"), ("domain_include", "device_tracker")}

        await db.execute("UPDATE agent_configs SET enabled = 1 WHERE agent_id = ?", (AGENT_ID,))
        await db.commit()
        await _migrate_to_54(db)  # idempotent, keeps the admin's choice
        await db.commit()
        config, rules = await _presence_rows(db)
        assert config[0] == 1
        assert len(rules) == 2
        cursor = await db.execute("SELECT COUNT(*) FROM schema_version WHERE version = 54")
        assert (await cursor.fetchone())[0] == 1
    finally:
        await shutdown_aiosqlite(db)


# ---------------------------------------------------------------------------
# PresenceAgent end to end (LLM stubbed)
# ---------------------------------------------------------------------------


class TestPresenceAgent:
    async def test_card_and_gating(self):
        card = PresenceAgent().agent_card
        assert card.agent_id == "presence-agent"
        assert PresenceAgent._agent_meta["db_gated"] is True

    async def test_who_is_home_turn(self):
        states = [LAURA, MAX, NORA]
        agent = PresenceAgent(ha_client=_ha_client(states), entity_index=_index(states), entity_matcher=None)
        reply = '```json\n{"action": "list_presence", "entity": "", "parameters": {}}\n```'
        with _rules({AGENT_ID: _DEFAULT_RULES}), patch("app.llm.client.complete", AsyncMock(return_value=reply)):
            result = await agent.handle_task(make_dispatch_task(description="who is home?"))
        assert result.speech.startswith("1 of 3 people is at home: Laura.")
        assert result.action_executed is not None
        assert result.action_executed.success is True

    async def test_unknown_person_gets_plain_statement_without_followup(self):
        states = [LAURA]
        agent = PresenceAgent(ha_client=_ha_client(states), entity_index=_index(states), entity_matcher=None)
        reply = '```json\n{"action": "query_presence", "entity": "Bartholomew", "parameters": {}}\n```'
        complete = AsyncMock(return_value=reply)
        with _rules({AGENT_ID: _DEFAULT_RULES}), patch("app.llm.client.complete", complete):
            result = await agent.handle_task(make_dispatch_task(description="is Bartholomew home?"))
        assert result.speech == "I could not find a person named 'Bartholomew'."
        assert not result.voice_followup
        # No second LLM call for a "which device did you mean?" clarification.
        assert complete.await_count == 1
