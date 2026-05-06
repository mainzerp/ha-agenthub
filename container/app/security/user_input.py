"""Live user input preparation for conversation ingress."""

from __future__ import annotations

from dataclasses import dataclass

from app.security.sanitization import check_injection_patterns, sanitize_input


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


def prepare_user_text(text: str) -> PreparedUserInput:
    """Sanitize one live user turn and detect prompt-injection patterns."""
    sanitized = sanitize_input(_fix_mojibake(text or ""))
    injection_detected = check_injection_patterns(sanitized)
    return PreparedUserInput(text=sanitized, injection_detected=injection_detected)
