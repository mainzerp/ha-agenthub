"""Calendar agent — manages calendar events via HA REST API."""

import logging

from app.agents.actionable import ActionableAgent
from app.agents.calendar_executor import ENTITY_ACTIONS, ENTITY_FREE_ACTIONS, execute_calendar_action
from app.agents.decorator import agent
from app.agents.user_identity import UserIdentityResolver
from app.models.agent import AgentCard, AgentErrorCode, DispatchTask, TaskResult

logger = logging.getLogger(__name__)


@agent(
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
    prompt_name="calendar",
    # The executor resolves calendars itself (the user's default calendars,
    # else every visible calendar): no action needs a recalled candidate.
    entity_actions=ENTITY_ACTIONS,
    entity_free_actions=ENTITY_FREE_ACTIONS,
    db_gated=True,
)
class CalendarAgent(ActionableAgent):
    """Manages calendar events: read, create, update, delete."""

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        ctx = self._get_current_task_context()
        device_id = ctx.device_id if ctx else None
        area_id = ctx.area_id if ctx else None
        language = ctx.language if ctx else None
        timezone = ctx.timezone if ctx else None
        current_task = self._get_current_task()

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

            try:
                default_calendar_ids = json.loads(user.get("calendar_entity_ids_json", "[]"))
            except json.JSONDecodeError:
                logger.warning(
                    "Malformed calendar_entity_ids_json for user %r; falling back to default calendar behavior",
                    user.get("display_name") or user.get("id"),
                )
                default_calendar_ids = None

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
            default_calendar_ids=default_calendar_ids,
        )

    def _handle_parse_miss(self, task: DispatchTask, response: str) -> TaskResult:
        clarification = self._parse_miss_clarification(task, response)
        if clarification is not None:
            return clarification
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
