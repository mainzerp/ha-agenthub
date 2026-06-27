"""Index creation for the SQLite schema."""

import aiosqlite


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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_trace_summary_conversation_id ON trace_summary(conversation_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_timers_logical_name ON scheduled_timers(logical_name)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_cache_validator_runs_started_at ON cache_validator_runs(started_at)"
    )
