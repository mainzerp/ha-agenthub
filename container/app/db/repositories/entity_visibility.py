"""Entity visibility rules CRUD."""

from __future__ import annotations

from typing import Any

from app.db.schema import get_db_read, get_db_write


class EntityVisibilityRepository:
    """CRUD for per-agent entity visibility rules."""

    @staticmethod
    async def get_rules(agent_id: str) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT rule_type, rule_value FROM entity_visibility_rules WHERE agent_id = ?",
                (agent_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def set_rules(agent_id: str, rules: list[dict[str, str]]) -> None:
        async with get_db_write() as db:
            await db.execute(
                "DELETE FROM entity_visibility_rules WHERE agent_id = ?",
                (agent_id,),
            )
            for rule in rules:
                await db.execute(
                    "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                    (agent_id, rule["rule_type"], rule["rule_value"]),
                )
        from app.entity.visibility import invalidate_visibility_rules_cache

        invalidate_visibility_rules_cache(agent_id)

    @staticmethod
    async def add_rule(agent_id: str, rule_type: str, rule_value: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                (agent_id, rule_type, rule_value),
            )
        from app.entity.visibility import invalidate_visibility_rules_cache

        invalidate_visibility_rules_cache(agent_id)

    @staticmethod
    async def remove_rule(agent_id: str, rule_type: str, rule_value: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "DELETE FROM entity_visibility_rules WHERE agent_id = ? AND rule_type = ? AND rule_value = ?",
                (agent_id, rule_type, rule_value),
            )
        from app.entity.visibility import invalidate_visibility_rules_cache

        invalidate_visibility_rules_cache(agent_id)

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT agent_id, rule_type, rule_value FROM entity_visibility_rules")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def list_domain_include_rules() -> list[dict[str, Any]]:
        """Return all domain_include rules: [{agent_id, rule_value}]."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT agent_id, rule_value FROM entity_visibility_rules WHERE rule_type = 'domain_include'"
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def list_device_class_include_rules() -> list[dict[str, Any]]:
        """Return all device_class_include rules: [{agent_id, rule_value}]."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT agent_id, rule_value FROM entity_visibility_rules WHERE rule_type = 'device_class_include'"
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def set_domain_agents(domain: str, agent_ids: list[str]) -> None:
        """Set which agents have domain_include for a given domain."""
        async with get_db_write() as db:
            await db.execute(
                "DELETE FROM entity_visibility_rules WHERE rule_type = 'domain_include' AND rule_value = ?",
                (domain,),
            )
            for agent_id in agent_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO entity_visibility_rules "
                    "(agent_id, rule_type, rule_value) VALUES (?, 'domain_include', ?)",
                    (agent_id, domain),
                )

    @staticmethod
    async def set_device_class_agents(device_class: str, agent_ids: list[str]) -> None:
        """Set which agents have device_class_include for a given device_class.

        Also ensures each agent has domain_include:sensor so the matcher
        can reach the device_class filter stage.
        """
        async with get_db_write() as db:
            await db.execute(
                "DELETE FROM entity_visibility_rules WHERE rule_type = 'device_class_include' AND rule_value = ?",
                (device_class,),
            )
            for agent_id in agent_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO entity_visibility_rules "
                    "(agent_id, rule_type, rule_value) VALUES (?, 'device_class_include', ?)",
                    (agent_id, device_class),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO entity_visibility_rules "
                    "(agent_id, rule_type, rule_value) VALUES (?, 'domain_include', 'sensor')",
                    (agent_id,),
                )
