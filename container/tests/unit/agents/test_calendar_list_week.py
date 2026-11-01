"""Regression: calendar "this week" style turns must reach list_events.

Live traces 169b477c82814280 and de3ebb58c0f146f5 ended in the generic
"I could not understand the calendar command" although the router picked
calendar-agent. Root causes covered here:

- the closed-contract candidate block ("none fits -> ask a clarifying
  question") was injected for calendar although no calendar action needs a
  recalled entity, so the LLM asked "which calendar?" instead of emitting
  list_events;
- the weekday of "today" was missing from the time context, so "this week",
  "Friday" and "next week" could not be resolved to real dates;
- a clarifying question followed by more text (options, a hint) was not
  recognised as a question, so the LLM's useful reply was replaced by the
  generic error.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("litellm", MagicMock())

from tests.helpers import make_dispatch_task  # noqa: E402

from app.agents.action_executor import parse_actions  # noqa: E402
from app.agents.base import BaseAgent  # noqa: E402
from app.agents.calendar import CalendarAgent  # noqa: E402
from app.models.agent import AgentErrorCode, TaskContext  # noqa: E402

pytestmark = pytest.mark.asyncio

# Real replies captured in the two live traces (llm_call span metadata).
_LIVE_WHICH_CALENDAR = (
    "Welchen Kalender möchtest du sehen? Zur Auswahl stehen:\n\n"
    "- Laura\n- Patric\n- Geburtstage\n- Deutschland, RP\n- LLM Vision Timeline"
)
_LIVE_WHICH_EVENT = (
    "Könntest du mir sagen, um welchen Termin es sich handelt und zu welcher Uhrzeit er "
    "stattfinden soll? Dann kann ich ihn für den 10. anlegen."
)

_WEEK_ACTION = (
    '{"action": "list_events", "parameters": {"start_date_time": "2026-10-18 15:09:00", '
    '"end_date_time": "2026-10-18 23:59:59"}}'
)


def _calendar_entry(slug: str, name: str):
    return SimpleNamespace(
        entity_id=f"calendar.{slug}",
        friendly_name=name,
        aliases=[],
        area=None,
        area_name=None,
        device_name=None,
        id_tokens=[slug],
        state="off",
        domain="calendar",
    )


_CALENDARS = [
    _calendar_entry("laura", "Laura"),
    _calendar_entry("patric", "Patric"),
    _calendar_entry("geburtstage", "Geburtstage"),
    _calendar_entry("deutschland_rp", "Deutschland, RP"),
    _calendar_entry("llm_vision_timeline", "LLM Vision Timeline"),
]


def _visible_passthrough():
    return patch(
        "app.agents.actionable.filter_visible_results",
        new_callable=AsyncMock,
        side_effect=lambda _agent_id, entries, _index: entries,
    )


async def _run(llm_response: str, *, description="Termine diese Woche im Kalender anzeigen", entries=None):
    """One calendar turn with a canned LLM reply; returns (result, system_prompt, do_execute mock)."""
    agent = CalendarAgent()
    index = AsyncMock()
    index.list_entries_async = AsyncMock(return_value=list(_CALENDARS if entries is None else entries))
    index.get_by_id_async = AsyncMock(return_value=None)
    agent._entity_index = index
    agent._entity_matcher = None
    agent._ha_client = AsyncMock()
    context = TaskContext(language="de", local_time="2026-10-18 15:09", timezone="Europe/Berlin")
    task = make_dispatch_task(description=description, context=context)
    with (
        patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="Calendar prompt."),
        patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value=llm_response) as mock_llm,
        patch.object(
            agent,
            "_do_execute",
            new_callable=AsyncMock,
            return_value={"success": True, "entity_id": None, "speech": "Events: Meeting."},
        ) as mock_exec,
        _visible_passthrough(),
    ):
        result = await agent.handle_task(task)
    return result, mock_llm.call_args.args[0][0]["content"], mock_exec


class TestParseListEvents:
    """The parser accepts every realistic list_events shape (it was never the culprit)."""

    @pytest.mark.parametrize(
        "reply",
        [
            _WEEK_ACTION,
            f"```json\n{_WEEK_ACTION}\n```\nHere is what is coming up this week.",
            f"Sure, I will look at your calendar.\n{_WEEK_ACTION}\nOne moment.",
            _WEEK_ACTION.replace("2026-10-18 15:09:00", "2026-10-18T15:09:00").replace(
                "2026-10-18 23:59:59", "2026-10-18T23:59:59"
            ),
        ],
    )
    async def test_week_listing_parses_without_entity(self, reply):
        actions = parse_actions(reply)
        assert len(actions) == 1
        assert actions[0]["action"] == "list_events"
        assert actions[0]["parameters"]["start_date_time"].startswith("2026-10-18")

    async def test_week_listing_without_calendar_param_is_accepted(self):
        actions = parse_actions(_WEEK_ACTION)
        assert actions and "calendar" not in actions[0]["parameters"]


class TestCandidateBlockIsOptional:
    async def test_candidates_do_not_force_a_calendar_question(self):
        result, system_prompt, mock_exec = await _run(_WEEK_ACTION)

        assert "Candidate entities (choose from this list only)" not in system_prompt
        assert "ask a short clarifying question" not in system_prompt
        assert "You MUST emit the 'entity_id' field" not in system_prompt
        assert "optional context" in system_prompt
        assert "NEVER ask the user to pick" in system_prompt
        for entry in _CALENDARS:
            assert entry.entity_id in system_prompt
        mock_exec.assert_awaited_once()
        assert result.error is None

    async def test_all_calendars_tied_do_not_add_ambiguity_note(self):
        tied = [_calendar_entry("a_cal", "Home"), _calendar_entry("b_cal", "Home")]
        _result, system_prompt, _exec = await _run(_WEEK_ACTION, description="Home calendar", entries=tied)
        assert "ambiguous" not in system_prompt
        assert "Do not guess" not in system_prompt


class TestWeekdayInTimeContext:
    async def test_weekday_follows_local_time(self):
        ctx = TaskContext(local_time="2026-10-18 15:09", timezone="Europe/Berlin")
        assert "Current local time: 2026-10-18 15:09 (Sunday)" in BaseAgent._build_time_location_context(ctx)
        ctx = TaskContext(local_time="2024-06-13 10:30", timezone="UTC")
        assert "Current local time: 2024-06-13 10:30 (Thursday)" in BaseAgent._build_time_location_context(ctx)

    async def test_unparseable_local_time_is_kept_as_is(self):
        ctx = TaskContext(local_time="soon", timezone="UTC")
        assert BaseAgent._build_time_location_context(ctx) == "Current local time: soon"

    async def test_calendar_turn_prompt_carries_weekday(self):
        _result, system_prompt, _exec = await _run(_WEEK_ACTION)
        assert "Current local time: 2026-10-18 15:09 (Sunday)" in system_prompt


class TestPromptCoversWeekRanges:
    async def test_prompt_documents_week_rules_and_examples(self):
        text = CalendarAgent()._load_prompt("calendar")
        assert "this week" in text and "next week" in text
        assert "NEVER ask which calendar" in text
        for needle in ("2024-06-13 10:30:00", "2024-06-16 23:59:59", "2024-06-17 00:00:00", "2024-06-23 23:59:59"):
            assert needle in text
        assert text.count('"action": "list_events"') >= 5


class TestParseMissKeepsTheLlmQuestion:
    @pytest.mark.parametrize("reply", [_LIVE_WHICH_CALENDAR, _LIVE_WHICH_EVENT])
    async def test_question_followed_by_text_reaches_the_user(self, reply):
        result, _prompt, mock_exec = await _run(reply)

        mock_exec.assert_not_awaited()
        assert result.error is None
        assert result.speech == reply
        assert result.voice_followup is True

    @pytest.mark.parametrize(
        "reply",
        [
            "Das Licht ist an. Brauchst du noch etwas? Ich bin hier.",
            "I checked the list. Which one did you want? That is all I know.",
            "Welches? " + "Das ist eine lange Antwort. " * 20,
        ],
    )
    async def test_answer_with_a_stray_question_mark_is_not_a_clarification(self, reply):
        result, _prompt, _exec = await _run(reply)

        assert result.error is not None
        assert result.error.code == AgentErrorCode.PARSE_ERROR
        assert result.voice_followup is not True

    async def test_non_question_prose_is_a_descriptive_parse_error(self, caplog):
        with caplog.at_level("WARNING", logger="app.agents.calendar"):
            result, _prompt, _exec = await _run("That is not something I can do.")

        assert result.error is not None
        assert result.error.code == AgentErrorCode.PARSE_ERROR
        assert "I could not understand the calendar command. Please try again." not in result.speech
        assert "day or time range" in result.speech
        assert result.metadata.get("parse_miss") == "no_action"
        assert any("raw LLM output: That is not something I can do." in r.getMessage() for r in caplog.records)
