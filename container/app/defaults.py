"""Shared runtime default values.

Keep seeded settings defaults and runtime fallbacks aligned across
lightweight modules without importing heavy runtime components.
"""

DEFAULT_LOCAL_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"

CACHE_DEFAULTS: dict[str, object] = {
    "cache.enabled": True,
    "cache.compound_utterance_bypass": True,
    "cache.routing.enabled": True,
    "cache.routing.semantic_fallback_enabled": True,
    "cache.routing.semantic_threshold": 0.92,
    "cache.routing.max_entries": 50000,
    "cache.action.enabled": True,
    "cache.action.semantic_fallback_enabled": True,
    "cache.action.semantic_threshold": 0.95,
    "cache.action.max_entries": 50000,
    "cache.lru.trigger_fraction": 0.95,
    "cache.lru.eviction_interval": 100,
}
