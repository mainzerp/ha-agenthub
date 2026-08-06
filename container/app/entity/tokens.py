"""Shared tokenization helpers for token-based entity preselection.

Language-agnostic by design: no stopword lists. Generic tokens are
tamed by a document-frequency cap in ``EntityIndex.find_by_tokens``
instead of language-specific word tables.
"""

from __future__ import annotations

import re
import unicodedata

from app.models.entity_index import EntityIndexEntry


def normalize_tokenize(text: str) -> set[str]:
    """Normalize text and split it into lowercase tokens.

    Mirrors ``matcher._normalize_for_containment``: lowercase, NFKD,
    strip combining marks (Mn), collapse German digraphs
    (ae->a, oe->o, ue->u), then split on non-word characters and drop
    empties.
    """
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return {t for t in re.split(r"\W+", text) if t}


def entry_tokens(entry: EntityIndexEntry) -> set[str]:
    """Union of normalized tokens across an entry's distinctive fields.

    Same field set as the token-overlap bonus in
    ``EntityMatcher._match_query``: friendly_name, area, area_name,
    device_name, aliases, and each id_tokens element.
    """
    tokens: set[str] = set()
    for src in (
        entry.friendly_name,
        entry.area or "",
        entry.area_name or "",
        entry.device_name or "",
    ):
        if src:
            tokens.update(normalize_tokenize(src))
    for alias in entry.aliases or []:
        tokens.update(normalize_tokenize(alias))
    for token in entry.id_tokens or []:
        tokens.update(normalize_tokenize(token))
    return tokens
