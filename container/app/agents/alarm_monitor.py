"""Background alarm monitor for opted-in input_datetime helpers.

Only ``input_datetime`` helpers that carry the configured Home Assistant label
(setting ``alarm_monitor.label``, default ``agenthub_alarm``) AND are visible
to the timer agent are treated as alarms. Labels live in the HA entity
registry; they are read through the ``label_entities()`` template function.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.a2a._request import build_send_request
from app.db.repository import SettingsRepository
from app.entity.visibility import entity_is_visible
from app.models.agent import BackgroundEvent, BackgroundTask, TaskContext

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 30.0  # seconds
_MATCH_WINDOW = 60  # seconds -- alarm fires if now is within this window AFTER the alarm time
_LABEL_SETTING_KEY = "alarm_monitor.label"
DEFAULT_ALARM_LABEL = "agenthub_alarm"
_LABEL_CACHE_TTL = 300.0  # seconds between label_entities() lookups
# Visibility is evaluated for the agent that owns alarms.
_ALARM_AGENT_ID = "timer-agent"
# Label ids and names are inserted into a Jinja template, so only plain
# word characters, spaces, and hyphens are accepted.
_SAFE_LABEL_RE = re.compile(r"^[\w\- ]{1,100}$")


class AlarmMonitor:
    """Polls opted-in input_datetime helpers and dispatches notifications when alarm time is reached."""

    def __init__(
        self,
        entity_index: Any,
        dispatcher: Any,
        ha_client: Any = None,
        *,
        settings_repo: Any = SettingsRepository,
    ) -> None:
        self._entity_index = entity_index
        self._dispatcher = dispatcher
        self._ha_client = ha_client
        self._settings_repo = settings_repo
        self._fired: set[str] = set()
        self._last_reset_date: str = ""
        self._task: asyncio.Task | None = None
        self._labeled_cache: tuple[float, str, frozenset[str]] | None = None
        self._unlabeled_warning_logged = False

    async def start(self) -> None:
        """Start the background monitoring task."""
        self._task = asyncio.create_task(self._run())
        logger.info("AlarmMonitor started (interval=%ss, window=%ss)", _CHECK_INTERVAL, _MATCH_WINDOW)

    async def stop(self) -> None:
        """Stop the background monitoring task."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("AlarmMonitor stopped")

    @property
    def fired_today(self) -> list[str]:
        """Return list of entity_ids that have fired today."""
        return list(self._fired)

    async def _run(self) -> None:
        """Main loop: check alarms every _CHECK_INTERVAL seconds."""
        while True:
            try:
                await self._check_alarms()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("AlarmMonitor check failed", exc_info=True)
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _resolve_home_timezone(self) -> tzinfo:
        """Return the HA configured timezone (input_datetime states are HA-local).

        Uses the shared cached HomeContext provider; falls back to UTC when the
        HA client is unavailable or the configured zone is invalid.
        """
        timezone_name = "UTC"
        try:
            from app.ha_client.home_context import home_context_provider

            if self._ha_client is not None:
                home_ctx = await home_context_provider.get(self._ha_client)
                timezone_name = getattr(home_ctx, "timezone", "UTC") or "UTC"
            elif home_context_provider._context is not None:
                timezone_name = home_context_provider._context.timezone or "UTC"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("AlarmMonitor failed to resolve HA timezone; using UTC", exc_info=True)
        try:
            return ZoneInfo(str(timezone_name))
        except Exception:
            logger.debug("AlarmMonitor got invalid HA timezone %r; using UTC", timezone_name, exc_info=True)
            return UTC

    async def _check_alarms(self) -> None:
        home_tz = await self._resolve_home_timezone()
        # Naive wall-clock "now" in HA local time; input_datetime states are naive HA-local values.
        now = datetime.now(home_tz).replace(tzinfo=None)
        today_str = now.strftime("%Y-%m-%d")

        # Reset fired set at midnight
        if today_str != self._last_reset_date:
            self._fired.clear()
            self._last_reset_date = today_str

        try:
            entries = await self._entity_index.list_entries_async(domains={"input_datetime"})
        except Exception:
            logger.warning("Failed to read input_datetime entries in AlarmMonitor", exc_info=True)
            return

        candidates = [
            entry
            for entry in entries
            if str(getattr(entry, "entity_id", "")).startswith("input_datetime.") and getattr(entry, "has_time", False)
        ]
        if not candidates:
            return

        label = await self._alarm_label()
        if not label:
            return
        labeled = await self._labeled_entity_ids(label)
        if not labeled:
            if not self._unlabeled_warning_logged:
                self._unlabeled_warning_logged = True
                logger.warning(
                    "AlarmMonitor: %d input_datetime helper(s) with a time exist, but none carries the HA label "
                    "%r (or the label lookup failed), so none will ring as an alarm. Add the label in Home "
                    "Assistant to every helper that should ring (setting %s), or ignore this if they are not alarms.",
                    len(candidates),
                    label,
                    _LABEL_SETTING_KEY,
                )
            return

        for entry in candidates:
            entity_id = entry.entity_id
            if entity_id not in labeled:
                continue

            state_val = entry.state or ""
            if not state_val or state_val == "unknown":
                continue

            alarm_time = self._parse_alarm_time(
                state_val,
                {"has_date": entry.has_date, "has_time": entry.has_time},
                now,
            )
            if alarm_time is None:
                continue

            fire_key = f"{entity_id}:{today_str}"
            # Fire only at or after the alarm time, never early.
            delta = (now - alarm_time).total_seconds()
            if 0 <= delta <= _MATCH_WINDOW and fire_key not in self._fired:
                if not await self._is_visible(entity_id):
                    logger.info("Alarm helper %s is not visible to %s; not ringing", entity_id, _ALARM_AGENT_ID)
                    continue
                self._fired.add(fire_key)
                friendly_name = entry.friendly_name or entity_id
                logger.info("Alarm triggered: %s (%s)", entity_id, friendly_name)
                await self._fire_notification(entry)

    async def _alarm_label(self) -> str:
        """Return the configured opt-in label; empty disables the monitor."""
        try:
            raw = await self._settings_repo.get_value(_LABEL_SETTING_KEY, DEFAULT_ALARM_LABEL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("AlarmMonitor could not read %s; using default", _LABEL_SETTING_KEY, exc_info=True)
            raw = DEFAULT_ALARM_LABEL
        label = DEFAULT_ALARM_LABEL if raw is None else str(raw).strip()
        if label and not _SAFE_LABEL_RE.match(label):
            logger.warning("AlarmMonitor: ignoring unsafe %s value %r", _LABEL_SETTING_KEY, label)
            return ""
        return label

    async def _labeled_entity_ids(self, label: str) -> frozenset[str]:
        """Return entity ids carrying ``label`` (HA label id or name), cached for a few minutes."""
        cached = self._labeled_cache
        now = time.monotonic()
        if cached is not None and cached[1] == label and now - cached[0] < _LABEL_CACHE_TTL:
            return cached[2]
        render = getattr(self._ha_client, "render_template", None)
        if not callable(render):
            return frozenset()
        template = "{{ label_entities('" + label + "') | join(',') }}"
        try:
            rendered = await render(template)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("AlarmMonitor label lookup failed for %r", label, exc_info=True)
            rendered = None
        ids = frozenset(item.strip() for item in str(rendered or "").split(",") if item.strip())
        # An empty answer may be a transient HA failure: only cache hits.
        self._labeled_cache = (now, label, ids) if ids else None
        return ids

    async def _is_visible(self, entity_id: str) -> bool:
        """Fail-closed visibility check for the timer agent."""
        try:
            return await entity_is_visible(_ALARM_AGENT_ID, entity_id, self._entity_index)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Visibility check failed for alarm helper %s; not ringing", entity_id, exc_info=True)
            return False

    def _parse_alarm_time(self, state_val: str, attrs: dict, now: datetime) -> datetime | None:
        """Parse input_datetime state into a datetime for comparison."""
        has_date = attrs.get("has_date", False)
        has_time = attrs.get("has_time", False)

        try:
            if has_date and has_time:
                return datetime.strptime(state_val, "%Y-%m-%d %H:%M:%S")
            elif has_time and not has_date:
                time_parts = state_val.split(":")
                return now.replace(
                    hour=int(time_parts[0]),
                    minute=int(time_parts[1]),
                    second=int(time_parts[2]) if len(time_parts) > 2 else 0,
                    microsecond=0,
                )
        except (ValueError, IndexError):
            return None
        return None

    async def _fire_notification(self, entry: Any) -> None:
        """Dispatch alarm notification through the dispatcher."""
        entity_id = entry.entity_id
        friendly_name = entry.friendly_name or entity_id
        try:
            event_context = TaskContext(source="background")
            event_context.background_event = BackgroundEvent(
                event_type="alarm_notification",
                payload={
                    "alarm_name": friendly_name,
                    "briefing": False,
                    "entity_id": entity_id,
                    "media_player": getattr(entry, "media_player", None),
                    "origin_device_id": getattr(entry, "origin_device_id", None),
                    "origin_area": getattr(entry, "area", None),
                    "language": getattr(entry, "language", None),
                },
            )
            task = BackgroundTask(context=event_context)
            request = build_send_request(
                "orchestrator",
                task,
                request_id=str(uuid.uuid4()),
            )
            await self._dispatcher.dispatch(request)
        except Exception:
            logger.error("Alarm notification dispatch failed for %s", entity_id, exc_info=True)
