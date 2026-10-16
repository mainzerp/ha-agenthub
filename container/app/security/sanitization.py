import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

MAX_INPUT_LENGTH = 500
USER_INPUT_START = "[USER_INPUT_START]"
USER_INPUT_END = "[USER_INPUT_END]"

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"^system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"you\s+are\s+now\s+(in\s+)?(.+?\s+)?mode", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(above|previous)", re.IGNORECASE),
]

_DIRECTIONAL_OVERRIDES = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
# COR-11: keep zero-width joiner / non-joiner so non-Latin scripts (Hindi,
# Arabic, family-emoji ligatures, ...) survive sanitization. Bidi overrides
# above are still stripped via the ``_DIRECTIONAL_OVERRIDES`` set.
_ALLOWED_FORMAT = {"\u200d", "\u200c"}


def sanitize_input(text: str, max_length: int = MAX_INPUT_LENGTH) -> str:
    """Strip control/format characters and truncate to ``max_length`` characters.

    ``max_length`` defaults to the conversation-turn limit; the AI-task route
    passes its own larger bound (task instructions plus image description).
    """
    if len(text) > max_length:
        logger.warning("Input truncated from %d to %d chars", len(text), max_length)
    text = text[:max_length]
    text = text.replace("\x00", "")
    cleaned = []
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            cleaned.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf") and ch not in _ALLOWED_FORMAT:
            continue
        if ord(ch) in _DIRECTIONAL_OVERRIDES:
            continue
        cleaned.append(ch)
    return "".join(cleaned).strip()


def check_injection_patterns(text: str) -> bool:
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            snippet = text[:50]
            logger.warning("Potential prompt injection detected: %s", snippet)
            return True
    return False


def wrap_user_input(text: str) -> str:
    return f"{USER_INPUT_START}\n{text}\n{USER_INPUT_END}"
