"""Lists-specific action execution.

Dispatches todo list read/write actions via HA REST API.
"""

from __future__ import annotations

import logging
from typing import Any

from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.visibility import filter_visible_results

logger = logging.getLogger(__name__)

_TODO_DOMAINS: frozenset[str] = frozenset({"todo"})


async def execute_lists_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    device_id: str | None = None,
    area_id: str | None = None,
    language: str | None = None,
    timezone: str | None = None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Dispatch a parsed lists action."""
    action_name = action.get("action", "").lower()

    if action_name == "list_lists":
        return await _list_lists(entity_index, entity_matcher, agent_id)
    if action_name == "list_items":
        return await _list_items(
            action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
        )
    if action_name == "add_item":
        return await _add_item(
            action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
        )
    if action_name == "complete_item":
        return await _complete_item(
            action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
        )
    if action_name == "remove_item":
        return await _remove_item(
            action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
        )
    if action_name == "clear_completed":
        return await _clear_completed(
            action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
        )

    return {
        "success": False,
        "entity_id": None,
        "new_state": None,
        "speech": f"Unknown lists action: {action_name}",
    }


async def _resolve_todo_entity(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve target todo entity. Returns (entity_id, friendly_name, speech_error)."""
    entity_query = action.get("entity", "")
    params = action.get("parameters") or {}
    explicit_list = str(params.get("list") or "").strip()
    if explicit_list:
        entity_query = explicit_list

    if not entity_query:
        # Try to find any visible todo entity
        entries = []
        if entity_index:
            if hasattr(entity_index, "list_entries_async"):
                entries = await entity_index.list_entries_async(domains=_TODO_DOMAINS)
            elif hasattr(entity_index, "list_entries"):
                entries = entity_index.list_entries(domains=_TODO_DOMAINS)
        if agent_id and entries:
            entries = await filter_visible_results(agent_id, entries, entity_index)
        if entries:
            first = entries[0]
            return str(getattr(first, "entity_id", "")), str(getattr(first, "friendly_name", "")), None
        return None, None, "No todo list is available."

    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_index or entity_matcher:
            from app.analytics.tracer import _optional_span

            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_TODO_DOMAINS,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    if not entity_id:
        return None, None, resolution["speech"] or f"Could not find a todo list matching '{entity_query}'."
    return entity_id, friendly_name, None


async def _get_todo_items(ha_client: Any, entity_id: str) -> list[dict[str, Any]]:
    """Fetch items from a todo entity via todo.get_items with return_response."""
    try:
        result = await ha_client.call_service(
            "todo",
            "get_items",
            entity_id,
            {},
            return_response=True,
        )
    except Exception as exc:
        logger.warning("todo.get_items failed for %s: %s", entity_id, exc)
        return []

    if not isinstance(result, dict):
        return []

    # HA returns response keyed by entity_id
    entry = result.get(entity_id) or result.get("response", {}).get(entity_id)
    if isinstance(entry, dict):
        items = entry.get("items", [])
        if isinstance(items, list):
            return items

    # Fallback: try to find items anywhere in the response
    for value in result.values():
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return value["items"]
        if isinstance(value, list):
            return value

    return []


