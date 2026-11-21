"""Shared tokenization helpers for token-based entity preselection.

Language-agnostic by design: no stopword lists. Generic tokens are
tamed by a document-frequency cap in ``EntityIndex.find_by_tokens``
instead of language-specific word tables.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from rapidfuzz import fuzz

from app.models.entity_index import EntityIndexEntry

# An entity token of at least this many characters contained in a longer query
# token counts as a hit, so German compounds match their parts
# ("Kellerlicht" contains "keller" and "licht").
COMPOUND_MIN_TOKEN_LEN = 4

# Fuzzy name tokens (speech-to-text misspellings such as "Estisch" for
# "Esstisch"): both folded tokens need at least this many characters, may
# differ in length by at most ``FUZZY_MAX_LEN_DIFF``, must share their first
# character, and need a similarity of at least ``FUZZY_MIN_SIMILARITY``.
FUZZY_MIN_TOKEN_LEN = 6
FUZZY_MAX_LEN_DIFF = 2
FUZZY_MIN_SIMILARITY = 0.85


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
    - identity: ``device_name`` plus each of ``id_tokens``, except single-digit
      ``id_tokens`` (``light.flur_4``: a spoken number such as a volume level
      must not hit every entity_id ending in that digit)
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
        identity_tokens.update(t for t in normalize_tokenize(token) if not (len(t) == 1 and t.isdigit()))
    return name_tokens, identity_tokens, area_tokens


def token_in_query(token: str, query_tokens: set[str]) -> bool:
    """True when ``token`` occurs in ``query_tokens`` verbatim or as a compound part."""
    if token in query_tokens:
        return True
    return len(token) >= COMPOUND_MIN_TOKEN_LEN and any(
        len(qt) >= COMPOUND_MIN_TOKEN_LEN and token in qt for qt in query_tokens
    )


def token_similarity(a: str, b: str) -> float:
    """Normalized similarity of two folded tokens in ``[0, 1]`` (rapidfuzz Indel ratio)."""
    return fuzz.ratio(a, b) / 100.0


class FuzzyTokenMatcher:
    """Fuzzy fallback for entity tokens that miss a query verbatim and as a compound part.

    Built once per query. Cheap prefilters run before the similarity:
    both tokens have at least ``FUZZY_MIN_TOKEN_LEN`` characters, their
    lengths differ by at most ``FUZZY_MAX_LEN_DIFF``, and they share the
    first character (query tokens are bucketed by it). The result per
    entity token is memoized, so a token shared by many entities
    ("licht") is compared once per query.
    """

    __slots__ = ("_buckets", "_memo")

    def __init__(self, query_tokens: Iterable[str]) -> None:
        self._buckets: dict[str, list[str]] = {}
        for qt in query_tokens:
            if len(qt) >= FUZZY_MIN_TOKEN_LEN and not qt.isdigit():
                self._buckets.setdefault(qt[0], []).append(qt)
        self._memo: dict[str, bool] = {}

    def matches(self, token: str) -> bool:
        """True when ``token`` is similar enough to one query token."""
        if len(token) < FUZZY_MIN_TOKEN_LEN:
            return False
        bucket = self._buckets.get(token[0])
        if not bucket:
            return False
        cached = self._memo.get(token)
        if cached is not None:
            return cached
        result = any(
            abs(len(token) - len(qt)) <= FUZZY_MAX_LEN_DIFF and token_similarity(token, qt) >= FUZZY_MIN_SIMILARITY
            for qt in bucket
        )
        self._memo[token] = result
        return result


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
