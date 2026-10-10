"""Per-dispatch marker recording that a Home Assistant write started.

The orchestrator opens a marker around every agent dispatch
(:func:`track_ha_actions`). Every HA write entry point of the HA client
(``HARestClient.call_service`` and its WebSocket fallback,
``HAWebSocketClient.call_service``, ``send_ws_command``, ``fire_event`` and
the automation config writes) flags it right before the request goes out
(:func:`note_ha_action_started`). When the dispatch then times out or
errors, the orchestrator reads the marker and does NOT re-dispatch the same
task to the fallback agent: the action may already have run, and a second
dispatch could execute it twice.

Read-only service calls and commands (``get*``, ``search*``,
``browse*``, ``list*``; see :func:`is_read_only_ha_call`) do not flag the
marker, so a timed-out lookup still falls back.

The marker object is mutable and travels through a ContextVar, so child
tasks created after the marker was published (``asyncio.gather`` legs,
stream reader tasks) share the same object and their flag is visible to
the dispatching code. Flagging is idempotent: the REST call and its
WebSocket fallback may both flag the same marker.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

_READ_ONLY_PREFIXES: tuple[str, ...] = ("get", "search", "browse", "list")


@dataclass
class HaActionMarker:
    """Mutable per-dispatch flag; ``started`` flips once an HA write begins."""

    started: bool = False


_current_marker: ContextVar[HaActionMarker | None] = ContextVar("ha_action_marker", default=None)


@contextlib.contextmanager
def track_ha_actions(marker: HaActionMarker | None = None) -> Iterator[HaActionMarker]:
    """Publish ``marker`` (or a fresh one) for the code running inside the block."""
    active = marker if marker is not None else HaActionMarker()
    token = _current_marker.set(active)
    try:
        yield active
    finally:
        # A generator finalized from a foreign context cannot reset the
        # token; the value then simply dies with that context.
        with contextlib.suppress(ValueError):
            _current_marker.reset(token)


def is_read_only_ha_call(name: str) -> bool:
    """True for a service or WS command name that only reads HA data.

    ``name`` is a service name (``get_events``) or the last segment of a WS
    command type (``config/entity_registry/list`` -> ``list``).
    """
    leaf = (name or "").rsplit("/", 1)[-1].strip().lower()
    return leaf.startswith(_READ_ONLY_PREFIXES)


def note_ha_action_started(name: str | None = None) -> None:
    """Flag the active dispatch marker (no-op outside a tracked dispatch).

    ``name`` is the service or WS command name; read-only calls do not flag.
    """
    if name is not None and is_read_only_ha_call(name):
        return
    marker = _current_marker.get()
    if marker is not None:
        marker.started = True
