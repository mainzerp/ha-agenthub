"""Individual matching signals for entity resolution."""

from __future__ import annotations

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from app.entity.aliases import AliasResolver
from app.entity.index import EntityIndex

try:
    from pyphonetics import Metaphone, Soundex  # type: ignore[import-untyped]
except ImportError:
    Soundex = None
    Metaphone = None


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


class EmbeddingSignal:
    """Vector similarity signal from the pre-embedded entity index."""

    @staticmethod
    async def score(
        query: str,
        entity_index: EntityIndex,
        n: int = 10,
    ) -> list[tuple[str, str, float]]:
        """Search entity index and return (entity_id, friendly_name, similarity_score) tuples.

        ChromaDB returns cosine distance (0=identical). Convert to similarity: 1 - distance.
        """
        results = await entity_index.search_async(query, n_results=n)
        scored = []
        for entry, distance in results:
            similarity = max(0.0, 1.0 - distance)
            scored.append((entry.entity_id, entry.friendly_name, similarity))
        return scored


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
        if Soundex is None or Metaphone is None:
            return 0.0
        try:
            soundex = Soundex()
            if soundex.phonetics(query.lower()) == soundex.phonetics(candidate.lower()):
                return 1.0
            metaphone = Metaphone()
            if metaphone.phonetics(query.lower()) == metaphone.phonetics(candidate.lower()):
                return 0.8
        except Exception:
            return 0.0
        return 0.0
