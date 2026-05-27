"""SQLite table definitions and initialization.

Manages the SQLite database schema for all structured data: configuration,
secrets, user accounts, conversation history, and analytics.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.config import settings
from app.defaults import CACHE_DEFAULTS, DEFAULT_LOCAL_EMBEDDING_MODEL

logger = logging.getLogger(__name__)

_write_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

_DB_WRITE_MAX_RETRIES = 3
_DB_WRITE_BASE_DELAY = 0.5


async def _db_path() -> Path:
    """Resolve the SQLite database path and ensure the parent directory exists."""
    p = Path(settings.sqlite_db_path)
    # Off-load directory creation to a thread to avoid blocking the event loop.
    await asyncio.to_thread(p.parent.mkdir, parents=True, exist_ok=True)
    return p


async def _open_write_connection() -> aiosqlite.Connection:
    """Open a fresh write connection with retry on OperationalError."""
    for attempt in range(1, _DB_WRITE_MAX_RETRIES + 1):
        try:
            conn = await aiosqlite.connect(str(await _db_path()), isolation_level=None)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except aiosqlite.OperationalError:
            logger.warning("DB write connection failed (attempt %d/%d)", attempt, _DB_WRITE_MAX_RETRIES, exc_info=True)
            if attempt < _DB_WRITE_MAX_RETRIES:
                await asyncio.sleep(_DB_WRITE_BASE_DELAY * (2 ** (attempt - 1)))
    raise aiosqlite.OperationalError("Failed to open write connection after all retries")


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Check whether a column exists in a given table."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return any(row[1] == column for row in rows)


async def _get_or_create_write_connection() -> aiosqlite.Connection:
    """Get or create the shared write connection."""
    global _write_conn
    if _write_conn is not None:
        try:
            await _write_conn.execute("SELECT 1")
        except aiosqlite.OperationalError:
            logger.warning("DB write connection stale, recreating")
            _write_conn = None
    if _write_conn is None:
        _write_conn = await _open_write_connection()
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
    db = await aiosqlite.connect(str(await _db_path()))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def get_db_write() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager returning the write database connection.

    Acquires _write_lock to serialize writes and begins an explicit
    transaction so that every block inside the context is atomic.
    """
    async with _write_lock:
        db = await _get_or_create_write_connection()
        await db.execute("BEGIN")
        try:
            yield db
        except BaseException:
            # BaseException ensures rollback on KeyboardInterrupt / SystemExit
            await db.rollback()
            raise
        else:
            await db.commit()


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
            voice_followup INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS send_device_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            device_type TEXT NOT NULL CHECK(device_type IN ('notify', 'tts')),
            ha_service_target TEXT NOT NULL,
            person_entity_id TEXT,
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

    await db.execute("""
        CREATE TABLE IF NOT EXISTS calendar_user_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            phonetic_key TEXT,
            calendar_entity_ids_json TEXT NOT NULL DEFAULT '[]',
            reminder_offsets_json TEXT NOT NULL DEFAULT '[1440, 60, 15]',
            is_default_user INTEGER NOT NULL DEFAULT 0,
            person_entity_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS calendar_entity_settings (
            entity_id TEXT PRIMARY KEY,
            friendly_name TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            is_universal INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS calendar_reminder_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid TEXT NOT NULL,
            calendar_entity_id TEXT NOT NULL,
            user_mapping_id INTEGER NOT NULL,
            offset_minutes INTEGER NOT NULL,
            fired_at INTEGER NOT NULL,
            UNIQUE(event_uid, calendar_entity_id, user_mapping_id, offset_minutes)
        )
    """)


