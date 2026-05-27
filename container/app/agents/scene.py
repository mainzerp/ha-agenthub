"""Scene activation agent with direct HA REST API execution."""

from app.agents.actionable import ActionableAgent
from app.agents.scene_executor import execute_scene_action
from app.models.agent import AgentCard


class SceneAgent(ActionableAgent):
    """Activates and manages scenes via HA REST API."""

    _prompt_name = "scene"
    _allowed_domains = frozenset({"scene"})

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        # FLOW-CTX-1 (0.18.6): use the originating satellite's area
        # as a tie-breaker for same-name scenes in multiple rooms.
        ctx = getattr(self, "_current_task_context", None)
        area_id = ctx.area_id if ctx else None
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        return await execute_scene_action(
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
            agent_id="scene-agent",
            name="Scene Agent",
            description="Activates Home Assistant scenes with optional transition timing. Lists available scenes and checks if a scene exists.",
            skills=["scene_activate", "scene_list", "scene_query"],
            endpoint="local://scene-agent",
        )
