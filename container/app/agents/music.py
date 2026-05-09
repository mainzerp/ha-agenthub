"""Music agent targeting Music Assistant integration with direct HA execution."""

from app.agents.actionable import ActionableAgent
from app.agents.music_executor import execute_music_action
from app.models.agent import AgentCard


class MusicAgent(ActionableAgent):
    """Controls music playback via Music Assistant (HA integration).

    Targets Music Assistant media_player entities and MA-specific services
    (music_assistant.play_media, music_assistant.search) for library search, queue management,
    and multi-room audio. Falls back to standard media_player services
    for basic transport controls (play/pause/skip/volume).
    """

    _prompt_name = "music"

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        return await execute_music_action(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="music-agent",
            name="Music Agent",
            description=(
                "Controls music playback via Music Assistant: play, pause, skip, volume, shuffle, repeat, "
                "library search, queue management, playlist/artist/album selection. "
                "Reports current track info and lists music players."
            ),
            skills=[
                "music_playback",
                "volume_control",
                "playlist_selection",
                "library_search",
                "queue_management",
                "shuffle",
                "repeat",
                "music_status",
                "playback_query",
            ],
            endpoint="local://music-agent",
        )
