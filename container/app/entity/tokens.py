"""Shared tokenization helpers for token-based entity preselection.

Language-agnostic by design: no stopword lists. Generic tokens are
tamed by a document-frequency cap in ``EntityIndex.find_by_tokens``
instead of language-specific word tables.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from app.models.entity_index import EntityIndexEntry

# An entity token of at least this many characters contained in a longer query
# token counts as a hit, so German compounds match their parts
# ("Kellerlicht" contains "keller" and "licht").
COMPOUND_MIN_TOKEN_LEN = 4


def fold_text(text: str) -> str:
    """Shared lookup folding for entity names and queries.

    Lowercase, NFKD, strip combining marks (Mn), expand ``ß`` to ``ss``
    and collapse German digraphs (ae->a, oe->o, ue->u) so umlaut and
    transliterated spellings ("Küche", "Kueche", "Kuche") compare equal.
    Used by the matcher, the token index, and the deterministic resolver
    so every resolution stage folds identically.
    """
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace("ß", "ss")
    return text.replace("ae", "a").replace("oe", "o").replace("ue", "u")


def normalize_tokenize(text: str) -> set[str]:
    """Normalize text and split it into lowercase tokens.

    Applies :func:`fold_text`, then splits on non-word characters AND
    underscores (``_`` is a ``\\w`` char but acts as a separator in
    user-typed snake_case queries) and drops empties.
    """
    return {t for t in re.split(r"[\W_]+", fold_text(text)) if t}


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


def token_in_query(token: str, query_tokens: set[str]) -> bool:
    """True when ``token`` occurs in ``query_tokens`` verbatim or as a compound part."""
    if token in query_tokens:
        return True
    return len(token) >= COMPOUND_MIN_TOKEN_LEN and any(
        len(qt) >= COMPOUND_MIN_TOKEN_LEN and token in qt for qt in query_tokens
    )


def text_names_any(text: str, names: Iterable[str]) -> bool:
    """True when ``text`` names one of ``names`` completely.

    A name counts only when EVERY one of its tokens occurs in the text
    (verbatim or as a compound part, see :func:`token_in_query`): a partial
    name ("light" for "Basement Light") does not identify an entity.
    Language-agnostic: compares the entity's own names, no word lists.
    """
    query_tokens = normalize_tokenize(text or "")
    if not query_tokens:
        return False
    for name in names:
        tokens = normalize_tokenize(name or "")
        if tokens and all(token_in_query(token, query_tokens) for token in tokens):
            return True
    return False
