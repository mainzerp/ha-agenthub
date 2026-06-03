"""Custom agent CRUD with runtime state sync."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, ClassVar

from app.db.repositories._utils import _now, _validate_column_name
from app.db.schema import get_db_read, get_db_write

logger = logging.getLogger(__name__)

_CUSTOM_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def normalize_custom_agent_name(name: str) -> str:
    """Return the stable DB name used by custom agent IDs."""
    from app.bootstrap._agents import BUILT_IN_AGENT_IDS

    raw = (name or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
    if not normalized or not _CUSTOM_AGENT_NAME_RE.fullmatch(normalized):
        raise ValueError("Custom agent name must be a slug of lowercase letters, numbers, hyphens, or underscores")
    if normalized.startswith("custom-"):
        raise ValueError("Custom agent name must not include the custom- prefix")
    agent_id = f"custom-{normalized}"
    if agent_id in BUILT_IN_AGENT_IDS:
        raise ValueError("Custom agent ID conflicts with a built-in agent")
    return normalized


def custom_agent_id_for_name(name: str) -> str:
    return f"custom-{normalize_custom_agent_name(name)}"


class CustomAgentRepository:
    """CRUD for runtime-created custom agents."""

    _VISIBILITY_RULE_TYPES: ClassVar[set[str]] = {
        "domain_include",
        "domain_exclude",
        "area_include",
        "area_exclude",
        "entity_include",
        "device_class_include",
        "device_class_exclude",
    }

    @staticmethod
    def normalize_name(name: str) -> str:
        return normalize_custom_agent_name(name)

    @staticmethod
    def agent_id_for_name(name: str) -> str:
        return custom_agent_id_for_name(name)

    @staticmethod
    def _decode_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        for field in ("mcp_tools", "entity_visibility", "intent_patterns"):
            raw = result.get(field)
            if raw:
                try:
                    result[field] = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Malformed JSON in %s for custom agent %s", field, result.get("name"))
                    result[field] = [] if field != "entity_visibility" else {}
        return result

    @staticmethod
    def _clean_model_override(model_override: str | None) -> str | None:
        cleaned = (model_override or "").strip()
        return cleaned or None

    @staticmethod
    def _normalize_tool_assignments(tools: list[dict[str, str]] | None) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for tool in tools or []:
            server_name = (tool.get("server_name") or tool.get("server") or "").strip()
            tool_name = (tool.get("tool_name") or tool.get("tool") or "").strip()
            if not server_name or not tool_name:
                raise ValueError("Each MCP tool assignment requires server_name and tool_name")
            normalized.append({"server_name": server_name, "tool_name": tool_name})
        return normalized

    @classmethod
    def _normalize_visibility_rules(cls, rules: list[dict[str, str]] | None) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for rule in rules or []:
            rule_type = (rule.get("rule_type") or "").strip()
            rule_value = (rule.get("rule_value") or "").strip()
            if rule_type not in cls._VISIBILITY_RULE_TYPES:
                raise ValueError(f"Invalid visibility rule type: {rule_type}")
            if not rule_value:
                raise ValueError("Visibility rule value is required")
            normalized.append({"rule_type": rule_type, "rule_value": rule_value})
        return normalized

    @staticmethod
    def _normalize_intent_patterns(patterns: list[str] | None) -> list[str]:
        return [str(pattern).strip() for pattern in patterns or [] if str(pattern).strip()]

    @staticmethod
    async def _general_agent_defaults(db) -> dict[str, Any]:
        cursor = await db.execute(
            "SELECT model, timeout, max_iterations, temperature, max_tokens, description "
            "FROM agent_configs WHERE agent_id = 'general-agent'"
        )
        row = await cursor.fetchone()
        if row is None:
            return {
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.5,
                "max_tokens": 1024,
                "description": "Custom runtime agent",
            }
        return dict(row)

    @classmethod
    async def _upsert_runtime_config_in_tx(
        cls,
        db,
        agent_id: str,
        *,
        model_override: str | None,
        enabled: bool,
        description: str | None,
    ) -> None:
        defaults = await cls._general_agent_defaults(db)
        model = cls._clean_model_override(model_override) or defaults.get("model") or "openrouter/openai/gpt-4o-mini"
        await db.execute(
            "INSERT INTO agent_configs "
            "(agent_id, enabled, model, timeout, max_iterations, temperature, max_tokens, description, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "enabled = excluded.enabled, model = excluded.model, "
            "description = excluded.description, updated_at = excluded.updated_at",
            (
                agent_id,
                1 if enabled else 0,
                model,
                defaults.get("timeout") or 5,
                defaults.get("max_iterations") or 3,
                defaults.get("temperature") if defaults.get("temperature") is not None else 0.5,
                defaults.get("max_tokens") or 1024,
                description if description is not None else "Custom runtime agent",
                _now(),
            ),
        )

    @staticmethod
    async def _replace_mcp_tools_in_tx(db, agent_id: str, tools: list[dict[str, str]]) -> None:
        await db.execute("DELETE FROM agent_mcp_tools WHERE agent_id = ?", (agent_id,))
        for tool in tools:
            await db.execute(
                "INSERT OR IGNORE INTO agent_mcp_tools (agent_id, server_name, tool_name) VALUES (?, ?, ?)",
                (agent_id, tool["server_name"], tool["tool_name"]),
            )

    @staticmethod
    async def _replace_visibility_rules_in_tx(db, agent_id: str, rules: list[dict[str, str]]) -> None:
        await db.execute("DELETE FROM entity_visibility_rules WHERE agent_id = ?", (agent_id,))
        for rule in rules:
            await db.execute(
                "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                (agent_id, rule["rule_type"], rule["rule_value"]),
            )

    @classmethod
    async def _sync_runtime_state_in_tx(cls, db, row: dict[str, Any]) -> None:
        name = cls.normalize_name(row["name"])
        agent_id = f"custom-{name}"
        enabled = bool(row.get("enabled", 1))
        await cls._upsert_runtime_config_in_tx(
            db,
            agent_id,
            model_override=row.get("model_override"),
            enabled=enabled,
            description=row.get("description"),
        )
        if not enabled:
            await cls._replace_mcp_tools_in_tx(db, agent_id, [])
            await cls._replace_visibility_rules_in_tx(db, agent_id, [])
            return
        await cls._replace_mcp_tools_in_tx(db, agent_id, cls._normalize_tool_assignments(row.get("mcp_tools") or []))
        await cls._replace_visibility_rules_in_tx(
            db,
            agent_id,
            cls._normalize_visibility_rules(row.get("entity_visibility") or []),
        )

    @staticmethod
    async def get(name: str) -> dict[str, Any] | None:
        name = CustomAgentRepository.normalize_name(name)
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM custom_agents WHERE name = ?", (name,))
            row = await cursor.fetchone()
            if row is None:
                return None
            return CustomAgentRepository._decode_row(row)

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM custom_agents")
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                for field in ("mcp_tools", "entity_visibility", "intent_patterns"):
                    raw = row.get(field)
                    if raw:
                        try:
                            row[field] = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("Malformed JSON in %s for custom agent %s", field, row.get("name"))
                            row[field] = [] if field != "entity_visibility" else {}
            return rows

    @staticmethod
    async def list_enabled() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM custom_agents WHERE enabled = 1")
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                for field in ("mcp_tools", "entity_visibility", "intent_patterns"):
                    raw = row.get(field)
                    if raw:
                        try:
                            row[field] = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("Malformed JSON in %s for custom agent %s", field, row.get("name"))
                            row[field] = [] if field != "entity_visibility" else {}
            return rows

    @staticmethod
    async def create(name: str, system_prompt: str, **kwargs: Any) -> None:
        name = CustomAgentRepository.normalize_name(name)
        fields = {
            "description",
            "model_override",
            "timeout_sec",
            "mcp_tools",
            "entity_visibility",
            "intent_patterns",
            "enabled",
        }
        data = {k: v for k, v in kwargs.items() if k in fields}
        for field in ("mcp_tools", "entity_visibility", "intent_patterns"):
            if field in data and isinstance(data[field], (list, dict)):
                data[field] = json.dumps(data[field])

        columns = ", ".join(["name", "system_prompt", *[_validate_column_name(k) for k in data]])
        placeholders = ", ".join(["?"] * (len(data) + 2))
        values = [name, system_prompt, *list(data.values())]

        async with get_db_write() as db:
            await db.execute(
                f"INSERT INTO custom_agents ({columns}) VALUES ({placeholders})",
                values,
            )

    @staticmethod
    async def update(name: str, **kwargs: Any) -> None:
        name = CustomAgentRepository.normalize_name(name)
        fields = {
            "description",
            "system_prompt",
            "model_override",
            "timeout_sec",
            "mcp_tools",
            "entity_visibility",
            "intent_patterns",
            "enabled",
        }
        data = {k: v for k, v in kwargs.items() if k in fields}
        if not data:
            return
        for field in ("mcp_tools", "entity_visibility", "intent_patterns"):
            if field in data and isinstance(data[field], (list, dict)):
                data[field] = json.dumps(data[field])
        data["updated_at"] = _now()

        set_clause = ", ".join(f"{_validate_column_name(k)} = ?" for k in data)
        values = [*list(data.values()), name]

        async with get_db_write() as db:
            await db.execute(
                f"UPDATE custom_agents SET {set_clause} WHERE name = ?",
                values,
            )

    @staticmethod
    async def delete(name: str) -> None:
        name = CustomAgentRepository.normalize_name(name)
        async with get_db_write() as db:
            await db.execute("DELETE FROM custom_agents WHERE name = ?", (name,))

    @classmethod
    async def create_with_runtime(
        cls,
        name: str,
        system_prompt: str,
        **kwargs: Any,
    ) -> str:
        name = cls.normalize_name(name)
        mcp_tools = cls._normalize_tool_assignments(kwargs.get("mcp_tools") or [])
        visibility_rules = cls._normalize_visibility_rules(kwargs.get("entity_visibility") or [])
        intent_patterns = cls._normalize_intent_patterns(kwargs.get("intent_patterns") or [])
        enabled = bool(kwargs.get("enabled", True))
        description = kwargs.get("description") or ""
        model_override = cls._clean_model_override(kwargs.get("model_override"))
        timeout_sec = kwargs.get("timeout_sec")
        row = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "model_override": model_override,
            "timeout_sec": timeout_sec,
            "mcp_tools": mcp_tools,
            "entity_visibility": visibility_rules,
            "intent_patterns": intent_patterns,
            "enabled": 1 if enabled else 0,
        }
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO custom_agents "
                "(name, description, system_prompt, model_override, timeout_sec, mcp_tools, entity_visibility, intent_patterns, enabled, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    description,
                    system_prompt,
                    model_override,
                    timeout_sec,
                    json.dumps(mcp_tools) if mcp_tools else None,
                    json.dumps(visibility_rules) if visibility_rules else None,
                    json.dumps(intent_patterns) if intent_patterns else None,
                    1 if enabled else 0,
                    _now(),
                ),
            )
            await cls._sync_runtime_state_in_tx(db, row)
        return name

    @classmethod
    async def update_with_runtime(cls, name: str, **kwargs: Any) -> bool:
        name = cls.normalize_name(name)
        fields = {
            "description",
            "system_prompt",
            "model_override",
            "timeout_sec",
            "mcp_tools",
            "entity_visibility",
            "intent_patterns",
            "enabled",
        }
        data = {key: value for key, value in kwargs.items() if key in fields}
        if "mcp_tools" in data:
            data["mcp_tools"] = cls._normalize_tool_assignments(data.get("mcp_tools") or [])
        if "entity_visibility" in data:
            data["entity_visibility"] = cls._normalize_visibility_rules(data.get("entity_visibility") or [])
        if "intent_patterns" in data:
            data["intent_patterns"] = cls._normalize_intent_patterns(data.get("intent_patterns") or [])
        if "model_override" in data:
            data["model_override"] = cls._clean_model_override(data.get("model_override"))
        if "enabled" in data:
            data["enabled"] = 1 if bool(data["enabled"]) else 0

        async with get_db_write() as db:
            if data:
                stored = dict(data)
                for field in ("mcp_tools", "entity_visibility", "intent_patterns"):
                    if field in stored and isinstance(stored[field], (list, dict)):
                        stored[field] = json.dumps(stored[field]) if stored[field] else None
                stored["updated_at"] = _now()
                set_clause = ", ".join(f"{_validate_column_name(key)} = ?" for key in stored)
                values = [*list(stored.values()), name]
                await db.execute(f"UPDATE custom_agents SET {set_clause} WHERE name = ?", values)
            cursor = await db.execute("SELECT * FROM custom_agents WHERE name = ?", (name,))
            row = await cursor.fetchone()
            if row is None:
                await db.commit()
                return False
            decoded = cls._decode_row(row)
            await cls._sync_runtime_state_in_tx(db, decoded)
            return True

    @classmethod
    async def delete_with_runtime(cls, name: str) -> bool:
        name = cls.normalize_name(name)
        agent_id = f"custom-{name}"
        async with get_db_write() as db:
            cursor = await db.execute("DELETE FROM custom_agents WHERE name = ?", (name,))
            await db.execute("DELETE FROM agent_configs WHERE agent_id = ?", (agent_id,))
            await db.execute("DELETE FROM agent_mcp_tools WHERE agent_id = ?", (agent_id,))
            await db.execute("DELETE FROM entity_visibility_rules WHERE agent_id = ?", (agent_id,))
            return (cursor.rowcount or 0) > 0

    @classmethod
    async def ensure_runtime_state(cls, row: dict[str, Any]) -> None:
        async with get_db_write() as db:
            await cls._sync_runtime_state_in_tx(db, row)
