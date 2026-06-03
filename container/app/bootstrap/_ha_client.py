"""Bootstrap: embedding engine, vector store, HA REST client, HomeContext, AliasResolver."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from app.agents.base import preload_prompt_cache
from app.cache.embedding import get_embedding_engine
from app.cache.sqlite_cache_store import SqliteCacheStore
from app.cache.vector_store import get_vector_store
from app.config import settings
from app.entity.aliases import AliasResolver
from app.ha_client.home_context import home_context_provider
from app.ha_client.rest import HARestClient

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def setup_ha_client(app: FastAPI, source: str):
    """Initialize embedding engine, vector store, HA REST client, HomeContext, AliasResolver.

    Stores ``ha_client`` and ``alias_resolver`` on ``app.state``.
    Pre-warms the prompt cache.

    Returns:
        ``(vector_store, cache_store, ha_client, alias_resolver)`` for use by later steps.
    """
    await get_embedding_engine()
    vector_store = await get_vector_store()

    cache_store = SqliteCacheStore(os.path.join(settings.chromadb_persist_dir, "cache.db"))

    ha_client = getattr(app.state, "ha_client", None)
    if ha_client is None:
        ha_client = HARestClient()
        await ha_client.initialize()
    else:
        await ha_client.reload()
    app.state.ha_client = ha_client

    try:
        await home_context_provider.refresh(ha_client)
    except Exception:
        logger.warning("Setup init (%s): failed to pre-warm HomeContext cache", source, exc_info=True)

    alias_resolver = getattr(app.state, "alias_resolver", None)
    if alias_resolver is None:
        alias_resolver = AliasResolver()
        await alias_resolver.load()
        app.state.alias_resolver = alias_resolver

    await asyncio.to_thread(preload_prompt_cache)

    return vector_store, cache_store, ha_client, alias_resolver
