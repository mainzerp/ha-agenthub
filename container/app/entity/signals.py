"""Individual matching signals for entity resolution."""

from __future__ import annotations

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from app.entity.aliases import AliasResolver

try:
    from pyphonetics import Metaphone, Soundex  # type: ignore[import-untyped]
except ImportError:
    Soundex = None
    Metaphone = None

# Hoisted codec singletons: pyphonetics codecs are stateless, so they are
# instantiated once at import instead of on every score() call (hot path:
# per-token phonetic matching calls score() for each token pair).
_SOUNDEX = Soundex() if Soundex is not None else None
_METAPHONE = Metaphone() if Metaphone is not None else None


class LevenshteinSignal:
    """Fuzzy string matching using Levenshtein distance via rapidfuzz."""

    @staticmethod
    def score(query: str, candidate: str) -> float:
        """Return similarity score 0.0-1.0 between query and candidate."""
        return fuzz.ratio(query.lower(), candidate.lower()) / 100.0


class JaroWinklerSignal:
    """Fuzzy string matching using Jaro-Winkler similarity via rapidfuzz."""

    @staticmethod
    def score(query: str, candidate: str) -> float:
        """Return Jaro-Winkler similarity score 0.0-1.0."""
        return JaroWinkler.similarity(query.lower(), candidate.lower())


class AliasSignal:
    """Exact alias lookup from SQLite aliases table."""

    @staticmethod
    async def score(query: str, alias_resolver: AliasResolver) -> tuple[str, float] | None:
        """Check if query exactly matches a known alias.

        Returns (entity_id, 1.0) on exact match, None otherwise.
        """
        entity_id = await alias_resolver.resolve(query.strip())
        if entity_id:
            return (entity_id, 1.0)
        return None


class PhoneticSignal:
    """Phonetic similarity signal using Soundex and Metaphone."""

    @staticmethod
    def score(query: str, candidate: str) -> float:
        """Return phonetic similarity score 0.0-1.0.

        Returns 1.0 for Soundex match, 0.8 for Metaphone match, 0.0 otherwise.
        """
        if _SOUNDEX is None or _METAPHONE is None:
            return 0.0
        try:
            if _SOUNDEX.phonetics(query.lower()) == _SOUNDEX.phonetics(candidate.lower()):
                return 1.0
            if _METAPHONE.phonetics(query.lower()) == _METAPHONE.phonetics(candidate.lower()):
                return 0.8
        except Exception:
            return 0.0
        return 0.0
