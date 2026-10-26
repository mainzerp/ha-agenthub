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
    "cache.validator.audit_retention_days": 90,
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

VOICE_DEFAULTS: dict[str, object] = {
    "voice.speaker_id.enabled": False,
    "voice.speaker_id.threshold": 0.55,
    "voice.speaker_id.margin": 0.12,
    "voice.speaker_id.min_duration_s": 1.0,
    "voice.speaker_id.min_enroll_duration_s": 1.5,
    "voice.speaker_id.enroll_min_similarity": 0.45,
    "voice.speaker_id.wait_ms": 500,
    "voice.speaker_id.num_threads": 2,
    "voice.speaker_id.keep_audio": False,
    "voice.speaker_id.audio_retention_days": 30,
}

# Inclusive (min, max) bounds enforced by the settings API for numeric voice settings.
VOICE_SETTING_RANGES: dict[str, tuple[float, float]] = {
    "voice.speaker_id.threshold": (0.0, 1.0),
    "voice.speaker_id.margin": (0.0, 1.0),
    "voice.speaker_id.min_duration_s": (0.0, 30.0),
    "voice.speaker_id.min_enroll_duration_s": (0.5, 30.0),
    "voice.speaker_id.enroll_min_similarity": (0.0, 1.0),
    "voice.speaker_id.wait_ms": (0, 3000),
    "voice.speaker_id.num_threads": (1, 8),
    "voice.speaker_id.audio_retention_days": (1, 365),
}

# Setting descriptions of the voice-turn recording settings, shared by the
# fresh-install seed and migration 53.
VOICE_KEEP_AUDIO_DESCRIPTION = (
    "Keep the audio of identified and unknown voice turns locally so they can be assigned to a voice profile"
)
VOICE_AUDIO_RETENTION_DESCRIPTION = "Days a recorded voice turn is kept before it is deleted (1-365)"

# AI-task helper agent configs (``agent_configs`` rows, never A2A agents):
# (agent_id, enabled, model, timeout, max_iterations, temperature, max_tokens,
# description). Shared by the fresh-install seed and migration 52. The model
# matches the seeded general-agent default; vision-agent needs an
# image-capable model.
AI_TASK_HELPER_AGENT_CONFIGS: tuple[tuple[str, int, str, int, int, float, int, str], ...] = (
    (
        "vision-agent",
        1,
        "openrouter/openai/gpt-4o-mini",
        30,
        1,
        0.2,
        1024,
        "Describes image attachments of AI tasks (needs a vision-capable model)",
    ),
    (
        "task-formatter",
        1,
        "openrouter/openai/gpt-4o-mini",
        15,
        1,
        0.1,
        2048,
        "Formats AI-task results as JSON for the requested structure",
    ),
)
