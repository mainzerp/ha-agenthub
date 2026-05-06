"""Tests for prepare_user_text and mojibake correction."""

from __future__ import annotations

from app.security.user_input import _fix_mojibake, prepare_user_text


def _mojibake(text: str) -> str:
    """Simulate the double-encoding: UTF-8 bytes read as Latin-1."""
    return text.encode("utf-8").decode("latin-1")


class TestFixMojibake:
    def test_corrects_umlaut_u(self):
        assert _fix_mojibake(_mojibake("Küche")) == "Küche"

    def test_corrects_umlaut_a(self):
        assert _fix_mojibake(_mojibake("Bäcker")) == "Bäcker"

    def test_corrects_umlaut_o(self):
        assert _fix_mojibake(_mojibake("Schröder")) == "Schröder"

    def test_corrects_eszett(self):
        assert _fix_mojibake(_mojibake("Straße")) == "Straße"

    def test_full_command(self):
        assert _fix_mojibake(_mojibake("Küche einschalten.")) == "Küche einschalten."

    def test_already_correct_passthrough(self):
        assert _fix_mojibake("Küche einschalten.") == "Küche einschalten."

    def test_ascii_passthrough(self):
        assert _fix_mojibake("Keller einschalten.") == "Keller einschalten."

    def test_empty_string(self):
        assert _fix_mojibake("") == ""


class TestPrepareUserText:
    def test_mojibake_fixed_before_sanitize(self):
        result = prepare_user_text(_mojibake("Küche einschalten."))
        assert result.text == "Küche einschalten."

    def test_correct_input_unchanged(self):
        result = prepare_user_text("Küche einschalten.")
        assert result.text == "Küche einschalten."

    def test_cache_key_matches_regardless_of_source_encoding(self):
        from app.cache._base_cache import make_text_id

        mojibake_input = prepare_user_text(_mojibake("Küche einschalten."))
        correct_input = prepare_user_text("Küche einschalten.")
        assert make_text_id(mojibake_input.text, "de") == make_text_id(correct_input.text, "de")
