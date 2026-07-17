"""Tests for app.agents -- all specialized agents, orchestrator, rewrite, and custom loader."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock litellm before importing any app modules that depend on it
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


class _RateLimitError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
_litellm_mock.RateLimitError = _RateLimitError
sys.modules.setdefault("litellm", _litellm_mock)

import app.llm.client  # noqa: E402,F401 -- force module load for patch targets
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.models.agent import (  # noqa: E402
    IngressTask,
    TaskContext,
)
from tests.helpers import make_ingress_task  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(description: str = "turn on kitchen light", context: TaskContext | None = None) -> IngressTask:
    return make_ingress_task(
        description=description,
        context=context,
    )


# ---------------------------------------------------------------------------
# BaseAgent abstract contract
# ---------------------------------------------------------------------------


class TestLanguageDetection:
    """Tests for the language detection utility."""

    def test_detect_german(self):
        from app.agents.language_detect import detect_user_language

        mock_ld = MagicMock()
        mock_top = MagicMock()
        mock_top.lang = "de"
        mock_top.prob = 0.91
        mock_ld.detect_langs.return_value = [mock_top]
        mock_ld.DetectorFactory = MagicMock()
        mock_ld.LangDetectException = Exception
        with patch.dict(sys.modules, {"langdetect": mock_ld}):
            result = detect_user_language(
                "Kannst du bitte das Licht in der Kueche einschalten und die Heizung auf zwanzig Grad stellen?", "en"
            )
        assert result == "de"

    def test_detect_english(self):
        from app.agents.language_detect import detect_user_language

        mock_ld = MagicMock()
        mock_top = MagicMock()
        mock_top.lang = "en"
        mock_top.prob = 0.92
        mock_ld.detect_langs.return_value = [mock_top]
        mock_ld.DetectorFactory = MagicMock()
        mock_ld.LangDetectException = Exception
        with patch.dict(sys.modules, {"langdetect": mock_ld}):
            result = detect_user_language("Turn on the kitchen light please", "en")
        assert result == "en"

    def test_short_text_fallback(self):
        from app.agents.language_detect import detect_user_language

        result = detect_user_language("ok", "de")
        assert result == "de"

    def test_empty_text_fallback(self):
        from app.agents.language_detect import detect_user_language

        result = detect_user_language("", "en")
        assert result == "en"

    def test_low_confidence_fallback(self):
        from app.agents.language_detect import detect_user_language

        mock_ld = MagicMock()
        mock_top = MagicMock()
        mock_top.lang = "en"
        mock_top.prob = 0.28
        mock_ld.detect_langs.return_value = [mock_top]
        mock_ld.DetectorFactory = MagicMock()
        mock_ld.LangDetectException = Exception
        with patch.dict(sys.modules, {"langdetect": mock_ld}):
            # Ambiguous / low confidence - should fall back to the provided default
            result = detect_user_language("na, was machst du?", "de")
        assert result == "de"


class TestResolveLanguage:
    """Tests for orchestrator _resolve_language."""

    @pytest.mark.asyncio
    async def test_resolve_language_auto_detect(self):
        """When setting is 'auto', language is detected from user text."""
        orch = OrchestratorAgent(dispatcher=MagicMock())
        mock_ld = MagicMock()
        mock_top = MagicMock()
        mock_top.lang = "de"
        mock_top.prob = 0.91
        mock_ld.detect_langs.return_value = [mock_top]
        mock_ld.DetectorFactory = MagicMock()
        mock_ld.LangDetectException = Exception
        with (
            patch("app.agents.orchestrator.SettingsRepository") as mock_repo,
            patch.dict(sys.modules, {"langdetect": mock_ld}),
        ):
            mock_repo.get_value = AsyncMock(return_value="auto")
            result = await orch._resolve_language(
                "Kannst du bitte das Licht in der Kueche einschalten und die Heizung auf zwanzig Grad stellen?", "en"
            )
        assert result == "de"

    @pytest.mark.asyncio
    async def test_resolve_language_manual_override(self):
        """When setting is a specific language code, it overrides detection."""
        orch = OrchestratorAgent(dispatcher=MagicMock())
        with patch("app.agents.orchestrator.SettingsRepository") as mock_repo:
            mock_repo.get_value = AsyncMock(return_value="fr")
            result = await orch._resolve_language("Turn on the light", "en")
        assert result == "fr"

    @pytest.mark.asyncio
    async def test_resolve_language_falls_back_to_turns(self):
        """When user text is ambiguous, detect from conversation turns."""
        orch = OrchestratorAgent(dispatcher=MagicMock())
        turns = [
            {"role": "user", "content": "Schalte bitte das Licht in der Kueche ein"},
            {"role": "assistant", "content": "Das Licht in der Kueche ist jetzt an."},
        ]

        def _detect_side_effect(text: str):
            low = MagicMock()
            low.lang = "en"
            low.prob = 0.28
            de = MagicMock()
            de.lang = "de"
            de.prob = 0.91
            if "Schalte bitte" in text:
                return [de]
            return [low]

        mock_ld = MagicMock()
        mock_ld.detect_langs.side_effect = _detect_side_effect
        mock_ld.DetectorFactory = MagicMock()
        mock_ld.LangDetectException = Exception
        with (
            patch("app.agents.orchestrator.SettingsRepository") as mock_repo,
            patch.dict(sys.modules, {"langdetect": mock_ld}),
        ):
            mock_repo.get_value = AsyncMock(return_value="auto")
            result = await orch._resolve_language("na, was machst du?", "en", turns=turns)
        assert result == "de"


# ---------------------------------------------------------------------------
# Span end_time tests
# ---------------------------------------------------------------------------
