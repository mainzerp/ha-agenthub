"""Media player control agent with direct HA REST API execution."""

from app.agents.actionable import ActionableAgent
from app.agents.media_executor import execute_media_action
from app.models.agent import AgentCard


class MediaAgent(ActionableAgent):
    """Controls generic media player devices via HA REST API."""

    _prompt_name = "media"
    _allowed_domains = frozenset({"media_player"})

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        ctx = getattr(self, "_current_task_context", None)
        area_id = ctx.area_id if ctx else None
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        return await execute_media_action(
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
            agent_id="media-agent",
            name="Media Agent",
            description="Controls generic media players (TV, Chromecast, streaming devices): on/off, play/pause/stop, volume, mute, input/source selection. Reports playback status. Not for music library/Music Assistant -- use music-agent.",
            skills=[
                "tv_control",
                "speaker_control",
                "casting",
                "playback",
                "volume_control",
                "mute",
                "source_selection",
                "media_status",
                "playback_query",
            ],
            endpoint="local://media-agent",
        )