def _find_items_by_query(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Find todo items whose summary matches the query (case-insensitive, substring)."""
    query_lower = query.lower().strip()
    if not query_lower:
        return []
    matches = []
    for item in items:
        summary = str(item.get("summary", "")).lower()
        if query_lower in summary or summary in query_lower:
            matches.append(item)
    return matches


def _format_item(item: dict[str, Any]) -> str:
    """Format a single todo item for speech."""
    summary = item.get("summary", "unknown")
    status = item.get("status", "needs_action")
    if status == "completed":
        return f"{summary} (done)"
    return summary


async def _list_lists(entity_index: Any, entity_matcher: Any, agent_id: str | None) -> dict:
    """List all available todo lists."""
    entries = []
    if entity_index:
        if hasattr(entity_index, "list_entries_async"):
            entries = await entity_index.list_entries_async(domains=_TODO_DOMAINS)
        elif hasattr(entity_index, "list_entries"):
            entries = entity_index.list_entries(domains=_TODO_DOMAINS)

    if agent_id and entries:
        entries = await filter_visible_results(agent_id, entries, entity_index)

    if not entries:
        return {
            "success": True,
            "entity_id": None,
            "new_state": None,
            "speech": "No todo lists are available.",
            "cacheable": False,
        }

    lines = []
    for entry in entries:
        fn = getattr(entry, "friendly_name", None) or getattr(entry, "entity_id", "unknown")
        lines.append(str(fn))

    return {
        "success": True,
        "entity_id": None,
        "new_state": None,
        "speech": "Available lists: " + ", ".join(lines) + ".",
        "cacheable": False,
        "metadata": {
            "lists": [
                {
                    "entity_id": getattr(e, "entity_id", ""),
                    "friendly_name": getattr(e, "friendly_name", ""),
                }
                for e in entries
            ]
        },
    }


async def _list_items(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """List items in a specific todo list."""
    entity_id, friendly_name, error = await _resolve_todo_entity(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error, "cacheable": False}
    assert entity_id is not None

    items = await _get_todo_items(ha_client, entity_id)
    if not items:
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"{friendly_name} is empty.",
            "cacheable": False,
        }

    lines = [_format_item(item) for item in items]
    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f"Items in {friendly_name}: " + "; ".join(lines) + ".",
        "cacheable": False,
        "metadata": {"items": items},
    }


async def _add_item(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Add item(s) to a todo list."""
    params = action.get("parameters") or {}
    item_text = str(params.get("item") or "").strip()
    if not item_text:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Please specify what to add.",
        }

    entity_id, friendly_name, error = await _resolve_todo_entity(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}

    # Support multiple items separated by commas
    items = [s.strip() for s in item_text.split(",") if s.strip()]
    added = []
    failed = []
    for it in items:
        try:
            await ha_client.call_service("todo", "add_item", entity_id, {"item": it})
            added.append(it)
        except Exception as exc:
            logger.warning("todo.add_item failed for %s: %s", entity_id, exc)
            failed.append(it)

    if failed and not added:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to add items to {friendly_name}.",
        }

    parts = []
    if added:
        parts.append(f"Added {', '.join(added)} to {friendly_name}.")
    if failed:
        parts.append(f"Could not add {', '.join(failed)}.")

    return {
        "success": bool(added),
        "entity_id": entity_id,
        "new_state": None,
        "speech": " ".join(parts),
    }


