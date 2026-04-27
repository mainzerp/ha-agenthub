"""Structural compound-utterance detector for routing-cache bypass only.

This module performs no intent classification, no agent selection, only
cache-lookup gating. Prime Directive 11 forbids hardcoded keyword routing
for primary intent decisions, so this helper stays purely structural:
sentence terminators plus segment word counts. Agent selection remains
owned by the live LLM classifier.
"""

from __future__ import annotations

import re

_SENTENCE_TERMINATORS = ".!?;"
_MIN_SEGMENT_WORDS = 3
_MIN_SUBSTANTIVE_SEGMENTS = 2
_SEGMENT_SPLIT_RE = re.compile(r"[.!?;]")


def looks_compound(text: str) -> bool:
    if text is None:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    substantive_segments = 0
    for segment in _SEGMENT_SPLIT_RE.split(stripped):
        token_count = len(segment.strip().split())
        if token_count >= _MIN_SEGMENT_WORDS:
            substantive_segments += 1
            if substantive_segments >= _MIN_SUBSTANTIVE_SEGMENTS:
                return True
    return False