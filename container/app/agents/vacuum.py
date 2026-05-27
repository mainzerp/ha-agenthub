"""Vacuum control agent with direct HA REST API execution."""

from app.agents.actionable import ActionableAgent
from app.agents.vacuum_executor import execute_vacuum_action
from app.models.agent import AgentCard


class VacuumAgent(ActionableAgent):
    """Controls robot vacuum devices via HA REST API."""

    _prompt_name = "vacuum"
    _allowed_domains = frozenset({"vacuum"})

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        # FLOW-CTX-1 (0.18.6): use the originating satellite's area
        # as a tie-breaker for ambiguous vacuum queries.
        ctx = getattr(self, "_current_task_context", None)
        area_id = ctx.area_id if ctx else None
        # 0.23.0: forward verbatim original-language tokens preserved
        # by the orchestrator so the matcher can try them before any
        # translated entity name.
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        return await execute_vacuum_action(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            span_collector=span_collector,
            preferred_area_id=area_id,
            verbatim_terms=verbatim_terms,
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="vacuum-agent",
            name="Vacuum Agent",
            description="Controls and queries robot vacuum cleaners: start cleaning, pause, stop, return to base, clean spot, locate, and set fan speed. Reports vacuum state including battery level, fan speed, and status. Lists all vacuum entities.",
            skills=[
                "vacuum_control",
                "start",
                "pause",
                "stop",
                "return_to_base",
                "clean_spot",
                "set_fan_speed",
                "locate",
                "query_vacuum_state",
                "list_vacuums",
            ],
            endpoint="local://vacuum-agent",
        )
