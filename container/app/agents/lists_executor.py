"""Lists-specific action execution.

Dispatches todo list read/write actions via HA REST API.

- Visibility is always applied (``lists-agent`` rules when no agent id is
  passed); the target must be a ``todo.*`` entity.
- Without a named list, the only visible list is used; several visible
  lists produce a clarifying question instead of a silent first pick.
- Item matching prefers a normalized exact match, then a unique substring
  match; several matches produce a clarifying question.
- Every result is ``cacheable=False``: list writes must never be replayed
  from the action cache.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.visibility import filter_visible_results

logger = logging.getLogger(__name__)

_TODO_DOMAINS: frozenset[str] = frozenset({"todo"})
_DEFAULT_AGENT_ID = "lists-agent"
_MAX_LISTED_CHOICES = 4

# Entity-candidate declaration (read by ``ListsAgent``'s ``@agent`` call, see
# ``ActionableAgent._entity_actions``). The executor resolves the target list
# itself (visible ``todo.*`` only): ``list_lists`` needs no list and an empty
# ``entity`` uses the only visible list or asks which one, so no action needs
# a recalled entity candidate. The dispatch only accepts these actions.
ENTITY_ACTIONS: frozenset[str] = frozenset()
ENTITY_FREE_ACTIONS: frozenset[str] = frozenset(
    {"list_lists", "list_items", "add_item", "complete_item", "remove_item", "clear_completed"}
)


def _result(success: bool, speech: str, entity_id: str | None = None, **extra: Any) -> dict:
    result: dict[str, Any] = {
        "success": success,
        "entity_id": entity_id,
        "new_state": None,
        "speech": speech,
        "cacheable": False,
    }
    result.update(extra)
    return result


def _choice_result(speech: str, path: str, entity_id: str | None = None) -> dict:
    """Clarifying question: requests a voice follow-up and is never rewritten as 'not found'."""
    return _result(False, speech, entity_id, voice_followup=True, metadata={"resolution_path": path})


def _join_choices(names: list[str]) -> str:
    shown = names[:_MAX_LISTED_CHOICES]
    if len(shown) == 1:
        return shown[0]
    text = ", ".join(shown[:-1]) + f" or {shown[-1]}"
    if len(names) > len(shown):
        text += f" (and {len(names) - len(shown)} more)"
    return text


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
) -> dict:
    """Dispatch a parsed lists action."""
    action_name = action.get("action", "").lower()
    agent_id = agent_id or _DEFAULT_AGENT_ID

    if action_name not in ENTITY_FREE_ACTIONS:
        return _result(False, f"Unknown lists action: {action_name}")
    if action_name == "list_lists":
        return await _list_lists(entity_index, entity_matcher, agent_id)
    if action_name == "list_items":
        return await _list_items(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if action_name == "add_item":
        return await _add_item(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if action_name == "complete_item":
        return await _complete_item(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if action_name == "remove_item":
        return await _remove_item(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if action_name == "clear_completed":
        return await _clear_completed(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)

    return _result(False, f"Unknown lists action: {action_name}")


async def _visible_todo_entries(entity_index: Any, agent_id: str | None) -> list[Any]:
    """Visible todo entities for the agent (fail closed on errors)."""
    entries: list[Any] = []
    if entity_index:
        try:
            if hasattr(entity_index, "list_entries_async"):
                entries = list(await entity_index.list_entries_async(domains=_TODO_DOMAINS))
            elif hasattr(entity_index, "list_entries"):
                entries = list(entity_index.list_entries(domains=_TODO_DOMAINS))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Listing todo entities failed", exc_info=True)
            return []
    entries = [e for e in entries if str(getattr(e, "entity_id", "")).startswith("todo.")]
    if not entries:
        return []
    try:
        return list(await filter_visible_results(agent_id or _DEFAULT_AGENT_ID, entries, entity_index))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Todo visibility filtering failed; hiding all lists", exc_info=True)
        return []


async def _resolve_todo_entity(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve target todo entity. Returns (entity_id, friendly_name, speech_error)."""
    target = await _resolve_todo_target(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if isinstance(target, dict):
        return None, None, target["speech"]
    return target[0], target[1], None


async def _resolve_todo_target(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
) -> tuple[str, str] | dict:
    """Resolve the target list. Returns ``(entity_id, friendly_name)`` or a ready result dict."""
    entity_query = action.get("entity", "")
    params = action.get("parameters") or {}
    explicit_list = str(params.get("list") or "").strip()
    if explicit_list:
        entity_query = explicit_list
    entity_query = str(entity_query or "").strip()

    if not entity_query:
        # No configured default list exists: use the only visible list,
        # ask when several are visible.
        entries = await _visible_todo_entries(entity_index, agent_id)
        if not entries:
            return _result(False, "No todo list is available.")
        if len(entries) > 1:
            names = [str(getattr(e, "friendly_name", "") or getattr(e, "entity_id", "")) for e in entries]
            return _choice_result(f"Which list do you mean: {_join_choices(names)}?", "list_ambiguous")
        first = entries[0]
        entity_id = str(getattr(first, "entity_id", ""))
        return entity_id, str(getattr(first, "friendly_name", "") or entity_id)

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
                    agent_id or _DEFAULT_AGENT_ID,
                    allowed_domains=_TODO_DOMAINS,
                )
                em_span["metadata"] = resolution["metadata"]
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    if entity_id and not str(entity_id).startswith("todo."):
        logger.warning("Resolved entity %s is not a todo list; rejecting", entity_id)
        entity_id = None
    if not entity_id:
        return _result(False, resolution["speech"] or f"Could not find a todo list matching '{entity_query}'.")
    return str(entity_id), str(friendly_name or entity_id)


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
    except asyncio.CancelledError:
        raise
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


