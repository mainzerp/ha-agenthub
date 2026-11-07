"""User-facing times use the Home Assistant time zone, not the container's (UTC)."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.agents import background_actions as ba
from app.agents.general import GeneralAgent, _memory_date_label
from app.api.routes.admin import _timers as admin_timers
from app.ha_client import home_context as hc

BERLIN = ZoneInfo("Europe/Berlin")


def _epoch(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """UTC wall-clock time to an epoch."""
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


@pytest.fixture(autouse=True)
def container_zone_utc():
    """Run with a UTC container zone where the platform supports it (POSIX)."""
    if not hasattr(time, "tzset"):
        yield
        return
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _fresh_provider(time_zone: str | None, *, fail: bool = False) -> hc.HomeContextProvider:
    provider = hc.HomeContextProvider()
    provider._load_overrides = AsyncMock(return_value=None)  # type: ignore[method-assign]
    ha_client = MagicMock()
    if fail:
        ha_client.get_config = AsyncMock(side_effect=RuntimeError("ha down"))
    else:
        ha_client.get_config = AsyncMock(return_value={"time_zone": time_zone, "location_name": "Home"})
    provider.ha_client = ha_client  # type: ignore[attr-defined]
    return provider


class TestSharedHelpers:
    def test_format_epoch_in_zone_crosses_midnight(self):
        # 23:30 UTC on Nov 7 is 00:30 on Nov 8 in Berlin (CET, UTC+1).
        epoch = _epoch(2026, 11, 7, 23, 30)
        assert hc.format_epoch_in_zone(epoch, UTC) == "2026-11-07 23:30:00"
        assert hc.format_epoch_in_zone(epoch, BERLIN) == "2026-11-08 00:30:00"

    def test_format_epoch_in_zone_dst_end(self):
        # 2026-10-25: CEST ends at 01:00 UTC; 00:30 UTC is 02:30 CEST, 01:30 UTC is 02:30 CET.
        assert hc.format_epoch_in_zone(_epoch(2026, 10, 25, 0, 30), BERLIN, "%H:%M %Z") == "02:30 CEST"
        assert hc.format_epoch_in_zone(_epoch(2026, 10, 25, 1, 30), BERLIN, "%H:%M %Z") == "02:30 CET"

    def test_format_epoch_in_zone_dst_start(self):
        # 2026-03-29: CET ends at 01:00 UTC; 00:30 UTC is 01:30 CET, 01:30 UTC is 03:30 CEST.
        assert hc.format_epoch_in_zone(_epoch(2026, 3, 29, 0, 30), BERLIN) == "2026-03-29 01:30:00"
        assert hc.format_epoch_in_zone(_epoch(2026, 3, 29, 1, 30), BERLIN) == "2026-03-29 03:30:00"

    def test_format_epoch_in_zone_unknown_zone_uses_container_local(self):
        epoch = _epoch(2026, 11, 7, 23, 30)
        assert hc.format_epoch_in_zone(epoch, None) == datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")

    @pytest.mark.parametrize("name", [None, "", "  ", "Not/AZone", 42])
    def test_parse_zone_rejects_empty_and_invalid(self, name):
        assert hc.parse_zone(name) is None

    def test_parse_zone_accepts_iana_name(self):
        assert hc.parse_zone("Europe/Berlin") == BERLIN

    async def test_resolve_home_zone_uses_cached_provider(self):
        provider = _fresh_provider("Europe/Berlin")
        with patch.object(hc, "home_context_provider", provider):
            assert await hc.resolve_home_zone(provider.ha_client) == BERLIN  # type: ignore[attr-defined]
            assert await hc.resolve_home_zone(provider.ha_client) == BERLIN  # type: ignore[attr-defined]
        provider.ha_client.get_config.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_resolve_home_zone_unknown_cases(self):
        assert await hc.resolve_home_zone(None) is None
        provider = _fresh_provider("Mars/Olympus")
        with patch.object(hc, "home_context_provider", provider):
            assert await hc.resolve_home_zone(provider.ha_client) is None  # type: ignore[attr-defined]
        broken = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("boom")))
        with patch.object(hc, "home_context_provider", broken):
            assert await hc.resolve_home_zone(MagicMock()) is None


class TestMissedTimerSummary:
    """background_actions.py: missed-while-offline due times."""

    def test_timer_due_time_is_home_time_across_midnight(self):
        # Due 23:30 UTC on Nov 7 = 00:30 on Nov 8 in Berlin; "now" is 00:45 Berlin the same day.
        due = _epoch(2026, 11, 7, 23, 30)
        now = _epoch(2026, 11, 7, 23, 45)
        text = ba._missed_summary([{"name": "pasta", "kind": "timer", "due_epoch": due}], BERLIN, now)
        assert text == "While AgentHub or Home Assistant was offline, these were missed: timer 'pasta' (due 00:30)."

    def test_other_day_includes_date_in_home_zone(self):
        due = _epoch(2026, 11, 7, 23, 30)
        now = _epoch(2026, 11, 9, 12, 0)
        assert "(due 2026-11-08 00:30)" in ba._missed_summary([{"kind": "timer", "due_epoch": due}], BERLIN, now)

    def test_dst_day_after_clock_change(self):
        # 2026-10-25 01:30 UTC is 02:30 CET (after the change).
        due = _epoch(2026, 10, 25, 1, 30)
        now = _epoch(2026, 10, 25, 9, 0)
        assert "(due 02:30)" in ba._missed_summary([{"kind": "alarm", "name": "w", "due_epoch": due}], BERLIN, now)

    async def test_timer_row_without_payload_zone_uses_home_zone(self):
        due = _epoch(2020, 11, 7, 23, 30)
        with (
            patch.object(ba, "resolve_home_zone", new=AsyncMock(return_value=BERLIN)),
            patch.object(ba, "dispatch_text_notification", new=AsyncMock()) as notify,
        ):
            await ba.dispatch_missed_notification(
                MagicMock(), missed=[{"name": "pasta", "kind": "timer", "due_epoch": due}], timezone=None
            )
        assert "timer 'pasta' (due 2020-11-08 00:30)" in notify.await_args.kwargs["text"]

    async def test_payload_zone_wins_over_home_zone(self):
        due = _epoch(2020, 11, 7, 23, 30)
        resolver = AsyncMock(return_value=BERLIN)
        with (
            patch.object(ba, "resolve_home_zone", new=resolver),
            patch.object(ba, "dispatch_text_notification", new=AsyncMock()) as notify,
        ):
            await ba.dispatch_missed_notification(
                MagicMock(), missed=[{"kind": "timer", "due_epoch": due}], timezone="UTC"
            )
        assert "(due 2020-11-07 23:30)" in notify.await_args.kwargs["text"]
        resolver.assert_not_awaited()

    async def test_unknown_home_zone_falls_back_to_container_local(self):
        due = _epoch(2020, 11, 7, 23, 30)
        expected = datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M")
        with (
            patch.object(ba, "resolve_home_zone", new=AsyncMock(return_value=None)),
            patch.object(ba, "dispatch_text_notification", new=AsyncMock()) as notify,
        ):
            await ba.dispatch_missed_notification(
                MagicMock(), missed=[{"kind": "timer", "due_epoch": due}], timezone="Not/AZone"
            )
        assert f"(due {expected})" in notify.await_args.kwargs["text"]


class _Scheduler:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def list(self):
        return self._rows


def _alarm_row(fires_at: int, payload: dict | None = None) -> dict:
    return {
        "id": "a1",
        "kind": "alarm",
        "logical_name": "wake",
        "fires_at": fires_at,
        "payload_json": json.dumps(payload or {}),
        "briefing": "0",
    }


def _request(rows: list[dict]) -> SimpleNamespace:
    state = SimpleNamespace(ha_client=None, timer_scheduler=_Scheduler(rows))
    return SimpleNamespace(app=SimpleNamespace(state=state))


class TestAdminAlarmState:
    """Timers page 'Scheduled Time' column (server-formatted state)."""

    async def test_alarm_state_is_home_time(self):
        fires_at = _epoch(2026, 11, 7, 6, 0)  # 07:00 in Berlin (CET)
        with patch.object(admin_timers, "resolve_home_zone", new=AsyncMock(return_value=BERLIN)):
            data = await admin_timers.get_timers_info(_request([_alarm_row(fires_at)]))
        assert data["alarms"][0]["state"] == "2026-11-07 07:00:00"
        assert data["alarms"][0]["fires_at"] == fires_at
        assert data["timezone"] == "Europe/Berlin"

    async def test_alarm_state_crosses_midnight(self):
        fires_at = _epoch(2026, 11, 7, 23, 30)
        with patch.object(admin_timers, "resolve_home_zone", new=AsyncMock(return_value=BERLIN)):
            data = await admin_timers.get_timers_info(_request([_alarm_row(fires_at)]))
        assert data["alarms"][0]["state"] == "2026-11-08 00:30:00"

    async def test_alarm_state_dst_summer_and_winter(self):
        summer = _epoch(2026, 3, 29, 1, 30)  # 03:30 CEST
        winter = _epoch(2026, 10, 25, 1, 30)  # 02:30 CET
        with patch.object(admin_timers, "resolve_home_zone", new=AsyncMock(return_value=BERLIN)):
            data = await admin_timers.get_timers_info(_request([_alarm_row(summer), _alarm_row(winter)]))
        assert sorted(a["state"] for a in data["alarms"]) == ["2026-03-29 03:30:00", "2026-10-25 02:30:00"]

    async def test_unknown_home_zone_uses_payload_zone_then_container_local(self):
        fires_at = _epoch(2026, 11, 7, 6, 0)
        with patch.object(admin_timers, "resolve_home_zone", new=AsyncMock(return_value=None)):
            with_payload = await admin_timers.get_timers_info(
                _request([_alarm_row(fires_at, {"timezone": "Europe/Berlin"})])
            )
            without = await admin_timers.get_timers_info(_request([_alarm_row(fires_at)]))
        assert with_payload["alarms"][0]["state"] == "2026-11-07 07:00:00"
        assert with_payload["timezone"] is None
        assert without["alarms"][0]["state"] == datetime.fromtimestamp(fires_at).strftime("%Y-%m-%d %H:%M:%S")


class TestMemoryDateLabel:
    """general.py: memory matches are dated in the task's zone."""

    def test_label_uses_task_zone_across_midnight(self):
        # 2026-11-05 20:00 Pacific = 04:00 UTC on Nov 6.
        epoch = _epoch(2026, 11, 6, 4, 0)
        assert _memory_date_label(epoch, "America/Los_Angeles") == "2026-11-05"
        assert _memory_date_label(epoch, "UTC") == "2026-11-06"
        # 00:30 Berlin on Nov 6 (CET) = 23:30 UTC on Nov 5.
        assert _memory_date_label(_epoch(2026, 11, 5, 23, 30), "Europe/Berlin") == "2026-11-06"

    def test_label_dst_boundary(self):
        # 2026-03-29 23:30 UTC is 01:30 CEST on Mar 30.
        assert _memory_date_label(_epoch(2026, 3, 29, 23, 30), "Europe/Berlin") == "2026-03-30"
        # 2026-10-24 22:30 UTC is 00:30 CEST on Oct 25.
        assert _memory_date_label(_epoch(2026, 10, 24, 22, 30), "Europe/Berlin") == "2026-10-25"

    @pytest.mark.parametrize("zone", [None, "", "Not/AZone"])
    def test_unknown_zone_falls_back_to_utc(self, zone):
        assert _memory_date_label(_epoch(2026, 11, 5, 23, 30), zone) == "2026-11-05"

    def test_bad_epoch_is_unknown_date(self):
        assert _memory_date_label("x", "Europe/Berlin") == "unknown date"

    def test_rendered_memory_block_uses_zone(self):
        match = {
            "similarity": 0.9,
            "last_turn_at": _epoch(2026, 11, 5, 23, 30),
            "snippet_turns": [{"user_text": "hi", "response_text": "hello"}],
        }
        berlin = GeneralAgent()._render_memory_context([match], "Europe/Berlin")
        default = GeneralAgent()._render_memory_context([match])
        assert "2026-11-06]" in berlin
        assert "2026-11-05]" in default
