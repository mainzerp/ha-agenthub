"""Tests for the action-cache validator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.cache_validator import (
    _ACTION_CONTRADICTIONS,
    _EXPECTED_STATES,
    _READONLY_ACTIONS,
    ActionCacheValidator,
)
from app.models.cache import ActionCacheEntry, CachedAction


def _make_validator(
    *,
    entries=None,
    entity_index=None,
    ha_client=None,
    llm_client=None,
):
    action_cache = MagicMock()
    action_cache.make_entry_id = MagicMock(return_value="entry-id-123")

    cache_manager = MagicMock()
    cache_manager.iter_action_entries = MagicMock(return_value=entries or [])
    cache_manager.update_action_entry = AsyncMock()
    cache_manager.invalidate_action = MagicMock()

    return ActionCacheValidator(
        action_cache=action_cache,
        cache_manager=cache_manager,
        entity_index=entity_index,
        ha_client=ha_client,
        llm_client=llm_client,
    )


def _make_entry(
    service: str = "light/turn_on",
    response_text: str = "Done, kitchen light is now on.",
    entity_id: str = "light.kitchen_ceiling",
    query_text: str = "turn on kitchen light",
) -> ActionCacheEntry:
    return ActionCacheEntry(
        query_text=query_text,
        language="en",
        agent_id="light-agent",
        response_text=response_text,
        cached_action=CachedAction(service=service, entity_id=entity_id, service_data={}),
    )


# ---------------------------------------------------------------------------
# _is_plausible
# ---------------------------------------------------------------------------


class TestIsPlausible:
    def test_is_plausible_turn_on_with_on(self):
        assert ActionCacheValidator._is_plausible("light/turn_on", "Done, Kitchen Light is now on.")

    def test_is_plausible_turn_off_with_on(self):
        assert not ActionCacheValidator._is_plausible("light/turn_off", "Done, Kitchen Light is now on.")

    def test_is_plausible_turn_on_with_off(self):
        assert not ActionCacheValidator._is_plausible("light/turn_on", "Kitchen Light has been turned off.")

    def test_is_plausible_set_color_with_off(self):
        assert not ActionCacheValidator._is_plausible("light/set_color", "The light is now off.")

    def test_is_plausible_readonly_with_done(self):
        assert not ActionCacheValidator._is_plausible("query_light_state", "Done, the light is now on.")


# ---------------------------------------------------------------------------
# _regenerate_response_text
# ---------------------------------------------------------------------------


class TestRegenerateResponseText:
    def test_regenerate_turn_on(self):
        result = ActionCacheValidator._regenerate_response_text("light/turn_on", "Kitchen Light")
        assert result == "Done, Kitchen Light is now on."

    def test_regenerate_lock(self):
        result = ActionCacheValidator._regenerate_response_text("lock/lock", "Front Door")
        assert result == "Done, Front Door is now locked."

    def test_regenerate_unlock(self):
        result = ActionCacheValidator._regenerate_response_text("lock/unlock", "Front Door")
        assert result == "Done, Front Door is now unlocked."

    def test_regenerate_turn_off(self):
        result = ActionCacheValidator._regenerate_response_text("light/turn_off", "Kitchen Light")
        assert result == "Done, Kitchen Light is now off."

    def test_regenerate_unknown_action(self):
        result = ActionCacheValidator._regenerate_response_text("cover/open_cover", "Garage Door")
        assert result == "Done, Garage Door open cover."


# ---------------------------------------------------------------------------
# _parse_service
# ---------------------------------------------------------------------------


class TestParseService:
    def test_parse_service_with_domain(self):
        assert ActionCacheValidator._parse_service("light/turn_on") == ("light", "turn_on")

    def test_parse_service_without_domain(self):
        assert ActionCacheValidator._parse_service("turn_on") == (None, "turn_on")


# ---------------------------------------------------------------------------
# _get_friendly_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_friendly_name_from_entity_index():
    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))
    validator = _make_validator(entity_index=entity_index)
    result = await validator._get_friendly_name("light.kitchen_ceiling")
    assert result == "Kitchen Ceiling"


@pytest.mark.asyncio
async def test_get_friendly_name_from_ha_client_fallback():
    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=None)
    ha_client = AsyncMock()
    ha_client.get_state = AsyncMock(
        return_value={
            "entity_id": "light.kitchen_ceiling",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen Ceiling"},
        }
    )
    validator = _make_validator(entity_index=entity_index, ha_client=ha_client)
    result = await validator._get_friendly_name("light.kitchen_ceiling")
    assert result == "Kitchen Ceiling"


@pytest.mark.asyncio
async def test_get_friendly_name_returns_none_when_unresolvable():
    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=None)
    ha_client = AsyncMock()
    ha_client.get_state = AsyncMock(return_value=None)
    validator = _make_validator(entity_index=entity_index, ha_client=ha_client)
    result = await validator._get_friendly_name("light.missing")
    assert result is None


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_corrects_one_deletes_one():
    """One correctable entry (has entity_id) and one uncorrectable (no entity_id)."""
    correctable = _make_entry(
        service="light/turn_off",
        response_text="Done, Kitchen Light is now on.",  # inconsistent: turn_off says "on"
        entity_id="light.kitchen_ceiling",
    )
    uncorrectable = ActionCacheEntry(
        query_text="turn off unknown light",
        language="en",
        agent_id="light-agent",
        response_text="Done, Unknown Light is now on.",
        cached_action=CachedAction(service="light/turn_off", entity_id="", service_data={}),
    )

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(
        entries=[correctable, uncorrectable],
        entity_index=entity_index,
    )

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="true")
        result = await validator.run_once()

    assert result["scanned"] == 2
    assert result["inconsistent"] == 2
    assert result["corrected"] == 1
    assert result["deleted"] == 1
    assert result["errors"] == 0

    # Corrected entry should have been stored
    validator._cache_manager.update_action_entry.assert_awaited_once()
    updated_entry = validator._cache_manager.update_action_entry.await_args[0][0]
    assert updated_entry.response_text == "Done, Kitchen Ceiling is now off."
    assert updated_entry.original_response_text == "Done, Kitchen Ceiling is now off."
    assert updated_entry.validated_at is not None

    # Uncorrectable entry should have been invalidated
    validator._cache_manager.invalidate_action.assert_called_once()


@pytest.mark.asyncio
async def test_run_once_skips_when_disabled():
    validator = _make_validator(entries=[_make_entry()])

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="false")
        result = await validator.run_once()

    assert result == {"scanned": 0, "inconsistent": 0, "corrected": 0, "deleted": 0, "errors": 0}
    validator._cache_manager.iter_action_entries.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_counts_valid_entries():
    valid = _make_entry(
        service="light/turn_on",
        response_text="Done, Kitchen Light is now on.",
    )
    validator = _make_validator(entries=[valid])

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="true")
        result = await validator.run_once()

    assert result["scanned"] == 1
    assert result["inconsistent"] == 0
    assert result["corrected"] == 0
    assert result["deleted"] == 0
    assert result["errors"] == 0
    validator._cache_manager.update_action_entry.assert_awaited_once()
    updated_entry = validator._cache_manager.update_action_entry.await_args[0][0]
    assert updated_entry.validated_at is not None


@pytest.mark.asyncio
async def test_run_once_skips_validated_entries():
    already_validated = _make_entry(
        service="light/turn_on",
        response_text="Done, Kitchen Light is now on.",
    )
    already_validated.validated_at = "2025-01-01T00:00:00+00:00"
    validator = _make_validator(entries=[already_validated])

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="true")
        result = await validator.run_once()

    assert result["scanned"] == 0
    assert result["inconsistent"] == 0
    assert result["corrected"] == 0
    validator._cache_manager.update_action_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_sets_validated_at_on_valid_entry():
    valid = _make_entry(
        service="light/turn_on",
        response_text="Done, Kitchen Light is now on.",
    )
    validator = _make_validator(entries=[valid])

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="true")
        result = await validator.run_once()

    assert result["scanned"] == 1
    assert result["inconsistent"] == 0
    updated_entry = validator._cache_manager.update_action_entry.await_args[0][0]
    assert updated_entry.validated_at is not None
    assert result["started_at"] is not None
    assert result["finished_at"] is not None


@pytest.mark.asyncio
async def test_run_once_sets_validated_at_on_corrected_entry():
    inconsistent = _make_entry(
        service="light/turn_off",
        response_text="Done, Kitchen Light is now on.",
        entity_id="light.kitchen_ceiling",
    )
    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))
    validator = _make_validator(entries=[inconsistent], entity_index=entity_index)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="true")
        result = await validator.run_once()

    assert result["scanned"] == 1
    assert result["inconsistent"] == 1
    assert result["corrected"] == 1
    updated_entry = validator._cache_manager.update_action_entry.await_args[0][0]
    assert updated_entry.validated_at is not None
    assert updated_entry.response_text == "Done, Kitchen Ceiling is now off."


@pytest.mark.asyncio
async def test_history_recorded_after_run():
    valid = _make_entry(
        service="light/turn_on",
        response_text="Done, Kitchen Light is now on.",
    )
    validator = _make_validator(entries=[valid])

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="true")
        result = await validator.run_once()

    history = validator.get_history()
    assert len(history) == 1
    assert history[0]["scanned"] == result["scanned"]
    assert history[0]["started_at"] == result["started_at"]
    assert history[0]["finished_at"] == result["finished_at"]


# ---------------------------------------------------------------------------
# _regenerate_response with LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_response_uses_llm_when_configured():
    entry = _make_entry(service="light/turn_on", response_text="wrong")
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value="  LLM generated text.  ")

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(
        entity_index=entity_index,
        llm_client=llm_client,
    )

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.max_tokens": "1024",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        result = await validator._regenerate_response(entry)

    assert result == "LLM generated text."
    llm_client.complete.assert_awaited_once()
    call_kwargs = llm_client.complete.await_args[1]
    assert call_kwargs["model"] == "groq/openai/gpt-oss-20b"
    assert call_kwargs["temperature"] == 0.2
    assert call_kwargs["max_tokens"] == 1024
    assert call_kwargs["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_regenerate_response_falls_back_to_template_on_llm_failure():
    entry = _make_entry(service="light/turn_on", response_text="wrong")
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(side_effect=RuntimeError("LLM down"))

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(
        entity_index=entity_index,
        llm_client=llm_client,
    )

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.max_tokens": "1024",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        result = await validator._regenerate_response(entry)

    assert result == "Done, Kitchen Ceiling is now on."


@pytest.mark.asyncio
async def test_regenerate_response_falls_back_when_model_empty():
    entry = _make_entry(service="light/turn_on", response_text="wrong")
    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(entity_index=entity_index)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "",
            }.get(key, default)
        )
        result = await validator._regenerate_response(entry)

    assert result == "Done, Kitchen Ceiling is now on."


# ---------------------------------------------------------------------------
# _validate_entry with LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_entry_llm_says_consistent():
    entry = _make_entry(
        query_text="Turn on the kitchen light",
        service="light/turn_on",
        response_text="Done, Kitchen Light is now on.",
    )
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value="consistent")

    validator = _make_validator(llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.1",
                "cache.validator.max_tokens": "32",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        is_valid, corrected = await validator._validate_entry(entry)

    assert is_valid is True
    assert corrected is None
    llm_client.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_entry_llm_says_correct_response():
    entry = _make_entry(
        query_text="Turn off the kitchen light",
        service="light/turn_off",
        response_text="Done, Kitchen Light is now on.",
    )
    llm_client = AsyncMock()
    # First call: validation says "correct_response"
    # Second call: regeneration generates new text
    llm_client.complete = AsyncMock(side_effect=["correct_response", "Done, Kitchen Light is now off."])

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(entity_index=entity_index, llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.1",
                "cache.validator.max_tokens": "32",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        is_valid, corrected = await validator._validate_entry(entry)

    assert is_valid is False
    assert corrected == "Done, Kitchen Light is now off."
    assert llm_client.complete.await_count == 2


@pytest.mark.asyncio
async def test_validate_entry_llm_says_invalidate():
    entry = _make_entry(
        query_text="Turn on the kitchen light",
        service="cover/open_cover",
        response_text="Done, Kitchen Light is now on.",
    )
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value="invalidate")

    validator = _make_validator(llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.1",
                "cache.validator.max_tokens": "32",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        is_valid, corrected = await validator._validate_entry(entry)

    assert is_valid is False
    assert corrected is None  # signals deletion
    llm_client.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_entry_llm_failure_falls_back_to_deterministic():
    entry = _make_entry(
        query_text="Turn off the kitchen light",
        service="light/turn_off",
        response_text="Done, Kitchen Light is now on.",
    )
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(side_effect=RuntimeError("LLM timeout"))

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(entity_index=entity_index, llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
            }.get(key, default)
        )
        is_valid, corrected = await validator._validate_entry(entry)

    assert is_valid is False
    assert corrected == "Done, Kitchen Ceiling is now off."


@pytest.mark.asyncio
async def test_validate_entry_llm_unparseable_falls_back():
    entry = _make_entry(
        query_text="Turn off the kitchen light",
        service="light/turn_off",
        response_text="Done, Kitchen Light is now on.",
    )
    llm_client = AsyncMock()
    # First call (validation) returns unparseable text.
    # Second call (regeneration) fails so fallback to deterministic template occurs.
    llm_client.complete = AsyncMock(side_effect=["maybe, it depends", RuntimeError("LLM down")])

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(entity_index=entity_index, llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
            }.get(key, default)
        )
        is_valid, corrected = await validator._validate_entry(entry)

    assert is_valid is False
    assert corrected == "Done, Kitchen Ceiling is now off."


@pytest.mark.asyncio
async def test_validate_entry_no_model_uses_deterministic():
    entry = _make_entry(
        query_text="Turn on the kitchen light",
        service="light/turn_on",
        response_text="Done, Kitchen Light is now on.",
    )
    validator = _make_validator()

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "",
            }.get(key, default)
        )
        is_valid, corrected = await validator._validate_entry(entry)

    assert is_valid is True
    assert corrected is None


@pytest.mark.asyncio
async def test_validate_entry_model_configured_but_no_client_uses_deterministic():
    entry = _make_entry(
        query_text="Turn on the kitchen light",
        service="light/turn_on",
        response_text="Done, Kitchen Light is now on.",
    )
    validator = _make_validator(llm_client=None)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
            }.get(key, default)
        )
        is_valid, corrected = await validator._validate_entry(entry)

    assert is_valid is True
    assert corrected is None


# ---------------------------------------------------------------------------
# run_periodic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_loop_sleeps_interval():
    validator = _make_validator()

    sleep_calls = []
    run_once_calls = []

    async def _mock_sleep(duration):
        sleep_calls.append(duration)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    async def _mock_run_once():
        run_once_calls.append(True)
        return {"scanned": 0, "inconsistent": 0, "corrected": 0, "deleted": 0, "errors": 0}

    validator.run_once = _mock_run_once

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.enabled": "true",
                "cache.validator.interval_minutes": "42",
            }.get(key, default)
        )
        with patch("asyncio.sleep", _mock_sleep), pytest.raises(asyncio.CancelledError):
            await validator.run_periodic()

    assert len(run_once_calls) == 2
    assert sleep_calls == [42 * 60, 42 * 60]


@pytest.mark.asyncio
async def test_periodic_loop_disabled_sleeps_short():
    validator = _make_validator()

    sleep_calls = []

    async def _mock_sleep(duration):
        sleep_calls.append(duration)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.enabled": "false",
            }.get(key, default)
        )
        with patch("asyncio.sleep", _mock_sleep), pytest.raises(asyncio.CancelledError):
            await validator.run_periodic()

    assert sleep_calls == [60, 60]
    validator._cache_manager.iter_action_entries.assert_not_called()


# ---------------------------------------------------------------------------
# _get_batch_size
# ---------------------------------------------------------------------------


class TestGetBatchSize:
    @pytest.mark.asyncio
    async def test_default_batch_size(self):
        validator = _make_validator()
        with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
            mock_settings.get_value = AsyncMock(return_value="10")
            size = await validator._get_batch_size()
        assert size == 10

    @pytest.mark.asyncio
    async def test_clamps_below_one(self):
        validator = _make_validator()
        with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
            mock_settings.get_value = AsyncMock(return_value="0")
            size = await validator._get_batch_size()
        assert size == 1

    @pytest.mark.asyncio
    async def test_clamps_above_fifty(self):
        validator = _make_validator()
        with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
            mock_settings.get_value = AsyncMock(return_value="100")
            size = await validator._get_batch_size()
        assert size == 50

    @pytest.mark.asyncio
    async def test_invalid_string_uses_default(self):
        validator = _make_validator()
        with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
            mock_settings.get_value = AsyncMock(return_value="abc")
            size = await validator._get_batch_size()
        assert size == 10


# ---------------------------------------------------------------------------
# _build_batch_prompt
# ---------------------------------------------------------------------------


class TestBuildBatchPrompt:
    def test_build_batch_prompt_two_entries(self):
        entry1 = _make_entry(
            query_text="turn on kitchen light", service="light/turn_on", response_text="Done, Kitchen Light is now on."
        )
        entry2 = _make_entry(
            query_text="turn off bedroom light",
            service="light/turn_off",
            response_text="Done, Bedroom Light is now off.",
        )
        prompt = ActionCacheValidator._build_batch_prompt([entry1, entry2])
        assert "Entry 1:" in prompt
        assert "Entry 2:" in prompt
        assert "turn on kitchen light" in prompt
        assert "turn off bedroom light" in prompt
        assert "JSON array response:" in prompt

    def test_build_batch_prompt_empty_list(self):
        prompt = ActionCacheValidator._build_batch_prompt([])
        assert "JSON array response:" in prompt
        assert "Entry 1:" not in prompt


# ---------------------------------------------------------------------------
# _parse_batch_response
# ---------------------------------------------------------------------------


class TestParseBatchResponse:
    def test_parse_valid_array(self):
        result = ActionCacheValidator._parse_batch_response('["consistent", "invalidate", "correct_response"]', 3)
        assert result == ["consistent", "invalidate", "correct_response"]

    def test_parse_markdown_code_block(self):
        result = ActionCacheValidator._parse_batch_response('```json\n["consistent", "invalidate"]\n```', 2)
        assert result == ["consistent", "invalidate"]

    def test_parse_wrong_count(self):
        result = ActionCacheValidator._parse_batch_response('["consistent"]', 2)
        assert result == [None, None]

    def test_parse_non_array(self):
        result = ActionCacheValidator._parse_batch_response('{"result": "consistent"}', 1)
        assert result == [None]

    def test_parse_invalid_json(self):
        result = ActionCacheValidator._parse_batch_response("not json", 2)
        assert result == [None, None]

    def test_parse_mixed_valid_invalid(self):
        result = ActionCacheValidator._parse_batch_response('["consistent", "unknown", "invalidate"]', 3)
        assert result == ["consistent", None, "invalidate"]

    def test_parse_shortcut_correct(self):
        result = ActionCacheValidator._parse_batch_response('["correct"]', 1)
        assert result == ["correct_response"]


# ---------------------------------------------------------------------------
# _llm_validate_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_validate_batch_success():
    entry1 = _make_entry(
        query_text="turn on kitchen light", service="light/turn_on", response_text="Done, Kitchen Light is now on."
    )
    entry2 = _make_entry(
        query_text="turn off bedroom light", service="light/turn_off", response_text="Done, Bedroom Light is now off."
    )
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value='["consistent", "consistent"]')

    validator = _make_validator(llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        results = await validator._llm_validate_batch([entry1, entry2])

    assert results == ["consistent", "consistent"]
    llm_client.complete.assert_awaited_once()
    call_kwargs = llm_client.complete.await_args[1]
    assert call_kwargs["max_tokens"] == 32  # 2 * 16


@pytest.mark.asyncio
async def test_llm_validate_batch_empty_returns_none_list():
    validator = _make_validator()
    results = await validator._llm_validate_batch([])
    assert results == []


@pytest.mark.asyncio
async def test_llm_validate_batch_no_model_returns_none():
    entry = _make_entry()
    llm_client = AsyncMock()
    validator = _make_validator(llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(return_value="")
        results = await validator._llm_validate_batch([entry])

    assert results == [None]
    llm_client.complete.assert_not_called()


@pytest.mark.asyncio
async def test_llm_validate_batch_llm_failure_returns_none():
    entry = _make_entry()
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(side_effect=RuntimeError("LLM down"))
    validator = _make_validator(llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        results = await validator._llm_validate_batch([entry])

    assert results == [None]


@pytest.mark.asyncio
async def test_llm_validate_batch_malformed_response_returns_none():
    entry = _make_entry()
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value="not json")
    validator = _make_validator(llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        results = await validator._llm_validate_batch([entry])

    assert results == [None]


# ---------------------------------------------------------------------------
# run_once batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_uses_batch_validation():
    """Two entries validated in a single batch LLM call."""
    entry1 = _make_entry(service="light/turn_on", response_text="Done, Kitchen Light is now on.")
    entry2 = _make_entry(service="light/turn_off", response_text="Done, Bedroom Light is now off.")
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value='["consistent", "consistent"]')

    validator = _make_validator(entries=[entry1, entry2], llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.enabled": "true",
                "cache.validator.batch_size": "10",
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        result = await validator.run_once()

    assert result["scanned"] == 2
    assert result["inconsistent"] == 0
    assert result["corrected"] == 0
    assert result["deleted"] == 0
    assert result["errors"] == 0
    assert llm_client.complete.await_count == 1


@pytest.mark.asyncio
async def test_run_once_batch_fallback_to_single_on_failure():
    """Batch fails completely, falls back to single-entry validation."""
    entry = _make_entry(
        service="light/turn_off",
        response_text="Done, Kitchen Light is now on.",
        entity_id="light.kitchen_ceiling",
    )
    llm_client = AsyncMock()
    # Batch call returns malformed JSON
    llm_client.complete = AsyncMock(return_value="not json")

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(
        entries=[entry],
        entity_index=entity_index,
        llm_client=llm_client,
    )

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.enabled": "true",
                "cache.validator.batch_size": "10",
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        result = await validator.run_once()

    assert result["scanned"] == 1
    assert result["inconsistent"] == 1
    assert result["corrected"] == 1
    # One batch call (failed) + one single-entry validation call + one regeneration call
    assert llm_client.complete.await_count == 3


@pytest.mark.asyncio
async def test_run_once_batch_size_one_disables_batching():
    """Batch size of 1 should still work but only process one at a time."""
    entry = _make_entry(service="light/turn_on", response_text="Done, Kitchen Light is now on.")
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value='["consistent"]')

    validator = _make_validator(entries=[entry], llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.enabled": "true",
                "cache.validator.batch_size": "1",
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        result = await validator.run_once()

    assert result["scanned"] == 1
    assert result["inconsistent"] == 0
    assert llm_client.complete.await_count == 1


@pytest.mark.asyncio
async def test_run_once_deletes_entries_with_no_cached_action():
    """Entries without cached_action should be deleted without LLM calls."""
    entry = ActionCacheEntry.model_construct(
        query_text="turn off unknown light",
        language="en",
        agent_id="light-agent",
        response_text="Done, Unknown Light is now on.",
        cached_action=None,
    )
    llm_client = AsyncMock()
    validator = _make_validator(entries=[entry], llm_client=llm_client)

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.enabled": "true",
                "cache.validator.batch_size": "10",
            }.get(key, default)
        )
        result = await validator.run_once()

    assert result["scanned"] == 0  # Not counted as scanned
    assert result["deleted"] == 1
    llm_client.complete.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_batch_correct_response_triggers_regeneration():
    """Batch says 'correct_response', individual regeneration is called."""
    entry = _make_entry(
        service="light/turn_off",
        response_text="Done, Kitchen Light is now on.",
        entity_id="light.kitchen_ceiling",
    )
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(
        side_effect=[
            '["correct_response"]',  # batch validation
            "Done, Kitchen Ceiling is now off.",  # regeneration
        ]
    )

    entity_index = MagicMock()
    entity_index.get_by_id_async = AsyncMock(return_value=MagicMock(friendly_name="Kitchen Ceiling"))

    validator = _make_validator(
        entries=[entry],
        entity_index=entity_index,
        llm_client=llm_client,
    )

    with patch("app.cache.cache_validator.SettingsRepository") as mock_settings:
        mock_settings.get_value = AsyncMock(
            side_effect=lambda key, default="": {
                "cache.validator.enabled": "true",
                "cache.validator.batch_size": "10",
                "cache.validator.model": "groq/openai/gpt-oss-20b",
                "cache.validator.temperature": "0.2",
                "cache.validator.max_tokens": "32",
                "cache.validator.reasoning_effort": "low",
            }.get(key, default)
        )
        result = await validator.run_once()

    assert result["scanned"] == 1
    assert result["inconsistent"] == 1
    assert result["corrected"] == 1


# ---------------------------------------------------------------------------
# Constants coverage
# ---------------------------------------------------------------------------


def test_expected_states_keys():
    assert _EXPECTED_STATES["turn_on"] == "on"
    assert _EXPECTED_STATES["turn_off"] == "off"
    assert _EXPECTED_STATES["lock"] == "locked"
    assert _EXPECTED_STATES["unlock"] == "unlocked"


def test_readonly_actions_coverage():
    assert "query_light_state" in _READONLY_ACTIONS
    assert "list_lights" in _READONLY_ACTIONS
    assert "turn_on" not in _READONLY_ACTIONS


def test_action_contradictions_coverage():
    assert "is now off" in _ACTION_CONTRADICTIONS["turn_on"]
    assert "is now on" in _ACTION_CONTRADICTIONS["turn_off"]
    assert _ACTION_CONTRADICTIONS["toggle"] == []
