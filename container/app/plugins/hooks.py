"""Plugin lifecycle hook definitions and event bus."""

from __future__ import annotations

import enum
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class LifecyclePhase(enum.Enum):
    """Lifecycle phases executed in order during system init/shutdown."""

    CONFIGURE = "configure"
    STARTUP = "startup"
    READY = "ready"
    SHUTDOWN = "shutdown"


class PipelineEvent(enum.StrEnum):
    """Pipeline hook event names emitted by the orchestrator pipeline."""

    PRE_CLASSIFY = "pipeline.pre_classify"
    POST_CLASSIFY = "pipeline.post_classify"
    PRE_DISPATCH = "pipeline.pre_dispatch"
    POST_DISPATCH = "pipeline.post_dispatch"
    PRE_MEDIATE = "pipeline.pre_mediate"


PIPELINE_EVENTS = frozenset(e.value for e in PipelineEvent)


class EventBus:
    """Simple subscribe/publish event bus for inter-plugin communication.

    Handlers are executed with error isolation -- one handler failing
    does not prevent other handlers from running.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[..., Awaitable[None]]]] = {}

    def subscribe(self, event_name: str, handler: Callable[..., Awaitable[None]]) -> None:
        """Register a handler for an event."""
        if event_name not in self._handlers:
            self._handlers[event_name] = []
        self._handlers[event_name].append(handler)

    async def publish(self, event_name: str, data: Any = None) -> None:
        """Publish an event, calling all subscribed handlers.

        Each handler is wrapped in try/except so a single failure does
        not affect other handlers.
        """
        handlers = self._handlers.get(event_name, [])
        for handler in handlers:
            try:
                await handler(data)
            except Exception:
                logger.exception(
                    "Event handler %s failed for event '%s'",
                    getattr(handler, "__qualname__", repr(handler)),
                    event_name,
                )

    def clear(self) -> None:
        """Remove all handlers."""
        self._handlers.clear()
