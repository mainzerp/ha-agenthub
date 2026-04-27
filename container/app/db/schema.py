"""SQLite table definitions and initialization.

Manages the SQLite database schema for all structured data: configuration,
secrets, user accounts, conversation history, and analytics.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite

from app.config import settings
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL

_write_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


def _db_path() -> Path:
    """Resolve the SQLite database path and ensure the parent directory exists."""
    p = Path(settings.sqlite_db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


async def _get_or_create_write_connection() -> aiosqlite.Connection:
    """Get or create the shared write connection."""
    global _write_conn
    if _write_conn is None:
        _write_conn = await aiosqlite.connect(str(_db_path()))
        _write_conn.row_factory = aiosqlite.Row
        await _write_conn.execute("PRAGMA journal_mode=WAL")
        await _write_conn.execute("PRAGMA foreign_keys=ON")
    return _write_conn


@asynccontextmanager
async def get_db_read() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager returning a per-call read-only database connection.

    A fresh ``aiosqlite`` connection is opened for every read scope and
    closed on exit. WAL mode is persistent on the database file (set on
    the write connection at startup), so concurrent readers do not block
    each other and do not block writers. ``PRAGMA query_only=ON`` enforces
    read-only access at the connection level.
    """
    db = await aiosqlite.connect(str(_db_path()))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def get_db_write() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager returning the write database connection.

    Acquires _write_lock to serialize writes.
    """
    async with _write_lock:
        db = await _get_or_create_write_connection()
        yield db


# Backward-compatible alias -- points to the write path (safe default).
get_db = get_db_write


async def close_db() -> None:
    """Close the shared write connection. Call on shutdown."""
    global _write_conn
    if _write_conn is not None:
        await _write_conn.close()
        _write_conn = None


async def init_db() -> None:
    """Initialize database schema and seed default data.

    Called at container startup. All operations are idempotent.
    """
    async with get_db() as db:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        await db.commit()


async def _create_tables(db: aiosqlite.Connection) -> None:
    """Create all tables if they do not exist."""

    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            value_type TEXT NOT NULL DEFAULT 'string',
            category TEXT NOT NULL DEFAULT 'general',
            description TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS agent_configs (
            agent_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            model TEXT,
            timeout INTEGER NOT NULL DEFAULT 5,
            max_iterations INTEGER NOT NULL DEFAULT 3,
            temperature REAL NOT NULL DEFAULT 0.2,
            max_tokens INTEGER NOT NULL DEFAULT 1024,
            description TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS custom_agents (
            name TEXT PRIMARY KEY,
            description TEXT,
            system_prompt TEXT NOT NULL,
            model_override TEXT,
            mcp_tools TEXT,
            entity_visibility TEXT,
            intent_patterns TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS entity_matching_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            description TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS aliases (
            alias TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # 0.23.0: organic LLM-expansion cache. Created EMPTY: NO seed data
    # for any language. Created in _create_tables (in addition to
    # migration v18) so fresh test DBs that skip migrations still have
    # the table. Both paths use IF NOT EXISTS and are safe to re-run.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS query_synonym_cache (
            token TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT '',
            expansions TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (token, language)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS mcp_servers (
            name TEXT PRIMARY KEY,
            transport TEXT NOT NULL,
            command_or_url TEXT NOT NULL,
            env_vars TEXT,
            timeout INTEGER NOT NULL DEFAULT 30,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS secrets (
            key TEXT PRIMARY KEY,
            encrypted_value BLOB NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_accounts (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_login TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS setup_state (
            step TEXT PRIMARY KEY,
            completed INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS entity_visibility_rules (
            agent_id TEXT NOT NULL,
            rule_type TEXT NOT NULL,
            rule_value TEXT NOT NULL,
            PRIMARY KEY (agent_id, rule_type, rule_value)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS plugins (
            name TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            version TEXT,
            description TEXT,
            loaded_at TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS agent_mcp_tools (
            agent_id TEXT NOT NULL,
            server_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            PRIMARY KEY (agent_id, server_name, tool_name)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            user_text TEXT NOT NULL,
            agent_id TEXT,
            response_text TEXT,
            action_executed TEXT,
            cache_hit TEXT,
            latency_ms REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            agent_id TEXT,
            data TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS trace_spans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            span_name TEXT NOT NULL,
            agent_id TEXT,
            parent_span TEXT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_ms REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS trace_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL UNIQUE,
            conversation_id TEXT,
            user_input TEXT,
            final_response TEXT,
            agents TEXT,
            total_duration_ms REAL,
            label TEXT,
            source TEXT,
            routing_agent TEXT,
            routing_confidence REAL,
            routing_duration_ms REAL,
            routing_reasoning TEXT,
            agent_instructions TEXT,
            conversation_turns TEXT,
            device_id TEXT,
            area_id TEXT,
            device_name TEXT,
            area_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS send_device_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            device_type TEXT NOT NULL CHECK(device_type IN ('notify', 'tts')),
            ha_service_target TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # 0.26.0: AgentHub-managed timer scheduler. Persists every non-native
    # timer (notification, delayed_action, sleep, snooze, internal plain)
    # so they survive container restarts. The HA timer.* helper-pool
    # concept was removed in 0.26.0; AgentHub owns timer state directly.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_timers (
            id TEXT PRIMARY KEY,
            logical_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            fires_at INTEGER NOT NULL,
            duration_seconds INTEGER NOT NULL,
            origin_device_id TEXT,
            origin_area TEXT,
            briefing INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            fired_at INTEGER,
            cancelled_at INTEGER
        )
    """)


