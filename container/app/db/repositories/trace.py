"""Trace span and trace summary CRUD."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from app.db.schema import get_db_read, get_db_write


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
            return cursor.lastrowid or 0

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
            assert row is not None
            return row[0]

    @staticmethod
    async def cleanup_old(days: int = 30) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM trace_spans WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
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
                "device_id, area_id, device_name, area_name, voice_followup) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    data.get("voice_followup"),
                ),
            )

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
                if row.get("conversation_turns"):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        row["conversation_turns"] = json.loads(row["conversation_turns"])
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
            assert row is not None
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
            return cursor.rowcount

    @staticmethod
    async def update_duration(trace_id: str, duration_ms: float) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE trace_summary SET total_duration_ms = ? WHERE trace_id = ?",
                (duration_ms, trace_id),
            )
