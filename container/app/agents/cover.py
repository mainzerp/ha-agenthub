"""Cover control agent with direct HA REST API execution."""

from app.agents.actionable import ActionableAgent
from app.agents.cover_executor import execute_cover_action
from app.models.agent import AgentCard


class CoverAgent(ActionableAgent):
    """Controls covers, blinds, curtains, shutters, garage doors, gates, awnings, and windows via HA REST API."""

    _prompt_name = "cover"

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        # FLOW-CTX-1 (0.18.6): use the originating satellite's area
        # as a tie-breaker for ambiguous cover queries.
        ctx = getattr(self, "_current_task_context", None)
        area_id = ctx.area_id if ctx else None
        # 0.23.0: forward verbatim original-language tokens preserved
        # by the orchestrator so the matcher can try them before any
        # translated entity name.
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        return await execute_cover_action(
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
            agent_id="cover-agent",
            name="Cover Agent",
            description="Controls and queries covers, blinds, curtains, shutters, garage doors, gates, awnings, and windows: open, close, stop, set position, and tilt control. Reports cover status including current position and tilt position. Lists all cover entities.",
            skills=[
                "cover_control",
                "open",
                "close",
                "stop",
                "set_position",
                "tilt_control",
                "query_cover_state",
                "list_covers",
                "entity_history",
                "recorder_history",
            ],
            endpoint="local://cover-agent",
        )
