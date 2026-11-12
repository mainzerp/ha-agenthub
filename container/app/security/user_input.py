"""Live user input preparation for conversation ingress."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from app.db.repository import SettingsRepository
from app.defaults import VOICE_DEFAULTS
from app.security.sanitization import MAX_INPUT_LENGTH, check_injection_patterns, sanitize_input

logger = logging.getLogger(__name__)

WAKE_WORDS_SETTING_KEY = "voice.wake_words"


@dataclass(frozen=True)
class PreparedUserInput:
    """Sanitized live user text plus prompt-injection detection metadata."""

    text: str
    injection_detected: bool


def _fix_mojibake(text: str) -> str:
    # Repairs double-encoded UTF-8: bytes of ü (0xC3 0xBC) decoded as Latin-1
    # produce Ã¼ — encoding back to Latin-1 and re-decoding as UTF-8 reverses this.
    # Correctly-encoded inputs raise UnicodeDecodeError and are returned unchanged.
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def prepare_user_text(text: str, *, max_length: int = MAX_INPUT_LENGTH) -> PreparedUserInput:
    """Sanitize one live user turn and detect prompt-injection patterns."""
    sanitized = sanitize_input(_fix_mojibake(text or ""), max_length=max_length)
    injection_detected = check_injection_patterns(sanitized)
    return PreparedUserInput(text=sanitized, injection_detected=injection_detected)


# Separators consumed after a matched wake phrase: STT adds commas, full
# stops, question/exclamation marks, colons, dashes, ellipses and closing quotes.
_WAKE_TRAILING_SEPARATORS = r"[\s,.!?:;\-\u2013\u2014\u2026\"'\u2018\u2019\u201c\u201d]*"
_WAKE_WORD_TOKEN_RE = re.compile(r"\w+")
_WORD_CHAR_RE = re.compile(r"\w")


def parse_wake_words(raw: str | None) -> tuple[str, ...]:
    """Split the comma-separated ``voice.wake_words`` value into distinct phrases.

    Blank entries are dropped and duplicates (case-insensitive) collapse to the
    first spelling; the order is kept.
    """
    seen: set[str] = set()
    phrases: list[str] = []
    for part in re.split(r"[,\n]", raw or ""):
        phrase = part.strip()
        key = phrase.casefold()
        if not phrase or key in seen:
            continue
        seen.add(key)
        phrases.append(phrase)
    return tuple(phrases)


@lru_cache(maxsize=32)
def _compile_wake_patterns(phrases: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile one anchored pattern per phrase.

    A phrase is its word tokens; tokens match with any non-word run between
    them ("hey jarvis" matches "Hey, Jarvis"), and the phrase must end on a
    word boundary so "alexa" never matches "alexander".
    """
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for phrase in phrases:
        tokens = _WAKE_WORD_TOKEN_RE.findall(phrase)
        if not tokens:
            continue
        body = r"\W+".join(re.escape(token) for token in tokens)
        compiled.append((phrase, re.compile(rf"\A\W*{body}(?!\w){_WAKE_TRAILING_SEPARATORS}", re.IGNORECASE)))
    return tuple(compiled)


def strip_leading_wake_word(text: str, wake_words: Iterable[str]) -> tuple[str, str | None]:
    """Remove one configured wake phrase from the very start of ``text``.

    Returns ``(remaining_text, matched_phrase)``. Matching is case-insensitive,
    anchored at the start (after leading whitespace/punctuation), word-boundary
    safe and tolerant of punctuation between the phrase words; separators
    after the phrase are consumed and the casing of the rest is preserved. The
    longest matching phrase wins. When nothing would remain (only the wake
    word was spoken), ``text`` is returned unchanged with ``None``.
    """
    patterns = _compile_wake_patterns(tuple(phrase for phrase in wake_words if phrase and phrase.strip()))
    best_end = -1
    best_phrase: str | None = None
    for phrase, pattern in patterns:
        match = pattern.match(text)
        if match is not None and match.end() > best_end:
            best_end = match.end()
            best_phrase = phrase
    if best_phrase is None:
        return text, None
    remaining = text[best_end:]
    if not _WORD_CHAR_RE.search(remaining):
        return text, None
    return remaining, best_phrase.strip()


async def load_wake_words() -> tuple[str, ...]:
    """Read ``voice.wake_words`` (TTL-cached by the settings repository); empty = feature off."""
    try:
        raw = await SettingsRepository.get_value(WAKE_WORDS_SETTING_KEY, str(VOICE_DEFAULTS[WAKE_WORDS_SETTING_KEY]))
    except Exception:
        logger.debug("Could not read %s; wake-word stripping skipped", WAKE_WORDS_SETTING_KEY, exc_info=True)
        return ()
    return parse_wake_words(raw)
