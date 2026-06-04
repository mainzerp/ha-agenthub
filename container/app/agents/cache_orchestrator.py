"""Cache orchestration module extracted from OrchestratorAgent.

Handles action-cache replay, routing-cache skip, cache storage
after dispatch, and cached action execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.analytics.collector import track_cache_event
from app.analytics.tracer import _optional_span
from app.cache.cache_manager import ActionReplayOutcome, CacheManager, RoutingSkipOutcome
from app.db.repository import SettingsRepository
from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.visibility import entity_is_visible
from app.models.agent import AgentTask
from app.models.cache import ActionCacheEntry, CachedAction

logger = logging.getLogger(__name__)

_FALLBACK_AGENT = "general-agent"
_CANCEL_INTERACTION_AGENT = "cancel-interaction"
_INTERNAL_ONLY_AGENTS: frozenset[str] = frozenset({"orchestrator", "rewrite-agent", "filler-agent"})

_CACHED_SERVICE_DATA_KEYS: frozenset[str] = frozenset(
    {
        "brightness",
        "brightness_pct",
        "color_name",
        "color_temp",
        "color_temp_kelvin",
        "rgb_color",
        "hs_color",
        "xy_color",
        "transition",
        "effect",
        "volume_level",
        "source",
        "media_content_id",
        "media_content_type",
        "temperature",
        "hvac_mode",
        "preset_mode",
        "fan_mode",
        "swing_mode",
        "position",
        "percentage",
    }
)


class CacheOrchestrator:
    """Manages cache replay, storage, and cached action execution."""

    def __init__(
        self,
        cache_manager: CacheManager | None = None,
        entity_index=None,
        ha_client=None,
        agent_registry=None,
        calendar_injector=None,
        get_turns: Callable[[str | None], Awaitable[list[dict[str, Any]]]] | None = None,
        store_turn: Callable[..., Awaitable[None]] | None = None,
        merge_voice_followup_and_organic: Callable[..., Awaitable[tuple[str, bool]]] | None = None,
        create_trace: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._cache_manager = cache_manager
        self._entity_index = entity_index
        self._ha_client = ha_client
        self._agent_registry = agent_registry
        self._calendar_injector = calendar_injector
        self._get_turns = get_turns
        self._store_turn = store_turn
        self._merge_voice_followup_and_organic = merge_voice_followup_and_organic
        self._create_trace = create_trace

    @property
    def cache_manager(self) -> CacheManager | None:
        return self._cache_manager

    @staticmethod
    def legacy_pipeline_enabled() -> bool:
        return os.environ.get("ORCHESTRATOR_LEGACY_PIPELINE") == "1"

    @staticmethod
    def bool_setting_default(default: bool) -> str:
        return "true" if default else "false"

    async def _get_bool_setting_impl(self, key: str, default: bool) -> bool:
        try:
            legacy_key = {
                "cache.compound_utterance_bypass": "routing.compound_utterance_bypass",
            }.get(key)
            raw = await SettingsRepository.get_value(key, None)
            if raw is None and legacy_key is not None:
                raw = await SettingsRepository.get_value(legacy_key, None)
        except Exception:
            logger.debug("Failed to read setting %s, using default %s", key, default, exc_info=True)
            return default
        if raw is None:
            return default
        normalized = str(raw).strip().lower()
        if not normalized:
            return default
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        return default

    @staticmethod
    def is_actionable_routing_agent(target_agent: str) -> bool:
        return (
            target_agent not in (_FALLBACK_AGENT, _CANCEL_INTERACTION_AGENT, "send-agent")
            and target_agent not in _INTERNAL_ONLY_AGENTS
        )

    @staticmethod
    def _is_readonly_action_result(action_executed) -> bool:
        if not isinstance(action_executed, dict):
            return False
        action_name = str(action_executed.get("action") or "").strip().lower()
        if not action_name:
            service_name = str(action_executed.get("service") or "").strip().lower()
            action_name = service_name.split("/", 1)[1] if "/" in service_name else service_name
        return action_name.startswith(("query_", "list_"))

    @staticmethod
    def build_synthetic_classifications(
        routing: RoutingSkipOutcome,
    ) -> list[tuple[str, str, float | None, list[str]]]:
        return [(routing.agent_id, routing.condensed_task, 1.0, [])]

    async def try_cache_replay(
        self,
        *,
        task: AgentTask | None = None,
        user_text: str,
        language: str = "en",
        requesting_agent_id: str = "orchestrator",
        span_collector=None,
        check_visibility: Callable[[str, str], Awaitable[bool]] | None = None,
        exec_cached_action: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
    ) -> tuple[ActionReplayOutcome | None, RoutingSkipOutcome | None]:
        """Try action replay first, then routing skip, before live classify."""
        if not self._cache_manager:
            return None, None
        if not await self._get_bool_setting_impl("cache.enabled", True):
            return None, None

        _check_vis = check_visibility or self.cached_action_is_still_visible
        _exec_action = exec_cached_action or self.execute_cached_action

        async def _resolve_entity(current_text: str, target_agent: str) -> str | None:
            if not self._entity_index:
                return None
            context = getattr(task, "context", None)
            resolution = await resolve_entity_deterministic_first(
                current_text,
                self._entity_index,
                None,
                target_agent,
                preferred_area_id=getattr(context, "area_id", None),
                verbatim_terms=[],
            )
            return resolution.get("entity_id")

        async with _optional_span(span_collector, "cache_lookup", agent_id="orchestrator") as cache_span:
            action_hit = await self._cache_manager.try_replay_action(
                query_text=user_text,
                language=language,
                requesting_agent_id=requesting_agent_id,
                resolve_entity=_resolve_entity,
                check_visibility=_check_vis,
                execute_cached_action=_exec_action,
            )
            if action_hit is not None:
                cache_span["metadata"]["hit_type"] = "action_hit"
                cache_span["metadata"]["similarity"] = action_hit.similarity
                cache_span["metadata"]["cached_agent_id"] = action_hit.agent_id
                cache_span["metadata"]["cache_tier"] = "action"
                return action_hit, None

            routing_hit = await self._cache_manager.try_routing_skip(
                query_text=user_text,
                language=language,
            )
            if routing_hit is not None:
                known_agents: set[str] = set()
                if self._agent_registry is not None:
                    known_agents = await self._agent_registry.get_known_agents()
                if routing_hit.agent_id not in known_agents:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self._cache_manager.invalidate_routing, routing_hit.entry_id)
                    cache_span["metadata"]["hit_type"] = "routing_stale_agent"
                    cache_span["metadata"]["cached_agent_id"] = routing_hit.agent_id
                    cache_span["metadata"]["cache_tier"] = "both_miss"
                    return None, None
                cache_span["metadata"]["hit_type"] = "routing_hit"
                cache_span["metadata"]["similarity"] = routing_hit.similarity
                cache_span["metadata"]["cached_agent_id"] = routing_hit.agent_id
                cache_span["metadata"]["cache_tier"] = "routing"
                return None, routing_hit

            cache_span["metadata"]["hit_type"] = "miss"
            cache_span["metadata"]["cache_tier"] = "both_miss"
            await track_cache_event(tier="both_miss", hit_type="miss")
            return None, None

    async def cached_action_is_still_visible(self, agent_id: str, entity_id: str) -> bool:
        """Re-evaluate per-agent visibility rules for a cached action's entity.

        Mirrors the include/exclude semantics enforced by
        ``EntityMatcher._apply_visibility_rules`` for the live path.
        Fail-closed: any error is treated as "not visible" so a cached
        action cannot fire when visibility cannot be evaluated.
        """
        if not entity_id:
            return False
        try:
            return await entity_is_visible(
                agent_id,
                entity_id,
                self._entity_index,
                fail_closed_on_metadata_gap=True,
            )
        except Exception:
            logger.debug(
                "Visibility evaluation failed for agent=%s entity=%s; treating as not visible",
                agent_id,
                entity_id,
                exc_info=True,
            )
            return False

    async def finalize_action_replay_hit(
        self,
        hit: ActionReplayOutcome,
        conversation_id: str,
        user_text: str,
        span_collector,
        *,
        task: AgentTask | None = None,
    ) -> dict[str, Any]:
        """Finalize a successful action-cache full hit."""
        target_agent = hit.agent_id or "unknown"
        task_context = getattr(task, "context", None) if task is not None else None
        raw_speech = hit.original_response_text or hit.response_text or ""
        speech = raw_speech

        reminder_text: str | None = None
        if self._calendar_injector is not None:
            try:
                async with _optional_span(span_collector, "calendar_inject", agent_id="orchestrator") as cal_span:
                    reminder_text = await self._calendar_injector.inject_reminders(
                        utterance=task.description if task else None,
                        device_id=task_context.device_id if task_context else None,
                        area_id=task_context.area_id if task_context else None,
                        user_id=task_context.user_id if task_context else None,
                        language=(task_context.language if task_context else "en") or "en",
                    )
                    cal_span["metadata"]["reminder_injected"] = bool(reminder_text)
                    cal_span["metadata"]["reminder_length"] = len(reminder_text or "")
            except Exception:
                logger.debug("Calendar reminder injection failed", exc_info=True)

        if self._cache_manager:
            async with _optional_span(span_collector, "rewrite", agent_id="rewrite-agent") as rw_span:
                speech = await self._cache_manager.apply_rewrite(hit, user_text=user_text, reminder_text=reminder_text)
                if hit.rewrite_applied:
                    rw_span["metadata"]["original_text"] = hit.original_response_text or ""
                    rw_span["metadata"]["rewritten_text"] = speech
                    rw_span["metadata"]["latency_ms"] = hit.rewrite_latency_ms
                    rw_span["metadata"]["success"] = True

        if reminder_text and not hit.rewrite_applied:
            separator = " " if speech and speech[-1] in ".!?" else ". "
            speech = f"{speech}{separator}{reminder_text}" if speech else reminder_text

        if hit.cached_action:
            async with _optional_span(span_collector, "ha_action", agent_id=target_agent) as ha_span:
                ha_span["metadata"]["action"] = hit.cached_action.service
                ha_span["metadata"]["entity"] = hit.cached_action.entity_id
                ha_span["metadata"]["success"] = hit.replay_result is not None
                ha_span["metadata"]["cached"] = True

        async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
            ret_span["metadata"]["from_agent"] = target_agent
            ret_span["metadata"]["agent_response"] = raw_speech
            ret_span["metadata"]["final_response"] = speech
            ret_span["metadata"]["mediated"] = bool(hit.rewrite_applied)
            ret_span["metadata"]["action_cache_hit"] = True
            ret_span["metadata"]["response_cache_hit"] = False
            ret_span["metadata"]["sanitized"] = False
            prior_turns = []
            if self._get_turns is not None:
                prior_turns = await self._get_turns(conversation_id)
            if self._store_turn is not None:
                await self._store_turn(conversation_id, user_text, speech, agent_id=target_agent)

        vf_effective = False
        if self._merge_voice_followup_and_organic is not None:
            speech, vf_effective = self._merge_voice_followup_and_organic(
                speech,
                agent_requested=False,
                mediated_followup=False,
            )

        if span_collector and self._create_trace is not None:
            try:
                await self._create_trace(
                    span_collector,
                    conversation_id,
                    user_text,
                    speech,
                    target_agent,
                    1.0,
                    None,
                    user_text,
                    [("orchestrator", user_text, 1.0)],
                    prior_turns,
                    task_context=task_context,
                    voice_followup=vf_effective,
                )
            except Exception:
                logger.warning("Failed to create trace summary", exc_info=True)

        return {
            "speech": speech,
            "routed_to": target_agent,
            "action_executed": hit.replay_result,
            "sanitized": True,
            "voice_followup": vf_effective,
        }

    async def store_after_dispatch(
        self,
        *,
        user_text: str,
        language: str,
        target_agent: str,
        condensed_task: str,
        confidence: float | None,
        speech: str,
        original_response_text: str = "",
        action_executed,
        has_error: bool,
        task: AgentTask | None = None,
        merged_multi_agent: bool = False,
        used_origin_context: bool = False,
    ) -> tuple[bool, bool]:
        """Store either an action-cache row or a routing-cache row, never both."""
        if merged_multi_agent or not self._cache_manager or not speech or has_error:
            return False, False
        if self.legacy_pipeline_enabled():
            return False, False
        if not await self._get_bool_setting_impl("cache.enabled", True):
            return False, False
        if target_agent in (_CANCEL_INTERACTION_AGENT, "send-agent") or target_agent in _INTERNAL_ONLY_AGENTS:
            return False, False

        entity_ids: list[str] = []
        if hasattr(action_executed, "model_dump"):
            action_executed = action_executed.model_dump()
        if isinstance(action_executed, dict):
            raw_entity_ids = action_executed.get("entity_ids")
            if isinstance(raw_entity_ids, list):
                entity_ids.extend(str(item) for item in raw_entity_ids if item)
            entity_id = str(action_executed.get("entity_id") or "").strip()
            if entity_id:
                entity_ids.append(entity_id)
            entity_ids = list(dict.fromkeys(entity_ids))

        confidence_value = confidence if confidence is not None else 0.0
        readonly_action = self._is_readonly_action_result(action_executed)
        if isinstance(action_executed, dict) and action_executed.get("success"):
            if readonly_action:
                try:
                    await self._cache_manager.store_routing_async(
                        user_text,
                        target_agent,
                        confidence_value,
                        condensed_task,
                        language=language,
                        entity_ids=entity_ids,
                    )
                    return False, True
                except Exception:
                    logger.warning("Failed to store routing decision", exc_info=True)
                    return False, False

            if action_executed.get("cacheable", True) is False:
                return False, False
            raw_service_data = action_executed.get("service_data") or {}
            if isinstance(raw_service_data, dict) and "condition" in raw_service_data:
                return False, False

            entity_id = str(action_executed.get("entity_id") or "").strip()
            action_name = str(action_executed.get("action") or "").strip().lower()
            if entity_id and action_name:
                cached_service_data: dict[str, Any] = {}
                if isinstance(raw_service_data, dict):
                    for key in _CACHED_SERVICE_DATA_KEYS:
                        if key in raw_service_data:
                            cached_service_data[key] = raw_service_data[key]
                domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
                cached_action = CachedAction(
                    service=f"{domain}/{action_name}" if domain else action_name,
                    entity_id=entity_id,
                    service_data=cached_service_data,
                )
                entry = ActionCacheEntry(
                    query_text=user_text,
                    language=language,
                    agent_id=target_agent,
                    condensed_task=condensed_task,
                    confidence=confidence_value,
                    response_text=speech,
                    original_response_text=original_response_text or speech,
                    cached_action=cached_action,
                    entity_ids=entity_ids,
                    origin_area_id=(task.context.area_id if used_origin_context and task and task.context else None),
                    origin_device_id=(
                        task.context.device_id if used_origin_context and task and task.context else None
                    ),
                    executed_at=datetime.now(UTC).isoformat(),
                )
                try:
                    await self._cache_manager.store_action_async(entry)
                    return True, False
                except Exception:
                    logger.warning("Failed to store action cache entry", exc_info=True)
                    return False, False
            return False, False

        try:
            await self._cache_manager.store_routing_async(
                user_text,
                target_agent,
                confidence_value,
                condensed_task,
                language=language,
                entity_ids=entity_ids,
            )
            return False, True
        except Exception:
            logger.warning("Failed to store routing decision", exc_info=True)
            return False, False

    async def execute_cached_action(self, cached_action) -> dict[str, Any] | None:
        """Execute a cached action via HA client. Fast path: direct REST call only.

        Skips the WebSocket observer used by the live executor path.
        For idempotent actions (turn_on, turn_off, etc.) the REST call
        itself is sufficient; we do not wait for state confirmation.
        """
        if not self._ha_client or not cached_action:
            return None

        service = cached_action.service or ""
        if "/" in service:
            domain, action = service.split("/", 1)
        else:
            domain, action = service, ""
        entity_id = cached_action.entity_id or ""
        service_data = cached_action.service_data or None

        if not domain or not action or not entity_id:
            return None

        try:
            await self._ha_client.call_service(
                domain,
                action,
                entity_id,
                service_data or None,
            )
            return {
                "success": True,
                "entity_id": entity_id,
                "action": action,
                "state": None,
                "source": "cached_call",
            }
        except Exception:
            logger.warning("Cached action execution failed", exc_info=True)
            return None
