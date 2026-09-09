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
    (ae->a, oe->o, ue->u), then split on non-word characters AND
    underscores (``_`` is a ``\\w`` char but acts as a separator in
    user-typed snake_case queries) and drop empties.
    """
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return {t for t in re.split(r"[\W_]+", text) if t}


def entry_tokens(entry: EntityIndexEntry) -> set[str]:
    """Union of normalized tokens across an entry's distinctive fields.

    Fields: friendly_name, area, area_name, device_name, aliases, and
    each id_tokens element.
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


def entry_field_tokens(
    entry: EntityIndexEntry,
) -> tuple[set[str], set[str], set[str]]:
    """Per-field-class normalized token sets for an entry.

    Returns ``(name_tokens, identity_tokens, area_tokens)``:

    - name: ``friendly_name`` plus each of ``aliases``
    - identity: ``device_name`` plus each of ``id_tokens``
    - area: ``area`` plus ``area_name``

    Used by the recall scorer so name evidence can outrank area evidence;
    ``entry_tokens`` stays the flat union the index posting map depends on.
    """
    name_tokens: set[str] = set(normalize_tokenize(entry.friendly_name or ""))
    identity_tokens: set[str] = set(normalize_tokenize(entry.device_name or ""))
    area_tokens: set[str] = set(normalize_tokenize(entry.area or ""))
    area_tokens.update(normalize_tokenize(entry.area_name or ""))
    for alias in entry.aliases or []:
        name_tokens.update(normalize_tokenize(alias))
    for token in entry.id_tokens or []:
        identity_tokens.update(normalize_tokenize(token))
    return name_tokens, identity_tokens, area_tokens
