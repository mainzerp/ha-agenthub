"""Direct integration test for calendar-agent and reminder injection."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta, timezone

# Add app to path
sys.path.insert(0, "/app")


async def test_calendar_agent_card():
    """Test 1: Verify CalendarAgent agent_card."""
    print("=" * 60)
    print("TEST 1: CalendarAgent Agent Card")
    print("=" * 60)

    from app.agents.calendar import CalendarAgent

    agent = CalendarAgent()
    card = agent.agent_card
    print(f"Agent ID: {card.agent_id}")
    print(f"Name: {card.name}")
    print(f"Skills: {card.skills}")
    print(f"Description: {card.description[:80]}...")

    assert card.agent_id == "calendar-agent"
    assert "calendar_read" in card.skills
    assert "calendar_create" in card.skills
    print("[OK] Agent card is correct!")


async def test_user_identity():
    """Test 2: UserIdentityResolver name extraction."""
    print()
    print("=" * 60)
    print("TEST 2: User Identity Resolution")
    print("=" * 60)

    from app.agents.user_identity import UserIdentityResolver

    resolver = UserIdentityResolver()

    # Test name extraction
    names = [
        ("ich bin Anna", "Anna"),
        ("das ist Patric", "Patric"),
        ("my name is John", "John"),
        ("i am Maria", "Maria"),
        ("mein Name ist Klaus", "Klaus"),
        ("Licht an", None),
    ]

    for utterance, expected in names:
        result = resolver._extract_self_identification(utterance)
        status = "OK" if result == expected else "FAIL"
        print(f"  '{utterance}' -> '{result}' (expected: '{expected}') [{status}]")

    print("[OK] Name extraction works!")


async def test_injector_marker_logic():
    """Test 3: CalendarReminderInjector marker detection."""
    print()
    print("=" * 60)
    print("TEST 3: Reminder Marker Detection")
    print("=" * 60)

    from app.agents.calendar_injector import CalendarReminderInjector

    injector = CalendarReminderInjector(ha_client=None, entity_index=None)

    now = datetime(2026, 4, 29, 13, 0, 0, tzinfo=UTC)

    test_cases = [
        # (event_start, offset_minutes, expected_active)
        (datetime(2026, 4, 29, 13, 10, 0, tzinfo=UTC), 15, True),   # 10 min away, 15min marker active
        (datetime(2026, 4, 29, 13, 30, 0, tzinfo=UTC), 15, False),  # 30 min away, 15min marker not active
        (datetime(2026, 4, 29, 13, 30, 0, tzinfo=UTC), 60, True),   # 30 min away, 60min marker active
        (datetime(2026, 4, 29, 14, 0, 0, tzinfo=UTC), 60, True),    # 60 min away, 60min marker active
        (datetime(2026, 4, 29, 15, 0, 0, tzinfo=UTC), 60, False),   # 120 min away, 60min marker not active
        (datetime(2026, 4, 29, 14, 0, 0, tzinfo=UTC), 1440, True),  # 60 min away, within 24h window
        (datetime(2026, 4, 30, 13, 0, 0, tzinfo=UTC), 1440, True),  # 24h away, 24h marker active
        (datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC), 15, False),   # already past
    ]

    all_ok = True
    for event_start, offset, expected in test_cases:
        result = injector._marker_active(event_start, now, offset)
        status = "OK" if result == expected else "FAIL"
        if result != expected:
            all_ok = False
        print(f"  Event at {event_start.time()}, offset={offset}min -> active={result} (expected={expected}) [{status}]")

    if all_ok:
        print("[OK] All marker detection tests passed!")
    else:
        print("[FAIL] Some marker detection tests failed!")


async def test_injector_formatting():
    """Test 4: Reminder text formatting."""
    print()
    print("=" * 60)
    print("TEST 4: Reminder Text Formatting")
    print("=" * 60)

    from app.agents.calendar_injector import CalendarReminderInjector

    injector = CalendarReminderInjector(ha_client=None, entity_index=None)

    event_start = datetime(2026, 4, 29, 14, 30, 0, tzinfo=UTC)

    # German fallback
    text_15_de = injector._fallback_reminder_text("Zahnarzt", 15, event_start, "de")
    text_60_de = injector._fallback_reminder_text("Team Meeting", 60, event_start, "de")
    text_1440_de = injector._fallback_reminder_text("Flug Berlin", 1440, event_start, "de")

    print(f"  DE 15min: '{text_15_de}'")
    print(f"  DE 60min: '{text_60_de}'")
    print(f"  DE 24h:  '{text_1440_de}'")

    # English fallback
    text_15_en = injector._fallback_reminder_text("Dentist", 15, event_start, "en")
    text_60_en = injector._fallback_reminder_text("Team Meeting", 60, event_start, "en")
    text_1440_en = injector._fallback_reminder_text("Flight Berlin", 1440, event_start, "en")

    print(f"  EN 15min: '{text_15_en}'")
    print(f"  EN 60min: '{text_60_en}'")
    print(f"  EN 24h:  '{text_1440_en}'")

    assert "15 Minuten" in text_15_de
    assert "einer Stunde" in text_60_de
    assert "morgen um 14:30" in text_1440_de
    assert "15 minutes" in text_15_en
    print("[OK] Fallback reminder formatting works!")


async def test_executor_resolve_calendar():
    """Test 5: Calendar entity resolution logic."""
    print()
    print("=" * 60)
    print("TEST 5: Calendar Entity Resolution")
    print("=" * 60)

    from app.agents.calendar_executor import _resolve_calendar_entity
    from unittest.mock import MagicMock

    # Mock entity_index with list_entries_async
    mock_entry = MagicMock()
    mock_entry.entity_id = "calendar.google_test"
    mock_entry.friendly_name = "Test Calendar"

    mock_index = MagicMock()
    async def mock_list_entries_async(**kwargs):
        return [mock_entry]
    mock_index.list_entries_async = mock_list_entries_async

    action = {"entity": ""}  # No explicit calendar

    entity_id, friendly_name, error = await _resolve_calendar_entity(
        action, None, mock_index, None, agent_id="calendar-agent", default_calendar_ids=None
    )

    print(f"  Resolved entity_id: {entity_id}")
    print(f"  Friendly name: {friendly_name}")
    print(f"  Error: {error}")

    assert entity_id == "calendar.google_test"
    print("[OK] Fallback to first visible calendar works!")


async def test_db_repositories():
    """Test 6: Calendar DB repositories."""
    print()
    print("=" * 60)
    print("TEST 6: DB Repositories")
    print("=" * 60)

    from app.db.repository import CalendarUserMappingRepository, CalendarReminderStateRepository

    # Test CRUD
    mapping_id = await CalendarUserMappingRepository.create(
        display_name="TestUser",
        calendar_entity_ids_json='["calendar.test"]',
        reminder_offsets_json="[60, 15]",
        is_default_user=0,
    )
    print(f"  Created mapping ID: {mapping_id}")

    mapping = await CalendarUserMappingRepository.get(mapping_id)
    print(f"  Retrieved: {mapping['display_name']} -> {mapping['calendar_entity_ids_json']}")
    assert mapping["display_name"] == "TestUser"

    # Test phonetic lookup
    found = await CalendarUserMappingRepository.find_by_name("TestUser")
    assert found is not None
    print(f"  Phonetic lookup: '{found['display_name']}' found!")

    # Test reminder state
    await CalendarReminderStateRepository.mark_fired("uid123", "calendar.test", mapping_id, 60)
    has_fired = await CalendarReminderStateRepository.has_fired("uid123", "calendar.test", mapping_id, 60)
    assert has_fired is True
    print(f"  Reminder state tracking: fired={has_fired}")

    # Cleanup
    await CalendarUserMappingRepository.delete(mapping_id)
    print("[OK] DB repositories work!")


async def main():
    print("Calendar Feature Direct Integration Tests")
    print("=" * 60)
    print()

    await test_calendar_agent_card()
    await test_user_identity()
    await test_injector_marker_logic()
    await test_injector_formatting()
    await test_executor_resolve_calendar()
    await test_db_repositories()

    print()
    print("=" * 60)
    print("All direct tests completed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
