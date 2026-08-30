"""Entity subsystem -- index, alias resolution, matching signals, and matcher."""

from app.entity.aliases import AliasResolver
from app.entity.index import EntityIndex
from app.entity.matcher import EntityMatcher, MatchResult
from app.entity.signals import AliasSignal, LevenshteinSignal

__all__ = [
    "AliasResolver",
    "AliasSignal",
    "EntityIndex",
    "EntityMatcher",
    "LevenshteinSignal",
    "MatchResult",
]
