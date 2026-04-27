"""Security system agent with direct HA REST API execution."""

from app.agents.actionable import ActionableAgent
from app.agents.security_executor import execute_security_action
from app.models.agent import AgentCard


class SecurityAgent(ActionableAgent):
    """Controls security devices via HA REST API."""

    _prompt_name = "security"

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        ctx = getattr(self, "_current_task_context", None)
        area_id = ctx.area_id if ctx else None
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        return await execute_security_action(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            span_collector=span_collector,
            preferred_area_id=area_id,
            task_context=ctx,
            verbatim_terms=verbatim_terms,
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="security-agent",
            name="Security Agent",
            description="Controls and queries locks, alarm panels, cameras, and security sensors (motion, door, window, doorbell, smoke, gas). Lock/unlock, arm/disarm, camera on/off. Reports status and lists all security devices. Reads Home Assistant Recorder history for those entities (e.g. door open events yesterday).",
            skills=[
                "lock_control",
                "alarm_control",
                "camera_control",
                "door_sensor",
                "window_sensor",
                "motion_sensor",
                "doorbell",
                "smoke_sensor",
                "security_status",
                "security_query",
                "entity_history",
                "recorder_history",
            ],
            endpoint="local://security-agent",
        )
