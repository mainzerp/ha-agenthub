"""Incremental schema migrations.

The historical linear ``if current_version < N:`` ladder has been
refactored into an ordered registry of ``(version, migration_callable)``
tuples. The runtime behaviour is identical to the previous ladder: the
current schema version is read once, then every migration whose version
is greater than the current version is applied in ascending order. Each
migration callable is responsible for recording its own version marker
via ``INSERT OR IGNORE INTO schema_version (version) VALUES (N)``.
"""

import logging
from collections.abc import Awaitable, Callable

import aiosqlite

from app.db.schema import _column_exists
from app.defaults import CACHE_DEFAULTS

logger = logging.getLogger(__name__)


async def _migrate_to_2(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_3(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_4(db: aiosqlite.Connection) -> None:
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
                    "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                    (agent_id, "domain_include", domain),
                )

    # Migrate legacy rule_types
    await db.execute("UPDATE entity_visibility_rules SET rule_type = 'entity_include' WHERE rule_type = 'entity'")
    await db.execute("UPDATE entity_visibility_rules SET rule_type = 'domain_include' WHERE rule_type = 'domain'")
    await db.execute("UPDATE entity_visibility_rules SET rule_type = 'area_include' WHERE rule_type = 'area'")

    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4)")


async def _migrate_to_5(db: aiosqlite.Connection) -> None:
    # Migration 5: Increase rewrite-agent max_tokens from 128 to 512
    await db.execute("""
        UPDATE agent_configs
        SET max_tokens = 512
        WHERE agent_id = 'rewrite-agent'
        AND max_tokens IN (128, 256)
    """)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (5)")


