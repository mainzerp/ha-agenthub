"""Timer and alarm agent with direct HA REST API execution."""

from app.agents.actionable import ActionableAgent
from app.agents.satellite_targeting import (
    resolve_satellite_target_name,
)
from app.agents.timer_executor import execute_timer_action
from app.models.agent import AgentCard, AgentErrorCode, AgentTask, TaskResult


class TimerAgent(ActionableAgent):
    """Controls timers and reminders via HA REST API."""

    _prompt_name = "timer"

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        # FLOW-CTX-1 (0.18.6): ``_current_task_context`` is now set
        # by ``ActionableAgent.handle_task`` for every subclass, so
        # we no longer need an override just to capture it here.
        ctx = getattr(self, "_current_task_context", None)
        device_id = ctx.device_id if ctx else None
        area_id = ctx.area_id if ctx else None
        language = ctx.language if ctx else None
        timezone = ctx.timezone if ctx else None
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        metadata: dict = {}

        params = action.get("parameters") or {}
        explicit_target_name = str(params.get("target_satellite") or "").strip()
        if explicit_target_name:
            resolved_target, resolution_error = await resolve_satellite_target_name(
                explicit_target_name,
                entity_index=entity_index,
                ha_client=ha_client,
            )
            if resolution_error is not None:
                return {
                    "speech": resolution_error.message,
                    "error": {
                        "code": AgentErrorCode.ENTITY_NOT_FOUND,
                        "message": resolution_error.message,
                        "recoverable": True,
                    },
                    "metadata": {
                        "satellite_targeting": {
                            "explicit_name": explicit_target_name,
                            "status": resolution_error.code,
                            "candidates": resolution_error.candidates,
                        }
                    },
                }

            if resolved_target is not None:
                device_id = resolved_target.device_id
                area_id = resolved_target.area_id
                metadata["satellite_targeting"] = {
                    "explicit_name": explicit_target_name,
                    "status": "resolved",
                    "entity_id": resolved_target.entity_id,
                    "device_id": resolved_target.device_id,
                    "area_id": resolved_target.area_id,
                    "area_name": resolved_target.area_name,
                }

        result = await execute_timer_action(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            device_id=device_id,
            area_id=area_id,
            language=language,
            timezone=timezone,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )
        if metadata:
            existing_metadata = result.get("metadata") if isinstance(result, dict) else None
            if isinstance(existing_metadata, dict):
                merged = dict(existing_metadata)
                merged.update(metadata)
                result["metadata"] = merged
            elif isinstance(result, dict):
                result["metadata"] = metadata
        return result

    def _handle_parse_miss(self, task: AgentTask, response: str) -> TaskResult:
        return self._error_result(
            AgentErrorCode.PARSE_ERROR,
            "I could not understand the timer command well enough to run it. Please try again.",
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="timer-agent",
            name="Timer Agent",
            description="Manages timers, alarms, reminders, and scheduled actions. Start, cancel, pause, resume, snooze timers. Sets alarms, schedules delayed actions and sleep timers. Reports timer status and remaining time.",
            skills=[
                "timer_set",
                "timer_cancel",
                "timer_pause",
                "timer_resume",
                "timer_snooze",
                "timer_query",
                "alarm",
                "reminder",
                "delayed_action",
                "sleep_timer",
            ],
            endpoint="local://timer-agent",
        )
