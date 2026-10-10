"""Central redaction of secret action parameters before they reach logs and traces.

Alarm/lock codes, PINs, passwords and tokens travel inside LLM action
payloads (``{"action": "alarm_disarm", "parameters": {"code": "1234"}}``).
The service call needs them verbatim, but every log line, trace span and
stored raw LLM response must only ever see a placeholder.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Exact (normalized) parameter names that always carry a secret.
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "code",
        "pin",
        "pincode",
        "pin-code",
        "passcode",
        "password",
        "passwd",
        "otp",
        "token",
        "secret",
        "api-key",
        "apikey",
    }
)

# Substrings that mark a secret-bearing key ("alarm_code", "access_token", ...).
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passcode",
    "token",
    "secret",
    "api-key",
    "apikey",
    "pin-code",
    "alarm-code",
    "lock-code",
    "door-code",
    "access-code",
    "security-code",
)

# ``"code": "1234"`` / ``'pin': 1234`` inside free text such as a raw LLM
# response that embeds a JSON action block.
_INLINE_SECRET_RE = re.compile(
    r"""(?ix)
    (["']?)                                   # optional opening quote of the key
    (code|pin|pin_code|pincode|passcode|password|passwd|otp|token|secret|api_key|apikey
     |access_token|alarm_code|lock_code)
    \1                                        # matching closing quote
    (\s*[:=]\s*)
    (?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[A-Za-z0-9._~+/=-]+)
    """
)


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("_", "-")


def is_sensitive_key(key: Any) -> bool:
    """True when a parameter name carries a secret (code, pin, password, token, ...)."""
    normalized = _normalize_key(key)
    if normalized in _SENSITIVE_EXACT_KEYS:
        return True
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def redact_sensitive_values(value: Any) -> Any:
    """Return a copy of ``value`` with secret-bearing dict entries replaced.

    Recurses into dicts, lists and tuples. Non-container values are returned
    unchanged; only values under a sensitive key are replaced (a ``None``
    value stays ``None`` so "no code given" remains visible).
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if item is not None and is_sensitive_key(key) else redact_sensitive_values(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_values(item) for item in value)
    return value


def redact_sensitive_text(text: str | None) -> str:
    """Replace inline ``key: value`` secrets (JSON or prose-like) in ``text``."""
    if not text:
        return text or ""
    return _INLINE_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{_quoted(m)}", text)


def _quoted(match: re.Match[str]) -> str:
    # Keep JSON well-formed when the key was quoted with double quotes.
    return f'"{REDACTED}"' if match.group(1) == '"' else REDACTED
