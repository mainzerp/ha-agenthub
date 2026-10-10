"""Timer prompt/parser contract: an empty ``entity`` means "no name".

``timer.txt`` lets the LLM emit an empty entity for an unnamed timer. The
parser accepts it for every timer action, and the executor resolves an
unnamed reference to the only running timer or asks when that is ambiguous.
Actions whose required target is missing get a clarifying question, never the
"could not understand the timer command" parse error.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("litellm", MagicMock())

from tests.helpers import make_dispatch_task  # noqa: E402

from app.agents.timer import TimerAgent  # noqa: E402
from app.models.agent import AgentErrorCode  # noqa: E402


def _scheduler(rows: list[dict] | None = None) -> MagicMock:
    scheduler = MagicMock()
    scheduler.schedule = AsyncMock(return_value="sched-1")
    scheduler.list = AsyncMock(return_value=list(rows or []))
    scheduler.cancel = AsyncMock(return_value=True)
    return scheduler


async def _run_timer(llm_response: str, scheduler: MagicMock, description: str = "timer request"):
    timer = TimerAgent()
    index = AsyncMock()
    index.list_entries_async = AsyncMock(return_value=[])
    index.get_by_id_async = AsyncMock(return_value=None)
    timer._entity_index = index
    timer._entity_matcher = None
    timer._ha_client = AsyncMock()
    with (
        patch.object(timer, "_load_prompt_async", new_callable=AsyncMock, return_value="You control timers."),
        patch.object(timer, "_call_llm", new_callable=AsyncMock, return_value=llm_response),
        patch("app.agents.timer_executor._helpers._get_scheduler", return_value=scheduler),
        patch(
            "app.agents.actionable.filter_visible_results",
            new_callable=AsyncMock,
            side_effect=lambda _agent_id, entries, _index: entries,
        ),
    ):
        return await timer.handle_task(make_dispatch_task(description=description))


def _block(action: str, parameters: str = "{}") -> str:
    return f'```json\n{{"action": "{action}", "entity": "", "parameters": {parameters}}}\n```'


@pytest.mark.asyncio
async def test_unnamed_start_timer_with_empty_entity_executes():
    scheduler = _scheduler()
    result = await _run_timer(_block("start_timer", '{"duration": "00:05:00"}'), scheduler, "set a timer for 5 minutes")

    assert result.error is None
    scheduler.schedule.assert_awaited_once()
    assert scheduler.schedule.await_args.kwargs["logical_name"] == "5 minutes timer"
    assert result.speech == "Started 5 minutes timer for 5 minutes."
    assert result.action_executed is not None
    assert result.action_executed.action == "start_timer"


@pytest.mark.asyncio
async def test_unnamed_cancel_timer_with_one_running_timer_cancels_it():
    scheduler = _scheduler([{"id": "t1", "logical_name": "5 minutes timer", "kind": "plain", "state": "pending"}])
    result = await _run_timer(_block("cancel_timer"), scheduler, "cancel the timer")

    assert result.error is None
    scheduler.cancel.assert_awaited_once_with(id_="t1")
    assert result.speech == "Cancelled 5 minutes timer."


@pytest.mark.asyncio
async def test_unnamed_cancel_timer_with_two_running_timers_asks():
    scheduler = _scheduler(
        [
            {"id": "t1", "logical_name": "pasta", "kind": "plain", "state": "pending"},
            {"id": "t2", "logical_name": "eggs", "kind": "plain", "state": "pending"},
        ]
    )
    result = await _run_timer(_block("cancel_timer"), scheduler, "cancel the timer")

    scheduler.cancel.assert_not_awaited()
    assert result.error is None
    assert "pasta" in result.speech and "eggs" in result.speech
    assert "which one to cancel" in result.speech
    assert result.voice_followup is True


@pytest.mark.asyncio
async def test_unnamed_query_timer_with_empty_entity_executes():
    scheduler = _scheduler([{"id": "t1", "logical_name": "pasta", "kind": "plain", "state": "pending", "fires_at": 0}])
    result = await _run_timer(_block("query_timer"), scheduler, "how long is left")

    assert result.error is None
    assert result.speech.startswith("pasta has")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters", "question"),
    [
        (
            "delayed_action",
            '{"delay_duration": "00:30:00", "target_action": "light/turn_off"}',
            "Which device should I control when the delay ends?",
        ),
        ("sleep_timer", '{"duration": "00:30:00"}', "Which media player should the sleep timer stop?"),
    ],
)
async def test_action_missing_its_target_gets_clarification(action, parameters, question):
    scheduler = _scheduler()
    result = await _run_timer(_block(action, parameters), scheduler, "schedule something")

    scheduler.schedule.assert_not_awaited()
    assert result.speech == question
    assert result.voice_followup is True
    assert result.error is None or result.error.code != AgentErrorCode.PARSE_ERROR


@pytest.mark.asyncio
async def test_llm_clarifying_question_reaches_user_instead_of_parse_error():
    """The LLM asks for a missing duration (prompt contract): no PARSE_ERROR."""
    result = await _run_timer("For how long should I set the timer?", _scheduler(), "set a timer")

    assert result.error is None
    assert result.speech == "For how long should I set the timer?"
    assert result.voice_followup is True


@pytest.mark.asyncio
async def test_rejected_unknown_timer_action_asks_to_rephrase_not_for_a_device():
    result = await _run_timer(_block("set_timer", '{"duration": "00:05:00"}'), _scheduler(), "set a timer")

    assert result.error is None
    assert result.voice_followup is True
    assert "device" not in result.speech.lower()
    assert "rephrase" in result.speech.lower()


@pytest.mark.asyncio
async def test_prose_without_action_or_question_stays_parse_error():
    result = await _run_timer("Starting the timer now.", _scheduler(), "set a timer for 5 minutes")

    assert result.error is not None
    assert result.error.code == AgentErrorCode.PARSE_ERROR
