"""Shared runtime default values.

Keep seeded settings defaults and runtime fallbacks aligned across
lightweight modules without importing heavy runtime components.
"""

DEFAULT_LOCAL_EMBEDDING_MODEL = "intfloat/multilingual-e5-base"

CACHE_DEFAULTS: dict[str, object] = {
    "cache.enabled": True,
    "cache.compound_utterance_bypass": True,
    "cache.routing.enabled": True,
    # Semantic routing tier (P4): active by default (no shadow mode).
    "cache.routing.semantic_enabled": True,
    "cache.routing.semantic_threshold": 0.92,
    "cache.routing.max_entries": 50000,
    "cache.action.enabled": True,
    "cache.action.semantic_threshold": 0.95,
    "cache.action.max_entries": 50000,
    "cache.lru.trigger_fraction": 0.95,
    "cache.lru.eviction_interval": 100,
    "cache.validator.enabled": True,
    "cache.validator.interval_minutes": 60,
    "cache.validator.model": "",
    "cache.validator.temperature": 0.2,
    "cache.validator.reasoning_effort": "low",
    "cache.validator.max_tokens": 1024,
    "cache.validator.batch_size": 10,
}

MEMORY_DEFAULTS: dict[str, object] = {
    "memory.enabled": True,
    "memory.scope": "user",
    "memory.wait_mode": "blocking",
    "memory.wait_timeout_ms": 800,
    "memory.similarity_threshold": 0.85,
    "memory.max_matches": 3,
    "memory.max_snippet_chars": 300,
    "memory.max_continuation_turns": 5,
}
