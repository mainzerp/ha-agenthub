"""Lightweight language detection utility using langdetect."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MIN_TEXT_LENGTH = 8
_MIN_CONFIDENCE = 0.5


def detect_user_language(text: str, fallback: str = "en") -> str:
    """Detect the language of *text* and return an ISO 639-1 code.

    Returns *fallback* when:
    - the text is shorter than 8 characters (langdetect is unreliable on short strings)
    - the top detection confidence is below 50%
    - langdetect raises any exception
    - langdetect is not installed
    """
    if not text or len(text.strip()) < _MIN_TEXT_LENGTH:
        return fallback
    try:
        from langdetect import DetectorFactory, LangDetectException, detect_langs  # type: ignore[import-untyped]

        DetectorFactory.seed = 0
    except ImportError:
        logger.debug("langdetect not installed, using fallback '%s'", fallback)
        return fallback
    try:
        results = detect_langs(text)
        if not results:
            return fallback
        top = results[0]
        logger.debug("Detected language '%s' (%.2f) for text: %s", top.lang, top.prob, text[:60])
        if top.prob < _MIN_CONFIDENCE:
            logger.debug(
                "Confidence %.2f below threshold %.2f, using fallback '%s'", top.prob, _MIN_CONFIDENCE, fallback
            )
            return fallback
        return top.lang
    except LangDetectException:
        logger.debug("Language detection failed, using fallback '%s'", fallback)
        return fallback
    except Exception:
        logger.debug("Language detection error, using fallback '%s'", fallback)
        return fallback
