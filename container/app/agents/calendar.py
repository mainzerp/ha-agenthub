"""Calendar agent — manages calendar events via HA REST API."""

from app.agents.actionable import ActionableAgent
from app.agents.calendar_executor import execute_calendar_action
from app.agents.user_identity import UserIdentityResolver
from app.models.agent import AgentCard, AgentErrorCode, AgentTask, TaskResult


class CalendarAgent(ActionableAgent):
    """Manages calendar events: read, create, update, delete."""

    _prompt_name = "calendar"

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        ctx = getattr(self, "_current_task_context", None)
        device_id = ctx.device_id if ctx else None
        area_id = ctx.area_id if ctx else None
        language = ctx.language if ctx else None
        timezone = ctx.timezone if ctx else None
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []

        resolver = UserIdentityResolver(ha_client=ha_client)
        user = await resolver.resolve_user(
            getattr(current_task, "description", None) if current_task else None,
            device_id=device_id,
            area_id=area_id,
            user_id=ctx.user_id if ctx else None,
        )
        default_calendar_ids = None
        if user:
            import json

            default_calendar_ids = json.loads(user.get("calendar_entity_ids_json", "[]"))

        return await execute_calendar_action(
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
            default_calendar_ids=default_calendar_ids,
        )

    def _handle_parse_miss(self, task: AgentTask, response: str) -> TaskResult:
        return self._error_result(
            AgentErrorCode.PARSE_ERROR,
            "I could not understand the calendar command. Please try again.",
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="calendar-agent",
            name="Calendar Agent",
            description=(
                "Manages calendar events. Read upcoming events, create new events, "
                "update or delete existing events. Uses calendar entities from Home Assistant. "
                "Examples: 'Was steht morgen im Kalender?', 'Termin beim Zahnarzt am Freitag um 14 Uhr', "
                "'Loese den Team-Meeting Termin'"
            ),
            skills=[
                "calendar_read",
                "calendar_create",
                "calendar_update",
                "calendar_delete",
                "calendar_query",
            ],
            endpoint="local://calendar-agent",
        )
