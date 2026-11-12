"""Leading wake-word stripping: pure function, setting loader, seed and migration 56."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.api.routes.conversation import _build_a2a_request
from app.db.schema import _create_indexes, _create_tables, _seed_defaults
from app.db.schema._migrations import _migrate_to_56, _run_migrations
from app.models.conversation import ConversationRequest
from app.security.user_input import load_wake_words, parse_wake_words, strip_leading_wake_word
from tests.helpers import shutdown_aiosqlite


class TestStripLeadingWakeWord:
    def test_german_single_word(self):
        assert strip_leading_wake_word("Alexa, schalte Kueche ein", ["alexa"]) == ("schalte Kueche ein", "alexa")

    def test_english_multi_word(self):
        result = strip_leading_wake_word("Hey Jarvis turn on the kitchen light", ["hey jarvis"])
        assert result == ("turn on the kitchen light", "hey jarvis")

    def test_case_insensitive_and_remaining_casing_preserved(self):
        text, matched = strip_leading_wake_word("ALEXA schalte Küche EIN", ["Alexa"])
        assert text == "schalte Küche EIN"
        assert matched == "Alexa"

    @pytest.mark.parametrize(
        "utterance",
        [
            "Hey, Jarvis! turn on the lamp",
            "hey   jarvis, turn on the lamp",
            "Hey - Jarvis: turn on the lamp",
            "hey jarvis... turn on the lamp",
            "  ,Hey Jarvis; turn on the lamp",
            "Hey Jarvis? turn on the lamp",
        ],
    )
    def test_punctuation_variants(self, utterance):
        assert strip_leading_wake_word(utterance, ["hey jarvis"]) == ("turn on the lamp", "hey jarvis")

    def test_leading_whitespace_and_punctuation_before_wake_word(self):
        assert strip_leading_wake_word('  "Alexa", licht an', ["alexa"])[0] == "licht an"

    def test_word_boundary_alexander_does_not_match(self):
        text = "Alexander, schalte das Licht ein"
        assert strip_leading_wake_word(text, ["alexa"]) == (text, None)

    def test_wake_word_prefix_of_longer_word_does_not_match_multi_word(self):
        text = "Hey Jarvisson turn on the light"
        assert strip_leading_wake_word(text, ["hey jarvis"]) == (text, None)

    def test_only_at_the_start(self):
        text = "Schalte Alexa aus"
        assert strip_leading_wake_word(text, ["alexa"]) == (text, None)

    def test_only_one_phrase_is_stripped(self):
        assert strip_leading_wake_word("Alexa, Alexa, licht an", ["alexa"]) == ("Alexa, licht an", "alexa")

    def test_longest_matching_phrase_wins(self):
        result = strip_leading_wake_word("Hey Jarvis turn on the light", ["hey", "hey jarvis"])
        assert result == ("turn on the light", "hey jarvis")
        result = strip_leading_wake_word("Hey Jarvis turn on the light", ["hey jarvis", "hey"])
        assert result == ("turn on the light", "hey jarvis")

    def test_shorter_phrase_used_when_longer_does_not_match(self):
        assert strip_leading_wake_word("Hey Anna turn on the light", ["hey jarvis", "hey"]) == (
            "Anna turn on the light",
            "hey",
        )

    def test_only_wake_word_returns_original(self):
        assert strip_leading_wake_word("Alexa", ["alexa"]) == ("Alexa", None)
        assert strip_leading_wake_word("Hey Jarvis!", ["hey jarvis"]) == ("Hey Jarvis!", None)
        assert strip_leading_wake_word("Alexa ...", ["alexa"]) == ("Alexa ...", None)

    def test_empty_config_is_noop(self):
        assert strip_leading_wake_word("Alexa, licht an", []) == ("Alexa, licht an", None)
        assert strip_leading_wake_word("Alexa, licht an", ()) == ("Alexa, licht an", None)

    def test_blank_entries_are_ignored(self):
        assert strip_leading_wake_word("Alexa, licht an", ["", "   ", "alexa"]) == ("licht an", "alexa")
        assert strip_leading_wake_word("Alexa, licht an", ["", "  "]) == ("Alexa, licht an", None)

    def test_punctuation_only_entry_is_ignored(self):
        assert strip_leading_wake_word("... licht an", ["..."]) == ("... licht an", None)

    def test_empty_text(self):
        assert strip_leading_wake_word("", ["alexa"]) == ("", None)

    def test_regex_metacharacters_in_config_are_literal(self):
        assert strip_leading_wake_word("Alexa, licht an", ["a.*"]) == ("Alexa, licht an", None)

    def test_accepts_generator(self):
        assert strip_leading_wake_word("Alexa, licht an", (w for w in ["alexa"])) == ("licht an", "alexa")


class TestParseWakeWords:
    def test_splits_trims_and_drops_blanks(self):
        assert parse_wake_words(" alexa, hey jarvis ,, ok nabu ,") == ("alexa", "hey jarvis", "ok nabu")

    def test_dedupes_case_insensitively(self):
        assert parse_wake_words("Alexa, alexa, ALEXA") == ("Alexa",)

    @pytest.mark.parametrize("raw", ["", "  ", None, ",,,"])
    def test_empty_values(self, raw):
        assert parse_wake_words(raw) == ()


class TestLoadWakeWords:
    async def test_reads_and_parses_setting(self):
        get_value = AsyncMock(return_value="alexa, hey jarvis")
        with patch("app.security.user_input.SettingsRepository.get_value", get_value):
            assert await load_wake_words() == ("alexa", "hey jarvis")
        assert get_value.await_args.args[0] == "voice.wake_words"

    async def test_empty_setting_means_off(self):
        with patch("app.security.user_input.SettingsRepository.get_value", AsyncMock(return_value="")):
            assert await load_wake_words() == ()

    async def test_missing_setting_means_off(self):
        with patch("app.security.user_input.SettingsRepository.get_value", AsyncMock(return_value=None)):
            assert await load_wake_words() == ()

    async def test_read_failure_means_off(self):
        with patch("app.security.user_input.SettingsRepository.get_value", AsyncMock(side_effect=RuntimeError("db"))):
            assert await load_wake_words() == ()


class TestBuildA2aRequestWakeWords:
    def test_strips_wake_word_from_task_description_and_marks_trace(self):
        collector = MagicMock()
        collector.source = "ha"
        conv_request = ConversationRequest(text="Alexa, schalte Kueche ein", conversation_id="c1")
        _request, task = _build_a2a_request(conv_request, "message/stream", collector, wake_words=("alexa",))
        assert task.description == "schalte Kueche ein"
        collector.set_request_attribute.assert_called_once_with("wake_word_stripped", "alexa")

    def test_no_wake_words_leaves_text_untouched(self):
        collector = MagicMock()
        collector.source = "ha"
        conv_request = ConversationRequest(text="Alexa, schalte Kueche ein", conversation_id="c1")
        _request, task = _build_a2a_request(conv_request, "message/stream", collector)
        assert task.description == "Alexa, schalte Kueche ein"
        collector.set_request_attribute.assert_not_called()

    def test_non_matching_wake_words_do_not_mark_trace(self):
        collector = MagicMock()
        collector.source = "ha"
        conv_request = ConversationRequest(text="Alexander, licht an", conversation_id="c1")
        _request, task = _build_a2a_request(conv_request, "message/send", collector, wake_words=("alexa",))
        assert task.description == "Alexander, licht an"
        collector.set_request_attribute.assert_not_called()


async def _fresh_install(path: Path) -> None:
    db = await aiosqlite.connect(str(path))
    try:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


async def _wake_word_row(path: Path) -> tuple | None:
    db = await aiosqlite.connect(str(path))
    try:
        cursor = await db.execute(
            "SELECT value, value_type, category FROM settings WHERE key = 'voice.wake_words'",
        )
        row = await cursor.fetchone()
        return tuple(row) if row is not None else None
    finally:
        await shutdown_aiosqlite(db)


class TestWakeWordSettingSeed:
    async def test_fresh_install_seeds_empty_setting(self, tmp_path: Path):
        path = tmp_path / "fresh.db"
        await _fresh_install(path)
        assert await _wake_word_row(path) == ("", "string", "voice")

    async def test_migration_56_adds_setting_to_existing_database(self, tmp_path: Path):
        path = tmp_path / "old.db"
        await _fresh_install(path)
        db = await aiosqlite.connect(str(path))
        try:
            await db.execute("DELETE FROM settings WHERE key = 'voice.wake_words'")
            await db.execute("DELETE FROM schema_version WHERE version > 55")
            await db.commit()
            await _run_migrations(db)
            await db.commit()
        finally:
            await shutdown_aiosqlite(db)
        assert await _wake_word_row(path) == ("", "string", "voice")

    async def test_migration_56_keeps_admin_value(self, tmp_path: Path):
        path = tmp_path / "admin.db"
        await _fresh_install(path)
        db = await aiosqlite.connect(str(path))
        try:
            await db.execute("UPDATE settings SET value = 'alexa' WHERE key = 'voice.wake_words'")
            await _migrate_to_56(db)
            await db.commit()
        finally:
            await shutdown_aiosqlite(db)
        assert await _wake_word_row(path) == ("alexa", "string", "voice")