async def _create_indexes(db: aiosqlite.Connection) -> None:
    """Create indexes for query performance."""
    await db.execute("CREATE INDEX IF NOT EXISTS idx_settings_category ON settings(category)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_aliases_entity_id ON aliases(entity_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_conversation_id ON conversations(conversation_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_agent_id ON conversations(agent_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event_type ON analytics(event_type)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_analytics_created_at ON analytics(created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_entity_visibility_agent ON entity_visibility_rules(agent_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_agent_mcp_tools_agent ON agent_mcp_tools(agent_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_spans_trace_id ON trace_spans(trace_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_spans_created_at ON trace_spans(created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_summary_trace_id ON trace_summary(trace_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_summary_created_at ON trace_summary(created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_summary_routing_agent ON trace_summary(routing_agent)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_summary_label ON trace_summary(label)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_timers_state_fires_at ON scheduled_timers(state, fires_at)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_timers_logical_name ON scheduled_timers(logical_name)")


async def _seed_defaults(db: aiosqlite.Connection) -> None:
    """Insert default seed data. Uses INSERT OR IGNORE to be idempotent."""

    # Default settings
    default_settings = [
        # Cache settings
        ("cache.routing.threshold", "0.92", "float", "cache", "Routing cache hit threshold"),
        ("cache.routing.max_entries", "50000", "int", "cache", "Routing cache max entries (LRU eviction)"),
        (
            "cache.response.threshold",
            "0.95",
            "float",
            "cache",
            "Action cache hit threshold (legacy key name: cache.response.threshold)",
        ),
        (
            "cache.response.partial_threshold",
            "0.80",
            "float",
            "cache",
            "Action cache partial match threshold (legacy key name: cache.response.partial_threshold)",
        ),
        (
            "cache.response.max_entries",
            "20000",
            "int",
            "cache",
            "Action cache max entries (LRU eviction; legacy key name: cache.response.max_entries)",
        ),
        (
            "cache.response.enabled",
            "true",
            "bool",
            "cache",
            "Enable action cache storage (legacy key name: cache.response.enabled)",
        ),
        # Embedding settings
        (
            "embedding.provider",
            "local",
            "string",
            "embedding",
            "Embedding provider: local, openrouter, groq, anthropic, or ollama",
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
            "You compose a short friendly spoken morning briefing from a JSON facts object. Mention the date and weekday, weather, calendar, news headlines, and any sensor readings the user configured. Keep it under 90 spoken seconds. Reply in the user's language.",
            "string",
            "agents",
            "System prompt for the wake-briefing composer LLM.",
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
        # HA URL is normally set in setup; seed empty so admin UI can upsert
        # before first run and ``GET /api/admin/settings`` stays consistent.
        ("ha_url", "", "string", "ha", "Home Assistant base URL (http/https)"),
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
        ("climate-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Climate and HVAC control"),
        ("media-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Media player control"),
        ("scene-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Scene activation"),
        ("automation-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Automation management"),
        ("security-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Security system control"),
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
        ("filler-agent", 1, "groq/llama-3.1-8b-instant", 3, 1, 0.7, 50, "Interim filler TTS phrase generation"),
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
        ("media-agent", "domain_include", "media_player"),
        ("scene-agent", "domain_include", "scene"),
        ("automation-agent", "domain_include", "automation"),
        ("timer-agent", "domain_include", "input_datetime"),
        ("timer-agent", "domain_include", "persistent_notification"),
        ("timer-agent", "domain_include", "media_player"),
        ("timer-agent", "domain_include", "calendar"),
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


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Run incremental schema migrations based on schema_version."""
    cursor = await db.execute("SELECT MAX(version) FROM schema_version")
    row = await cursor.fetchone()
    current_version = row[0] if row and row[0] else 1

    if current_version < 2:
        # Migration 2: Lower default temperature for action-oriented agents
        await db.execute("""
            UPDATE agent_configs
            SET temperature = 0.2
            WHERE agent_id IN (
                'light-agent', 'music-agent', 'timer-agent',
                'climate-agent', 'media-agent', 'scene-agent',
                'automation-agent', 'security-agent'
            )
            AND temperature = 0.7
        """)
        await db.execute("""
            UPDATE agent_configs
            SET temperature = 0.5
            WHERE agent_id = 'general-agent'
            AND temperature = 0.7
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (2)")

    if current_version < 3:
        # Migration 3: Increase light-agent max_tokens and default timeout
        await db.execute("""
            UPDATE agent_configs
            SET max_tokens = 512
            WHERE agent_id = 'light-agent'
            AND max_tokens = 256
        """)
        await db.execute("""
            UPDATE settings
            SET value = '10'
            WHERE key = 'a2a.default_timeout'
            AND value = '5'
        """)
        await db.execute("""
            UPDATE agent_configs
            SET max_tokens = 512
            WHERE agent_id = 'music-agent'
            AND max_tokens = 256
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (3)")

    if current_version < 4:
        # Migration 4: Seed default entity visibility rules for agents with zero rules
        agents_with_defaults = {
            "light-agent": ["light", "switch"],
            "music-agent": ["media_player"],
            "climate-agent": ["climate"],
            "media-agent": ["media_player"],
            "scene-agent": ["scene"],
            "automation-agent": ["automation"],
            "security-agent": ["alarm_control_panel", "lock"],
        }

        for agent_id, domains in agents_with_defaults.items():
            cursor = await db.execute(
                "SELECT COUNT(*) FROM entity_visibility_rules WHERE agent_id = ?",
                (agent_id,),
            )
            row = await cursor.fetchone()
            if row and row[0] == 0:
                for domain in domains:
                    await db.execute(
                        "INSERT OR IGNORE INTO entity_visibility_rules "
                        "(agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                        (agent_id, "domain_include", domain),
                    )

        # Migrate legacy rule_types
        await db.execute("UPDATE entity_visibility_rules SET rule_type = 'entity_include' WHERE rule_type = 'entity'")
        await db.execute("UPDATE entity_visibility_rules SET rule_type = 'domain_include' WHERE rule_type = 'domain'")
        await db.execute("UPDATE entity_visibility_rules SET rule_type = 'area_include' WHERE rule_type = 'area'")

        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4)")

    if current_version < 5:
        # Migration 5: Increase rewrite-agent max_tokens from 128 to 512
        await db.execute("""
            UPDATE agent_configs
            SET max_tokens = 512
            WHERE agent_id = 'rewrite-agent'
            AND max_tokens IN (128, 256)
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (5)")

    if current_version < 6:
        # Migration 6: Upgrade timer/scene/automation agents to ActionableAgent
        # Increase max_tokens from 256 to 512
        await db.execute("""
            UPDATE agent_configs
            SET max_tokens = 512
            WHERE agent_id IN ('timer-agent', 'scene-agent', 'automation-agent')
            AND max_tokens = 256
        """)
        # Add timer-agent visibility rules
        await db.execute(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            ("timer-agent", "domain_include", "timer"),
        )
        await db.execute(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            ("timer-agent", "domain_include", "input_datetime"),
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (6)")

    if current_version < 7:
        # Migration 7: Upgrade media-agent to ActionableAgent
        # Increase max_tokens from 256 to 512
        await db.execute("""
            UPDATE agent_configs
            SET max_tokens = 512
            WHERE agent_id = 'media-agent'
            AND max_tokens = 256
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (7)")

    if current_version < 8:
        # Migration 8: Timer agent extensions -- add visibility for notification, media_player, calendar
        new_rules = [
            ("timer-agent", "domain_include", "persistent_notification"),
            ("timer-agent", "domain_include", "media_player"),
            ("timer-agent", "domain_include", "calendar"),
        ]
        await db.executemany(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            new_rules,
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (8)")

    if current_version < 9:
        # Migration 9: Add conversation_turns column to trace_summary
        with suppress(Exception):
            await db.execute("ALTER TABLE trace_summary ADD COLUMN conversation_turns TEXT")
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (9)")

    if current_version < 10:
        # Migration 10: Increase max_tokens to prevent response truncation
        await db.execute("""
            UPDATE agent_configs SET max_tokens = 1024
            WHERE max_tokens IN (256, 512)
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (10)")

    if current_version < 11:
        # Migration 11: Add reasoning_effort column to agent_configs
        with suppress(Exception):
            await db.execute("ALTER TABLE agent_configs ADD COLUMN reasoning_effort TEXT")
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (11)")

    if current_version < 12:
        # Migration 12: Send device mappings for send-agent
        await db.execute("""
            CREATE TABLE IF NOT EXISTS send_device_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                device_type TEXT NOT NULL CHECK(device_type IN ('notify', 'tts')),
                ha_service_target TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (12)")

    if current_version < 13:
        # Migration 13: Add end_time column to trace_spans
        with suppress(Exception):
            await db.execute("ALTER TABLE trace_spans ADD COLUMN end_time TEXT")
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (13)")

    if current_version < 14:
        # Migration 14: Ensure device_class_include rules exist for all agents
        dc_rules = [
            # climate-agent sensor filtering
            ("climate-agent", "device_class_include", "temperature"),
            ("climate-agent", "device_class_include", "humidity"),
            ("climate-agent", "device_class_include", "pressure"),
            ("climate-agent", "device_class_include", "dew_point"),
            ("climate-agent", "device_class_include", "atmospheric_pressure"),
            ("climate-agent", "device_class_include", "moisture"),
            ("climate-agent", "device_class_include", "precipitation_intensity"),
            ("climate-agent", "device_class_include", "wind_speed"),
            ("climate-agent", "device_class_include", "wind_direction"),
            # security-agent sensor filtering
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
            # light-agent sensor filtering
            ("light-agent", "device_class_include", "illuminance"),
        ]
        await db.executemany(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            dc_rules,
        )
        # Also ensure domain_include sensor rules exist for agents that need device_class filtering
        sensor_domain_rules = [
            ("climate-agent", "domain_include", "sensor"),
            ("security-agent", "domain_include", "sensor"),
            ("security-agent", "domain_include", "binary_sensor"),
            ("light-agent", "domain_include", "sensor"),
        ]
        await db.executemany(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            sensor_domain_rules,
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (14)")

    if current_version < 15:
        # Migration 15: Add weather domain visibility for climate-agent
        await db.execute(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            ("climate-agent", "domain_include", "weather"),
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (15)")

    if current_version < 16:
        # Migration 16 (0.18.6, FLOW-CTX-1): record the originating
        # satellite + area on every trace_summary so the dashboard
        # can show "Kitchen Satellite / Kitchen" next to each
        # conversation instead of an opaque device_id UUID.
        for column in ("device_id", "area_id", "device_name", "area_name"):
            with suppress(Exception):
                await db.execute(f"ALTER TABLE trace_summary ADD COLUMN {column} TEXT")
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (16)")

    if current_version < 17:
        # Migration 17 (0.22.0): lower default entity_matching.confidence_threshold
        # from 0.75 to 0.60. Idempotent: only updates rows still on the old default,
        # preserving any admin customizations.
        await db.execute(
            """
            UPDATE settings
            SET value = '0.60'
            WHERE key = 'entity_matching.confidence_threshold'
              AND value = '0.75'
            """
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (17)")

    if current_version < 18:
        # Migration 18 (0.23.0): create the on-demand query_synonym_cache
        # table used by the language-agnostic entity matcher to cache
        # LLM-produced expansions of cold query tokens. The table is
        # created EMPTY -- there is intentionally NO seed data for any
        # language. Entries are added organically when matcher misses
        # trigger a single LLM expansion call.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS query_synonym_cache (
                token TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                expansions TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (token, language)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_query_synonym_cache_last_used ON query_synonym_cache(last_used_at)"
        )
        # Switch the embedding default to a multilingual model on
        # databases that still carry the old English-only default.
        # Admin overrides are preserved.
        await db.execute(
            """
            UPDATE settings
            SET value = 'intfloat/multilingual-e5-small'
            WHERE key = 'embedding.local_model'
              AND value = 'all-MiniLM-L6-v2'
            """
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (18)")

    if current_version < 19:
        # Migration 19 (0.26.0): create the AgentHub-managed timer
        # scheduler table and remove the obsolete timer-agent visibility
        # seed for the HA timer.* domain. AgentHub no longer touches HA
        # timer.* helpers; it owns timer state internally.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_timers (
                id TEXT PRIMARY KEY,
                logical_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                fires_at INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL,
                origin_device_id TEXT,
                origin_area TEXT,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                fired_at INTEGER,
                cancelled_at INTEGER
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_timers_state_fires_at ON scheduled_timers(state, fires_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_timers_logical_name ON scheduled_timers(logical_name)"
        )
        await db.execute(
            "DELETE FROM entity_visibility_rules "
            "WHERE agent_id = 'timer-agent' AND rule_type = 'domain_include' AND rule_value = 'timer'"
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (19)")

    if current_version < 20:
        # Migration 20 (0.31.0): align the persisted cache setting
        # descriptions with the current routing-cache/action-cache model
        # without renaming the legacy cache.response.* keys.
        await db.executemany(
            """
            UPDATE settings
            SET description = ?
            WHERE key = ? AND description = ?
            """,
            [
                (
                    "Action cache hit threshold (legacy key name: cache.response.threshold)",
                    "cache.response.threshold",
                    "Response cache hit threshold",
                ),
                (
                    "Action cache partial match threshold (legacy key name: cache.response.partial_threshold)",
                    "cache.response.partial_threshold",
                    "Response cache partial match threshold",
                ),
                (
                    "Action cache max entries (LRU eviction; legacy key name: cache.response.max_entries)",
                    "cache.response.max_entries",
                    "Response cache max entries (LRU eviction)",
                ),
                (
                    "Enable action cache storage (legacy key name: cache.response.enabled)",
                    "cache.response.enabled",
                    "Enable response cache storage",
                ),
            ],
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (20)")

    if current_version < 21:
        # Migration 21 (1.0.0): persist wake-briefing alarm flags explicitly.
        with suppress(Exception):
            await db.execute("ALTER TABLE scheduled_timers ADD COLUMN briefing INTEGER NOT NULL DEFAULT 0")
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (21)")

    if current_version < 22:
        # Migration 22 (1.2.0): MCP transport "http" was removed.
        # Rewrite any legacy rows so the registry only sees stdio/sse.
        await db.execute("UPDATE mcp_servers SET transport = 'sse' WHERE transport = 'http'")
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (22)")
