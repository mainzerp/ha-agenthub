"""Action-cache validator: scan entries for semantic consistency."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime

from app.cache.action_cache import ActionCache
from app.cache.cache_manager import CacheManager
from app.db.repository import SettingsRepository
from app.models.cache import ActionCacheEntry

logger = logging.getLogger(__name__)

_ACTION_CONTRADICTIONS: dict[str, list[str]] = {
    "turn_on": ["is now off", "turned off", "has been turned off", "is off"],
    "set_brightness": ["is now off", "turned off", "has been turned off", "is off"],
    "set_color": ["is now off", "turned off", "has been turned off", "is off"],
    "set_color_temp": ["is now off", "turned off", "has been turned off", "is off"],
    "turn_off": ["is now on", "turned on", "has been turned on", "is on"],
    "lock": ["unlocked"],
    "unlock": ["locked"],
    "toggle": [],
}

_READONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "query_light_state",
        "list_lights",
        "query_entity_history",
        "list_climate",
        "list_automations",
        "list_security",
        "list_media_players",
        "list_music_players",
        "list_scenes",
        "list_timers",
        "list_alarms",
        "list_lists",
        "query_weather",
        "query_weather_forecast",
    }
)

_EXPECTED_STATES: dict[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
}


class ActionCacheValidator:
    """Periodically scan action-cache entries and correct inconsistent response_text."""

    def __init__(
        self,
        action_cache: ActionCache,
        cache_manager: CacheManager,
        entity_index=None,
        ha_client=None,
        llm_client=None,
    ) -> None:
        self._action_cache = action_cache
        self._cache_manager = cache_manager
        self._entity_index = entity_index
        self._ha_client = ha_client
        self._llm_client = llm_client
        self._history: deque[dict] = deque(maxlen=50)

    async def run_periodic(self) -> None:
        """Asyncio sleep loop that runs validation at configured intervals."""
        while True:
            try:
                enabled_raw = await SettingsRepository.get_value("cache.validator.enabled", "true")
                enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
                if not enabled:
                    await asyncio.sleep(60)
                    continue

                interval_raw = await SettingsRepository.get_value("cache.validator.interval_minutes", "60")
                try:
                    interval_min = int(str(interval_raw))
                except (TypeError, ValueError):
                    interval_min = 60

                if interval_min <= 0:
                    await asyncio.sleep(60)
                    continue

                await self.run_once()
                await asyncio.sleep(interval_min * 60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Cache validator periodic run failed", exc_info=True)
                await asyncio.sleep(60)

    async def run_once(self) -> dict[str, int | str]:
        """Run a single validation scan and return counts."""
        try:
            enabled_raw = await SettingsRepository.get_value("cache.validator.enabled", "true")
            enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            enabled = True

        if not enabled:
            return {"scanned": 0, "inconsistent": 0, "corrected": 0, "deleted": 0, "errors": 0}

        scanned = 0
        inconsistent = 0
        corrected = 0
        deleted = 0
        errors = 0

        try:
            entries = list(self._cache_manager.iter_action_entries(page_size=1000))
        except Exception:
            logger.warning("Failed to iterate action cache entries", exc_info=True)
            return {"scanned": 0, "inconsistent": 0, "corrected": 0, "deleted": 0, "errors": 1}

        started_at = datetime.now(UTC).isoformat()

        for entry in entries:
            if entry.validated_at:
                continue
            scanned += 1
            try:
                is_valid, corrected_text = await self._validate_entry(entry)
                if is_valid:
                    entry.validated_at = datetime.now(UTC).isoformat()
                    try:
                        await self._cache_manager.update_action_entry(entry)
                    except Exception:
                        logger.warning("Failed to update validated cache entry", exc_info=True)
                        errors += 1
                    continue
                inconsistent += 1
                if corrected_text is not None:
                    entry.response_text = corrected_text
                    entry.original_response_text = corrected_text
                    entry.validated_at = datetime.now(UTC).isoformat()
                    try:
                        await self._cache_manager.update_action_entry(entry)
                        corrected += 1
                    except Exception:
                        logger.warning("Failed to update corrected cache entry", exc_info=True)
                        errors += 1
                else:
                    try:
                        entry_id = self._action_cache.make_entry_id(
                            entry.query_text,
                            language=entry.language,
                        )
                        self._cache_manager.invalidate_action(entry_id)
                        deleted += 1
                    except Exception:
                        logger.warning("Failed to invalidate inconsistent cache entry", exc_info=True)
                        errors += 1
            except Exception:
                logger.warning("Failed to validate cache entry", exc_info=True)
                errors += 1

        finished_at = datetime.now(UTC).isoformat()
        result = {
            "scanned": scanned,
            "inconsistent": inconsistent,
            "corrected": corrected,
            "deleted": deleted,
            "errors": errors,
            "started_at": started_at,
            "finished_at": finished_at,
        }
        self._history.append(result)
        logger.info(
            "Cache validator scan complete: scanned=%d inconsistent=%d corrected=%d deleted=%d errors=%d",
            scanned,
            inconsistent,
            corrected,
            deleted,
            errors,
        )
        return result

    def get_history(self) -> list[dict]:
        """Return the last 50 validation run records."""
        return list(self._history)

    async def _validate_entry(self, entry: ActionCacheEntry) -> tuple[bool, str | None]:
        """Return (is_valid, corrected_response_or_None)."""
        if entry.cached_action is None:
            return False, None

        service = entry.cached_action.service
        response_text = entry.response_text or ""

        # Try LLM-first validation when configured
        model = await SettingsRepository.get_value("cache.validator.model", "")
        if model and self._llm_client is not None:
            llm_result = await self._llm_validate_consistency(entry)
            if llm_result == "consistent":
                return True, None
            if llm_result == "correct_response":
                corrected_text = await self._regenerate_response(entry)
                return False, corrected_text
            if llm_result == "invalidate":
                return False, None
            # None (failure/unparseable) → fall through to deterministic check

        if not self._is_plausible(service, response_text):
            corrected_text = await self._regenerate_response(entry)
            return False, corrected_text

        return True, None

    async def _llm_validate_consistency(self, entry: ActionCacheEntry) -> str | None:
        """Ask the LLM to evaluate consistency across query, action, and response.

        Returns one of: "consistent", "correct_response", "invalidate", or None on failure.
        """
        service = entry.cached_action.service
        entity_id = entry.cached_action.entity_id or "none"
        service_data = entry.cached_action.service_data or {}
        response_text = entry.response_text or ""
        query_text = entry.query_text or ""

        prompt = (
            f"You are validating a smart-home assistant cache entry.\n\n"
            f"User query: '{query_text}'\n"
            f"Action performed: {service}\n"
            f"Target entity: {entity_id}\n"
            f"Service data: {service_data}\n"
            f"Assistant response: '{response_text}'\n\n"
            f"Evaluate whether the action matches the user query and whether the response is consistent with the action.\n"
            f"Answer with exactly one word:\n"
            f"- 'consistent' if the action matches the query and the response is appropriate\n"
            f"- 'correct_response' if the action matches the query but the response is inconsistent or wrong\n"
            f"- 'invalidate' if the action does not match the query or the entry is fundamentally wrong\n"
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            temperature_raw = await SettingsRepository.get_value("cache.validator.temperature", "0.2")
            try:
                temperature = float(str(temperature_raw))
            except (TypeError, ValueError):
                temperature = 0.2
            temperature = min(temperature, 0.2)

            max_tokens_raw = await SettingsRepository.get_value("cache.validator.max_tokens", "32")
            try:
                max_tokens = int(str(max_tokens_raw))
            except (TypeError, ValueError):
                max_tokens = 32

            reasoning_effort = await SettingsRepository.get_value("cache.validator.reasoning_effort", "low")

            llm_result = await self._llm_client.complete(
                agent_id="cache_validator",
                messages=messages,
                model=await SettingsRepository.get_value("cache.validator.model", ""),
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )
        except Exception:
            logger.debug("LLM consistency validation failed, falling back to deterministic check", exc_info=True)
            return None

        content = (llm_result or "").strip().lower()
        if content.startswith("consistent"):
            return "consistent"
        if content.startswith("correct_response") or content.startswith("correct"):
            return "correct_response"
        if content.startswith("invalidate"):
            return "invalidate"

        logger.debug("LLM validation returned unparseable response: %r", llm_result)
        return None

    async def _regenerate_response(self, entry: ActionCacheEntry) -> str | None:
        """Try LLM first if configured, otherwise use deterministic template."""
        if entry.cached_action is None:
            return None

        entity_id = entry.cached_action.entity_id
        service = entry.cached_action.service

        friendly_name = None
        if entity_id:
            friendly_name = await self._get_friendly_name(entity_id)

        # Try LLM first if configured and client available
        model = await SettingsRepository.get_value("cache.validator.model", "")
        if model and self._llm_client is not None:
            try:
                _, action_name = self._parse_service(service)
                action_name = (action_name or "").lower()
                expected_state = _EXPECTED_STATES.get(action_name, "")
                action_desc = action_name.replace("_", " ")
                target_name = friendly_name or entity_id or "device"
                prompt = (
                    f"Generate a single concise confirmation sentence in English that the device '{target_name}' "
                    f"has been successfully {action_desc}. The expected state is '{expected_state}'. "
                    f"Output ONLY the sentence, no extra text.\n"
                    f"Example: Done, Kitchen Light is now on."
                )
                messages = [{"role": "user", "content": prompt}]

                temperature_raw = await SettingsRepository.get_value("cache.validator.temperature", "0.2")
                try:
                    temperature = float(str(temperature_raw))
                except (TypeError, ValueError):
                    temperature = 0.2

                max_tokens_raw = await SettingsRepository.get_value("cache.validator.max_tokens", "1024")
                try:
                    max_tokens = int(str(max_tokens_raw))
                except (TypeError, ValueError):
                    max_tokens = 1024

                reasoning_effort = await SettingsRepository.get_value("cache.validator.reasoning_effort", "low")

                llm_result = await self._llm_client.complete(
                    agent_id="cache_validator",
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
                if llm_result and llm_result.strip():
                    return llm_result.strip()
            except Exception:
                logger.debug("LLM regeneration failed, falling back to template", exc_info=True)

        # Fallback to deterministic template
        target_name = friendly_name or entity_id
        if not target_name:
            return None
        return self._regenerate_response_text(service, target_name)

    async def _get_friendly_name(self, entity_id: str) -> str | None:
        """Resolve friendly_name from entity_index or HA client."""
        if self._entity_index is not None:
            try:
                if hasattr(self._entity_index, "get_by_id_async"):
                    entry = await self._entity_index.get_by_id_async(entity_id)
                elif hasattr(self._entity_index, "get_by_id"):
                    entry = self._entity_index.get_by_id(entity_id)
                else:
                    entry = None
                if entry is not None:
                    return getattr(entry, "friendly_name", None) or getattr(entry, "name", None)
            except Exception:
                logger.debug("Failed to resolve friendly_name from entity_index for %s", entity_id, exc_info=True)

        if self._ha_client is not None:
            try:
                state = await self._ha_client.get_state(entity_id)
                if isinstance(state, dict):
                    attrs = state.get("attributes") or {}
                    friendly_name = attrs.get("friendly_name")
                    if friendly_name:
                        return friendly_name
            except Exception:
                logger.debug("Failed to resolve friendly_name from HA client for %s", entity_id, exc_info=True)

        return None

    @staticmethod
    def _parse_service(service: str) -> tuple[str | None, str | None]:
        """Split 'light/turn_on' into ('light', 'turn_on')."""
        if "/" in service:
            domain, action = service.split("/", 1)
            return domain, action
        return None, service

    @staticmethod
    def _is_plausible(service: str, response_text: str) -> bool:
        """Check whether response_text is semantically consistent with the service."""
        _, action = ActionCacheValidator._parse_service(service)
        action = (action or "").lower()
        text_lower = response_text.lower()

        # Read-only actions should not have action-completion phrasing
        if action in _READONLY_ACTIONS:
            return not ("done," in text_lower or "is now" in text_lower)

        # For state-changing actions, check for contradictory phrases
        contradictions = _ACTION_CONTRADICTIONS.get(action, [])
        return all(phrase not in text_lower for phrase in contradictions)

    @staticmethod
    def _regenerate_response_text(service: str, friendly_name: str) -> str | None:
        """Generate a deterministic confirmation sentence."""
        _, action = ActionCacheValidator._parse_service(service)
        action = (action or "").lower()
        expected_state = _EXPECTED_STATES.get(action)
        if expected_state:
            return f"Done, {friendly_name} is now {expected_state}."
        # Fallback for unknown actions
        return f"Done, {friendly_name} {action.replace('_', ' ')}."