async def _migrate_to_6(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_7(db: aiosqlite.Connection) -> None:
    # Migration 7: Upgrade media-agent to ActionableAgent
    # Increase max_tokens from 256 to 512
    await db.execute("""
        UPDATE agent_configs
        SET max_tokens = 512
        WHERE agent_id = 'media-agent'
        AND max_tokens = 256
    """)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (7)")


async def _migrate_to_8(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_9(db: aiosqlite.Connection) -> None:
    # Migration 9: Add conversation_turns column to trace_summary
    try:
        await db.execute("ALTER TABLE trace_summary ADD COLUMN conversation_turns TEXT")
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            logger.error("Migration failed adding column conversation_turns: %s", e)
            raise
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (9)")


async def _migrate_to_10(db: aiosqlite.Connection) -> None:
    # Migration 10: Increase max_tokens to prevent response truncation
    await db.execute("""
        UPDATE agent_configs SET max_tokens = 1024
        WHERE max_tokens IN (256, 512)
    """)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (10)")


async def _migrate_to_11(db: aiosqlite.Connection) -> None:
    # Migration 11: Add reasoning_effort column to agent_configs
    try:
        await db.execute("ALTER TABLE agent_configs ADD COLUMN reasoning_effort TEXT")
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            logger.error("Migration failed adding column reasoning_effort: %s", e)
            raise
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (11)")


async def _migrate_to_12(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_13(db: aiosqlite.Connection) -> None:
    # Migration 13: Add end_time column to trace_spans
    try:
        await db.execute("ALTER TABLE trace_spans ADD COLUMN end_time TEXT")
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            logger.error("Migration failed adding column end_time: %s", e)
            raise
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (13)")


async def _migrate_to_14(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_15(db: aiosqlite.Connection) -> None:
    # Migration 15: Add weather domain visibility for climate-agent
    await db.execute(
        "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
        ("climate-agent", "domain_include", "weather"),
    )
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (15)")


async def _migrate_to_16(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_17(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_18(db: aiosqlite.Connection) -> None:
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
    await db.execute("CREATE INDEX IF NOT EXISTS ix_query_synonym_cache_last_used ON query_synonym_cache(last_used_at)")
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


async def _migrate_to_19(db: aiosqlite.Connection) -> None:
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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_timers_logical_name ON scheduled_timers(logical_name)")
    await db.execute(
        "DELETE FROM entity_visibility_rules "
        "WHERE agent_id = 'timer-agent' AND rule_type = 'domain_include' AND rule_value = 'timer'"
    )
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (19)")


async def _migrate_to_20(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_21(db: aiosqlite.Connection) -> None:
    # Migration 21 (1.0.0): persist wake-briefing alarm flags explicitly.
    try:
        await db.execute("ALTER TABLE scheduled_timers ADD COLUMN briefing INTEGER NOT NULL DEFAULT 0")
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            logger.error("Migration failed adding column briefing: %s", e)
            raise
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (21)")


async def _migrate_to_22(db: aiosqlite.Connection) -> None:
    # Migration 22 (1.2.0): MCP transport "http" was removed.
    # Rewrite any legacy rows so the registry only sees stdio/sse.
    await db.execute("UPDATE mcp_servers SET transport = 'sse' WHERE transport = 'http'")
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (22)")


async def _migrate_to_23(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_24(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_25(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_26(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_27(db: aiosqlite.Connection) -> None:
    # Migration 27: Add is_universal flag to calendar_entity_settings
    if not await _column_exists(db, "calendar_entity_settings", "is_universal"):
        await db.execute("ALTER TABLE calendar_entity_settings ADD COLUMN is_universal INTEGER NOT NULL DEFAULT 0")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_entity_settings_universal ON calendar_entity_settings(is_universal)"
    )
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (27)")


async def _migrate_to_28(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_29(db: aiosqlite.Connection) -> None:
    # Migration 29: Remove dead timer-agent visibility rules for input_datetime and calendar
    await db.execute(
        "DELETE FROM entity_visibility_rules WHERE agent_id = ? AND rule_type = ? AND rule_value IN (?, ?)",
        ("timer-agent", "domain_include", "input_datetime", "calendar"),
    )
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (29)")


async def _migrate_to_30(db: aiosqlite.Connection) -> None:
    # Migration 30: Add voice_followup column to trace_summary
    try:
        await db.execute("ALTER TABLE trace_summary ADD COLUMN voice_followup INTEGER DEFAULT 0")
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            logger.error("Migration failed adding column voice_followup: %s", e)
            raise
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (30)")


async def _migrate_to_31(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_32(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_33(db: aiosqlite.Connection) -> None:
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


async def _migrate_to_34(db: aiosqlite.Connection) -> None:
    # Migration 34: Bump filler-agent max_tokens from 50 to 1024.
    # The old default (50) caused finish_reason=length and empty responses.
    await db.execute("""
        UPDATE agent_configs SET max_tokens = 1024
        WHERE agent_id = 'filler-agent' AND max_tokens < 1024
    """)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (34)")


async def _migrate_to_35(db: aiosqlite.Connection) -> None:
    # Migration 35: Persistent cache validator run history.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cache_validator_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned INTEGER NOT NULL DEFAULT 0,
            inconsistent INTEGER NOT NULL DEFAULT 0,
            corrected INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_cache_validator_runs_started_at ON cache_validator_runs(started_at)"
    )
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (35)")


async def _migrate_to_36(db: aiosqlite.Connection) -> None:
    # Migration 36: Add conversation_id index on trace_summary for
    # dashboard per-conversation lookups.
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_summary_conversation_id ON trace_summary(conversation_id)")
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (36)")


async def _migrate_to_37(db: aiosqlite.Connection) -> None:
    # Migration 37: Add per-custom-agent timeout configuration.
    if not await _column_exists(db, "custom_agents", "timeout_sec"):
        await db.execute("ALTER TABLE custom_agents ADD COLUMN timeout_sec INTEGER")
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (37)")


async def _migrate_to_38(db: aiosqlite.Connection) -> None:
    # Migration 38: Add verbatim_terms column to trace_summary for entity extraction display.
    if not await _column_exists(db, "trace_summary", "verbatim_terms"):
        await db.execute("ALTER TABLE trace_summary ADD COLUMN verbatim_terms TEXT")
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (38)")


# Ordered registry of (version, migration_callable). Each migration records
# its own version marker. Applied in ascending order for versions greater
# than the current schema version.
MIGRATIONS: list[tuple[int, Callable[[aiosqlite.Connection], Awaitable[None]]]] = [
    (2, _migrate_to_2),
    (3, _migrate_to_3),
    (4, _migrate_to_4),
    (5, _migrate_to_5),
    (6, _migrate_to_6),
    (7, _migrate_to_7),
    (8, _migrate_to_8),
    (9, _migrate_to_9),
    (10, _migrate_to_10),
    (11, _migrate_to_11),
    (12, _migrate_to_12),
    (13, _migrate_to_13),
    (14, _migrate_to_14),
    (15, _migrate_to_15),
    (16, _migrate_to_16),
    (17, _migrate_to_17),
    (18, _migrate_to_18),
    (19, _migrate_to_19),
    (20, _migrate_to_20),
    (21, _migrate_to_21),
    (22, _migrate_to_22),
    (23, _migrate_to_23),
    (24, _migrate_to_24),
    (25, _migrate_to_25),
    (26, _migrate_to_26),
    (27, _migrate_to_27),
    (28, _migrate_to_28),
    (29, _migrate_to_29),
    (30, _migrate_to_30),
    (31, _migrate_to_31),
    (32, _migrate_to_32),
    (33, _migrate_to_33),
    (34, _migrate_to_34),
    (35, _migrate_to_35),
    (36, _migrate_to_36),
    (37, _migrate_to_37),
    (38, _migrate_to_38),
]


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Run incremental schema migrations based on schema_version."""
    cursor = await db.execute("SELECT MAX(version) FROM schema_version")
    row = await cursor.fetchone()
    current_version = row[0] if row and row[0] else 1

    for version, migrate in MIGRATIONS:
        if version > current_version:
            await migrate(db)
