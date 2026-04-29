"""Async CRUD operations via aiosqlite.

Provides repository classes for each SQLite table with typed
async methods for common operations.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.db.schema import get_db_read, get_db_write


def _now() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _validate_column_name(col: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", col):
        raise ValueError(f"Invalid column name: {col}")
    return col


# P3-6: in-memory TTL cache for ``SettingsRepository.get_value``.
# Settings change rarely but ``get_value`` is called per-request from
# many hot paths (orchestrator, filler thresholds, dispatch timeouts,
# routing thresholds). One DB hit per call adds up; the TTL keeps the
# cache from going stale across long-running processes / out-of-band
# DB writes.
_SETTINGS_VALUE_CACHE_TTL_SEC = 60.0
# Sentinel used to cache "key absent" results so we don't re-hit the DB
# for unset keys every call. Stored with the same TTL.
_MISSING = object()

_CUSTOM_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_BUILTIN_AGENT_IDS = {
    "orchestrator",
    "light-agent",
    "music-agent",
    "general-agent",
    "timer-agent",
    "climate-agent",
    "media-agent",
    "scene-agent",
    "automation-agent",
    "security-agent",
    "send-agent",
    "rewrite-agent",
    "filler-agent",
}


def normalize_custom_agent_name(name: str) -> str:
    """Return the stable DB name used by custom agent IDs."""
    raw = (name or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
    if not normalized or not _CUSTOM_AGENT_NAME_RE.fullmatch(normalized):
        raise ValueError("Custom agent name must be a slug of lowercase letters, numbers, hyphens, or underscores")
    if normalized.startswith("custom-"):
        raise ValueError("Custom agent name must not include the custom- prefix")
    agent_id = f"custom-{normalized}"
    if agent_id in _BUILTIN_AGENT_IDS:
        raise ValueError("Custom agent ID conflicts with a built-in agent")
    return normalized


def custom_agent_id_for_name(name: str) -> str:
    return f"custom-{normalize_custom_agent_name(name)}"


class SettingsRepository:
    """CRUD for the settings key-value store."""

    # ``{key: (value_or_MISSING, expires_at_monotonic)}``.
    # Class-level on purpose: ``SettingsRepository`` is a stateless
    # collection of staticmethods used as a namespace.
    _value_cache: ClassVar[dict[str, tuple[Any, float]]] = {}
    _value_cache_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    async def _cache_get(cls, key: str) -> tuple[bool, Any]:
        async with cls._value_cache_lock:
            entry = cls._value_cache.get(key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if expires_at <= time.monotonic():
                cls._value_cache.pop(key, None)
                return False, None
            return True, value

    @classmethod
    async def _cache_put(cls, key: str, value: Any) -> None:
        async with cls._value_cache_lock:
            cls._value_cache[key] = (value, time.monotonic() + _SETTINGS_VALUE_CACHE_TTL_SEC)

    @classmethod
    def _cache_invalidate(cls, key: str | None = None) -> None:
        """Drop a single key (or the whole cache when ``key`` is ``None``)."""
        if key is None:
            cls._value_cache.clear()
        else:
            cls._value_cache.pop(key, None)

    @staticmethod
    async def get(key: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT key, value, value_type, category, description FROM settings WHERE key = ?",
                (key,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    @staticmethod
    async def get_value(key: str, default: str | None = None) -> str | None:
        # P3-6: serve from in-memory TTL cache when available. The
        # cached entry stores either the actual DB value or ``_MISSING``
        # (key absent in DB); ``default`` is applied to ``_MISSING``
        # hits at call time so different callers can use different
        # defaults.
        hit, cached = await SettingsRepository._cache_get(key)
        if hit:
            return default if cached is _MISSING else cached
        async with get_db_read() as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
        if row is None:
            await SettingsRepository._cache_put(key, _MISSING)
            return default
        value = row[0]
        await SettingsRepository._cache_put(key, value)
        return value

    @staticmethod
    async def set(
        key: str, value: str, value_type: str = "string", category: str = "general", description: str | None = None
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO settings (key, value, value_type, category, description, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
                (key, value, value_type, category, description, _now(), value, _now()),
            )
            await db.commit()
        # P3-6: invalidate so subsequent ``get_value`` reflects the write.
        SettingsRepository._cache_invalidate(key)

    @staticmethod
    async def get_by_category(category: str) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT key, value, value_type, description FROM settings WHERE category = ?",
                (category,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT key, value, value_type, category, description FROM settings")
            return [dict(row) for row in await cursor.fetchall()]


class AgentConfigRepository:
    """CRUD for agent configurations."""

    @staticmethod
    async def get(agent_id: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM agent_configs WHERE agent_id = ?", (agent_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM agent_configs")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def list_enabled() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM agent_configs WHERE enabled = 1")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def upsert(agent_id: str, **kwargs: Any) -> None:
        allowed = {
            "enabled",
            "model",
            "timeout",
            "max_iterations",
            "temperature",
            "max_tokens",
            "description",
            "reasoning_effort",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = _now()

        columns = ", ".join(["agent_id", *[_validate_column_name(k) for k in fields]])
        placeholders = ", ".join(["?"] * (len(fields) + 1))
        updates = ", ".join(f"{_validate_column_name(k)}=excluded.{_validate_column_name(k)}" for k in fields)

        values = [agent_id, *list(fields.values())]
        async with get_db_write() as db:
            await db.execute(
                f"INSERT INTO agent_configs ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(agent_id) DO UPDATE SET {updates}",
                values,
            )
            await db.commit()

    @staticmethod
    async def delete(agent_id: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM agent_configs WHERE agent_id = ?", (agent_id,))
            await db.commit()


class SecretsRepository:
    """CRUD for Fernet-encrypted secrets."""

    @staticmethod
    async def get(key: str) -> bytes | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT encrypted_value FROM secrets WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    async def set(key: str, encrypted_value: bytes) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO secrets (key, encrypted_value, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET encrypted_value=?, updated_at=?",
                (key, encrypted_value, _now(), encrypted_value, _now()),
            )
            await db.commit()

    @staticmethod
    async def delete(key: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM secrets WHERE key = ?", (key,))
            await db.commit()

    @staticmethod
    async def list_keys() -> list[str]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT key FROM secrets")
            return [row[0] for row in await cursor.fetchall()]


class AdminAccountRepository:
    """CRUD for admin accounts."""

    @staticmethod
    async def create(
        username: str,
        password_hash: str,
        *,
        force_overwrite: bool = False,
    ) -> None:
        """Create an admin account.

        ``force_overwrite=True`` uses ``INSERT OR REPLACE`` (only the
        one-time setup bootstrap should pass this). The default uses
        ``INSERT OR IGNORE`` so an authenticated session cannot silently
        overwrite an existing admin row via an unrelated code path.
        """
        verb = "INSERT OR REPLACE" if force_overwrite else "INSERT OR IGNORE"
        async with get_db_write() as db:
            await db.execute(
                f"{verb} INTO admin_accounts (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, _now()),
            )
            await db.commit()

    @staticmethod
    async def update_password(username: str, password_hash: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE admin_accounts SET password_hash = ? WHERE username = ?",
                (password_hash, username),
            )
            await db.commit()

    @staticmethod
    async def get(username: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM admin_accounts WHERE username = ?", (username,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def update_last_login(username: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE admin_accounts SET last_login = ? WHERE username = ?",
                (_now(), username),
            )
            await db.commit()

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT username, created_at, last_login FROM admin_accounts")
            return [dict(row) for row in await cursor.fetchall()]


class SetupStateRepository:
    """CRUD for setup wizard state tracking."""

    @staticmethod
    async def get_step(step: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM setup_state WHERE step = ?", (step,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def set_step_completed(step: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE setup_state SET completed = 1, completed_at = ? WHERE step = ?",
                (_now(), step),
            )
            await db.commit()

    @staticmethod
    async def is_complete() -> bool:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM setup_state WHERE completed = 0")
            row = await cursor.fetchone()
            return row[0] == 0

    @staticmethod
    async def get_all_steps() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM setup_state")
            return [dict(row) for row in await cursor.fetchall()]


class AliasRepository:
    """CRUD for entity aliases."""

    @staticmethod
    async def get(alias: str) -> str | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT entity_id FROM aliases WHERE alias = ?", (alias,))
            row = await cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    async def set(alias: str, entity_id: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO aliases (alias, entity_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(alias) DO UPDATE SET entity_id=?",
                (alias, entity_id, _now(), entity_id),
            )
            await db.commit()

    @staticmethod
    async def delete(alias: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM aliases WHERE alias = ?", (alias,))
            await db.commit()

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT alias, entity_id FROM aliases")
            return [dict(row) for row in await cursor.fetchall()]


class QuerySynonymCacheRepository:
    """0.23.0: organic LLM-expansion cache for cold query tokens.

    Storage is the empty ``query_synonym_cache`` table created by
    migration v18. Entries are added at query time; nothing is seeded
    for any language.
    """

    @staticmethod
    async def get(token: str, language: str) -> list[str] | None:
        token = (token or "").strip().lower()
        language = (language or "").strip().lower()
        if not token:
            return None
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT expansions FROM query_synonym_cache WHERE token = ? AND language = ?",
                (token, language),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            if isinstance(data, list):
                return [str(x) for x in data if isinstance(x, str) and x]
        except Exception:
            return []
        return []

    @staticmethod
    async def put(token: str, language: str, expansions: list[str]) -> None:
        token = (token or "").strip().lower()
        language = (language or "").strip().lower()
        if not token:
            return
        cleaned = [str(x).strip() for x in (expansions or []) if isinstance(x, str) and x.strip()]
        payload = json.dumps(cleaned[:8])
        now = int(time.time())
        async with get_db_write() as db:
            await db.execute(
                """
                INSERT INTO query_synonym_cache
                    (token, language, expansions, created_at, last_used_at, hit_count)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(token, language) DO UPDATE SET
                    expansions = excluded.expansions,
                    last_used_at = excluded.last_used_at
                """,
                (token, language, payload, now, now),
            )
            await db.commit()

    @staticmethod
    async def touch(token: str, language: str) -> None:
        token = (token or "").strip().lower()
        language = (language or "").strip().lower()
        if not token:
            return
        now = int(time.time())
        async with get_db_write() as db:
            await db.execute(
                """
                UPDATE query_synonym_cache
                SET last_used_at = ?, hit_count = hit_count + 1
                WHERE token = ? AND language = ?
                """,
                (now, token, language),
            )
            await db.commit()

    @staticmethod
    async def evict_lru(max_rows: int = 5000) -> int:
        async with get_db_write() as db:
            cur = await db.execute("SELECT COUNT(*) FROM query_synonym_cache")
            row = await cur.fetchone()
            total = int(row[0]) if row else 0
            if total <= max_rows:
                return 0
            to_drop = total - max_rows
            await db.execute(
                """
                DELETE FROM query_synonym_cache
                WHERE rowid IN (
                    SELECT rowid FROM query_synonym_cache
                    ORDER BY last_used_at ASC
                    LIMIT ?
                )
                """,
                (to_drop,),
            )
            await db.commit()
            return to_drop

    @staticmethod
    async def purge_expired(ttl_seconds: int) -> int:
        if ttl_seconds <= 0:
            return 0
        cutoff = int(time.time()) - int(ttl_seconds)
        async with get_db_write() as db:
            cur = await db.execute(
                "DELETE FROM query_synonym_cache WHERE last_used_at < ?",
                (cutoff,),
            )
            await db.commit()
            return cur.rowcount or 0

    @staticmethod
    async def clear_all() -> int:
        async with get_db_write() as db:
            cur = await db.execute("DELETE FROM query_synonym_cache")
            await db.commit()
            return cur.rowcount or 0

    @staticmethod
    async def count() -> int:
        async with get_db_read() as db:
            cur = await db.execute("SELECT COUNT(*) FROM query_synonym_cache")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def list_top(limit: int = 50) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cur = await db.execute(
                """
                SELECT token, language, expansions, created_at, last_used_at, hit_count
                FROM query_synonym_cache
                ORDER BY hit_count DESC, last_used_at DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            try:
                d["expansions"] = json.loads(d["expansions"])
            except Exception:
                d["expansions"] = []
            out.append(d)
        return out


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
            if result.get(field):
                result[field] = json.loads(result[field])
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
                    if row.get(field):
                        row[field] = json.loads(row[field])
            return rows

    @staticmethod
    async def list_enabled() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM custom_agents WHERE enabled = 1")
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                for field in ("mcp_tools", "entity_visibility", "intent_patterns"):
                    if row.get(field):
                        row[field] = json.loads(row[field])
            return rows

    @staticmethod
    async def create(name: str, system_prompt: str, **kwargs: Any) -> None:
        name = CustomAgentRepository.normalize_name(name)
        fields = {"description", "model_override", "mcp_tools", "entity_visibility", "intent_patterns", "enabled"}
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
            await db.commit()

    @staticmethod
    async def update(name: str, **kwargs: Any) -> None:
        name = CustomAgentRepository.normalize_name(name)
        fields = {
            "description",
            "system_prompt",
            "model_override",
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
            await db.commit()

    @staticmethod
    async def delete(name: str) -> None:
        name = CustomAgentRepository.normalize_name(name)
        async with get_db_write() as db:
            await db.execute("DELETE FROM custom_agents WHERE name = ?", (name,))
            await db.commit()

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
        row = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "model_override": model_override,
            "mcp_tools": mcp_tools,
            "entity_visibility": visibility_rules,
            "intent_patterns": intent_patterns,
            "enabled": 1 if enabled else 0,
        }
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO custom_agents "
                "(name, description, system_prompt, model_override, mcp_tools, entity_visibility, intent_patterns, enabled, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    description,
                    system_prompt,
                    model_override,
                    json.dumps(mcp_tools) if mcp_tools else None,
                    json.dumps(visibility_rules) if visibility_rules else None,
                    json.dumps(intent_patterns) if intent_patterns else None,
                    1 if enabled else 0,
                    _now(),
                ),
            )
            await cls._sync_runtime_state_in_tx(db, row)
            await db.commit()
        return name

    @classmethod
    async def update_with_runtime(cls, name: str, **kwargs: Any) -> bool:
        name = cls.normalize_name(name)
        fields = {
            "description",
            "system_prompt",
            "model_override",
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
            await db.commit()
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
            await db.commit()
            return (cursor.rowcount or 0) > 0

    @classmethod
    async def ensure_runtime_state(cls, row: dict[str, Any]) -> None:
        async with get_db_write() as db:
            await cls._sync_runtime_state_in_tx(db, row)
            await db.commit()


class McpServerRepository:
    """CRUD for MCP server configurations."""

    @staticmethod
    async def get(name: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,))
            row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            if result.get("env_vars"):
                result["env_vars"] = json.loads(result["env_vars"])
            return result

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM mcp_servers")
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("env_vars"):
                    row["env_vars"] = json.loads(row["env_vars"])
            return rows

    @staticmethod
    async def create(
        name: str, transport: str, command_or_url: str, env_vars: dict | None = None, timeout: int = 30
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO mcp_servers (name, transport, command_or_url, env_vars, timeout) VALUES (?, ?, ?, ?, ?)",
                (name, transport, command_or_url, json.dumps(env_vars) if env_vars else None, timeout),
            )
            await db.commit()

    @staticmethod
    async def delete(name: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
            await db.commit()

    @staticmethod
    async def list_enabled() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM mcp_servers WHERE enabled = 1")
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("env_vars"):
                    row["env_vars"] = json.loads(row["env_vars"])
            return rows

    @staticmethod
    async def upsert(
        name: str, transport: str, command_or_url: str, env_vars: dict | None = None, timeout: int = 30
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO mcp_servers (name, transport, command_or_url, env_vars, timeout, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET transport=?, command_or_url=?, env_vars=?, timeout=?, updated_at=?",
                (
                    name,
                    transport,
                    command_or_url,
                    json.dumps(env_vars) if env_vars else None,
                    timeout,
                    _now(),
                    transport,
                    command_or_url,
                    json.dumps(env_vars) if env_vars else None,
                    timeout,
                    _now(),
                ),
            )
            await db.commit()

    @staticmethod
    async def set_enabled(name: str, enabled: bool) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE mcp_servers SET enabled = ?, updated_at = ? WHERE name = ?",
                (1 if enabled else 0, _now(), name),
            )
            await db.commit()


class AgentMcpToolsRepository:
    """CRUD for MCP tool assignments to agents (built-in and custom)."""

    @staticmethod
    async def get_tools(agent_id: str) -> list[dict[str, str]]:
        """Return list of {server_name, tool_name} for an agent."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT server_name, tool_name FROM agent_mcp_tools WHERE agent_id = ?",
                (agent_id,),
            )
            return [
                {"server_name": row["server_name"], "tool_name": row["tool_name"]} for row in await cursor.fetchall()
            ]

    @staticmethod
    async def assign_tool(agent_id: str, server_name: str, tool_name: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO agent_mcp_tools (agent_id, server_name, tool_name) VALUES (?, ?, ?)",
                (agent_id, server_name, tool_name),
            )
            await db.commit()

    @staticmethod
    async def unassign_tool(agent_id: str, server_name: str, tool_name: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "DELETE FROM agent_mcp_tools WHERE agent_id = ? AND server_name = ? AND tool_name = ?",
                (agent_id, server_name, tool_name),
            )
            await db.commit()

    @staticmethod
    async def replace_tools(agent_id: str, tools: list[dict[str, str]] | None) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM agent_mcp_tools WHERE agent_id = ?", (agent_id,))
            for tool in tools or []:
                server_name = tool.get("server_name") or tool.get("server") or ""
                tool_name = tool.get("tool_name") or tool.get("tool") or ""
                if not server_name or not tool_name:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO agent_mcp_tools (agent_id, server_name, tool_name) VALUES (?, ?, ?)",
                    (agent_id, server_name, tool_name),
                )
            await db.commit()

    @staticmethod
    async def clear_agent(agent_id: str) -> None:
        await AgentMcpToolsRepository.replace_tools(agent_id, [])

    @staticmethod
    async def get_all_assignments() -> list[dict[str, str]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT agent_id, server_name, tool_name FROM agent_mcp_tools")
            return [dict(row) for row in await cursor.fetchall()]


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
            await db.commit()

    @staticmethod
    async def add_rule(agent_id: str, rule_type: str, rule_value: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES (?, ?, ?)",
                (agent_id, rule_type, rule_value),
            )
            await db.commit()

    @staticmethod
    async def remove_rule(agent_id: str, rule_type: str, rule_value: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "DELETE FROM entity_visibility_rules WHERE agent_id = ? AND rule_type = ? AND rule_value = ?",
                (agent_id, rule_type, rule_value),
            )
            await db.commit()

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
            await db.commit()

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
                # Ensure agent has domain_include:sensor so matcher passes domain filter
                await db.execute(
                    "INSERT OR IGNORE INTO entity_visibility_rules "
                    "(agent_id, rule_type, rule_value) VALUES (?, 'domain_include', 'sensor')",
                    (agent_id,),
                )
            await db.commit()


class PluginRepository:
    """CRUD for plugin metadata."""

    @staticmethod
    async def get(name: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM plugins WHERE name = ?", (name,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM plugins")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def upsert(name: str, file_path: str, **kwargs: Any) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO plugins (name, file_path, enabled, version, description, loaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET file_path=?, enabled=?, version=?, description=?, loaded_at=?",
                (
                    name,
                    file_path,
                    kwargs.get("enabled", 1),
                    kwargs.get("version"),
                    kwargs.get("description"),
                    _now(),
                    file_path,
                    kwargs.get("enabled", 1),
                    kwargs.get("version"),
                    kwargs.get("description"),
                    _now(),
                ),
            )
            await db.commit()


class ConversationRepository:
    """CRUD for conversation history."""

    @staticmethod
    async def insert(
        conversation_id: str,
        user_text: str,
        agent_id: str | None = None,
        response_text: str | None = None,
        action_executed: str | None = None,
        cache_hit: str | None = None,
        latency_ms: float | None = None,
    ) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO conversations "
                "(conversation_id, user_text, agent_id, response_text, "
                "action_executed, cache_hit, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, user_text, agent_id, response_text, action_executed, cache_hit, latency_ms),
            )
            await db.commit()
            return cursor.lastrowid

    @staticmethod
    async def list_recent(limit: int = 50) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM conversations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get_by_conversation_id(conversation_id: str) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM conversations WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def search(
        agent_id: str | None = None,
        search_text: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if search_text:
            conditions.append("(user_text LIKE ? OR response_text LIKE ?)")
            like = f"%{search_text}%"
            params.extend([like, like])
        if start_date:
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= ?")
            params.append(end_date)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * per_page
        params.extend([per_page, offset])

        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT * FROM conversations {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def count(
        agent_id: str | None = None,
        search_text: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if search_text:
            conditions.append("(user_text LIKE ? OR response_text LIKE ?)")
            like = f"%{search_text}%"
            params.extend([like, like])
        if start_date:
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= ?")
            params.append(end_date)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM conversations {where}",
                params,
            )
            row = await cursor.fetchone()
            return row[0]


class AnalyticsRepository:
    """CRUD for analytics events."""

    @staticmethod
    async def insert(event_type: str, agent_id: str | None = None, data: dict | None = None) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO analytics (event_type, agent_id, data) VALUES (?, ?, ?)",
                (event_type, agent_id, json.dumps(data) if data else None),
            )
            await db.commit()

    @staticmethod
    async def query_by_range(
        event_type: str | None = None, start: str | None = None, end: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if start:
            conditions.append("created_at >= ?")
            params.append(start)
        if end:
            conditions.append("created_at <= ?")
            params.append(end)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT * FROM analytics {where} ORDER BY created_at DESC LIMIT ?",
                params,
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("data"):
                    row["data"] = json.loads(row["data"])
            return rows


class EntityMatchingConfigRepository:
    """CRUD for entity matching configuration."""

    @staticmethod
    async def get(key: str) -> str | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT value FROM entity_matching_config WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    async def set(key: str, value: str, description: str | None = None) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO entity_matching_config (key, value, description, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
                (key, value, description, _now(), value, _now()),
            )
            await db.commit()

    @staticmethod
    async def get_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT key, value, description FROM entity_matching_config")
            return [dict(row) for row in await cursor.fetchall()]


class TraceSpanRepository:
    """CRUD for trace span data."""

    @staticmethod
    async def insert(
        trace_id: str,
        span_name: str,
        start_time: str,
        duration_ms: float,
        agent_id: str | None = None,
        parent_span: str | None = None,
        status: str = "ok",
        metadata: dict | None = None,
        end_time: str | None = None,
    ) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO trace_spans "
                "(trace_id, span_name, agent_id, parent_span, start_time, "
                "end_time, duration_ms, status, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace_id,
                    span_name,
                    agent_id,
                    parent_span,
                    start_time,
                    end_time,
                    duration_ms,
                    status,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    @staticmethod
    async def insert_batch(spans: list[dict[str, Any]]) -> None:
        async with get_db_write() as db:
            for span in spans:
                meta = dict(span.get("metadata") or {})
                if span.get("span_id"):
                    meta["span_id"] = span["span_id"]
                await db.execute(
                    "INSERT INTO trace_spans "
                    "(trace_id, span_name, agent_id, parent_span, start_time, "
                    "end_time, duration_ms, status, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        span["trace_id"],
                        span["span_name"],
                        span.get("agent_id"),
                        span.get("parent_span"),
                        span["start_time"],
                        span.get("end_time"),
                        span["duration_ms"],
                        span.get("status", "ok"),
                        json.dumps(meta) if meta else None,
                    ),
                )
            await db.commit()

    @staticmethod
    async def list_traces(page: int = 1, per_page: int = 50) -> list[dict[str, Any]]:
        offset = (page - 1) * per_page
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT trace_id, MIN(start_time) as start_time, "
                "COUNT(*) as span_count, "
                "SUM(duration_ms) as total_duration_ms, "
                "GROUP_CONCAT(DISTINCT agent_id) as agents "
                "FROM trace_spans GROUP BY trace_id "
                "ORDER BY start_time DESC LIMIT ? OFFSET ?",
                (per_page, offset),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get_trace_spans(trace_id: str) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM trace_spans WHERE trace_id = ? ORDER BY start_time",
                (trace_id,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("metadata"):
                    row["metadata"] = json.loads(row["metadata"])
            return rows

    @staticmethod
    async def count_traces() -> int:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT COUNT(DISTINCT trace_id) FROM trace_spans")
            row = await cursor.fetchone()
            return row[0]

    @staticmethod
    async def cleanup_old(days: int = 30) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM trace_spans WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            await db.commit()
            return cursor.rowcount


class TraceSummaryRepository:
    """CRUD for trace summary records."""

    @staticmethod
    async def create(data: dict[str, Any]) -> None:
        agents = data.get("agents")
        if isinstance(agents, (list, dict)):
            agents = json.dumps(agents)
        agent_instructions = data.get("agent_instructions")
        if isinstance(agent_instructions, (list, dict)):
            agent_instructions = json.dumps(agent_instructions)
        conversation_turns = data.get("conversation_turns")
        if isinstance(conversation_turns, list):
            conversation_turns = json.dumps(conversation_turns)
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO trace_summary "
                "(trace_id, conversation_id, user_input, final_response, "
                "agents, total_duration_ms, label, source, routing_agent, "
                "routing_confidence, routing_duration_ms, routing_reasoning, "
                "agent_instructions, conversation_turns, "
                "device_id, area_id, device_name, area_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data.get("trace_id"),
                    data.get("conversation_id"),
                    data.get("user_input"),
                    data.get("final_response"),
                    agents,
                    data.get("total_duration_ms"),
                    data.get("label"),
                    data.get("source"),
                    data.get("routing_agent"),
                    data.get("routing_confidence"),
                    data.get("routing_duration_ms"),
                    data.get("routing_reasoning"),
                    agent_instructions,
                    conversation_turns,
                    data.get("device_id"),
                    data.get("area_id"),
                    data.get("device_name"),
                    data.get("area_name"),
                ),
            )
            await db.commit()

    @staticmethod
    async def list_filtered(
        search: str | None = None,
        agent: str | None = None,
        label: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            conditions.append("user_input LIKE ?")
            params.append(f"%{search}%")
        if agent:
            conditions.append("routing_agent = ?")
            params.append(agent)
        if label:
            conditions.append("label = ?")
            params.append(label)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * per_page
        params.extend([per_page, offset])

        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT * FROM trace_summary {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("agents"):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        row["agents"] = json.loads(row["agents"])
                if row.get("agent_instructions"):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        row["agent_instructions"] = json.loads(row["agent_instructions"])
            return rows

    @staticmethod
    async def count_filtered(
        search: str | None = None,
        agent: str | None = None,
        label: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            conditions.append("user_input LIKE ?")
            params.append(f"%{search}%")
        if agent:
            conditions.append("routing_agent = ?")
            params.append(agent)
        if label:
            conditions.append("label = ?")
            params.append(label)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM trace_summary {where}",
                params,
            )
            row = await cursor.fetchone()
            return row[0]

    @staticmethod
    async def get(trace_id: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM trace_summary WHERE trace_id = ?", (trace_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            if result.get("agents"):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    result["agents"] = json.loads(result["agents"])
            if result.get("agent_instructions"):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    result["agent_instructions"] = json.loads(result["agent_instructions"])
            if result.get("conversation_turns"):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    result["conversation_turns"] = json.loads(result["conversation_turns"])
            return result

    @staticmethod
    async def update_label(trace_id: str, label: str | None) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE trace_summary SET label = ? WHERE trace_id = ?",
                (label, trace_id),
            )
            await db.commit()

    @staticmethod
    async def list_labels() -> list[str]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT DISTINCT label FROM trace_summary WHERE label IS NOT NULL AND label != '' ORDER BY label"
            )
            return [row[0] for row in await cursor.fetchall()]

    @staticmethod
    async def list_agents() -> list[str]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT DISTINCT routing_agent FROM trace_summary "
                "WHERE routing_agent IS NOT NULL AND routing_agent != '' "
                "ORDER BY routing_agent"
            )
            agents = [row[0] for row in await cursor.fetchall()]
            if "orchestrator" not in agents and agents:
                agents.insert(0, "orchestrator")
            return agents

    @staticmethod
    async def export_filtered(
        search: str | None = None,
        agent: str | None = None,
        label: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            conditions.append("user_input LIKE ?")
            params.append(f"%{search}%")
        if agent:
            conditions.append("routing_agent = ?")
            params.append(agent)
        if label:
            conditions.append("label = ?")
            params.append(label)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(10000)

        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT * FROM trace_summary {where} ORDER BY created_at DESC LIMIT ?",
                params,
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("agents"):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        row["agents"] = json.loads(row["agents"])
                if row.get("agent_instructions"):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        row["agent_instructions"] = json.loads(row["agent_instructions"])
            return rows

    @staticmethod
    async def cleanup_old(days: int = 30) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM trace_summary WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            await db.commit()
            return cursor.rowcount

    @staticmethod
    async def update_duration(trace_id: str, duration_ms: float) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE trace_summary SET total_duration_ms = ? WHERE trace_id = ?",
                (duration_ms, trace_id),
            )
            await db.commit()


def _normalize_device_name(name: str) -> str:
    """Normalize a device display name for fuzzy comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", name.lower())).strip()


class SendDeviceMappingRepository:
    """CRUD for send device name-to-service mappings."""

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        """Return all device mappings."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, created_at "
                "FROM send_device_mappings ORDER BY display_name"
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get(mapping_id: int) -> dict[str, Any] | None:
        """Get a single mapping by ID."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, created_at "
                "FROM send_device_mappings WHERE id = ?",
                (mapping_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def find_by_name(name: str) -> dict[str, Any] | None:
        """Find a mapping by display_name (case-insensitive, with normalized fallback)."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, created_at "
                "FROM send_device_mappings WHERE display_name = ? COLLATE NOCASE",
                (name.strip(),),
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            # Fallback: normalized comparison (handles apostrophes, hyphens, etc.)
            normalized_input = _normalize_device_name(name)
            if not normalized_input:
                return None
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, created_at FROM send_device_mappings"
            )
            for row in await cursor.fetchall():
                if _normalize_device_name(row["display_name"]) == normalized_input:
                    return dict(row)
            return None

    @staticmethod
    async def create(display_name: str, device_type: str, ha_service_target: str) -> int:
        """Insert a new mapping. Returns the new row ID."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO send_device_mappings (display_name, device_type, ha_service_target, created_at) "
                "VALUES (?, ?, ?, ?)",
                (display_name.strip(), device_type, ha_service_target, _now()),
            )
            await db.commit()
            return cursor.lastrowid

    @staticmethod
    async def update(mapping_id: int, **kwargs: Any) -> bool:
        """Update fields of an existing mapping. Returns True if row existed."""
        allowed = {"display_name", "device_type", "ha_service_target"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        set_clause = ", ".join(f"{_validate_column_name(k)} = ?" for k in fields)
        values = [*list(fields.values()), mapping_id]
        async with get_db_write() as db:
            cursor = await db.execute(
                f"UPDATE send_device_mappings SET {set_clause} WHERE id = ?",
                values,
            )
            await db.commit()
            return cursor.rowcount > 0

    @staticmethod
    async def delete(mapping_id: int) -> bool:
        """Delete a mapping by ID. Returns True if row existed."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM send_device_mappings WHERE id = ?",
                (mapping_id,),
            )
            await db.commit()
            return cursor.rowcount > 0


class ScheduledTimersRepository:
    """CRUD for the AgentHub-managed timer scheduler.

    Backs ``app.agents.timer_scheduler.TimerScheduler``. Rows survive
    container restart so pending timers are rehydrated on startup.
    """

    @staticmethod
    async def insert(
        *,
        id: str,
        logical_name: str,
        kind: str,
        created_at: int,
        fires_at: int,
        duration_seconds: int,
        origin_device_id: str | None,
        origin_area: str | None,
        briefing: bool = False,
        payload_json: str,
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO scheduled_timers "
                "(id, logical_name, kind, created_at, fires_at, duration_seconds, "
                "origin_device_id, origin_area, briefing, payload_json, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    id,
                    logical_name,
                    kind,
                    int(created_at),
                    int(fires_at),
                    int(duration_seconds),
                    origin_device_id,
                    origin_area,
                    1 if briefing else 0,
                    payload_json,
                ),
            )
            await db.commit()

    @staticmethod
    async def list_pending(*, kinds: set[str] | frozenset[str] | None = None) -> list[dict]:
        async with get_db_read() as db:
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                sql = (
                    "SELECT * FROM scheduled_timers WHERE state = 'pending' "
                    f"AND kind IN ({placeholders}) ORDER BY fires_at ASC, id ASC"
                )
                cursor = await db.execute(sql, tuple(sorted(kinds)))
            else:
                cursor = await db.execute(
                    "SELECT * FROM scheduled_timers WHERE state = 'pending' ORDER BY fires_at ASC, id ASC"
                )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def list_pending_for(
        *,
        logical_name: str | None = None,
        area: str | None = None,
        kinds: set[str] | frozenset[str] | None = None,
    ) -> list[dict]:
        clauses = ["state = 'pending'"]
        params: list[Any] = []
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(sorted(kinds))
        if logical_name is not None:
            clauses.append("LOWER(logical_name) = LOWER(?)")
            params.append(logical_name)
        if area is not None:
            clauses.append("origin_area = ?")
            params.append(area)
        sql = "SELECT * FROM scheduled_timers WHERE " + " AND ".join(clauses) + " ORDER BY fires_at ASC, id ASC"
        async with get_db_read() as db:
            cursor = await db.execute(sql, tuple(params))
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get(id_: str) -> dict | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM scheduled_timers WHERE id = ?", (id_,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def mark_fired(id_: str, fired_at: int) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE scheduled_timers SET state = 'fired', fired_at = ? WHERE id = ?",
                (int(fired_at), id_),
            )
            await db.commit()

    @staticmethod
    async def mark_cancelled(id_: str, cancelled_at: int) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE scheduled_timers SET state = 'cancelled', cancelled_at = ? WHERE id = ? AND state = 'pending'",
                (int(cancelled_at), id_),
            )
            await db.commit()

    @staticmethod
    async def cancel_by_logical_name(logical_name: str, cancelled_at: int) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "UPDATE scheduled_timers SET state = 'cancelled', cancelled_at = ? "
                "WHERE state = 'pending' AND LOWER(logical_name) = LOWER(?)",
                (int(cancelled_at), logical_name),
            )
            await db.commit()
            return cursor.rowcount

    @staticmethod
    async def purge_terminal_older_than(cutoff_epoch: int) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM scheduled_timers "
                "WHERE state IN ('fired', 'cancelled', 'expired') "
                "AND COALESCE(fired_at, cancelled_at, created_at) < ?",
                (int(cutoff_epoch),),
            )
            await db.commit()
            return cursor.rowcount

    @staticmethod
    async def update_scheduled_timer(
        id_: str,
        *,
        logical_name: str | None = None,
        fires_at: int | None = None,
        duration_seconds: int | None = None,
        briefing: bool | None = None,
        payload_json: str | None = None,
    ) -> bool:
        """Update mutable fields on a pending scheduled_timers row.

        Only rows with ``state = 'pending'`` are affected; already-fired or
        cancelled rows return ``False`` without touching the DB.

        Returns ``True`` if exactly one row was updated, ``False`` otherwise.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if logical_name is not None:
            clauses.append("logical_name = ?")
            params.append(logical_name)
        if fires_at is not None:
            clauses.append("fires_at = ?")
            params.append(int(fires_at))
        if duration_seconds is not None:
            clauses.append("duration_seconds = ?")
            params.append(int(duration_seconds))
        if briefing is not None:
            clauses.append("briefing = ?")
            params.append(1 if briefing else 0)
        if payload_json is not None:
            clauses.append("payload_json = ?")
            params.append(payload_json)
        if not clauses:
            return False
        params.append(id_)
        sql = "UPDATE scheduled_timers SET " + ", ".join(clauses) + " WHERE id = ? AND state = 'pending'"
        async with get_db_write() as db:
            cursor = await db.execute(sql, tuple(params))
            await db.commit()
            return cursor.rowcount > 0
