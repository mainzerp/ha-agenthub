"""Lists agent — manages todo and shopping lists via HA REST API."""

from app.agents.actionable import ActionableAgent
from app.agents.lists_executor import execute_lists_action
from app.models.agent import AgentCard, AgentErrorCode, AgentTask, TaskResult


class ListsAgent(ActionableAgent):
    """Manages todo lists and shopping lists via HA REST API."""

    _prompt_name = "lists"
    _allowed_domains = frozenset({"todo", "shopping_list"})

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        ctx = getattr(self, "_current_task_context", None)
        device_id = ctx.device_id if ctx else None
        area_id = ctx.area_id if ctx else None
        language = ctx.language if ctx else None
        timezone = ctx.timezone if ctx else None
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []

        return await execute_lists_action(
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

    def _handle_parse_miss(self, task: AgentTask, response: str) -> TaskResult:
        return self._error_result(
            AgentErrorCode.PARSE_ERROR,
            "I could not understand the lists command. Please try again.",
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="lists-agent",
            name="Lists Agent",
            description=(
                "Manages todo lists and shopping lists in Home Assistant. "
                "List available lists, view items, add items, mark items as completed, "
                "remove items, and clear completed items. "
                "Examples: 'Was ist auf der Einkaufsliste?', 'Fuege Milch zur Einkaufsliste hinzu', "
                "'Markiere Butter als erledigt', 'Leere die Einkaufsliste'"
            ),
            skills=[
                "list_lists",
                "list_items",
                "add_item",
                "complete_item",
                "remove_item",
                "clear_completed",
            ],
            endpoint="local://lists-agent",
        )