def _normalize(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _find_items_by_query(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Find todo items matching the query.

    Normalized exact summary matches win; otherwise items whose summary
    contains the query. The reverse direction (summary inside the query)
    is not matched, so "oat milk" never selects "Milk".
    """
    query_norm = _normalize(query)
    if not query_norm:
        return []
    exact = [item for item in items if _normalize(item.get("summary")) == query_norm]
    if exact:
        return exact
    return [item for item in items if query_norm in _normalize(item.get("summary"))]


def _format_item(item: dict[str, Any]) -> str:
    """Format a single todo item for speech."""
    summary = item.get("summary", "unknown")
    status = item.get("status", "needs_action")
    if status == "completed":
        return f"{summary} (done)"
    return summary


def _ambiguity_question(query: str, matches: list[dict[str, Any]]) -> str:
    names = [str(m.get("summary", "")) for m in matches]
    return f"Which '{query}' do you mean: {_join_choices(names)}?"


async def _list_lists(entity_index: Any, entity_matcher: Any, agent_id: str | None) -> dict:
    """List all available todo lists."""
    entries = await _visible_todo_entries(entity_index, agent_id)

    if not entries:
        return _result(True, "No todo lists are available.")

    lines = []
    for entry in entries:
        fn = getattr(entry, "friendly_name", None) or getattr(entry, "entity_id", "unknown")
        lines.append(str(fn))

    return _result(
        True,
        "Available lists: " + ", ".join(lines) + ".",
        metadata={
            "lists": [
                {
                    "entity_id": getattr(e, "entity_id", ""),
                    "friendly_name": getattr(e, "friendly_name", ""),
                }
                for e in entries
            ]
        },
    )


async def _list_items(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
) -> dict:
    """List items in a specific todo list."""
    target = await _resolve_todo_target(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if isinstance(target, dict):
        return target
    entity_id, friendly_name = target

    items = await _get_todo_items(ha_client, entity_id)
    if not items:
        return _result(True, f"{friendly_name} is empty.", entity_id)

    lines = [_format_item(item) for item in items]
    return _result(
        True,
        f"Items in {friendly_name}: " + "; ".join(lines) + ".",
        entity_id,
        metadata={"items": items},
    )


async def _add_item(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
) -> dict:
    """Add item(s) to a todo list."""
    params = action.get("parameters") or {}
    item_text = str(params.get("item") or "").strip()
    if not item_text:
        return _result(False, "Please specify what to add.")

    target = await _resolve_todo_target(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if isinstance(target, dict):
        return target
    entity_id, friendly_name = target

    # Support multiple items separated by commas
    items = [s.strip() for s in item_text.split(",") if s.strip()]
    added = []
    failed = []
    for it in items:
        try:
            await ha_client.call_service("todo", "add_item", entity_id, {"item": it})
            added.append(it)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("todo.add_item failed for %s: %s", entity_id, exc)
            failed.append(it)

    if failed and not added:
        return _result(False, f"Failed to add items to {friendly_name}.", entity_id)

    parts = []
    if added:
        parts.append(f"Added {', '.join(added)} to {friendly_name}.")
    if failed:
        parts.append(f"Could not add {', '.join(failed)}.")

    return _result(bool(added), " ".join(parts), entity_id)


async def _call_item_service(
    ha_client: Any,
    service: str,
    entity_id: str,
    item: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> bool:
    """Call a per-item todo service by uid, falling back to the summary."""
    identifiers = [i for i in (item.get("uid"), item.get("summary")) if i]
    for identifier in dict.fromkeys(identifiers):
        try:
            await ha_client.call_service("todo", service, entity_id, {"item": identifier, **(extra or {})})
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("todo.%s failed for %s (%s): %s", service, entity_id, identifier, exc)
    return False


def _finish_item_result(
    entity_id: str,
    done_phrase: str,
    done: list[str],
    failed: list[str],
    not_found: list[str],
    questions: list[str],
    failed_phrase: str,
    friendly_name: str,
    extra_notes: list[str] | None = None,
) -> dict:
    """Merge per-item outcomes; ``done_phrase`` is e.g. "Completed {items} in"."""
    parts = []
    if done:
        parts.append(f"{done_phrase.format(items=', '.join(done))} {friendly_name}.")
    if failed:
        parts.append(f"Could not {failed_phrase} {', '.join(failed)}.")
    if not_found:
        if done or questions:
            parts.append(f"Could not find {', '.join(not_found)}.")
        else:
            parts.append(f"Could not find '{', '.join(not_found)}' in {friendly_name}.")
    parts.extend(extra_notes or [])
    parts.extend(questions)
    speech = " ".join(parts)
    if questions:
        # A clarifying question is pending: request the follow-up turn.
        return _result(
            bool(done),
            speech,
            entity_id,
            voice_followup=True,
            metadata={"resolution_path": "item_ambiguous"},
        )
    return _result(bool(done), speech, entity_id)


async def _complete_item(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
) -> dict:
    """Mark item(s) as completed in a todo list."""
    params = action.get("parameters") or {}
    item_text = str(params.get("item") or "").strip()
    if not item_text:
        return _result(False, "Please specify which item to complete.")

    target = await _resolve_todo_target(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if isinstance(target, dict):
        return target
    entity_id, friendly_name = target

    items = await _get_todo_items(ha_client, entity_id)
    open_items = [item for item in items if item.get("status") != "completed"]

    # Support multiple items separated by commas
    queries = [s.strip() for s in item_text.split(",") if s.strip()]
    completed: list[str] = []
    failed: list[str] = []
    not_found: list[str] = []
    questions: list[str] = []
    notes: list[str] = []

    for query in queries:
        matches = _find_items_by_query(open_items, query)
        if not matches:
            if _find_items_by_query(items, query):
                notes.append(f"{query} is already done.")
            else:
                not_found.append(query)
            continue
        if len(matches) > 1:
            questions.append(_ambiguity_question(query, matches))
            continue
        target_item = matches[0]
        if await _call_item_service(ha_client, "update_item", entity_id, target_item, {"status": "completed"}):
            completed.append(str(target_item.get("summary", query)))
        else:
            failed.append(query)

    return _finish_item_result(
        entity_id,
        "Completed {items} in",
        completed,
        failed,
        not_found,
        questions,
        "complete",
        friendly_name,
        notes,
    )


async def _remove_item(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
) -> dict:
    """Remove item(s) from a todo list."""
    params = action.get("parameters") or {}
    item_text = str(params.get("item") or "").strip()
    if not item_text:
        return _result(False, "Please specify which item to remove.")

    target = await _resolve_todo_target(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if isinstance(target, dict):
        return target
    entity_id, friendly_name = target

    items = await _get_todo_items(ha_client, entity_id)

    # Support multiple items separated by commas
    queries = [s.strip() for s in item_text.split(",") if s.strip()]
    removed: list[str] = []
    failed: list[str] = []
    not_found: list[str] = []
    questions: list[str] = []

    for query in queries:
        matches = _find_items_by_query(items, query)
        if not matches:
            not_found.append(query)
            continue
        if len(matches) > 1:
            questions.append(_ambiguity_question(query, matches))
            continue
        target_item = matches[0]
        if await _call_item_service(ha_client, "remove_item", entity_id, target_item):
            removed.append(str(target_item.get("summary", query)))
        else:
            failed.append(query)

    return _finish_item_result(
        entity_id,
        "Removed {items} from",
        removed,
        failed,
        not_found,
        questions,
        "remove",
        friendly_name,
    )


async def _clear_completed(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
) -> dict:
    """Remove all completed items from a todo list."""
    target = await _resolve_todo_target(action, ha_client, entity_index, entity_matcher, agent_id, span_collector)
    if isinstance(target, dict):
        return target
    entity_id, friendly_name = target

    items = await _get_todo_items(ha_client, entity_id)
    completed_items = [item for item in items if item.get("status") == "completed"]

    if not completed_items:
        return _result(True, f"No completed items in {friendly_name}.", entity_id)

    removed = []
    failed = []
    for item in completed_items:
        if await _call_item_service(ha_client, "remove_item", entity_id, item):
            removed.append(item.get("summary", ""))
        else:
            failed.append(item.get("summary", ""))

    parts = []
    if removed:
        parts.append(f"Cleared {len(removed)} completed item(s) from {friendly_name}.")
    if failed:
        parts.append(f"Could not remove {len(failed)} item(s).")

    return _result(bool(removed), " ".join(parts), entity_id)
