"""Bootstrap: CacheManager, stale purge task, CacheValidator, validator periodic task."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.cache.cache_manager import CacheManager
from app.util.tasks import spawn

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.agents.rewrite import RewriteAgent
    from app.entity.index import EntityIndex
    from app.ha_client.rest import HARestClient

logger = logging.getLogger(__name__)


async def _purge_stale_response_cache(cache_manager: CacheManager) -> None:
    """One-time startup task: purge stale read-only response cache entries."""
    try:
        count = await cache_manager.purge_readonly_entries()
        if count:
            logger.info("Purged %d stale read-only response cache entries", count)
        else:
            logger.info("No stale read-only response cache entries to purge")
    except Exception:
        logger.warning("Failed to purge stale response cache entries", exc_info=True)


async def setup_cache(
    app: FastAPI,
    source: str,
    cache_store,
    rewrite_agent: RewriteAgent,
    entity_index: EntityIndex,
    ha_client: HARestClient,
    llm_client,
) -> CacheManager:
    """Create CacheManager, spawn purge task, create CacheValidator, spawn validator task.

    Stores ``cache_manager``, ``cache_validator``, and ``cache_store`` on ``app.state``.
    """
    cache_manager = getattr(app.state, "cache_manager", None)
    if cache_manager is None:
        cache_manager = CacheManager(cache_store, rewrite_agent=rewrite_agent)
        await cache_manager.initialize()
        app.state.cache_manager = cache_manager
        app.state.cache_store = cache_store

    purge_task = getattr(app.state, "purge_task", None)
    if purge_task is None or purge_task.done():
        app.state.purge_task = spawn(_purge_stale_response_cache(cache_manager))

    cache_validator = getattr(app.state, "cache_validator", None)
    if cache_validator is None:
        from app.cache.cache_validator import ActionCacheValidator

        cache_validator = ActionCacheValidator(
            action_cache=cache_manager.action_cache,
            cache_manager=cache_manager,
            entity_index=entity_index,
            ha_client=ha_client,
            llm_client=llm_client,
        )
        app.state.cache_validator = cache_validator

    validator_task = getattr(app.state, "cache_validator_task", None)
    if validator_task is None or validator_task.done():
        app.state.cache_validator_task = spawn(cache_validator.run_periodic(), name="cache_validator")

    return cache_manager
