"""Shared background-task registry helper for setup-dependent bootstrapping.

``spawn_background`` schedules a background task and registers it on
``app.state._background_tasks`` so every spawned task is discoverable in one
place, while still storing it under its conventional attribute name (e.g.
``app.state.purge_task``) so existing shutdown code that reads these via
``getattr(app.state, "purge_task")`` keeps working unchanged.

Task creation, garbage-collection safety (a strong reference held until the
task completes) and exception logging are delegated to
:func:`app.util.tasks.spawn`. Delegating -- rather than calling
``asyncio.create_task`` directly here -- preserves the existing test patches
of ``app.util.tasks.asyncio.create_task`` that intercept background task
creation during bootstrap.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from app.util.tasks import spawn

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def spawn_background(
    app: FastAPI,
    coro: Coroutine[Any, Any, Any],
    attr_name: str,
    **create_task_kwargs: Any,
) -> asyncio.Task[Any]:
    """Schedule ``coro`` as a tracked, registered background task.

    The task is appended to ``app.state._background_tasks`` (an ordered
    registry list) and also assigned to ``app.state.<attr_name>`` so legacy
    attribute reads keep working. The asyncio task name defaults to
    ``attr_name`` but can be overridden by passing ``name=`` in
    ``create_task_kwargs`` (this mirrors the historical call that stored the
    task under ``cache_validator_task`` while naming it ``cache_validator``).
    """
    task_name = create_task_kwargs.pop("name", attr_name)
    task = spawn(coro, name=task_name)

    registry = getattr(app.state, "_background_tasks", None)
    if registry is None:
        registry = []
        app.state._background_tasks = registry
    registry.append(task)

    setattr(app.state, attr_name, task)
    return task
