"""Light control agent with direct HA REST API execution."""

from app.agents.action_executor import execute_action
from app.agents.actionable import ActionableAgent
from app.models.agent import AgentCard


class LightAgent(ActionableAgent):
    """Controls lighting devices via HA REST API."""

    _prompt_name = "light"
    _allowed_domains = frozenset({"light", "switch", "sensor"})

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        # FLOW-CTX-1 (0.18.6): use the originating satellite's area
        # as a tie-breaker when the natural-language query is
        # area-ambiguous ("mach das licht an" from the kitchen
        # satellite should prefer ``light.kitchen_*``).
        ctx = getattr(self, "_current_task_context", None)
        area_id = ctx.area_id if ctx else None
        return await execute_action(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            span_collector=span_collector,
            preferred_area_id=area_id,
            task_context=ctx,
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="light-agent",
            name="Light Agent",
            description="Controls and queries lights, switches, and illuminance sensors: on/off, toggle, brightness, color, color temperature. Reports light/switch status and light-level readings. Lists all lights and switches. Reads Home Assistant Recorder history for lights, switches, and illuminance sensors (e.g. how long a light was on yesterday).",
            skills=[
                "light_control",
                "switch_control",
                "brightness",
                "color",
                "toggle",
                "illuminance_sensor",
                "light_status",
                "light_query",
                "switch_status",
                "switch_query",
                "entity_history",
                "recorder_history",
            ],
            endpoint="local://light-agent",
        )
