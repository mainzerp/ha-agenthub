"""Cancel-interaction acknowledgement helpers."""

from __future__ import annotations

import asyncio
import logging

from app.agents.base import _load_prompt_path_async, _prompt_path
from app.llm.client import complete
from app.security.sanitization import wrap_user_input

logger = logging.getLogger(__name__)

_CANCEL_LLM_TIMEOUT_SEC = 1.5
_CANCEL_MAX_CHARS = 80
_LANGUAGE_NAMES: dict[str, str] = {
    "de": "German (Deutsch)",
    "en": "English",
}


def cancel_interaction_ack(language: str | None) -> str:
    """Return a brief TTS-safe line after cancel-interaction classification."""
    lang = (language or "en").lower()
    if lang.startswith("de"):
        return "Alles klar, verstanden."
    return "Okay, got it."


def _is_acceptable(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped.split()) < 3:
        return False
    if len(stripped) > _CANCEL_MAX_CHARS:
        return False
    if "?" in stripped or "\n" in stripped or "\r" in stripped:
        return False
    if any(ch in stripped for ch in ("*", "`", "#", "[", "]")):
        return False
    return stripped.count(".") + stripped.count("!") <= 1


async def generate_cancel_speech(language: str | None, user_text: str | None) -> str:
    """Return an LLM-generated cancel acknowledgement with safe fallback."""
    fallback = cancel_interaction_ack(language)
    lang_code = (language or "en").lower().split("-", 1)[0]
    language_name = _LANGUAGE_NAMES.get(lang_code, language or "English")

    try:
        prompt_template = await _load_prompt_path_async(_prompt_path("cancel_speech"))
        system_prompt = prompt_template.format(language=language_name)
        user_payload = wrap_user_input((user_text or "").strip()[:200] or "(dismiss)")
        result = await asyncio.wait_for(
            complete(
                agent_id="filler-agent",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=30,
                temperature=0.6,
            ),
            timeout=_CANCEL_LLM_TIMEOUT_SEC,
        )
    except TimeoutError:
        logger.warning("Cancel-ACK LLM timed out (>%.1fs)", _CANCEL_LLM_TIMEOUT_SEC)
        return fallback
    except Exception:
        logger.warning("Cancel-ACK LLM failed, using deterministic fallback", exc_info=True)
        return fallback

    cleaned = (result or "").strip().strip('"').strip("'")
    if not _is_acceptable(cleaned):
        logger.debug("Cancel-ACK LLM output rejected by guardrails: %r", result)
        return fallback
    return cleaned