async def _complete_item(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Mark item(s) as completed in a todo list."""
    params = action.get("parameters") or {}
    item_text = str(params.get("item") or "").strip()
    if not item_text:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Please specify which item to complete.",
        }

    entity_id, friendly_name, error = await _resolve_todo_entity(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}
    assert entity_id is not None

    items = await _get_todo_items(ha_client, entity_id)

    # Support multiple items separated by commas
    queries = [s.strip() for s in item_text.split(",") if s.strip()]
    completed = []
    failed = []
    not_found = []

    for query in queries:
        matches = _find_items_by_query(items, query)
        if not matches:
            not_found.append(query)
            continue
        if len(matches) > 1:
            # If ambiguous, prefer an incomplete item
            incomplete = [m for m in matches if m.get("status") != "completed"]
            target = incomplete[0] if incomplete else matches[0]
        else:
            target = matches[0]

        target_uid = target.get("uid") or target.get("summary", query)
        try:
            await ha_client.call_service(
                "todo",
                "update_item",
                entity_id,
                {"item": target_uid, "status": "completed"},
            )
            completed.append(target.get("summary", query))
        except Exception as exc:
            logger.warning("todo.update_item failed for %s: %s", entity_id, exc)
            # Fallback: try by summary if uid failed
            try:
                await ha_client.call_service(
                    "todo",
                    "update_item",
                    entity_id,
                    {"item": target.get("summary", query), "status": "completed"},
                )
                completed.append(target.get("summary", query))
            except Exception:
                failed.append(query)

    if not completed and not_found:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Could not find '{', '.join(not_found)}' in {friendly_name}.",
        }

    parts = []
    if completed:
        parts.append(f"Completed {', '.join(completed)} in {friendly_name}.")
    if failed:
        parts.append(f"Could not complete {', '.join(failed)}.")
    if not_found and completed:
        parts.append(f"Could not find {', '.join(not_found)}.")

    return {
        "success": bool(completed),
        "entity_id": entity_id,
        "new_state": None,
        "speech": " ".join(parts),
    }


async def _remove_item(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Remove item(s) from a todo list."""
    params = action.get("parameters") or {}
    item_text = str(params.get("item") or "").strip()
    if not item_text:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Please specify which item to remove.",
        }

    entity_id, friendly_name, error = await _resolve_todo_entity(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}
    assert entity_id is not None

    items = await _get_todo_items(ha_client, entity_id)

    # Support multiple items separated by commas
    queries = [s.strip() for s in item_text.split(",") if s.strip()]
    removed = []
    failed = []
    not_found = []

    for query in queries:
        matches = _find_items_by_query(items, query)
        if not matches:
            not_found.append(query)
            continue
        if len(matches) > 1:
            not_found.append(f"{query} (ambiguous)")
            continue

        target = matches[0]
        target_uid = target.get("uid") or target.get("summary", query)
        try:
            await ha_client.call_service(
                "todo",
                "remove_item",
                entity_id,
                {"item": target_uid},
            )
            removed.append(target.get("summary", query))
        except Exception as exc:
            logger.warning("todo.remove_item failed for %s: %s", entity_id, exc)
            # Fallback: try by summary
            try:
                await ha_client.call_service(
                    "todo",
                    "remove_item",
                    entity_id,
                    {"item": target.get("summary", query)},
                )
                removed.append(target.get("summary", query))
            except Exception:
                failed.append(query)

    if not removed and not_found:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Could not find '{', '.join(not_found)}' in {friendly_name}.",
        }

    parts = []
    if removed:
        parts.append(f"Removed {', '.join(removed)} from {friendly_name}.")
    if failed:
        parts.append(f"Could not remove {', '.join(failed)}.")
    if not_found and removed:
        parts.append(f"Could not find {', '.join(not_found)}.")

    return {
        "success": bool(removed),
        "entity_id": entity_id,
        "new_state": None,
        "speech": " ".join(parts),
    }


async def _clear_completed(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Remove all completed items from a todo list."""
    entity_id, friendly_name, error = await _resolve_todo_entity(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, verbatim_terms=verbatim_terms
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}
    assert entity_id is not None

    items = await _get_todo_items(ha_client, entity_id)
    completed_items = [item for item in items if item.get("status") == "completed"]

    if not completed_items:
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"No completed items in {friendly_name}.",
        }

    removed = []
    failed = []
    for item in completed_items:
        target_uid = item.get("uid") or item.get("summary", "")
        try:
            await ha_client.call_service(
                "todo",
                "remove_item",
                entity_id,
                {"item": target_uid},
            )
            removed.append(item.get("summary", ""))
        except Exception as exc:
            logger.warning("todo.remove_item failed for %s: %s", entity_id, exc)
            try:
                await ha_client.call_service(
                    "todo",
                    "remove_item",
                    entity_id,
                    {"item": item.get("summary", "")},
                )
                removed.append(item.get("summary", ""))
            except Exception:
                failed.append(item.get("summary", ""))

    parts = []
    if removed:
        parts.append(f"Cleared {len(removed)} completed item(s) from {friendly_name}.")
    if failed:
        parts.append(f"Could not remove {len(failed)} item(s).")

    return {
        "success": bool(removed),
        "entity_id": entity_id,
        "new_state": None,
        "speech": " ".join(parts),
    }
