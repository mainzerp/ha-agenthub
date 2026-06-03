"""Dispatch manager extracted from OrchestratorAgent.

Handles single-agent and multi-agent dispatch, fallback dispatch,
response mediation, and response normalization.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.a2a.protocol import JsonRpcRequest
from app.agents.agent_registry import CachedAgentRegistry
from app.agents.cancel_speech import generate_cancel_speech
from app.analytics.collector import track_agent_timeout, track_request
from app.analytics.tracer import _optional_span
from app.db.repository import SettingsRepository
from app.ha_client.home_context import home_context_provider
from app.models.agent import AgentTask, TaskContext

logger = logging.getLogger(__name__)

_FALLBACK_AGENT = "general-agent"
_CANCEL_INTERACTION_AGENT = "cancel-interaction"

_CANNED_TIMEOUT_SPEECH = "I couldn't process that request in time."
_CANNED_GENERAL_ERROR_SPEECH = "I couldn't process that request right now."


class DispatchManager:
    """Manages single-agent dispatch, fallback, sequential send, and mediation."""

    def __init__(
        self,
        dispatcher,
        agent_registry: CachedAgentRegistry | None = None,
        ha_client=None,
        call_llm: Callable[..., Awaitable[str]] | None = None,
        load_prompt_async: Callable[[str], Awaitable[str]] | None = None,
        resolve_dispatch_timeout: Callable[[str], Awaitable[float]] | None = None,
        wrap_user_input: Callable[[str], str] | None = None,
        mediation_model: str | None = None,
        mediation_temperature: float = 0.3,
        mediation_max_tokens: int = 2048,
        settings_repo=None,
    ) -> None:
        self._dispatcher = dispatcher
        self._agent_registry = agent_registry
        self._ha_client = ha_client
        self._call_llm = call_llm
        self._load_prompt_async = load_prompt_async
        self._resolve_dispatch_timeout_fn = resolve_dispatch_timeout
        self._wrap_user_input = wrap_user_input or (lambda x: x)
        self._mediation_model = mediation_model
        self._mediation_temperature = mediation_temperature
        self._mediation_max_tokens = mediation_max_tokens
        self._settings_repo = settings_repo

    async def resolve_dispatch_timeout(self, agent_id: str, default_timeout: int = 5) -> float:
        if self._resolve_dispatch_timeout_fn is not None:
            return await self._resolve_dispatch_timeout_fn(agent_id)
        if self._agent_registry is not None:
            return await self._agent_registry.resolve_dispatch_timeout(
                agent_id,
                default_timeout=default_timeout,
                settings_repo=self._settings_repo or SettingsRepository,
            )
        return float(default_timeout)

    @staticmethod
    def normalize_agent_result(result_data: Any, agent_id: str | None = None) -> dict[str, Any]:
        """Coerce an agent result into a dict, logging warnings on unexpected types."""
        if isinstance(result_data, dict):
            return result_data
        if hasattr(result_data, "model_dump"):
            return result_data.model_dump(exclude_none=True)
        if isinstance(result_data, str):
            logger.warning(
                "Agent %s returned string result instead of dict; wrapping as speech",
                agent_id or "unknown",
            )
            return {"speech": result_data}
        if result_data is not None:
            logger.warning(
                "Agent %s returned unexpected result type %s; coercing to empty dict",
                agent_id or "unknown",
                type(result_data).__name__,
            )
        return {}

    async def dispatch_fallback(
        self,
        request: JsonRpcRequest,
        target_agent: str,
        span_collector,
        reason: str,
    ) -> tuple[str, Any] | None:
        """Attempt dispatch to the fallback agent.

        Returns ``(new_agent_id, response)`` on success, or ``None`` when
        the original target is already the fallback agent or the fallback
        itself timed out.
        """
        if target_agent == _FALLBACK_AGENT:
            return None

        if request.params is None:
            request.params = {}
        request.params["agent_id"] = _FALLBACK_AGENT
        try:
            t_fb = time.perf_counter()
            fb_timeout = await self.resolve_dispatch_timeout(_FALLBACK_AGENT)
            async with _optional_span(span_collector, "dispatch_fallback", agent_id=_FALLBACK_AGENT) as fb_span:
                response = await asyncio.wait_for(
                    self._dispatcher.dispatch(request),
                    timeout=fb_timeout,
                )
                fb_latency_ms = (time.perf_counter() - t_fb) * 1000
                fb_span["metadata"]["latency_ms"] = round(fb_latency_ms, 1)
                fb_span["metadata"]["from_agent"] = target_agent
                fb_span["metadata"]["reason"] = reason
                fb_span["metadata"]["dispatch_timeout_sec"] = fb_timeout
                fb_result_data = self.normalize_agent_result(response, agent_id=_FALLBACK_AGENT)
                fb_span["metadata"]["agent_response"] = fb_result_data.get("speech") or ""
            await track_request(_FALLBACK_AGENT, cache_hit=False, latency_ms=fb_latency_ms)
            return _FALLBACK_AGENT, response
        except TimeoutError:
            await track_agent_timeout(_FALLBACK_AGENT, int(fb_timeout))
            return None
        except RuntimeError:
            return None

    async def dispatch_single(
        self,
        target_agent: str,
        condensed_task: str,
        user_text: str,
        conversation_id: str | None,
        turns: list[dict[str, Any]],
        span_collector,
        incoming_context: TaskContext | None = None,
        skip_dispatch_span: bool = False,
        *,
        resolved_language: str | None = None,
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Dispatch a single task to one agent and return (agent_id, speech, result_dict)."""
        t_dispatch = time.perf_counter()
        context = TaskContext(conversation_turns=turns)
        if incoming_context:
            context.device_id = incoming_context.device_id
            context.area_id = incoming_context.area_id
            context.device_name = incoming_context.device_name
            context.area_name = incoming_context.area_name
            context.source = incoming_context.source
            context.language = incoming_context.language
            context.injection_detected = incoming_context.injection_detected
        if resolved_language:
            context.language = resolved_language

        if target_agent == _CANCEL_INTERACTION_AGENT:
            async with _optional_span(span_collector, "dispatch", agent_id=_CANCEL_INTERACTION_AGENT) as span:
                speech = await generate_cancel_speech(context.language, user_text)
                latency_ms = (time.perf_counter() - t_dispatch) * 1000
                span["metadata"]["latency_ms"] = latency_ms
                await track_request(
                    _CANCEL_INTERACTION_AGENT,
                    cache_hit=False,
                    latency_ms=latency_ms,
                )
                return _CANCEL_INTERACTION_AGENT, speech, {"speech": speech, "action_executed": None}

        if self._ha_client:
            try:
                from zoneinfo import ZoneInfo

                home_ctx = await home_context_provider.get(self._ha_client)
                context.timezone = home_ctx.timezone
                context.location_name = home_ctx.location_name
                try:
                    tz = ZoneInfo(home_ctx.timezone)
                    now = datetime.now(tz)
                    context.local_time = now.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    context.local_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            except Exception:
                logger.debug("Failed to populate home context", exc_info=True)

        agent_task = AgentTask(
            description=condensed_task,
            user_text=user_text,
            conversation_id=conversation_id,
            context=context,
        )
        request = JsonRpcRequest(
            method="message/send",
            params={
                "agent_id": target_agent,
                "task": agent_task,
                "_span_collector": span_collector,
            },
            id=conversation_id or "orchestrator-dispatch",
        )
        try:
            t0 = time.perf_counter()
            noop_span: dict[str, Any] = {"metadata": {}}
            dispatch_ctx = (
                contextlib.nullcontext(noop_span)
                if skip_dispatch_span
                else _optional_span(span_collector, "dispatch", agent_id=target_agent)
            )
            dispatch_timeout = await self.resolve_dispatch_timeout(target_agent)
            async with dispatch_ctx as span:
                _t_dispatch_pre = time.perf_counter()
                response = await asyncio.wait_for(
                    self._dispatcher.dispatch(request),
                    timeout=dispatch_timeout,
                )
                _t_dispatch_post = time.perf_counter()
                latency_ms = (time.perf_counter() - t0) * 1000
                span["metadata"]["latency_ms"] = round(latency_ms, 1)
                span["metadata"]["dispatch_timeout_sec"] = dispatch_timeout
                result_data = self.normalize_agent_result(response, agent_id=target_agent)
                _t_norm = time.perf_counter()
                logger.info(
                    "dispatch_manager agent=%s dispatch_inner=%.1fms normalize=%.1fms total=%.1fms",
                    target_agent,
                    (_t_dispatch_post - _t_dispatch_pre) * 1000,
                    (_t_norm - _t_dispatch_post) * 1000,
                    (_t_norm - _t_dispatch_pre) * 1000,
                )
                span["metadata"]["agent_response"] = result_data.get("speech") or ""
                span["metadata"]["condensed_task"] = condensed_task
            logger.debug("Agent %s responded in %.1fms", target_agent, latency_ms)
            await track_request(target_agent, cache_hit=False, latency_ms=latency_ms)
        except TimeoutError:
            logger.warning(
                "Agent %s timed out after %.1fs, falling back",
                target_agent,
                dispatch_timeout,
            )
            await track_agent_timeout(target_agent, int(dispatch_timeout))
            fb_result = await self.dispatch_fallback(request, target_agent, span_collector, "timeout")
            if fb_result is not None:
                target_agent, response = fb_result
            else:
                return target_agent, _CANNED_TIMEOUT_SPEECH, None
        except RuntimeError as exc:
            logger.warning(
                "Agent %s error: %s -- falling back to %s",
                target_agent,
                str(exc),
                _FALLBACK_AGENT,
            )
            fb_result = await self.dispatch_fallback(request, target_agent, span_collector, "agent_error")
            if fb_result is not None:
                target_agent, response = fb_result
            elif target_agent == _FALLBACK_AGENT:
                error_code = str(exc)[:64]
                return (
                    _FALLBACK_AGENT,
                    _CANNED_GENERAL_ERROR_SPEECH,
                    {
                        "speech": _CANNED_GENERAL_ERROR_SPEECH,
                        "error": {
                            "code": error_code,
                            "recoverable": True,
                        },
                    },
                )
            else:
                return target_agent, _CANNED_GENERAL_ERROR_SPEECH, None

        result = self.normalize_agent_result(response, agent_id=target_agent)
        speech = result.get("speech", "")

        error = result.get("error")
        if error:
            error_code = error.get("code", "unknown")
            logger.info(
                "Agent %s returned error: %s (recoverable=%s)",
                target_agent,
                error_code,
                error.get("recoverable", True),
            )

        return target_agent, speech, result
