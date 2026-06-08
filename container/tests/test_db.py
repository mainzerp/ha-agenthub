"""Tests for app.db -- schema creation, seed data, and repository CRUD."""

from __future__ import annotations

import json
import time

import aiosqlite
import pytest

from app.db.repository import (
    AdminAccountRepository,
    AgentConfigRepository,
    AliasRepository,
    CustomAgentRepository,
    EntityVisibilityRepository,
    McpServerRepository,
    ScheduledTimersRepository,
    SecretsRepository,
    SendDeviceMappingRepository,
    SettingsRepository,
    SetupStateRepository,
    TraceSummaryRepository,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


class TestSchemaCreation:
    async def test_all_expected_tables_exist(self, db_repository):
        expected_tables = {
            "schema_version",
            "settings",
            "agent_configs",
            "custom_agents",
            "entity_matching_config",
            "aliases",
            "mcp_servers",
            "secrets",
            "admin_accounts",
            "setup_state",
            "entity_visibility_rules",
            "plugins",
            "conversations",
            "analytics",
            "trace_spans",
            "trace_summary",
            "send_device_mappings",
        }
        async with aiosqlite.connect(str(db_repository)) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            rows = await cursor.fetchall()
            actual_tables = {row[0] for row in rows}
        assert expected_tables.issubset(actual_tables), f"Missing: {expected_tables - actual_tables}"

    async def test_indexes_created(self, db_repository):
        async with aiosqlite.connect(str(db_repository)) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
            rows = await cursor.fetchall()
            index_names = {row[0] for row in rows}
        assert "idx_settings_category" in index_names
        assert "idx_aliases_entity_id" in index_names

    async def test_schema_version_seeded(self, db_repository):
        async with aiosqlite.connect(str(db_repository)) as db:
            cursor = await db.execute("SELECT version FROM schema_version")
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1


class TestScheduledTimersRepository:
    async def test_insert_persists_briefing_flag(self, db_repository):
        now = int(time.time())
        await ScheduledTimersRepository.insert(
            id="sched-briefing",
            logical_name="wake alarm",
            kind="alarm",
            created_at=now,
            fires_at=now + 120,
            duration_seconds=120,
            origin_device_id=None,
            origin_area="bedroom",
            payload_json=json.dumps({"alarm_label": "Wake Alarm"}),
            briefing=True,
        )

        row = await ScheduledTimersRepository.get("sched-briefing")
        assert row is not None
        assert row["briefing"] == 1

    async def test_update_scheduled_timer_renames(self, db_repository):
        now = int(time.time())
        await ScheduledTimersRepository.insert(
            id="sched-upd-rename",
            logical_name="old-name",
            kind="plain",
            created_at=now,
            fires_at=now + 120,
            duration_seconds=120,
            origin_device_id=None,
            origin_area=None,
            payload_json=json.dumps({}),
        )

        updated = await ScheduledTimersRepository.update_scheduled_timer(
            "sched-upd-rename",
            logical_name="new-name",
        )
        assert updated is True

        row = await ScheduledTimersRepository.get("sched-upd-rename")
        assert row is not None
        assert row["logical_name"] == "new-name"

    async def test_update_scheduled_timer_reschedules(self, db_repository):
        now = int(time.time())
        original_fires_at = now + 60
        new_fires_at = now + 3600
        await ScheduledTimersRepository.insert(
            id="sched-upd-fire",
            logical_name="resched-me",
            kind="plain",
            created_at=now,
            fires_at=original_fires_at,
            duration_seconds=60,
            origin_device_id=None,
            origin_area=None,
            payload_json=json.dumps({}),
        )

        updated = await ScheduledTimersRepository.update_scheduled_timer(
            "sched-upd-fire",
            fires_at=new_fires_at,
        )
        assert updated is True

        row = await ScheduledTimersRepository.get("sched-upd-fire")
        assert row is not None
        assert int(row["fires_at"]) == new_fires_at

    async def test_update_scheduled_timer_no_op_on_cancelled(self, db_repository):
        now = int(time.time())
        await ScheduledTimersRepository.insert(
            id="sched-upd-cancelled",
            logical_name="original",
            kind="plain",
            created_at=now,
            fires_at=now + 600,
            duration_seconds=600,
            origin_device_id=None,
            origin_area=None,
            payload_json=json.dumps({}),
        )
        await ScheduledTimersRepository.mark_cancelled("sched-upd-cancelled", now)

        updated = await ScheduledTimersRepository.update_scheduled_timer(
            "sched-upd-cancelled",
            logical_name="should-not-change",
        )
        assert updated is False

        row = await ScheduledTimersRepository.get("sched-upd-cancelled")
        assert row is not None
        assert row["logical_name"] == "original"
        assert row["state"] == "cancelled"

    async def test_update_scheduled_timer_empty_fields_returns_false(self, db_repository):
        now = int(time.time())
        await ScheduledTimersRepository.insert(
            id="sched-upd-empty",
            logical_name="keep-me",
            kind="plain",
            created_at=now,
            fires_at=now + 300,
            duration_seconds=300,
            origin_device_id=None,
            origin_area=None,
            payload_json=json.dumps({}),
        )

        updated = await ScheduledTimersRepository.update_scheduled_timer("sched-upd-empty")
        assert updated is False

        row = await ScheduledTimersRepository.get("sched-upd-empty")
        assert row is not None
        assert row["logical_name"] == "keep-me"


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


class TestSeedData:
    async def test_default_settings_populated(self, db_repository):
        all_settings = await SettingsRepository.get_all()
        keys = {s["key"] for s in all_settings}
        assert "cache.routing.semantic_threshold" in keys
        assert "embedding.provider" in keys
        assert "a2a.default_timeout" in keys

    async def test_current_runtime_defaults_seeded(self, db_repository):
        assert await SettingsRepository.get_value("embedding.local_model") == "intfloat/multilingual-e5-small"
        assert await SettingsRepository.get_value("entity_matching.confidence_threshold") == "0.60"
        assert await SettingsRepository.get_value("general.conversation_context_turns") == "3"

    async def test_default_agent_configs_populated(self, db_repository):
        agents = await AgentConfigRepository.list_all()
        agent_ids = {a["agent_id"] for a in agents}
        assert "orchestrator" in agent_ids
        assert "light-agent" in agent_ids
        assert "general-agent" in agent_ids

    async def test_default_entity_matching_weights(self, db_repository):
        async with aiosqlite.connect(str(db_repository)) as db:
            cursor = await db.execute("SELECT key, value FROM entity_matching_config")
            rows = await cursor.fetchall()
        weights = {row[0]: row[1] for row in rows}
        assert "weight.levenshtein" in weights
        assert "weight.embedding" in weights
        assert float(weights["weight.embedding"]) == 0.30

    async def test_setup_steps_seeded(self, db_repository):
        steps = await SetupStateRepository.get_all_steps()
        step_names = {s["step"] for s in steps}
        assert "admin_password" in step_names
        assert "review_complete" in step_names
        assert len(steps) == 5

    async def test_default_visibility_rules_seeded(self, db_repository):
        rules = await EntityVisibilityRepository.get_rules("light-agent")
        rule_map = {r["rule_value"]: r["rule_type"] for r in rules}
        assert rule_map.get("light") == "domain_include"
        assert rule_map.get("switch") == "domain_include"

        rules = await EntityVisibilityRepository.get_rules("music-agent")
        rule_map = {r["rule_value"]: r["rule_type"] for r in rules}
        assert rule_map.get("media_player") == "domain_include"


# ---------------------------------------------------------------------------
# Repository CRUD -- settings
# ---------------------------------------------------------------------------


class TestSettingsRepository:
    async def test_get_existing_setting(self, db_repository):
        result = await SettingsRepository.get("cache.routing.semantic_threshold")
        assert result is not None
        assert result["value"] == "0.92"

    async def test_get_value_existing(self, db_repository):
        val = await SettingsRepository.get_value("cache.routing.semantic_threshold")
        assert val == "0.92"

    async def test_get_value_missing_returns_default(self, db_repository):
        val = await SettingsRepository.get_value("nonexistent.key", "fallback")
        assert val == "fallback"

    async def test_get_missing_key_returns_none(self, db_repository):
        result = await SettingsRepository.get("does.not.exist")
        assert result is None

    async def test_set_new_setting(self, db_repository):
        await SettingsRepository.set("test.key", "test_value", category="test")
        val = await SettingsRepository.get_value("test.key")
        assert val == "test_value"

    async def test_set_overwrites_existing(self, db_repository):
        await SettingsRepository.set("cache.routing.semantic_threshold", "0.85")
        val = await SettingsRepository.get_value("cache.routing.semantic_threshold")
        assert val == "0.85"

    async def test_get_by_category(self, db_repository):
        results = await SettingsRepository.get_by_category("cache")
        assert len(results) > 0
        assert all(r["key"].startswith("cache.") for r in results)

    async def test_get_all_returns_many(self, db_repository):
        results = await SettingsRepository.get_all()
        assert len(results) > 10

    async def test_set_preserves_value_type_on_conflict(self, db_repository):
        """ON CONFLICT update should preserve existing value_type."""
        before = await SettingsRepository.get("cache.routing.semantic_threshold")
        assert before is not None
        assert before["value_type"] == "float"

        await SettingsRepository.set("cache.routing.semantic_threshold", "0.80")
        after = await SettingsRepository.get("cache.routing.semantic_threshold")
        assert after["value"] == "0.80"
        assert after["value_type"] == "float"

    async def test_set_preserves_category_on_conflict(self, db_repository):
        """ON CONFLICT update should preserve existing category."""
        before = await SettingsRepository.get("cache.routing.semantic_threshold")
        assert before["category"] == "cache"

        await SettingsRepository.set("cache.routing.semantic_threshold", "0.75")
        after = await SettingsRepository.get("cache.routing.semantic_threshold")
        assert after["category"] == "cache"


# ---------------------------------------------------------------------------
# Repository CRUD -- agent_configs
# ---------------------------------------------------------------------------


class TestAgentConfigRepository:
    async def test_get_existing(self, db_repository):
        cfg = await AgentConfigRepository.get("light-agent")
        assert cfg is not None
        assert cfg["agent_id"] == "light-agent"
        assert cfg["enabled"] == 1

    async def test_get_missing_returns_none(self, db_repository):
        cfg = await AgentConfigRepository.get("nonexistent-agent")
        assert cfg is None

    async def test_list_all(self, db_repository):
        agents = await AgentConfigRepository.list_all()
        assert len(agents) >= 11

    async def test_list_enabled(self, db_repository):
        enabled = await AgentConfigRepository.list_enabled()
        for agent in enabled:
            assert agent["enabled"] == 1

    async def test_upsert_update_existing(self, db_repository):
        await AgentConfigRepository.upsert("light-agent", enabled=0)
        cfg = await AgentConfigRepository.get("light-agent")
        assert cfg["enabled"] == 0

    async def test_upsert_create_new(self, db_repository):
        await AgentConfigRepository.upsert("new-test-agent", enabled=1, description="Test")
        cfg = await AgentConfigRepository.get("new-test-agent")
        assert cfg is not None
        assert cfg["description"] == "Test"


# ---------------------------------------------------------------------------
# Repository CRUD -- custom_agents
# ---------------------------------------------------------------------------


class TestCustomAgentRepository:
    async def test_create_and_get(self, db_repository):
        await CustomAgentRepository.create(
            "test-custom",
            system_prompt="You are a test agent",
            description="Test custom agent",
            mcp_tools=["tool1", "tool2"],
            intent_patterns=["pattern_a"],
        )
        agent = await CustomAgentRepository.get("test-custom")
        assert agent is not None
        assert agent["system_prompt"] == "You are a test agent"
        assert agent["mcp_tools"] == ["tool1", "tool2"]
        assert agent["intent_patterns"] == ["pattern_a"]

    async def test_list_all(self, db_repository):
        await CustomAgentRepository.create("ca-1", system_prompt="p1")
        await CustomAgentRepository.create("ca-2", system_prompt="p2")
        agents = await CustomAgentRepository.list_all()
        names = {a["name"] for a in agents}
        assert "ca-1" in names
        assert "ca-2" in names

    async def test_list_enabled(self, db_repository):
        await CustomAgentRepository.create("ca-en", system_prompt="p", enabled=1)
        await CustomAgentRepository.create("ca-dis", system_prompt="p", enabled=0)
        enabled = await CustomAgentRepository.list_enabled()
        names = {a["name"] for a in enabled}
        assert "ca-en" in names
        assert "ca-dis" not in names

    async def test_update(self, db_repository):
        await CustomAgentRepository.create("ca-upd", system_prompt="old")
        await CustomAgentRepository.update("ca-upd", system_prompt="new", description="updated")
        agent = await CustomAgentRepository.get("ca-upd")
        assert agent["system_prompt"] == "new"
        assert agent["description"] == "updated"

    async def test_delete(self, db_repository):
        await CustomAgentRepository.create("ca-del", system_prompt="temp")
        await CustomAgentRepository.delete("ca-del")
        agent = await CustomAgentRepository.get("ca-del")
        assert agent is None

    async def test_json_fields_serialize_correctly(self, db_repository):
        await CustomAgentRepository.create(
            "ca-json",
            system_prompt="p",
            entity_visibility={"domains": ["light"]},
        )
        agent = await CustomAgentRepository.get("ca-json")
        assert isinstance(agent["entity_visibility"], dict)
        assert agent["entity_visibility"]["domains"] == ["light"]


# ---------------------------------------------------------------------------
# Repository CRUD -- aliases
# ---------------------------------------------------------------------------


class TestAliasRepository:
    async def test_set_and_get(self, db_repository):
        await AliasRepository.set("nightstand lamp", "light.bedroom_nightstand")
        result = await AliasRepository.get("nightstand lamp")
        assert result == "light.bedroom_nightstand"

    async def test_get_missing_returns_none(self, db_repository):
        result = await AliasRepository.get("nonexistent_alias")
        assert result is None

    async def test_delete(self, db_repository):
        await AliasRepository.set("temp_alias", "light.temp")
        await AliasRepository.delete("temp_alias")
        result = await AliasRepository.get("temp_alias")
        assert result is None

    async def test_list_all(self, db_repository):
        await AliasRepository.set("alias_a", "light.a")
        await AliasRepository.set("alias_b", "light.b")
        all_aliases = await AliasRepository.list_all()
        alias_keys = {a["alias"] for a in all_aliases}
        assert "alias_a" in alias_keys
        assert "alias_b" in alias_keys

    async def test_upsert_overwrite(self, db_repository):
        await AliasRepository.set("dup", "light.old")
        await AliasRepository.set("dup", "light.new")
        result = await AliasRepository.get("dup")
        assert result == "light.new"


# ---------------------------------------------------------------------------
# Repository CRUD -- mcp_servers
# ---------------------------------------------------------------------------


class TestMcpServerRepository:
    async def test_create_and_get(self, db_repository):
        await McpServerRepository.create("test-mcp", "stdio", "python mcp_server.py")
        server = await McpServerRepository.get("test-mcp")
        assert server is not None
        assert server["transport"] == "stdio"

    async def test_create_with_env_vars(self, db_repository):
        await McpServerRepository.create(
            "mcp-env",
            "http",
            "http://localhost:8000",
            env_vars={"API_KEY": "secret123"},
        )
        server = await McpServerRepository.get("mcp-env")
        assert server["env_vars"] == {"API_KEY": "secret123"}

    async def test_delete(self, db_repository):
        await McpServerRepository.create("mcp-del", "stdio", "cmd")
        await McpServerRepository.delete("mcp-del")
        server = await McpServerRepository.get("mcp-del")
        assert server is None

    async def test_list_all(self, db_repository):
        await McpServerRepository.create("mcp-a", "stdio", "a")
        await McpServerRepository.create("mcp-b", "http", "b")
        servers = await McpServerRepository.list_all()
        names = {s["name"] for s in servers}
        assert "mcp-a" in names
        assert "mcp-b" in names

    async def test_list_enabled(self, db_repository):
        await McpServerRepository.create("mcp-on", "stdio", "cmd")
        servers = await McpServerRepository.list_enabled()
        names = {s["name"] for s in servers}
        assert "mcp-on" in names

    async def test_upsert_roundtrips_env_vars_as_dict(self, db_repository):
        """Q-7 regression: ``upsert`` serializes env_vars to JSON on write
        and ``get`` deserializes it back to a ``dict``."""
        env = {"API_KEY": "secret123", "REGION": "eu-west-1"}
        await McpServerRepository.upsert(
            "mcp-upsert",
            "http",
            "http://localhost:8000",
            env_vars=env,
            timeout=45,
        )
        server = await McpServerRepository.get("mcp-upsert")
        assert server is not None
        assert isinstance(server["env_vars"], dict)
        assert server["env_vars"] == env
        assert server["timeout"] == 45

        # Second upsert updates the row and round-trips the new env dict.
        new_env = {"API_KEY": "rotated"}
        await McpServerRepository.upsert(
            "mcp-upsert",
            "http",
            "http://localhost:8000",
            env_vars=new_env,
            timeout=60,
        )
        server = await McpServerRepository.get("mcp-upsert")
        assert isinstance(server["env_vars"], dict)
        assert server["env_vars"] == new_env
        assert server["timeout"] == 60


# ---------------------------------------------------------------------------
# Repository CRUD -- secrets
# ---------------------------------------------------------------------------


class TestSecretsRepository:
    async def test_store_and_retrieve(self, db_repository):
        encrypted = b"encrypted_secret_data"
        await SecretsRepository.set("ha_token", encrypted)
        result = await SecretsRepository.get("ha_token")
        assert result == encrypted

    async def test_stored_value_is_bytes(self, db_repository):
        await SecretsRepository.set("test_secret", b"\x00\x01\x02")
        result = await SecretsRepository.get("test_secret")
        assert isinstance(result, bytes)

    async def test_get_missing_returns_none(self, db_repository):
        result = await SecretsRepository.get("not_a_key")
        assert result is None


# ---------------------------------------------------------------------------
# Repository CRUD -- trace_summary
# ---------------------------------------------------------------------------


class TestTraceSummaryRepository:
    async def test_create_and_get(self, db_repository):
        await TraceSummaryRepository.create(
            {
                "trace_id": "trace-001",
                "conversation_id": "conv-001",
                "user_input": "Turn on the kitchen light",
                "final_response": "Done, the kitchen light is on.",
                "agents": ["orchestrator", "light-agent"],
                "total_duration_ms": 345.6,
                "label": None,
                "source": "ha",
                "routing_agent": "light-agent",
                "routing_confidence": 0.95,
                "routing_duration_ms": 120.0,
                "routing_reasoning": None,
                "agent_instructions": {"light-agent": "Turn on the kitchen light"},
            }
        )
        result = await TraceSummaryRepository.get("trace-001")
        assert result is not None
        assert result["trace_id"] == "trace-001"
        assert result["user_input"] == "Turn on the kitchen light"
        assert result["routing_agent"] == "light-agent"
        assert result["routing_confidence"] == 0.95
        assert isinstance(result["agents"], list)
        assert "light-agent" in result["agents"]
        assert isinstance(result["agent_instructions"], dict)
        assert result["agent_instructions"]["light-agent"] == "Turn on the kitchen light"

    async def test_list_filtered(self, db_repository):
        await TraceSummaryRepository.create(
            {
                "trace_id": "trace-f1",
                "user_input": "Play some jazz music",
                "routing_agent": "music-agent",
                "agents": ["music-agent"],
                "source": "chat",
            }
        )
        await TraceSummaryRepository.create(
            {
                "trace_id": "trace-f2",
                "user_input": "Turn off the bedroom light",
                "routing_agent": "light-agent",
                "agents": ["light-agent"],
                "source": "ha",
            }
        )
        # No filter
        all_rows = await TraceSummaryRepository.list_filtered()
        assert len(all_rows) >= 2
        # Filter by agent
        agent_rows = await TraceSummaryRepository.list_filtered(agent="music-agent")
        assert all(r["routing_agent"] == "music-agent" for r in agent_rows)
        # Filter by search
        search_rows = await TraceSummaryRepository.list_filtered(search="jazz")
        assert len(search_rows) >= 1

    async def test_update_label(self, db_repository):
        await TraceSummaryRepository.create(
            {
                "trace_id": "trace-lbl",
                "user_input": "Test label",
                "routing_agent": "general-agent",
                "agents": [],
            }
        )
        await TraceSummaryRepository.update_label("trace-lbl", "important")
        result = await TraceSummaryRepository.get("trace-lbl")
        assert result["label"] == "important"

    async def test_list_labels(self, db_repository):
        await TraceSummaryRepository.create(
            {
                "trace_id": "trace-la",
                "user_input": "a",
                "label": "bug",
                "agents": [],
            }
        )
        await TraceSummaryRepository.create(
            {
                "trace_id": "trace-lb",
                "user_input": "b",
                "label": "slow",
                "agents": [],
            }
        )
        labels = await TraceSummaryRepository.list_labels()
        assert "bug" in labels
        assert "slow" in labels

    async def test_delete(self, db_repository):
        await SecretsRepository.set("del_key", b"data")
        await SecretsRepository.delete("del_key")
        result = await SecretsRepository.get("del_key")
        assert result is None

    async def test_list_keys(self, db_repository):
        await SecretsRepository.set("k1", b"v1")
        await SecretsRepository.set("k2", b"v2")
        keys = await SecretsRepository.list_keys()
        assert "k1" in keys
        assert "k2" in keys


# ---------------------------------------------------------------------------
# Repository CRUD -- admin_accounts
# ---------------------------------------------------------------------------


class TestAdminAccountRepository:
    async def test_create_and_get(self, db_repository):
        await AdminAccountRepository.create("admin", "$2b$12$fakebcrypthash")
        account = await AdminAccountRepository.get("admin")
        assert account is not None
        assert account["username"] == "admin"
        assert account["password_hash"] == "$2b$12$fakebcrypthash"

    async def test_get_missing_returns_none(self, db_repository):
        account = await AdminAccountRepository.get("nobody")
        assert account is None

    async def test_password_hash_stored(self, db_repository):
        await AdminAccountRepository.create("user1", "$2b$12$somehash")
        account = await AdminAccountRepository.get("user1")
        assert account["password_hash"].startswith("$2b$12$")

    async def test_update_last_login(self, db_repository):
        await AdminAccountRepository.create("loginuser", "$2b$12$hash")
        await AdminAccountRepository.update_last_login("loginuser")
        account = await AdminAccountRepository.get("loginuser")
        assert account["last_login"] is not None

    async def test_list_all(self, db_repository):
        await AdminAccountRepository.create("u1", "$2b$12$h1")
        await AdminAccountRepository.create("u2", "$2b$12$h2")
        accounts = await AdminAccountRepository.list_all()
        usernames = {a["username"] for a in accounts}
        assert "u1" in usernames
        assert "u2" in usernames

    async def test_duplicate_username_ignored_by_default(self, db_repository):
        """Default ``INSERT OR IGNORE`` keeps the original password (defense-in-depth)."""
        await AdminAccountRepository.create("dupuser", "$2b$12$h")
        await AdminAccountRepository.create("dupuser", "$2b$12$h2")
        account = await AdminAccountRepository.get("dupuser")
        assert account is not None
        assert account["password_hash"] == "$2b$12$h"

    async def test_duplicate_username_replaces_with_force(self, db_repository):
        """``force_overwrite=True`` (setup bootstrap) replaces the existing row."""
        await AdminAccountRepository.create("dupuser", "$2b$12$h")
        await AdminAccountRepository.create("dupuser", "$2b$12$h2", force_overwrite=True)
        account = await AdminAccountRepository.get("dupuser")
        assert account is not None
        assert account["password_hash"] == "$2b$12$h2"


# ---------------------------------------------------------------------------
# Repository CRUD -- setup_state
# ---------------------------------------------------------------------------


class TestSetupStateRepository:
    async def test_get_step(self, db_repository):
        step = await SetupStateRepository.get_step("admin_password")
        assert step is not None
        assert step["completed"] == 0

    async def test_set_step_completed(self, db_repository):
        await SetupStateRepository.set_step_completed("admin_password")
        step = await SetupStateRepository.get_step("admin_password")
        assert step["completed"] == 1
        assert step["completed_at"] is not None

    async def test_is_complete_initially_false(self, db_repository):
        result = await SetupStateRepository.is_complete()
        assert result is False

    async def test_is_complete_after_all_completed(self, db_repository):
        steps = await SetupStateRepository.get_all_steps()
        for s in steps:
            await SetupStateRepository.set_step_completed(s["step"])
        result = await SetupStateRepository.is_complete()
        assert result is True

    async def test_get_all_steps(self, db_repository):
        steps = await SetupStateRepository.get_all_steps()
        assert len(steps) == 5


# ---------------------------------------------------------------------------
# Schema migration v2 -- temperature defaults
# ---------------------------------------------------------------------------


class TestMigrationV2:
    async def test_migration_v2_lowers_agent_temperatures(self, db_repository):
        """Migration v2 should lower temperatures for action agents from 0.7 to 0.2/0.5."""
        from app.db.schema import _run_migrations

        # Simulate pre-migration state: set agents to old 0.7 default
        old_temp_agents = [
            "light-agent",
            "music-agent",
            "timer-agent",
            "climate-agent",
            "media-agent",
            "scene-agent",
            "automation-agent",
            "security-agent",
            "general-agent",
        ]
        for aid in old_temp_agents:
            await AgentConfigRepository.upsert(aid, temperature=0.7)

        # Remove migration v2 marker so migration runs
        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM schema_version WHERE version = 2")
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        # Verify temperatures updated
        for aid in [
            "light-agent",
            "music-agent",
            "timer-agent",
            "climate-agent",
            "media-agent",
            "scene-agent",
            "automation-agent",
            "security-agent",
        ]:
            cfg = await AgentConfigRepository.get(aid)
            assert cfg["temperature"] == 0.2, f"{aid} should be 0.2"
        general = await AgentConfigRepository.get("general-agent")
        assert general["temperature"] == 0.5
        # Unchanged agents
        orchestrator = await AgentConfigRepository.get("orchestrator")
        assert orchestrator["temperature"] == 0.3
        rewrite = await AgentConfigRepository.get("rewrite-agent")
        assert rewrite["temperature"] == 0.8

    async def test_migration_v2_skips_user_modified_temperatures(self, db_repository):
        """Migration should not overwrite user-customized temperature values."""
        from app.db.schema import _run_migrations

        # Simulate user changing light-agent temperature to 0.4
        await AgentConfigRepository.upsert("light-agent", temperature=0.4)

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            # Reset schema version to allow migration to re-run
            await db.execute("DELETE FROM schema_version WHERE version = 2")
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        light = await AgentConfigRepository.get("light-agent")
        assert light["temperature"] == 0.4  # Should NOT be overwritten


# ---------------------------------------------------------------------------
# Schema migration v4 -- entity visibility defaults and legacy migration
# ---------------------------------------------------------------------------


class TestMigrationV4:
    async def test_migration_v4_seeds_defaults_for_empty_agents(self, db_repository):
        """Migration v4 should insert domain_include defaults for agents with zero rules."""
        from app.db.repository import EntityVisibilityRepository
        from app.db.schema import _run_migrations

        # Clear all visibility rules to simulate pre-migration state
        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM entity_visibility_rules")
            await db.execute("DELETE FROM schema_version WHERE version >= 4")
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        # Verify default rules seeded for light-agent
        rules = await EntityVisibilityRepository.get_rules("light-agent")
        rule_values = {r["rule_value"] for r in rules if r["rule_type"] == "domain_include"}
        assert "light" in rule_values
        assert "switch" in rule_values

        # Verify music-agent
        rules = await EntityVisibilityRepository.get_rules("music-agent")
        rule_values = {r["rule_value"] for r in rules if r["rule_type"] == "domain_include"}
        assert "media_player" in rule_values

    async def test_migration_v4_skips_agents_with_existing_rules(self, db_repository):
        """Migration v4 should not overwrite existing user-configured rules."""
        from app.db.repository import EntityVisibilityRepository
        from app.db.schema import _run_migrations

        # Clear migration marker and set custom rules for light-agent
        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM entity_visibility_rules")
            await db.execute("DELETE FROM schema_version WHERE version >= 4")
            await db.execute(
                "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                ("light-agent", "domain_include", "custom_domain"),
            )
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        # light-agent should only have the user's custom rule, not defaults
        rules = await EntityVisibilityRepository.get_rules("light-agent")
        rule_values = {r["rule_value"] for r in rules}
        assert "custom_domain" in rule_values
        assert "light" not in rule_values  # default NOT inserted

    async def test_migration_v4_migrates_legacy_entity_rule_type(self, db_repository):
        """Migration v4 should convert 'entity' -> 'entity_include'."""
        from app.db.repository import EntityVisibilityRepository
        from app.db.schema import _run_migrations

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM entity_visibility_rules")
            await db.execute("DELETE FROM schema_version WHERE version >= 4")
            await db.execute(
                "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                ("test-agent", "entity", "light.kitchen"),
            )
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        rules = await EntityVisibilityRepository.get_rules("test-agent")
        assert len(rules) == 1
        assert rules[0]["rule_type"] == "entity_include"
        assert rules[0]["rule_value"] == "light.kitchen"

    async def test_migration_v4_migrates_legacy_domain_rule_type(self, db_repository):
        """Migration v4 should convert 'domain' -> 'domain_include'."""
        from app.db.repository import EntityVisibilityRepository
        from app.db.schema import _run_migrations

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM entity_visibility_rules")
            await db.execute("DELETE FROM schema_version WHERE version >= 4")
            await db.execute(
                "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                ("test-agent", "domain", "light"),
            )
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        rules = await EntityVisibilityRepository.get_rules("test-agent")
        assert len(rules) == 1
        assert rules[0]["rule_type"] == "domain_include"

    async def test_migration_v4_migrates_legacy_area_rule_type(self, db_repository):
        """Migration v4 should convert 'area' -> 'area_include'."""
        from app.db.repository import EntityVisibilityRepository
        from app.db.schema import _run_migrations

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM entity_visibility_rules")
            await db.execute("DELETE FROM schema_version WHERE version >= 4")
            await db.execute(
                "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                ("test-agent", "area", "kitchen"),
            )
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        rules = await EntityVisibilityRepository.get_rules("test-agent")
        assert len(rules) == 1
        assert rules[0]["rule_type"] == "area_include"


# ---------------------------------------------------------------------------
# Schema migration v5 -- rewrite-agent max_tokens bump
# ---------------------------------------------------------------------------


class TestMigrationV5:
    async def test_migration_v5_bumps_rewrite_agent_max_tokens(self, db_repository):
        """Migration v5 should increase rewrite-agent max_tokens from 128 to 512 (then migration 10 to 1024)."""
        from app.db.schema import _run_migrations

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            # Simulate pre-migration state
            await db.execute("UPDATE agent_configs SET max_tokens = 128 WHERE agent_id = 'rewrite-agent'")
            await db.execute("DELETE FROM schema_version WHERE version >= 5")
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        row = await AgentConfigRepository.get("rewrite-agent")
        assert row["max_tokens"] == 1024

    async def test_migration_v5_skips_user_modified_max_tokens(self, db_repository):
        """Migration v5 should not overwrite user-customized max_tokens."""
        from app.db.schema import _run_migrations

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("UPDATE agent_configs SET max_tokens = 1024 WHERE agent_id = 'rewrite-agent'")
            await db.execute("DELETE FROM schema_version WHERE version >= 5")
            await db.commit()
            await _run_migrations(db)
            await db.commit()

        row = await AgentConfigRepository.get("rewrite-agent")
        assert row["max_tokens"] == 1024  # preserved


# ---------------------------------------------------------------------------
# Schema migration v21 -- scheduled timer briefing column
# ---------------------------------------------------------------------------


class TestMigrationV21:
    async def test_migration_v21_adds_briefing_column_idempotently(self, db_repository):
        from app.db.schema import _run_migrations

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM schema_version WHERE version >= 21")
            await db.execute("ALTER TABLE scheduled_timers RENAME TO scheduled_timers_old")
            await db.execute(
                """
                CREATE TABLE scheduled_timers (
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
                """
            )
            await db.execute(
                """
                INSERT INTO scheduled_timers (
                    id, logical_name, kind, created_at, fires_at, duration_seconds,
                    origin_device_id, origin_area, payload_json, state, fired_at, cancelled_at
                )
                SELECT
                    id, logical_name, kind, created_at, fires_at, duration_seconds,
                    origin_device_id, origin_area, payload_json, state, fired_at, cancelled_at
                FROM scheduled_timers_old
                """
            )
            await db.execute("DROP TABLE scheduled_timers_old")
            await db.commit()

            await _run_migrations(db)
            await _run_migrations(db)
            await db.commit()

            columns = await (await db.execute("PRAGMA table_info(scheduled_timers)")).fetchall()
            schema_versions = await (
                await db.execute("SELECT version FROM schema_version WHERE version = 21")
            ).fetchall()

        column_names = {row[1] for row in columns}
        assert "briefing" in column_names
        assert len(schema_versions) == 1


class TestMigrationV22:
    async def test_migration_v22_rewrites_legacy_http_transport_to_sse(self, db_repository):
        from app.db.schema import _run_migrations

        await McpServerRepository.create("legacy-http", "http", "http://localhost:9000/sse")

        async with aiosqlite.connect(str(db_repository)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("DELETE FROM schema_version WHERE version >= 22")
            await db.commit()

            await _run_migrations(db)
            await db.commit()

            row = await (await db.execute("SELECT transport FROM mcp_servers WHERE name = 'legacy-http'")).fetchone()
            schema_versions = await (
                await db.execute("SELECT version FROM schema_version WHERE version = 22")
            ).fetchall()

        assert row is not None
        assert row["transport"] == "sse"
        assert len(schema_versions) == 1


# ---------------------------------------------------------------------------
# SendDeviceMappingRepository
# ---------------------------------------------------------------------------


class TestSendDeviceMappingRepository:
    async def test_create_and_get(self, db_repository):
        row_id = await SendDeviceMappingRepository.create(
            "Laura Handy",
            "notify",
            "mobile_app_lauras_iphone",
        )
        assert row_id is not None
        mapping = await SendDeviceMappingRepository.get(row_id)
        assert mapping["display_name"] == "Laura Handy"
        assert mapping["device_type"] == "notify"
        assert mapping["ha_service_target"] == "mobile_app_lauras_iphone"

    async def test_find_by_name_case_insensitive(self, db_repository):
        await SendDeviceMappingRepository.create(
            "Laura Handy",
            "notify",
            "mobile_app_lauras_iphone",
        )
        result = await SendDeviceMappingRepository.find_by_name("laura handy")
        assert result is not None
        assert result["display_name"] == "Laura Handy"

    async def test_find_by_name_not_found(self, db_repository):
        result = await SendDeviceMappingRepository.find_by_name("nonexistent")
        assert result is None

    async def test_list_all(self, db_repository):
        await SendDeviceMappingRepository.create("Device A", "notify", "svc_a")
        await SendDeviceMappingRepository.create("Device B", "tts", "media_player.b")
        mappings = await SendDeviceMappingRepository.list_all()
        assert len(mappings) == 2

    async def test_update(self, db_repository):
        row_id = await SendDeviceMappingRepository.create("Old Name", "notify", "svc_old")
        ok = await SendDeviceMappingRepository.update(row_id, display_name="New Name")
        assert ok is True
        mapping = await SendDeviceMappingRepository.get(row_id)
        assert mapping["display_name"] == "New Name"

    async def test_delete(self, db_repository):
        row_id = await SendDeviceMappingRepository.create("To Delete", "notify", "svc_del")
        ok = await SendDeviceMappingRepository.delete(row_id)
        assert ok is True
        mapping = await SendDeviceMappingRepository.get(row_id)
        assert mapping is None

    async def test_delete_nonexistent(self, db_repository):
        ok = await SendDeviceMappingRepository.delete(9999)
        assert ok is False

    async def test_find_by_name_apostrophe_mismatch(self, db_repository):
        await SendDeviceMappingRepository.create(
            "Patric's Handy",
            "notify",
            "mobile_app_patrics_handy",
        )
        result = await SendDeviceMappingRepository.find_by_name("patrics handy")
        assert result is not None
        assert result["display_name"] == "Patric's Handy"

    async def test_find_by_name_special_chars_fallback(self, db_repository):
        await SendDeviceMappingRepository.create(
            "Laura's Tablet",
            "notify",
            "mobile_app_lauras_tablet",
        )
        result = await SendDeviceMappingRepository.find_by_name("lauras tablet")
        assert result is not None
        assert result["display_name"] == "Laura's Tablet"

    async def test_find_by_name_exact_still_works(self, db_repository):
        await SendDeviceMappingRepository.create(
            "Patric's Handy",
            "notify",
            "mobile_app_patrics_handy",
        )
        result = await SendDeviceMappingRepository.find_by_name("Patric's Handy")
        assert result is not None
        assert result["display_name"] == "Patric's Handy"


# ---------------------------------------------------------------------------
# Read/Write Split
# ---------------------------------------------------------------------------


class TestReadWriteSplit:
    """Verify that read and write paths function correctly."""

    async def test_concurrent_reads_do_not_block(self, db_repository):
        """Multiple concurrent reads should complete without serialization issues."""
        import asyncio

        async def read_settings():
            return await SettingsRepository.get_all()

        # Run 5 concurrent reads
        results = await asyncio.gather(*[read_settings() for _ in range(5)])
        # All should return the same seeded data
        assert all(len(r) > 0 for r in results)
        assert all(len(r) == len(results[0]) for r in results)

    async def test_write_then_read_consistent(self, db_repository):
        """A write followed by a read should see the written data."""
        await SettingsRepository.set(
            "test.rw_split", "hello", value_type="string", category="test", description="rw split test"
        )
        result = await SettingsRepository.get_value("test.rw_split")
        assert result == "hello"