async def _create_indexes(db: aiosqlite.Connection) -> None:
    """Create indexes for query performance."""
    await db.execute("CREATE INDEX IF NOT EXISTS idx_settings_category ON settings(category)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_agent_configs_enabled ON agent_configs(enabled)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_custom_agents_enabled ON custom_agents(enabled)")
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
        try:
            await db.execute("ALTER TABLE trace_summary ADD COLUMN conversation_turns TEXT")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                logger.error("Migration failed adding column conversation_turns: %s", e)
                raise
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
        try:
            await db.execute("ALTER TABLE agent_configs ADD COLUMN reasoning_effort TEXT")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                logger.error("Migration failed adding column reasoning_effort: %s", e)
                raise
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
        try:
            await db.execute("ALTER TABLE trace_spans ADD COLUMN end_time TEXT")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                logger.error("Migration failed adding column end_time: %s", e)
                raise
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
        cursor = await db.execute("PRAGMA table_info(trace_summary)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column in ("device_id", "area_id", "device_name", "area_name"):
            if column not in existing_columns:
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
        try:
            await db.execute("ALTER TABLE scheduled_timers ADD COLUMN briefing INTEGER NOT NULL DEFAULT 0")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                logger.error("Migration failed adding column briefing: %s", e)
                raise
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (21)")

    if current_version < 22:
        # Migration 22 (1.2.0): MCP transport "http" was removed.
        # Rewrite any legacy rows so the registry only sees stdio/sse.
        await db.execute("UPDATE mcp_servers SET transport = 'sse' WHERE transport = 'http'")
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (22)")

    if current_version < 23:
        # Migration 23 (1.3.0): canonicalize cache settings to cache.action.*
        # and cache.routing.semantic_threshold.
        await db.executemany(
            """
            INSERT OR IGNORE INTO settings (key, value, value_type, category, description, updated_at)
            SELECT ?, value, value_type, category, ?, updated_at
            FROM settings
            WHERE key = ?
            """,
            [
                (
                    "cache.routing.semantic_threshold",
                    "Routing cache semantic hit threshold",
                    "cache.routing.threshold",
                ),
                (
                    "cache.action.enabled",
                    "Enable action cache lookup and storage",
                    "cache.response.enabled",
                ),
                (
                    "cache.action.semantic_threshold",
                    "Action cache semantic hit threshold",
                    "cache.response.threshold",
                ),
                (
                    "cache.action.max_entries",
                    "Action cache max entries (LRU eviction)",
                    "cache.response.max_entries",
                ),
            ],
        )
        await db.executemany(
            "DELETE FROM settings WHERE key = ?",
            [
                ("cache.routing.threshold",),
                ("cache.response.enabled",),
                ("cache.response.threshold",),
                ("cache.response.partial_threshold",),
                ("cache.response.max_entries",),
            ],
        )
        await db.executemany(
            "INSERT OR IGNORE INTO settings (key, value, value_type, category, description, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [
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
            ],
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (23)")

    if current_version < 24:
        # Migration 24: Seed cache LRU policy settings (early-eviction trigger,
        # eviction sweep interval). Both lift previously hardcoded constants in
        # _base_cache.py into runtime settings.
        await db.executemany(
            "INSERT OR IGNORE INTO settings (key, value, value_type, category, description, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [
                (
                    "cache.lru.trigger_fraction",
                    str(CACHE_DEFAULTS["cache.lru.trigger_fraction"]),
                    "float",
                    "cache",
                    "Fraction of max_entries that triggers early LRU eviction",
                ),
                (
                    "cache.lru.eviction_interval",
                    str(CACHE_DEFAULTS["cache.lru.eviction_interval"]),
                    "int",
                    "cache",
                    "Number of store operations between LRU eviction sweeps",
                ),
            ],
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (24)")

    if current_version < 25:
        # Migration 25: Calendar user mappings and reminder state tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS calendar_user_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                phonetic_key TEXT,
                calendar_entity_ids_json TEXT NOT NULL DEFAULT '[]',
                reminder_offsets_json TEXT NOT NULL DEFAULT '[1440, 60, 15]',
                is_default_user INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS calendar_reminder_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_uid TEXT NOT NULL,
                calendar_entity_id TEXT NOT NULL,
                user_mapping_id INTEGER NOT NULL,
                offset_minutes INTEGER NOT NULL,
                fired_at INTEGER NOT NULL,
                UNIQUE(event_uid, calendar_entity_id, user_mapping_id, offset_minutes)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_user_mappings_normalized ON calendar_user_mappings(normalized_name)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_user_mappings_phonetic ON calendar_user_mappings(phonetic_key)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_reminder_state_event ON calendar_reminder_state(event_uid, calendar_entity_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_reminder_state_user ON calendar_reminder_state(user_mapping_id)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO agent_configs (agent_id, enabled, model, timeout, max_iterations, temperature, max_tokens, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("calendar-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Calendar event management"),
        )
        await db.execute(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            ("calendar-agent", "domain_include", "calendar"),
        )
        await db.executemany(
            "INSERT OR IGNORE INTO settings (key, value, value_type, category, description, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [
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
            ],
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (25)")

    if current_version < 26:
        # Migration 26: Add person_entity_id to send_device_mappings and calendar_user_mappings
        if not await _column_exists(db, "send_device_mappings", "person_entity_id"):
            await db.execute("ALTER TABLE send_device_mappings ADD COLUMN person_entity_id TEXT")
        if not await _column_exists(db, "calendar_user_mappings", "person_entity_id"):
            await db.execute("ALTER TABLE calendar_user_mappings ADD COLUMN person_entity_id TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_send_device_mappings_person ON send_device_mappings(person_entity_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_user_mappings_person ON calendar_user_mappings(person_entity_id)"
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (26)")

    if current_version < 27:
        # Migration 27: Add is_universal flag to calendar_entity_settings
        if not await _column_exists(db, "calendar_entity_settings", "is_universal"):
            await db.execute("ALTER TABLE calendar_entity_settings ADD COLUMN is_universal INTEGER NOT NULL DEFAULT 0")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_entity_settings_universal ON calendar_entity_settings(is_universal)"
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (27)")

    if current_version < 28:
        # Migration 28: Add lists-agent config and visibility rules
        await db.execute(
            "INSERT OR IGNORE INTO agent_configs (agent_id, enabled, model, timeout, max_iterations, temperature, max_tokens, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("lists-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Todo and shopping list management"),
        )
        await db.execute(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            ("lists-agent", "domain_include", "todo"),
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (28)")

    if current_version < 29:
        # Migration 29: Remove dead timer-agent visibility rules for input_datetime and calendar
        await db.execute(
            "DELETE FROM entity_visibility_rules WHERE agent_id = ? AND rule_type = ? AND rule_value IN (?, ?)",
            ("timer-agent", "domain_include", "input_datetime", "calendar"),
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (29)")

    if current_version < 30:
        # Migration 30: Add voice_followup column to trace_summary
        try:
            await db.execute("ALTER TABLE trace_summary ADD COLUMN voice_followup INTEGER DEFAULT 0")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                logger.error("Migration failed adding column voice_followup: %s", e)
                raise
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (30)")

    if current_version < 31:
        # Migration 31: Add visibility rules for new cover-agent, vacuum-agent,
        # and extend climate-agent with fan and humidifier domains.
        new_rules = [
            ("cover-agent", "domain_include", "cover"),
            ("vacuum-agent", "domain_include", "vacuum"),
            ("climate-agent", "domain_include", "fan"),
            ("climate-agent", "domain_include", "humidifier"),
        ]
        await db.executemany(
            "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
            new_rules,
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (31)")

    if current_version < 32:
        # Migration 32: Add agent configs for cover-agent and vacuum-agent,
        # and update climate-agent description to include fans and humidifiers.
        new_configs = [
            ("cover-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Cover and blind control"),
            ("vacuum-agent", 0, "openrouter/openai/gpt-4o-mini", 5, 3, 0.2, 1024, "Robot vacuum control"),
        ]
        await db.executemany(
            "INSERT OR IGNORE INTO agent_configs (agent_id, enabled, model, timeout, max_iterations, temperature, max_tokens, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            new_configs,
        )
        await db.execute(
            "UPDATE agent_configs SET description = ? WHERE agent_id = ?",
            ("Climate, HVAC, fans, and humidifiers", "climate-agent"),
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (32)")

    if current_version < 33:
        # Migration 33: Seed custom_openai_provider settings for custom OpenAI-compatible providers.
        await db.executemany(
            "INSERT OR IGNORE INTO settings (key, value, value_type, category, description, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [
                (
                    "custom_openai_provider.name",
                    "",
                    "string",
                    "llm",
                    "Custom OpenAI provider name",
                ),
                (
                    "custom_openai_provider.base_url",
                    "",
                    "string",
                    "llm",
                    "Custom OpenAI provider base URL",
                ),
                (
                    "custom_openai_provider.headers",
                    "{}",
                    "json",
                    "llm",
                    "Custom OpenAI provider extra headers",
                ),
            ],
        )
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (33)")

    if current_version < 34:
        # Migration 34: Bump filler-agent max_tokens from 50 to 1024.
        # The old default (50) caused finish_reason=length and empty responses.
        await db.execute("""
            UPDATE agent_configs SET max_tokens = 1024
            WHERE agent_id = 'filler-agent' AND max_tokens < 1024
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (34)")
