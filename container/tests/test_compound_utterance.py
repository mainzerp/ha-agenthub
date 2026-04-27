from __future__ import annotations

import pytest

from app.agents.compound_utterance import looks_compound


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Schalte das Licht aus. Spiel etwas Musik.", True),
        ("Turn off the kitchen lights. Then play music please.", True),
        ("Allume la cuisine. Joue de la musique.", True),
        ("Do task A; do B now please.", True),
        ("Bitte Kueche ausschalten. Dann neben sie machen wir es auf Ruhe Musik.", True),
        ("Turn on the kitchen light.", False),
        ("Turn off the kitchen and play music.", False),  # Single sentence; documented miss.
        ("Licht aus, Musik an.", False),  # Comma-joined; documented miss.
        ("e.g. turn it off.", False),  # Only one substantive segment after punctuation splitting.
        ("", False),  # Empty input is never compound.
        ("   ", False),  # Whitespace-only input is never compound.
        (None, False),  # Null input is never compound.
        ("Play rock and roll.", False),  # Single sentence; detector does not key on conjunctions.
    ],
)
def test_looks_compound_unit(text, expected):
    assert looks_compound(text) is expected