"""Default seed data for the SQLite schema."""

import aiosqlite

from app.defaults import CACHE_DEFAULTS, DEFAULT_LOCAL_EMBEDDING_MODEL


async def _seed_defaults(db: aiosqlite.Connection) -> None:
    """Insert default seed data. Uses INSERT OR IGNORE to be idempotent."""

    # Default settings
    default_settings = [
        # Cache settings
        (
            "cache.enabled",
            "true" if CACHE_DEFAULTS["cache.enabled"] else "false",
            "bool",
            "cache",
            "Enable cache lookups and writes globally",
        ),
        (
            "cache.compound_utterance_bypass",
            "true" if CACHE_DEFAULTS["cache.compound_utterance_bypass"] else "false",
            "bool",
            "cache",
            "Bypass cache lookup for structurally compound utterances",
        ),
        (
            "cache.routing.enabled",
            "true" if CACHE_DEFAULTS["cache.routing.enabled"] else "false",
            "bool",
            "cache",
            "Enable routing cache lookup and storage",
        ),
        (
            "cache.routing.semantic_threshold",
            str(CACHE_DEFAULTS["cache.routing.semantic_threshold"]),
            "float",
            "cache",
            "Routing cache semantic hit threshold",
        ),
        (
            "cache.routing.max_entries",
            str(CACHE_DEFAULTS["cache.routing.max_entries"]),
            "int",
            "cache",
            "Routing cache max entries (LRU eviction)",
        ),
        (
            "cache.action.enabled",
            "true" if CACHE_DEFAULTS["cache.action.enabled"] else "false",
            "bool",
            "cache",
            "Enable action cache lookup and storage",
        ),
        (
            "cache.action.semantic_threshold",
            str(CACHE_DEFAULTS["cache.action.semantic_threshold"]),
            "float",
            "cache",
            "Action cache semantic hit threshold",
        ),
        (
            "cache.action.max_entries",
            str(CACHE_DEFAULTS["cache.action.max_entries"]),
            "int",
            "cache",
            "Action cache max entries (LRU eviction)",
        ),
        (
            "cache.validator.enabled",
            "true" if CACHE_DEFAULTS["cache.validator.enabled"] else "false",
            "bool",
            "cache",
            "Enable periodic action-cache validation",
        ),
        (
            "cache.validator.interval_minutes",
            str(CACHE_DEFAULTS["cache.validator.interval_minutes"]),
            "number",
            "cache",
            "Minutes between periodic action-cache validation scans (0 = disabled)",
        ),
        (
            "cache.validator.model",
            CACHE_DEFAULTS["cache.validator.model"],
            "string",
            "cache",
            "LLM model for cache validator response regeneration (empty = template only)",
        ),
        (
            "cache.validator.temperature",
            str(CACHE_DEFAULTS["cache.validator.temperature"]),
            "float",
            "cache",
            "Temperature for cache validator LLM regeneration",
        ),
        (
            "cache.validator.reasoning_effort",
            CACHE_DEFAULTS["cache.validator.reasoning_effort"],
            "string",
            "cache",
            "Reasoning effort for cache validator LLM calls",
        ),
        (
            "cache.validator.max_tokens",
            str(CACHE_DEFAULTS["cache.validator.max_tokens"]),
            "int",
            "cache",
            "Max tokens for cache validator LLM regeneration",
        ),
        (
            "cache.validator.batch_size",
            str(CACHE_DEFAULTS["cache.validator.batch_size"]),
            "int",
            "cache",
            "Number of cache entries to validate in a single LLM batch call",
        ),
        # Embedding settings
        (
            "embedding.provider",
            "local",
            "string",
            "embedding",
            "Embedding provider: local, openrouter, groq, anthropic, ollama, or custom_openai",
        ),
        # 0.23.0: default to a multilingual sentence-transformer so that
        # query tokens such as "bedroom" / "schlafzimmer" / "chambre" /
        # "dormitorio" land near each other in vector space without any
        # per-language seed data. Admins can override.
        (
            "embedding.local_model",
            DEFAULT_LOCAL_EMBEDDING_MODEL,
            "string",
            "embedding",
            "Local embedding model name (multilingual recommended)",
        ),
        (
            "embedding.external_model",
            "",
            "string",
            "embedding",
            "External embedding model (e.g., openai/text-embedding-3-small)",
        ),
        ("embedding.dimension", "384", "int", "embedding", "Embedding dimension (auto-detected from model)"),
        # Entity matching settings
        (
            "entity_matching.confidence_threshold",
            "0.60",
            "float",
            "entity_matching",
            "Minimum confidence for entity match",
        ),
        ("entity_matching.top_n_candidates", "3", "int", "entity_matching", "Top-N candidates for LLM disambiguation"),
        (
            "entity_matching.oversample_factor",
            "20",
            "int",
            "entity_matching",
            "Embedding shortlist multiplier when agent visibility/preferred-domain hints are present",
        ),
        # 0.23.0: language-agnostic on-demand expansion cache.
        (
            "entity_matching.expansion.enabled",
            "true",
            "bool",
            "entity_matching",
            "Enable on-demand LLM expansion of cold query tokens",
        ),
        (
            "entity_matching.expansion.ttl_seconds",
            "2592000",
            "int",
            "entity_matching",
            "Query synonym cache TTL in seconds (default 30 days)",
        ),
        (
            "entity_matching.expansion.max_cache_rows",
            "5000",
            "int",
            "entity_matching",
            "Query synonym cache LRU cap",
        ),
        (
            "entity_matching.log_misses",
            "true",
            "bool",
            "entity_matching",
            "Emit structured entity_match_diag log on matcher misses",
        ),
        (
            "agents.actionable.primary_text_source",
            "original_when_translated",
            "string",
            "agents",
            "Primary user message for actionable agents: 'original_when_translated' or 'description_first'",
        ),
        (
            "wake_briefing.enabled",
            "true",
            "bool",
            "agents",
            "Enable LLM-composed wake briefings for internal alarms.",
        ),
        (
            "wake_briefing.sources.weather",
            "true",
            "bool",
            "agents",
            "Include weather summary in wake briefings.",
        ),
        (
            "wake_briefing.sources.date",
            "true",
            "bool",
            "agents",
            "Include current date and weekday in wake briefings.",
        ),
        (
            "wake_briefing.sources.news",
            "true",
            "bool",
            "agents",
            "Include news headlines in wake briefings using general-agent tools.",
        ),
        (
            "wake_briefing.sources.calendar",
            "true",
            "bool",
            "agents",
            "Include calendar events for the next 24 hours in wake briefings.",
        ),
        (
            "wake_briefing.sources.sensors",
            "false",
            "bool",
            "agents",
            "Include configured sensor states in wake briefings.",
        ),
        (
            "wake_briefing.sensor_entities",
            "[]",
            "json",
            "agents",
            "List of sensor entity_ids to read for wake briefings.",
        ),
        (
            "wake_briefing.news_query",
            "top news today",
            "string",
            "agents",
            "User-text dispatched to general-agent for news.",
        ),
        (
            "wake_briefing.news_count",
            "3",
            "int",
            "agents",
            "Requested number of news headlines for wake briefings.",
        ),
        (
            "wake_briefing.timeout_seconds",
            "10",
            "int",
            "agents",
            "Total budget for composing a wake briefing before falling back.",
        ),
        (
            "wake_briefing.composer_prompt",
            # Default is also in prompts/wake_briefing.txt; this seed is for
            # backward compat with existing installs. When this setting is
            # empty, wake_briefing.py loads the canonical prompt file instead.
            "You compose a short friendly spoken morning briefing from a JSON facts object. Mention the date and weekday, weather, calendar, news headlines, and any sensor readings the user configured. Keep it under 90 spoken seconds. Reply in the user's language.",
            "string",
            "agents",
            "System prompt for the wake-briefing composer LLM.",
        ),
        (
            "calendar.reminder_injection.enabled",
            "true",
            "bool",
            "calendar",
            "Enable proactive calendar reminder injection into orchestrator responses",
        ),
        (
            "calendar.reminder_injection.offsets",
            "[1440, 60, 15]",
            "json",
            "calendar",
            "Reminder offset markers in minutes (comma-separated)",
        ),
        (
            "calendar.reminder_injection.lookahead_hours",
            "24",
            "int",
            "calendar",
            "How many hours ahead to look for upcoming calendar events",
        ),
        # Rewrite agent settings
        ("rewrite.model", "groq/llama-3.1-8b-instant", "string", "rewrite", "LLM model for rewrite agent"),
        ("rewrite.temperature", "0.8", "float", "rewrite", "Temperature for rewrite agent"),
        # Personality settings
        ("personality.prompt", "", "string", "personality", "Personality system prompt for response mediation"),
        # Communication settings
        (
            "communication.streaming_mode",
            "websocket",
            "string",
            "communication",
            "Streaming mode: websocket, sse, none",
        ),
        ("communication.ws_reconnect_interval", "5", "int", "communication", "WebSocket reconnect interval in seconds"),
        ("communication.stream_buffer_size", "1", "int", "communication", "Token batching buffer size"),
        # A2A settings
        ("a2a.default_timeout", "10", "int", "a2a", "Default agent timeout in seconds"),
        ("a2a.max_iterations", "3", "int", "a2a", "Max iterations per agent to prevent loops"),
        # General settings
        (
            "general.conversation_context_turns",
            "3",
            "int",
            "general",
            "Number of prior conversation turns to keep (user+assistant pairs)",
        ),
        # Home context settings
        (
            "home.timezone",
            "",
            "string",
            "home",
            "Manual timezone override (e.g., Europe/Berlin). Empty = auto-detect from HA.",
        ),
        (
            "home.location_name",
            "",
            "string",
            "home",
            "Manual home location name override. Empty = auto-detect from HA.",
        ),
        # Orchestrator settings
        (
            "orchestrator.organic_followup_enabled",
            "false",
            "bool",
            "orchestrator",
            "Offer a follow-up prompt after successful voice responses",
        ),
        (
            "orchestrator.organic_followup_probability",
            "0.08",
            "float",
            "orchestrator",
            "Probability (0.0-1.0) of appending a follow-up offer to successful responses",
        ),
        (
            "orchestrator.mediation_streaming_enabled",
            "false",
            "bool",
            "orchestrator",
            "Stream mediated tokens incrementally to the client for earlier TTS start",
        ),
        # HA URL is normally set in setup; seed empty so admin UI can upsert
        # before first run and ``GET /api/admin/settings`` stays consistent.
        ("ha_url", "", "string", "ha", "Home Assistant base URL (http/https)"),
        # Entity sync
        (
            "entity_sync.interval_minutes",
            "30",
            "number",
            "sync",
            "Minutes between periodic entity index syncs (0 = disabled)",
        ),
        # Filler agent
        (
            "filler.enabled",
            "false",
            "bool",
            "filler",
            "Enable interim filler responses for slow agents",
        ),
        (
            "filler.threshold_ms",
            "1000",
            "number",
            "filler",
            "Milliseconds to wait before sending filler",
        ),
        # Mediation
        (
            "mediation.model",
            "",
            "string",
            "mediation",
            "LLM model for mediation/merge (empty = use orchestrator model)",
        ),
        (
            "mediation.temperature",
            "0.3",
            "number",
            "mediation",
            "Temperature for mediation/merge LLM calls",
        ),
        (
            "mediation.max_tokens",
            "8192",
            "number",
            "mediation",
            "Max tokens for mediation/merge LLM calls (increase for reasoning models)",
        ),
        # Language
        (
            "language",
            "auto",
            "string",
            "general",
            "Response language: 'auto' = detect from user input, or a specific ISO code like 'de', 'en'",
        ),
    ]

    await db.executemany(
        "INSERT OR IGNORE INTO settings (key, value, value_type, category, description) VALUES (?, ?, ?, ?, ?)",
        default_settings,
    )

    # Default agent configs
    default_agents = [
        ("orchestrator", 1, "groq/llama-3.1-8b-instant", 10, 3, 0.3, 1024, "Intent classification and task routing"),
        ("light-agent", 1, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Lighting control"),
        ("music-agent", 1, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Music and media playback"),
        ("general-agent", 1, "openrouter/openai/gpt-4o-mini", 5, 3, 0.5, 1024, "Fallback and general Q&A"),
        (
            "wake-briefing-composer",
            1,
            "openrouter/openai/gpt-4o-mini",
            5,
            3,
            0.5,
            1024,
            "LLM-composed wake briefings for internal alarms",
        ),
        ("timer-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Timers and alarms"),
        ("calendar-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Calendar event management"),
        ("climate-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Climate, HVAC, fans, and humidifiers"),
        ("media-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Media player control"),
        ("cover-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Cover and blind control"),
        ("vacuum-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Robot vacuum control"),
        ("scene-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Scene activation"),
        ("automation-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Automation management"),
        ("security-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Security system control"),
        ("lists-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Todo and shopping list management"),
        (
            "send-agent",
            0,
            "openrouter/openai/gpt-4o-mini",
            5,
            1,
            0.2,
            512,
            "Send content to devices via notification or TTS",
        ),
        ("rewrite-agent", 0, "groq/llama-3.1-8b-instant", 2, 1, 0.8, 1024, "Cached response phrasing variation"),
        ("filler-agent", 1, "groq/llama-3.1-8b-instant", 3, 1, 0.7, 1024, "Interim filler TTS phrase generation"),
    ]

    await db.executemany(
        "INSERT OR IGNORE INTO agent_configs "
        "(agent_id, enabled, model, timeout, max_iterations, temperature, max_tokens, description) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        default_agents,
    )

    # Default entity matching weights
    default_matching = [
        ("weight.levenshtein", "0.20", "Levenshtein distance signal weight"),
        ("weight.jaro_winkler", "0.20", "Jaro-Winkler similarity signal weight"),
        ("weight.phonetic", "0.15", "Phonetic matching signal weight"),
        ("weight.embedding", "0.30", "Embedding similarity signal weight"),
        ("weight.alias", "0.15", "Alias resolution signal weight"),
    ]

    await db.executemany(
        "INSERT OR IGNORE INTO entity_matching_config (key, value, description) VALUES (?, ?, ?)",
        default_matching,
    )

    # Setup wizard steps
    setup_steps = [
        ("admin_password",),
        ("ha_connection",),
        ("container_api_key",),
        ("llm_providers",),
        ("review_complete",),
    ]

    await db.executemany(
        "INSERT OR IGNORE INTO setup_state (step) VALUES (?)",
        setup_steps,
    )

    # Initial schema version
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")

    # Default entity visibility rules
    default_visibility_rules = [
        ("light-agent", "domain_include", "light"),
        ("light-agent", "domain_include", "switch"),
        ("music-agent", "domain_include", "media_player"),
        ("climate-agent", "domain_include", "climate"),
        ("climate-agent", "domain_include", "weather"),
        ("climate-agent", "domain_include", "fan"),
        ("climate-agent", "domain_include", "humidifier"),
        ("media-agent", "domain_include", "media_player"),
        ("scene-agent", "domain_include", "scene"),
        ("automation-agent", "domain_include", "automation"),
        ("timer-agent", "domain_include", "persistent_notification"),
        ("timer-agent", "domain_include", "media_player"),
        ("calendar-agent", "domain_include", "calendar"),
        ("lists-agent", "domain_include", "todo"),
        ("cover-agent", "domain_include", "cover"),
        ("vacuum-agent", "domain_include", "vacuum"),
        ("security-agent", "domain_include", "alarm_control_panel"),
        ("security-agent", "domain_include", "lock"),
        ("security-agent", "domain_include", "camera"),
        # Sensor device_class rules for specialist agents
        ("climate-agent", "domain_include", "sensor"),
        ("climate-agent", "device_class_include", "temperature"),
        ("climate-agent", "device_class_include", "humidity"),
        ("climate-agent", "device_class_include", "pressure"),
        ("climate-agent", "device_class_include", "dew_point"),
        ("climate-agent", "device_class_include", "atmospheric_pressure"),
        ("climate-agent", "device_class_include", "moisture"),
        ("climate-agent", "device_class_include", "precipitation_intensity"),
        ("climate-agent", "device_class_include", "wind_speed"),
        ("climate-agent", "device_class_include", "wind_direction"),
        ("security-agent", "domain_include", "sensor"),
        ("security-agent", "domain_include", "binary_sensor"),
        ("security-agent", "device_class_include", "motion"),
        ("security-agent", "device_class_include", "occupancy"),
        ("security-agent", "device_class_include", "door"),
        ("security-agent", "device_class_include", "window"),
        ("security-agent", "device_class_include", "tamper"),
        ("security-agent", "device_class_include", "vibration"),
        ("security-agent", "device_class_include", "smoke"),
        ("security-agent", "device_class_include", "gas"),
        ("security-agent", "device_class_include", "carbon_monoxide"),
        ("security-agent", "device_class_include", "doorbell"),
        ("security-agent", "device_class_include", "opening"),
        ("security-agent", "device_class_include", "safety"),
        ("light-agent", "domain_include", "sensor"),
        ("light-agent", "device_class_include", "illuminance"),
    ]

    await db.executemany(
        "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
        default_visibility_rules,
    )
