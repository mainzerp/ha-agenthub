"""Agent registry for agent card management and discovery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.agent import AgentCard

if TYPE_CHECKING:
    from app.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class AgentRegistry:
    """In-memory registry of agent cards and handler instances."""

    def __init__(self) -> None:
        self._cards: dict[str, AgentCard] = {}
        self._handlers: dict[str, BaseAgent] = {}

    async def register(self, agent: BaseAgent, *, replace: bool = False) -> None:
        """Register an agent (card + handler) in the registry."""
        card = agent.agent_card
        if card.agent_id in self._handlers and not replace:
            raise ValueError(f"Agent ID already registered: {card.agent_id}")
        self._cards[card.agent_id] = card
        self._handlers[card.agent_id] = agent
        if replace:
            logger.info("Replaced agent registration: %s", card.agent_id)
        else:
            logger.info("Registered agent: %s", card.agent_id)

    async def unregister(self, agent_id: str) -> None:
        """Remove an agent from the registry."""
        self._cards.pop(agent_id, None)
        self._handlers.pop(agent_id, None)
        logger.info("Unregistered agent: %s", agent_id)

    async def discover(self, agent_id: str) -> AgentCard | None:
        """Return the AgentCard for a given agent_id, or None."""
        return self._cards.get(agent_id)

    async def list_agents(self) -> list[AgentCard]:
        """Return all registered agent cards."""
        return list(self._cards.values())

    async def receives_speaker_context(self, agent_id: str) -> bool:
        """True when the in-process agent opts into the requester / speaker prompt line.

        Reads the handler's ``_receives_speaker_context`` class attribute
        (built-in, custom and plugin agents alike). Unknown or remote agents
        return False.
        """
        handler = self._handlers.get(agent_id)
        return getattr(handler, "_receives_speaker_context", False) is True

    async def _get_handler_for_transport(self, agent_id: str) -> BaseAgent | None:
        """Return the agent handler instance for in-process transport only."""
        return self._handlers.get(agent_id)


# Module-level singleton
registry = AgentRegistry()
