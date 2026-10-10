"""Default timer-agent visibility for alarm helpers and announcement satellites (issue #132).

AlarmMonitor rings a labelled ``input_datetime`` helper only when it is
visible to ``timer-agent``, and timer/alarm announcements skip
``assist_satellite`` targets invisible to it. Migration 46 and the seed
restore both ``domain_include`` rules so default installs work without a
visibility change.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.agents import background_actions as ba
from app.agents.alarm_monitor import AlarmMonitor
from app.agents.timer import TimerAgent
from app.db.schema import _create_indexes, _create_tables, _seed_defaults
from app.db.schema._migrations import _migrate_to_46, _run_migrations
from app.entity.visibility import entity_is_visible
from tests.helpers import shutdown_aiosqlite

_OLD_TIMER_DEFAULTS = {
    ("domain_include", "persistent_notification"),
    ("domain_include", "media_player"),
}
_RESTORED = {
    ("domain_include", "input_datetime"),
    ("domain_include", "assist_satellite"),
}


async def _fresh_install(path: Path) -> None:
    """Run the same steps as ``init_db`` on a new database file."""
    db = await aiosqlite.connect(str(path))
    try:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


async def _timer_rules(path: Path) -> list[tuple[str, str]]:
    db = await aiosqlite.connect(str(path))
    try:
        cursor = await db.execute(
            "SELECT rule_type, rule_value FROM entity_visibility_rules WHERE agent_id = 'timer-agent' "
            "ORDER BY rule_type, rule_value"
        )
        return [(row[0], row[1]) for row in await cursor.fetchall()]
    finally:
        await shutdown_aiosqlite(db)


async def _db_at_version_45(path: Path, timer_rules: set[tuple[str, str]]) -> None:
    """Simulate an install upgraded up to schema 45 with the given timer-agent rules."""
    await _fresh_install(path)
    db = await aiosqlite.connect(str(path))
    try:
        await db.execute("DELETE FROM entity_visibility_rules WHERE agent_id = 'timer-agent'")
        await db.executemany(
            "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES ('timer-agent', ?, ?)",
            sorted(timer_rules),
        )
        await db.execute("DELETE FROM schema_version WHERE version > 45")
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


async def _upgrade(path: Path) -> None:
    db = await aiosqlite.connect(str(path))
    try:
        await _run_migrations(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


# ---------------------------------------------------------------------------
# Migration 46 and seed
# ---------------------------------------------------------------------------


async def test_migration_adds_both_rules_to_old_default_rules(tmp_path: Path) -> None:
    path = tmp_path / "old_defaults.db"
    await _db_at_version_45(path, _OLD_TIMER_DEFAULTS)

    await _upgrade(path)

    assert set(await _timer_rules(path)) == _OLD_TIMER_DEFAULTS | _RESTORED


async def test_migration_leaves_timer_agent_without_include_rules_unchanged(tmp_path: Path) -> None:
    # No domain_include rule means "every domain visible"; adding an include
    # would restrict the agent, so the migration must skip it.
    path = tmp_path / "no_includes.db"
    area_only = {("area_exclude", "garage")}
    await _db_at_version_45(path, area_only)

    await _upgrade(path)

    assert set(await _timer_rules(path)) == area_only


async def test_migration_leaves_timer_agent_without_any_rules_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "no_rules.db"
    await _db_at_version_45(path, set())

    await _upgrade(path)

    assert await _timer_rules(path) == []


async def test_migration_respects_admin_domain_exclude(tmp_path: Path) -> None:
    path = tmp_path / "excluded.db"
    rules = _OLD_TIMER_DEFAULTS | {("domain_exclude", "assist_satellite")}
    await _db_at_version_45(path, rules)

    await _upgrade(path)

    assert set(await _timer_rules(path)) == rules | {("domain_include", "input_datetime")}


async def test_fresh_install_has_both_rules(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    await _fresh_install(path)

    assert set(await _timer_rules(path)) == _OLD_TIMER_DEFAULTS | _RESTORED


async def test_fresh_and_migrated_install_are_identical_and_free_of_duplicates(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    migrated = tmp_path / "migrated.db"
    await _fresh_install(fresh)
    await _db_at_version_45(migrated, _OLD_TIMER_DEFAULTS)
    await _upgrade(migrated)

    # Re-running startup (seed + migrations) and migration 46 itself is a no-op.
    await _fresh_install(fresh)
    db = await aiosqlite.connect(str(fresh))
    try:
        await _migrate_to_46(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)

    fresh_rules = await _timer_rules(fresh)
    assert fresh_rules == await _timer_rules(migrated)
    assert len(fresh_rules) == len(set(fresh_rules))


# ---------------------------------------------------------------------------
# Runtime behaviour with the default rules
# ---------------------------------------------------------------------------


@pytest.fixture()
async def default_rules_db(db_repository: Path) -> Path:
    """Repository-patched temp DB with seed data AND all migrations applied."""
    await _upgrade(db_repository)
    return db_repository


async def test_default_rules_hide_unrelated_domains(default_rules_db: Path) -> None:
    assert await entity_is_visible("timer-agent", "input_datetime.wake_up", None) is True
    assert await entity_is_visible("timer-agent", "assist_satellite.kitchen", None) is True
    assert await entity_is_visible("timer-agent", "light.kitchen", None) is False


async def test_alarm_monitor_rings_labelled_helper_with_default_rules(default_rules_db: Path) -> None:
    entry = SimpleNamespace(
        entity_id="input_datetime.dishwasher_start",
        friendly_name="Dishwasher Start",
        state="08:30:00",
        has_date=False,
        has_time=True,
    )
    entity_index = MagicMock(spec=["list_entries_async"])
    entity_index.list_entries_async = AsyncMock(return_value=[entry])
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    ha_client = MagicMock(spec=["render_template"])
    ha_client.render_template = AsyncMock(return_value="input_datetime.dishwasher_start")
    settings_repo = SimpleNamespace(get_value=AsyncMock(return_value="agenthub_alarm"))
    monitor = AlarmMonitor(entity_index, dispatcher, ha_client=ha_client, settings_repo=settings_repo)
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 4, 24, 8, 30, 10)

    with patch("app.agents.alarm_monitor.datetime", clock):
        await monitor._check_alarms()

    dispatcher.dispatch.assert_awaited_once()


async def test_announcement_satellite_resolves_with_default_rules(default_rules_db: Path) -> None:
    satellite = SimpleNamespace(entity_id="assist_satellite.kitchen_pi", area="kitchen", domain="assist_satellite")
    entity_index = MagicMock(spec=["list_entries_async"])
    entity_index.list_entries_async = AsyncMock(return_value=[satellite])

    got = await ba._resolve_satellite_device(MagicMock(), "kitchen", entity_index=entity_index)

    assert got == "assist_satellite.kitchen_pi"


def test_timer_agent_recall_never_offers_alarm_helpers() -> None:
    # Visibility of input_datetime is for AlarmMonitor only; keyword recall
    # must not list helpers as LLM action candidates.
    assert "input_datetime" not in TimerAgent._allowed_domains
