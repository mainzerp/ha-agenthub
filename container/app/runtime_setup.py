"""Helpers for initializing setup-dependent runtime services in-process.

Most service-specific bootstrap logic lives in ``app.bootstrap.*`` modules.
This module retains only the orchestrator, backward-compatible helpers, and
the ``ensure_setup_runtime_initialized`` gate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING

from app.db.repository import SetupStateRepository

_background_tasks: set[asyncio.Task] = set()


def _spawn(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` as a tracked background task.

    The task is stored in a module-level set until completion so it
    cannot be silently dropped by the GC. Exceptions raised inside the
    coroutine are logged with traceback and do not propagate.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Background task %s failed", t.get_name(), exc_info=t.exception())

    task.add_done_callback(_done)
    return task


if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def _initialize_setup_dependent_services(app: FastAPI, *, source: str) -> None:
    """Idempotent core of the setup-dependent bootstrap.

    FLOW-SETUP-1 (P1-2): single canonical implementation used by both the
    FastAPI ``lifespan`` on fresh container startup (``source="lifespan"``)
    and by :func:`ensure_setup_runtime_initialized` which runs after the
    user completes the setup wizard in a long-running process
    (``source="post-setup"``). Each step is individually idempotent --
    re-entering after a partial init reuses whatever ``app.state``
    already carries instead of re-instantiating.
    """
    registry = getattr(app.state, "registry", None)
    dispatcher = getattr(app.state, "dispatcher", None)
    mcp_registry = getattr(app.state, "mcp_registry", None)
    mcp_tool_manager = getattr(app.state, "mcp_tool_manager", None)
    if registry is None or dispatcher is None or mcp_registry is None or mcp_tool_manager is None:
        raise RuntimeError("Core runtime state is not ready for setup initialization")

    logger.info("Setup init (%s): initializing setup-dependent services", source)

    from app.bootstrap._agents import setup_agents, setup_rewrite_agent
    from app.bootstrap._cache import setup_cache
    from app.bootstrap._entity import setup_entity_index, setup_entity_observers
    from app.bootstrap._entity_matcher import setup_entity_matcher
    from app.bootstrap._ha_client import setup_ha_client
    from app.bootstrap._llm import setup_llm_client
    from app.bootstrap._mcp import setup_mcp
    from app.bootstrap._monitors import setup_monitors

    # Phase 1: HA client, embedding engine, vector store, HomeContext, AliasResolver, preload
    vector_store, ha_client, alias_resolver = await setup_ha_client(app, source)

    # Phase 2: Entity index creation + background prime
    entity_index = await setup_entity_index(app, source, ha_client, vector_store)

    # Phase 3: Entity matcher (depends on entity_index, alias_resolver)
    await setup_entity_matcher(app, source, ha_client, entity_index, alias_resolver)

    # Phase 4: Rewrite agent (depends on ha_client, entity_index)
    rewrite_agent = await setup_rewrite_agent(app, source, ha_client, entity_index)

    # Phase 5: Cache manager + purge + validator (depends on vector_store, rewrite_agent, llm_client)
    llm_client = await setup_llm_client(app, source)
    await setup_cache(app, source, vector_store, rewrite_agent, entity_index, ha_client, llm_client)

    # Phase 6: MCP servers (depends on mcp_registry, mcp_tool_manager)
    await setup_mcp(app, source)

    # Phase 7: Domain agent registration + custom loader
    await setup_agents(app, source, ha_client, entity_index, mcp_tool_manager, registry)

    # Phase 8: WebSocket observers, deferred hidden sync, periodic sync (depends on cache_manager)
    await setup_entity_observers(app, source, ha_client, entity_index, app.state.cache_manager)

    # Phase 9: Monitors (depends on entity_index, dispatcher)
    await setup_monitors(app, source, entity_index, dispatcher)

    logger.info("Setup init (%s): completed", source)


async def ensure_setup_runtime_initialized(app: FastAPI) -> bool:
    """Initialize setup-dependent runtime services after setup completion.

    Returns ``True`` when initialization work was performed in this call,
    otherwise ``False``.
    """
    if getattr(app.state, "setup_runtime_initialized", False):
        return False

    lock = getattr(app.state, "setup_runtime_init_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.setup_runtime_init_lock = lock

    async with lock:
        if getattr(app.state, "setup_runtime_initialized", False):
            return False
        if not await SetupStateRepository.is_complete():
            return False

        await _initialize_setup_dependent_services(app, source="post-setup")

        app.state.setup_runtime_initialized = True
        logger.info("Setup-dependent runtime initialized in-process")
        return True
