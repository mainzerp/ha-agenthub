"""Shared Home Assistant Recorder history execution for domain agents."""

from __future__ import annotations

import logging
from typing import Any

from app.ha_client.history_util import (
    display_zone_for_context,
    parse_history_window,
    summarize_history_for_speech,
)
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)


async def execute_recorder_history_query(
    entity_id: str,
    friendly_name: str,
    parameters: dict[str, Any],
    ha_client: Any,
    *,
    allowed_domains: frozenset[str],
    task_context: TaskContext | None = None,
) -> dict[str, Any]:
    """Load Recorder state changes for a resolved entity and return speech.

    Callers must resolve ``entity_id`` / ``friendly_name`` (including visibility
    and domain policy) before invoking this helper.
    """
    dom = entity_id.split(".")[0] if "." in entity_id else ""
    if dom not in allowed_domains:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Recorder history is not available for this entity type ({dom}).",
            "cacheable": False,
        }

    start_utc, end_utc = parse_history_window(parameters, task_context)
    if start_utc is None or end_utc is None:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": (
                "Invalid or too long history window. Use period: yesterday, last_24_hours, or last_7_days, "
                "or start/end as ISO-8601 times, spanning at most 7 days."
            ),
            "cacheable": False,
        }

    if not hasattr(ha_client, "get_history_period"):
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": "History is not available from this Home Assistant client.",
            "cacheable": False,
        }

    try:
        history = await ha_client.get_history_period(
            start_utc,
            entity_id=entity_id,
            end_time_utc=end_utc,
            significant_changes_only=True,
            minimal_response=True,
        )
    except Exception:
        logger.error("Recorder history failed for %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": "Could not load recorder history. Check the server logs for details.",
            "cacheable": False,
        }

    speech = summarize_history_for_speech(
        entity_id,
        friendly_name,
        history,
        display_tz=display_zone_for_context(task_context),
    )
    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": None,
        "speech": speech,
        "cacheable": False,
    }
