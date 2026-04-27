"""Wake briefing composition for internal scheduler alarms."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.a2a.orchestrator_gateway import OrchestratorGateway
from app.db.repository import AgentConfigRepository, SettingsRepository
from app.entity.matcher import MatchResult
from app.entity.visibility import filter_visible_results
from app.llm.client import complete
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

_WAKE_BRIEFING_AGENT_ID = "wake-briefing-composer"


def _fallback_alarm_message(alarm_payload: dict[str, Any]) -> str:
    alarm_label = str(alarm_payload.get("alarm_label") or alarm_payload.get("alarm_name") or "alarm").strip() or "alarm"
    return f"Alarm '{alarm_label}' has triggered."


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _get_setting(settings_repo: Any, key: str, default: str) -> str:
    value = await settings_repo.get_value(key, default)
    return default if value is None else str(value)


async def _get_setting_bool(settings_repo: Any, key: str, default: bool) -> bool:
    raw = await settings_repo.get_value(key, "true" if default else "false")
    return _coerce_bool(raw, default=default)


async def _get_setting_int(settings_repo: Any, key: str, default: int) -> int:
    raw = await settings_repo.get_value(key, str(default))
    return _coerce_int(raw, default=default)


async def _get_setting_json_list(settings_repo: Any, key: str, default: list[str]) -> list[str]:
    raw = await settings_repo.get_value(key, json.dumps(default))
    if raw is None:
        return list(default)
    try:
        value = json.loads(raw)
    except Exception:
        return list(default)
    if not isinstance(value, list):
        return list(default)
    return [str(item).strip() for item in value if str(item).strip()]


def _resolve_timezone(timezone_name: str | None) -> ZoneInfo | UTC:
    if timezone_name:
        try:
            return ZoneInfo(str(timezone_name))
        except Exception:
            logger.debug("Invalid wake briefing timezone %r; falling back to UTC", timezone_name, exc_info=True)
    return UTC


def _build_news_request(news_query: str, news_count: int) -> str:
    cleaned = (news_query or "").strip() or "top news today"
    bounded_count = max(1, min(10, int(news_count)))
    suffix = f"Return {bounded_count} concise headline{'s' if bounded_count != 1 else ''}."
    if cleaned.endswith((".", "!", "?")):
        return f"{cleaned} {suffix}"
    return f"{cleaned}. {suffix}"


def _build_task_context(alarm_payload: dict[str, Any], *, language: str, timezone_name: str) -> TaskContext:
    return TaskContext(
        source="background",
        language=language or "en",
        timezone=timezone_name or "UTC",
        device_id=alarm_payload.get("origin_device_id"),
        area_id=alarm_payload.get("origin_area"),
    )


async def _dispatch_agent_source(
    gateway: OrchestratorGateway,
    *,
    description: str,
    user_text: str,
    conversation_id: str,
    context: TaskContext,
) -> str | None:
    result = await gateway.dispatch_text(
        description,
        user_text=user_text,
        conversation_id=conversation_id,
        context=context,
    )
    speech = (result.get("speech") or "").strip() if isinstance(result, dict) else ""
    return speech or None


async def _date_facts(alarm_payload: dict[str, Any], tzinfo: ZoneInfo | UTC) -> dict[str, str]:
    scheduled_for_epoch = _coerce_int(alarm_payload.get("scheduled_for_epoch"), default=int(time.time()))
    scheduled_for = datetime.fromtimestamp(scheduled_for_epoch, tz=tzinfo)
    return {
        "date": scheduled_for.date().isoformat(),
        "weekday": scheduled_for.strftime("%A"),
        "time": scheduled_for.strftime("%H:%M"),
    }


async def _calendar_facts(
    ha_client: Any,
    entity_index: Any,
    *,
    start: datetime,
    end: datetime,
) -> list[dict[str, str]]:
    if entity_index is None:
        return []

    list_entries_async = getattr(entity_index, "list_entries_async", None)
    list_entries = getattr(entity_index, "list_entries", None)
    if callable(list_entries_async):
        entries = await list_entries_async(domains={"calendar"})
    elif callable(list_entries):
        entries = list_entries(domains={"calendar"})
    else:
        return []

    visible = await filter_visible_results(
        _WAKE_BRIEFING_AGENT_ID,
        [
            MatchResult(
                entity_id=entry.entity_id,
                friendly_name=entry.friendly_name or entry.entity_id,
                score=1.0,
            )
            for entry in entries
            if getattr(entry, "entity_id", "").startswith("calendar.")
        ],
        entity_index,
    )
    visible_ids = sorted({result.entity_id for result in visible}, key=str.casefold)

    collected: list[dict[str, str]] = []
    for entity_id in visible_ids:
        events = await ha_client.get_calendar_events(entity_id, start.isoformat(), end.isoformat())
        for event in events:
            if not isinstance(event, dict):
                continue
            summary = str(event.get("summary") or "").strip()
            start_text = str(event.get("start") or event.get("start_date_time") or "").strip()
            if not summary and not start_text:
                continue
            collected.append(
                {
                    "entity_id": entity_id,
                    "summary": summary,
                    "start": start_text,
                }
            )
    return collected


async def _sensor_facts(
    ha_client: Any,
    entity_index: Any,
    sensor_entities: list[str],
) -> list[dict[str, str]]:
    if not sensor_entities:
        return []

    visible = await filter_visible_results(
        _WAKE_BRIEFING_AGENT_ID,
        [MatchResult(entity_id=entity_id, friendly_name=entity_id, score=1.0) for entity_id in sensor_entities],
        entity_index,
    )

    collected: list[dict[str, str]] = []
    for result in sorted(visible, key=lambda item: item.entity_id.casefold()):
        state = await ha_client.get_state(result.entity_id)
        if not isinstance(state, dict):
            continue
        state_text = str(state.get("state") or "").strip()
        if not state_text or state_text in {"unknown", "unavailable"}:
            continue
        attrs = state.get("attributes") or {}
        collected.append(
            {
                "entity_id": result.entity_id,
                "friendly_name": str(attrs.get("friendly_name") or result.entity_id),
                "state": state_text,
                "unit": str(attrs.get("unit_of_measurement") or "").strip(),
            }
        )
    return collected


async def _compose_wake_briefing_inner(
    gateway: OrchestratorGateway,
    alarm_payload: dict[str, Any],
    *,
    ha_client: Any,
    entity_index: Any,
    settings_repo: Any,
) -> str:
    if not await _get_setting_bool(settings_repo, "wake_briefing.enabled", True):
        raise RuntimeError("Wake briefing is disabled")

    timeout_seconds = max(1, await _get_setting_int(settings_repo, "wake_briefing.timeout_seconds", 10))
    language = str(alarm_payload.get("language") or "en").strip() or "en"
    timezone_name = str(alarm_payload.get("timezone") or "UTC").strip() or "UTC"
    tzinfo = _resolve_timezone(timezone_name)
    context = _build_task_context(alarm_payload, language=language, timezone_name=timezone_name)
    conversation_seed = f"wake-briefing-{uuid.uuid4().hex}"
    start = datetime.now(tzinfo)
    end = start + timedelta(hours=24)

    async def _build_facts() -> dict[str, Any]:
        keys: list[str] = []
        tasks: list[Any] = []

        if await _get_setting_bool(settings_repo, "wake_briefing.sources.date", True):
            keys.append("date_fields")
            tasks.append(asyncio.wait_for(_date_facts(alarm_payload, tzinfo), timeout=1.0))

        if await _get_setting_bool(settings_repo, "wake_briefing.sources.weather", True):
            keys.append("weather")
            tasks.append(
                asyncio.wait_for(
                    _dispatch_agent_source(
                        gateway,
                        description="weather today and short forecast",
                        user_text="weather today and short forecast",
                        conversation_id=f"{conversation_seed}-weather",
                        context=context,
                    ),
                    timeout=3.0,
                )
            )

        if await _get_setting_bool(settings_repo, "wake_briefing.sources.news", True):
            news_query = await _get_setting(settings_repo, "wake_briefing.news_query", "top news today")
            news_count = await _get_setting_int(settings_repo, "wake_briefing.news_count", 3)
            news_request = _build_news_request(news_query, news_count)
            keys.append("news")
            tasks.append(
                asyncio.wait_for(
                    _dispatch_agent_source(
                        gateway,
                        description=news_request,
                        user_text=news_request,
                        conversation_id=f"{conversation_seed}-news",
                        context=context,
                    ),
                    timeout=6.0,
                )
            )

        if await _get_setting_bool(settings_repo, "wake_briefing.sources.calendar", True):
            keys.append("calendar")
            tasks.append(asyncio.wait_for(_calendar_facts(ha_client, entity_index, start=start, end=end), timeout=3.0))

        if await _get_setting_bool(settings_repo, "wake_briefing.sources.sensors", False):
            sensor_entities = await _get_setting_json_list(settings_repo, "wake_briefing.sensor_entities", [])
            keys.append("sensors")
            tasks.append(asyncio.wait_for(_sensor_facts(ha_client, entity_index, sensor_entities), timeout=2.0))

        if not tasks:
            return {}

        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        facts: dict[str, Any] = {}
        for key, value in zip(keys, gathered, strict=False):
            if isinstance(value, Exception):
                logger.debug("Wake briefing source %s failed", key, exc_info=value)
                continue
            if value in (None, "", [], {}):
                continue
            if key == "date_fields" and isinstance(value, dict):
                facts.update(value)
            else:
                facts[key] = value
        return facts

    async def _compose() -> str:
        facts = await _build_facts()
        if not facts:
            raise RuntimeError("Wake briefing facts are empty")

        composer_prompt = await _get_setting(
            settings_repo,
            "wake_briefing.composer_prompt",
            "You compose a short friendly spoken morning briefing from a JSON facts object. Mention the date and weekday, weather, calendar, news headlines, and any sensor readings the user configured. Keep it under 90 spoken seconds. Reply in the user's language.",
        )
        system_prompt = f"{composer_prompt}\nUser language: {language}. Reply in that language."

        complete_kwargs: dict[str, Any] = {
            "max_tokens": 400,
            "temperature": 0.6,
        }
        # Wake briefing always inherits the general-agent model so the
        # composer reuses the operator's already-configured provider/key.
        try:
            general_row = await AgentConfigRepository.get("general-agent")
        except Exception:
            general_row = None
        if general_row:
            inherited_model = (general_row.get("model") or "").strip()
            if inherited_model:
                complete_kwargs["model"] = inherited_model

        return await complete(
            agent_id=_WAKE_BRIEFING_AGENT_ID,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
            **complete_kwargs,
        )

    return await asyncio.wait_for(_compose(), timeout=timeout_seconds)


async def compose_wake_briefing(
    gateway: OrchestratorGateway,
    alarm_payload: dict,
    *,
    ha_client: Any,
    entity_index: Any,
    settings_repo=SettingsRepository,
) -> str:
    """Compose a spoken wake briefing for a fired internal alarm."""
    fallback_message = _fallback_alarm_message(alarm_payload)
    try:
        return await _compose_wake_briefing_inner(
            gateway,
            dict(alarm_payload or {}),
            ha_client=ha_client,
            entity_index=entity_index,
            settings_repo=settings_repo,
        )
    except Exception:
        logger.warning("Wake briefing composition failed; falling back to plain alarm message", exc_info=True)
        return fallback_message
