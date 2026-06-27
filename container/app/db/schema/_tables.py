"""Table creation DDL for the SQLite schema."""

import aiosqlite


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
            timeout_sec INTEGER,
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
            verbatim_terms TEXT,
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
