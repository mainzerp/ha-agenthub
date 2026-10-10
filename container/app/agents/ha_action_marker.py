"""Per-dispatch marker recording that a Home Assistant service call started.

The orchestrator opens a marker around every agent dispatch
(:func:`track_ha_actions`); the shared executor primitive
(``action_executor.call_service_with_verification``) flags it right before
the HA service call goes out (:func:`note_ha_action_started`). When the
dispatch then times out or errors, the orchestrator reads the marker and
does NOT re-dispatch the same task to the fallback agent: the action may
already have run, and a second dispatch could execute it twice.

The marker object is mutable and travels through a ContextVar, so child
tasks created after the marker was published (``asyncio.gather`` legs,
stream reader tasks) share the same object and their flag is visible to
the dispatching code.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class HaActionMarker:
    """Mutable per-dispatch flag; ``started`` flips once an HA call begins."""

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


def note_ha_action_started() -> None:
    """Flag the active dispatch marker (no-op outside a tracked dispatch)."""
    marker = _current_marker.get()
    if marker is not None:
        marker.started = True
